#!/usr/bin/env python3
"""
webrecon.py - unified recon runner

Runs, in one command against a target domain:
  1. Subdomain enumeration   (crt.sh CT-log query + optional local wordlist brute force)
  2. Directory/file enum     (threaded wordlist bruteforce against the base target)
  3. WhatWeb fingerprinting  (shells out to `whatweb` if installed; falls back to a
                              lightweight built-in header/tech fingerprint otherwise)
  4. Advanced page enum      (crawl for links, forms, JS files, HTML comments,
                              interesting headers, robots.txt / sitemap.xml)

Everything is collected into one in-memory tree and printed as a single
ASCII tree at the end (and optionally written to JSON).

Usage:
    python3 webrecon.py example.com
    python3 webrecon.py example.com -w /usr/share/wordlists/dirb/common.txt -t 40
    python3 webrecon.py example.com --no-subs --json out.json
    python3 webrecon.py example.com --https-only -o report_tree.txt

Requires: requests   (pip install requests --break-system-packages)
Optional: whatweb on PATH for richer fingerprinting (falls back gracefully).

Author's note: mirrors the resource-management style used across this
toolchain (webmap / recon / pwaudit) - explicit close()/session teardown,
no reliance on __del__, and a single coherent CLI entrypoint.
"""

import argparse
import concurrent.futures
import json
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urljoin, urlparse

try:
    import requests
except ImportError:
    print("[!] Missing dependency: requests")
    print("    Install with: pip install requests --break-system-packages")
    sys.exit(1)

requests.packages.urllib3.disable_warnings() if hasattr(requests, "packages") else None

DEFAULT_DIR_WORDLIST_FALLBACK = [
    "admin", "login", "api", "backup", "config", "dashboard", "uploads",
    "images", "static", "assets", "js", "css", "test", "dev", "staging",
    ".git", ".env", "robots.txt", "sitemap.xml", "wp-admin", "wp-login.php",
    "server-status", "phpinfo.php", "console", "swagger", "graphql",
    "actuator", "health", ".well-known/security.txt", "old", "tmp",
]

COMMON_HEADERS_OF_INTEREST = [
    "server", "x-powered-by", "x-aspnet-version", "x-generator",
    "content-security-policy", "strict-transport-security",
    "x-frame-options", "via", "x-runtime",
]

TREE_BRANCH = "├── "
TREE_LAST = "└── "
TREE_PIPE = "│   "
TREE_BLANK = "    "


# ------------------------------------------------------------------ #
# Data model
# ------------------------------------------------------------------ #

@dataclass
class TreeNode:
    label: str
    children: List["TreeNode"] = field(default_factory=list)

    def add(self, label: str) -> "TreeNode":
        node = TreeNode(label)
        self.children.append(node)
        return node

    def render(self, prefix: str = "", is_last: bool = True, is_root: bool = True) -> str:
        lines = []
        if is_root:
            lines.append(self.label)
        else:
            connector = TREE_LAST if is_last else TREE_BRANCH
            lines.append(prefix + connector + self.label)

        new_prefix = prefix if is_root else prefix + (TREE_BLANK if is_last else TREE_PIPE)
        for i, child in enumerate(self.children):
            last = (i == len(self.children) - 1)
            lines.append(child.render(new_prefix, last, is_root=False))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"label": self.label, "children": [c.to_dict() for c in self.children]}


# ------------------------------------------------------------------ #
# Shared HTTP session (explicit lifecycle, no __del__)
# ------------------------------------------------------------------ #

class SafeSession:
    """Thin wrapper around requests.Session with explicit close()."""

    def __init__(self, timeout: float = 6.0, user_agent: str = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent or "Mozilla/5.0 (X11; Linux x86_64) webrecon/1.0"
        })
        self.timeout = timeout
        self._closed = False

    def get(self, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", False)
        kwargs.setdefault("allow_redirects", True)
        return self.session.get(url, **kwargs)

    def close(self):
        if not self._closed:
            self.session.close()
            self._closed = True


# ------------------------------------------------------------------ #
# 1. Subdomain enumeration
# ------------------------------------------------------------------ #

def enum_subdomains_crtsh(domain: str, http: SafeSession) -> List[str]:
    found = set()
    try:
        resp = http.get(f"https://crt.sh/?q=%25.{domain}&output=json", timeout=15)
        if resp.status_code == 200 and resp.text.strip():
            data = json.loads(resp.text)
            for entry in data:
                name_value = entry.get("name_value", "")
                for name in name_value.split("\n"):
                    name = name.strip().lstrip("*.")
                    if name.endswith(domain):
                        found.add(name)
    except Exception:
        pass
    return sorted(found)


def enum_subdomains_brute(domain: str, sub_wordlist: List[str], threads: int) -> List[str]:
    found = []
    lock = threading.Lock()

    def check(sub):
        fqdn = f"{sub}.{domain}"
        try:
            socket.gethostbyname(fqdn)
            with lock:
                found.append(fqdn)
        except socket.gaierror:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(check, sub_wordlist))
    return sorted(found)


# ------------------------------------------------------------------ #
# 2. Directory / file enumeration
# ------------------------------------------------------------------ #

def enum_directories(base_url: str, wordlist: List[str], threads: int, http_timeout: float) -> List[dict]:
    results = []
    lock = threading.Lock()

    def probe(path):
        url = urljoin(base_url + "/", path)
        try:
            r = requests.get(url, timeout=http_timeout, verify=False, allow_redirects=False)
            if r.status_code < 404 or r.status_code in (401, 403):
                with lock:
                    results.append({"path": path, "status": r.status_code, "length": len(r.content)})
        except requests.RequestException:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(probe, wordlist))
    results.sort(key=lambda x: x["path"])
    return results


# ------------------------------------------------------------------ #
# 3. WhatWeb / fingerprinting
# ------------------------------------------------------------------ #

def run_whatweb(url: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["whatweb", "-a", "3", url],
            capture_output=True, text=True, timeout=30
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return None


def fallback_fingerprint(url: str, http: SafeSession) -> List[str]:
    lines = []
    try:
        r = http.get(url)
        for h in COMMON_HEADERS_OF_INTEREST:
            if h in {k.lower(): v for k, v in r.headers.items()}:
                val = {k.lower(): v for k, v in r.headers.items()}[h]
                lines.append(f"{h}: {val}")
        set_cookie = r.headers.get("Set-Cookie", "")
        if "PHPSESSID" in set_cookie:
            lines.append("tech-hint: PHP")
        if "wordpress" in r.text.lower() or "wp-content" in r.text.lower():
            lines.append("tech-hint: WordPress")
        if "csrf" in r.text.lower():
            lines.append("hint: CSRF token present")
        lines.append(f"status: {r.status_code}, size: {len(r.content)} bytes")
    except Exception as e:
        lines.append(f"error: {e}")
    return lines


# ------------------------------------------------------------------ #
# 4. Advanced page enumeration
# ------------------------------------------------------------------ #

LINK_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
SCRIPT_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.I)
COMMENT_RE = re.compile(r'<!--(.*?)-->', re.S)
FORM_RE = re.compile(r'<form[^>]*>', re.I)
EMAIL_RE = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')


def enum_page(base_url: str, http: SafeSession) -> dict:
    out = {"links": [], "js_files": [], "comments": [], "forms": 0,
           "emails": [], "robots": None, "sitemap": None}
    try:
        r = http.get(base_url)
        html = r.text

        links = set(LINK_RE.findall(html))
        out["links"] = sorted(links)[:50]

        js = set(SCRIPT_RE.findall(html))
        out["js_files"] = sorted(js)[:30]

        comments = [c.strip()[:120] for c in COMMENT_RE.findall(html) if c.strip()]
        out["comments"] = comments[:20]

        out["forms"] = len(FORM_RE.findall(html))

        emails = set(EMAIL_RE.findall(html))
        out["emails"] = sorted(emails)

    except Exception:
        pass

    for path, key in (("robots.txt", "robots"), ("sitemap.xml", "sitemap")):
        try:
            r = http.get(urljoin(base_url + "/", path))
            if r.status_code == 200 and len(r.text.strip()) > 0:
                out[key] = r.text.strip()[:400]
        except Exception:
            pass

    return out


# ------------------------------------------------------------------ #
# Wordlist loading
# ------------------------------------------------------------------ #

def load_wordlist(path: Optional[str], fallback: List[str]) -> List[str]:
    if not path:
        return fallback
    try:
        with open(path, "r", errors="ignore") as f:
            words = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        return words if words else fallback
    except OSError:
        print(f"[!] Could not read wordlist {path}, using built-in fallback list")
        return fallback


DEFAULT_SUB_WORDLIST_FALLBACK = [
    "www", "mail", "ftp", "api", "dev", "staging", "test", "admin",
    "vpn", "portal", "app", "beta", "cdn", "static", "blog", "shop",
    "m", "ns1", "ns2", "webmail", "cpanel", "autodiscover", "git",
]


# ------------------------------------------------------------------ #
# Main orchestration
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(
        description="Unified subdomain + directory + whatweb + page recon, tree output."
    )
    parser.add_argument("target", help="Target domain, e.g. example.com")
    parser.add_argument("-w", "--dir-wordlist", help="Wordlist for directory bruteforce")
    parser.add_argument("-W", "--sub-wordlist", help="Wordlist for subdomain bruteforce")
    parser.add_argument("-t", "--threads", type=int, default=25, help="Thread count (default 25)")
    parser.add_argument("--timeout", type=float, default=6.0, help="Per-request timeout seconds")
    parser.add_argument("--https-only", action="store_true", help="Only probe https://")
    parser.add_argument("--no-subs", action="store_true", help="Skip subdomain enumeration")
    parser.add_argument("--no-dirs", action="store_true", help="Skip directory enumeration")
    parser.add_argument("--no-whatweb", action="store_true", help="Skip whatweb/fingerprinting")
    parser.add_argument("--no-pages", action="store_true", help="Skip advanced page enumeration")
    parser.add_argument("-o", "--output", help="Write rendered tree to this text file")
    parser.add_argument("--json", help="Also write full results as JSON to this path")
    args = parser.parse_args()

    target = args.target.strip().rstrip("/")
    domain = urlparse(target).netloc or target
    scheme = "https"
    base_url = f"{scheme}://{domain}"

    root = TreeNode(f"{domain}")
    http = SafeSession(timeout=args.timeout)

    t0 = time.time()
    print(f"[*] Starting unified recon against {domain}")

    # Confirm the host resolves / is reachable, try https then http
    reachable_base = None
    for s in (["https"] if args.https_only else ["https", "http"]):
        try:
            r = http.get(f"{s}://{domain}", timeout=args.timeout)
            reachable_base = f"{s}://{domain}"
            break
        except requests.RequestException:
            continue
    if not reachable_base:
        print(f"[!] Could not reach {domain} over http(s). Continuing with subdomain/DNS-only checks where possible.")
    else:
        base_url = reachable_base
        print(f"[+] Base reachable at {base_url}")

    # 1. Subdomains
    if not args.no_subs:
        print("[*] Enumerating subdomains (crt.sh)...")
        sub_node = root.add("subdomains")
        crt_subs = enum_subdomains_crtsh(domain, http)
        crt_group = sub_node.add(f"crt.sh ({len(crt_subs)} found)")
        for s in crt_subs[:100]:
            crt_group.add(s)

        sub_wl = load_wordlist(args.sub_wordlist, DEFAULT_SUB_WORDLIST_FALLBACK)
        print(f"[*] Brute-forcing subdomains ({len(sub_wl)} candidates)...")
        brute_subs = enum_subdomains_brute(domain, sub_wl, args.threads)
        brute_group = sub_node.add(f"brute-force ({len(brute_subs)} found)")
        for s in brute_subs:
            brute_group.add(s)

    # 2. Directories
    if not args.no_dirs and reachable_base:
        dir_wl = load_wordlist(args.dir_wordlist, DEFAULT_DIR_WORDLIST_FALLBACK)
        print(f"[*] Enumerating directories/files ({len(dir_wl)} candidates, {args.threads} threads)...")
        dir_results = enum_directories(base_url, dir_wl, args.threads, args.timeout)
        dir_node = root.add(f"directories ({len(dir_results)} found)")
        for d in dir_results:
            dir_node.add(f"/{d['path']}  [{d['status']}]  {d['length']}b")

    # 3. WhatWeb / fingerprint
    if not args.no_whatweb and reachable_base:
        print("[*] Running fingerprinting (whatweb if available)...")
        ww_node = root.add("fingerprint")
        ww_output = run_whatweb(base_url)
        if ww_output:
            for line in ww_output.splitlines():
                ww_node.add(line.strip())
        else:
            ww_node.add("whatweb not found on PATH — using built-in fallback")
            for line in fallback_fingerprint(base_url, http):
                ww_node.add(line)

    # 4. Advanced page enumeration
    if not args.no_pages and reachable_base:
        print("[*] Running advanced page enumeration...")
        page = enum_page(base_url, http)
        page_node = root.add("page enumeration")

        links_node = page_node.add(f"links ({len(page['links'])})")
        for l in page["links"][:25]:
            links_node.add(l)

        js_node = page_node.add(f"js files ({len(page['js_files'])})")
        for j in page["js_files"]:
            js_node.add(j)

        comments_node = page_node.add(f"html comments ({len(page['comments'])})")
        for c in page["comments"]:
            comments_node.add(c)

        page_node.add(f"forms found: {page['forms']}")

        if page["emails"]:
            email_node = page_node.add(f"emails found ({len(page['emails'])})")
            for e in page["emails"]:
                email_node.add(e)

        if page["robots"]:
            robots_node = page_node.add("robots.txt")
            for line in page["robots"].splitlines()[:15]:
                if line.strip():
                    robots_node.add(line.strip())

        if page["sitemap"]:
            sm_node = page_node.add("sitemap.xml (preview)")
            sm_node.add(page["sitemap"][:200].replace("\n", " "))

    http.close()

    elapsed = time.time() - t0
    print(f"[+] Done in {elapsed:.1f}s\n")

    rendered = root.render()
    print(rendered)

    if args.output:
        with open(args.output, "w") as f:
            f.write(rendered + "\n")
        print(f"\n[+] Tree written to {args.output}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(root.to_dict(), f, indent=2)
        print(f"[+] JSON written to {args.json}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
        sys.exit(130)

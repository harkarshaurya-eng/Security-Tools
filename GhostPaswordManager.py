#!/usr/bin/env python3
"""
Ghost Password Manager
=======================
A terminal-UI password manager: generate, save, list, and search passwords,
each tagged by site with an optional linked email/Gmail address.

Storage:
  - Vault: ~/.ghostpm/vault.enc  (Fernet-encrypted JSON blob)
  - Salt:  ~/.ghostpm/salt       (random 16 bytes, PBKDF2 input)
  - Master password derives the encryption key via PBKDF2-HMAC-SHA256
    (600,000 iterations). The master password itself is never stored.

Run it with no arguments — everything happens in the menu:
    python3 ghost.py
"""

import base64
import getpass
import json
import os
import secrets
import string
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

VAULT_DIR = Path(os.environ.get("GHOSTPM_DIR", Path.home() / ".ghostpm"))
VAULT_FILE = VAULT_DIR / "vault.enc"
SALT_FILE = VAULT_DIR / "salt"
KDF_ITERATIONS = 600_000
DEFAULT_LENGTH = 20

# ---------- Blue-flame palette (truecolor ANSI) ----------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def rgb(r, g, b):
    return f"\033[38;2;{r};{g};{b}m"


DEEP_BLACK = rgb(5, 7, 10)
NAVY = rgb(7, 17, 31)
GLASS_BLUE = rgb(11, 29, 50)
ELECTRIC = rgb(0, 140, 255)
FLAME = rgb(0, 183, 255)
BRIGHT_FLAME = rgb(93, 232, 255)
WHITE_ICE = rgb(234, 247, 255)


def clear():
    os.system("cls" if os.name == "nt" else "clear")


GHOST_ART = r'''
        .-======-.
      .'          '.
     /   .-==-.     \
    ;   /  __  \     ;
    |  |  ( () ) |   |
    ;   \  '--'  /   ;
     \   '-....-'   /
      '.          .'
        )        (
       /|  .--.   |\
      / | ( () )  | \
     /  |  '--'   |  \
    ;   ;         ;   ;
    |   |         |   |
     \_/ \_/ \_/ \_/ \_/
'''


def banner():
    clear()
    print(BRIGHT_FLAME + BOLD + GHOST_ART + RESET)
    title = "G H O S T   P A S S W O R D   M A N A G E R"
    print(FLAME + BOLD + title.center(60) + RESET)
    print(ELECTRIC + ("─" * 60) + RESET)


def box_line(text="", width=60):
    pad = width - 4 - len(strip_ansi(text))
    pad = max(pad, 0)
    print(f"{ELECTRIC}│ {RESET}{text}{' ' * pad}{ELECTRIC} │{RESET}")


def strip_ansi(s):
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


def hr(width=60, char="─"):
    print(ELECTRIC + (char * width) + RESET)


def prompt(label):
    return input(f"{BRIGHT_FLAME}{label}{RESET} ").strip()


def pause():
    input(f"\n{DIM}Press Enter to continue...{RESET}")


# ---------- crypto ----------

def derive_key(master_password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(master_password.encode("utf-8")))


def prompt_master(confirm: bool = False) -> str:
    pw = getpass.getpass("Master password: ")
    if confirm:
        pw2 = getpass.getpass("Confirm master password: ")
        if pw != pw2:
            print(f"{FLAME}Passwords did not match.{RESET}")
            sys.exit(1)
    if not pw:
        print(f"{FLAME}Master password cannot be empty.{RESET}")
        sys.exit(1)
    return pw


def vault_exists() -> bool:
    return VAULT_FILE.exists() and SALT_FILE.exists()


def create_vault(master_password: str):
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(VAULT_DIR, 0o700)
    salt = secrets.token_bytes(16)
    SALT_FILE.write_bytes(salt)
    os.chmod(SALT_FILE, 0o600)
    save_vault({}, master_password)


def load_vault(master_password: str):
    salt = SALT_FILE.read_bytes()
    key = derive_key(master_password, salt)
    f = Fernet(key)
    try:
        decrypted = f.decrypt(VAULT_FILE.read_bytes())
    except InvalidToken:
        return None
    return json.loads(decrypted.decode("utf-8"))


def save_vault(data: dict, master_password: str):
    salt = SALT_FILE.read_bytes()
    key = derive_key(master_password, salt)
    f = Fernet(key)
    encrypted = f.encrypt(json.dumps(data).encode("utf-8"))
    tmp = VAULT_FILE.with_suffix(".tmp")
    tmp.write_bytes(encrypted)
    tmp.replace(VAULT_FILE)
    os.chmod(VAULT_FILE, 0o600)


# ---------- password generation ----------

def generate_password(length: int = DEFAULT_LENGTH, use_symbols: bool = True) -> str:
    if length < 8:
        length = 8

    lower, upper, digits = string.ascii_lowercase, string.ascii_uppercase, string.digits
    symbols = "!@#$%^&*()-_=+[]{};:,.?"
    pools = [lower, upper, digits] + ([symbols] if use_symbols else [])
    alphabet = "".join(pools)

    chars = [secrets.choice(pool) for pool in pools]
    chars += [secrets.choice(alphabet) for _ in range(length - len(pools))]

    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]

    return "".join(chars)


# ---------- UI actions ----------

def action_generate(vault, master):
    banner()
    print(f"{BOLD}Generate & Save Password{RESET}\n")
    tag = prompt("Site / tag:")
    if not tag:
        print(f"{FLAME}Tag cannot be empty.{RESET}")
        pause()
        return

    if tag in vault:
        overwrite = prompt(f"'{tag}' already exists. Overwrite? (y/N):").lower()
        if overwrite != "y":
            return

    print()
    print(f"  {BOLD}{FLAME}[1]{RESET} {WHITE_ICE}Generate a random password{RESET}")
    print(f"  {BOLD}{FLAME}[2]{RESET} {WHITE_ICE}Create your own password{RESET}")
    mode = prompt("\nChoose:")

    if mode == "2":
        while True:
            password = prompt("Enter password:")
            if password:
                break
            print(f"{FLAME}Password cannot be empty.{RESET}")
    else:
        length_raw = prompt(f"Length (default {DEFAULT_LENGTH}):")
        length = int(length_raw) if length_raw.isdigit() else DEFAULT_LENGTH

        symbols_raw = prompt("Include symbols? (Y/n):").lower()
        use_symbols = symbols_raw != "n"

        password = generate_password(length, use_symbols)

    email = prompt("Gmail / email for this account (optional):")
    vault[tag] = {
        "password": password,
        "email": email,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    save_vault(vault, master)

    print()
    hr()
    box_line(f"{WHITE_ICE}Tag:{RESET}      {BOLD}{tag}{RESET}")
    box_line(f"{WHITE_ICE}Password:{RESET} {BRIGHT_FLAME}{BOLD}{password}{RESET}")
    if email:
        box_line(f"{WHITE_ICE}Email:{RESET}    {email}")
    hr()
    pause()


def action_list(vault, master):
    banner()
    print(f"{BOLD}All Saved Passwords{RESET}\n")
    if not vault:
        print(f"{DIM}Vault is empty.{RESET}")
        pause()
        return

    hr()
    for tag, entry in sorted(vault.items()):
        box_line(f"{BOLD}{FLAME}{tag}{RESET}")
        box_line(f"  password: {BRIGHT_FLAME}{entry['password']}{RESET}")
        box_line(f"  created:  {DIM}{entry.get('created', 'unknown')}{RESET}")
        hr()
    pause()


def action_search(vault, master):
    banner()
    print(f"{BOLD}Search Passwords{RESET}\n")
    query = prompt("Search tag or email:").lower()
    if not query:
        return

    results = {
        tag: entry
        for tag, entry in vault.items()
        if query in tag.lower() or query in entry.get("email", "").lower()
    }

    print()
    hr()
    if not results:
        box_line(f"{DIM}No matches found.{RESET}")
    for tag, entry in sorted(results.items()):
        box_line(f"{BOLD}{FLAME}{tag}{RESET}")
        box_line(f"  password: {BRIGHT_FLAME}{entry['password']}{RESET}")
        if entry.get("email"):
            box_line(f"  email:    {entry['email']}")
        hr()
    pause()


def action_list_with_email(vault, master):
    banner()
    print(f"{BOLD}Passwords with Linked Gmail / Email{RESET}\n")
    entries = {t: e for t, e in vault.items() if e.get("email")}

    hr()
    if not entries:
        box_line(f"{DIM}No entries have a linked email.{RESET}")
    for tag, entry in sorted(entries.items()):
        box_line(f"{BOLD}{FLAME}{tag}{RESET}")
        box_line(f"  email:    {WHITE_ICE}{entry['email']}{RESET}")
        box_line(f"  password: {BRIGHT_FLAME}{entry['password']}{RESET}")
        hr()
    pause()


def unlock_or_create():
    if not vault_exists():
        banner()
        print(f"{BOLD}No vault found — let's create one.{RESET}\n")
        master = prompt_master(confirm=True)
        create_vault(master)
        print(f"\n{FLAME}Vault created at {VAULT_FILE}{RESET}")
        pause()
        return master, {}

    banner()
    for attempt in range(3):
        master = prompt_master()
        vault = load_vault(master)
        if vault is not None:
            return master, vault
        print(f"{FLAME}Wrong master password.{RESET}")
    print(f"{FLAME}Too many failed attempts. Exiting.{RESET}")
    sys.exit(1)


def main_menu():
    master, vault = unlock_or_create()

    menu = {
        "1": ("Add Password (generate or create)", action_generate),
        "2": ("List Passwords", action_list),
        "3": ("Search Passwords", action_search),
        "4": ("List Passwords + Gmail", action_list_with_email),
        "5": ("Exit", None),
    }

    while True:
        banner()
        for key, (label, _) in menu.items():
            print(f"  {BOLD}{FLAME}[{key}]{RESET} {WHITE_ICE}{label}{RESET}")
        hr()
        choice = prompt("\nChoose an option:")

        if choice == "5" or choice.lower() in ("q", "quit", "exit"):
            print(f"\n{DIM}Vanishing into the mist...{RESET}\n")
            break

        entry = menu.get(choice)
        if entry is None:
            continue

        _, func = entry
        vault = load_vault(master)  # always re-read latest state before acting
        if vault is None:
            print(f"{FLAME}Vault could not be decrypted. Exiting.{RESET}")
            sys.exit(1)
        func(vault, master)


if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        print(f"\n{DIM}Vanishing into the mist...{RESET}\n")
        sys.exit(0)

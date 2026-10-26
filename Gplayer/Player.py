#!/usr/bin/env python3
"""ytplay - stream a text playlist (like imp.txt) through yt-dlp + mpv, in your terminal.

  imp.txt lines look like:   -->yeh ishq hai      (one song per line; a URL works too)

  python3 ytplay.py imp.txt              play the list
  python3 ytplay.py imp.txt -s -l        shuffle + loop forever
  python3 ytplay.py imp.txt --suffix " official audio"   steer the YouTube search

Needs:  sudo pacman -S mpv yt-dlp      (BlackArch / Arch)
Songs are streamed, nothing is saved to disk.

Keys:  space pause | n next | b back/restart | <- -> seek 10s | up/down volume
       s shuffle | l loop | 1-9 jump to song | q quit
"""
import argparse, json, os, random, re, select, shutil, socket, subprocess, sys
import tempfile, threading, time

try:
    import termios, tty
except ImportError:          # not on a POSIX terminal
    termios = None


def fg(n):
    return "\033[38;5;%dm" % n


RST, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
PINK, GOLD, GREY, BLUE, GREEN, RED = 213, 222, 245, 111, 120, 203


def load_playlist(path):
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            s = re.sub(r"^\s*(-+>+|>+|[-*\u2022])\s*", "", s).strip()   # drop the "-->" marker
            if s:
                items.append(s)
    return items


def resolve(query, suffix):
    """Ask yt-dlp for the best match -> (title, url, duration) or None."""
    target = query if query.startswith(("http://", "https://")) else "ytsearch1:" + query + suffix
    try:
        out = subprocess.run(
            ["yt-dlp", "--no-playlist", "--no-warnings", "-q", "--skip-download",
             "--print", "%(title)s|||%(webpage_url)s|||%(duration)s", target],
            capture_output=True, text=True, timeout=45).stdout
    except Exception:
        return None
    for line in reversed(out.strip().splitlines()):
        parts = line.split("|||")
        if len(parts) == 3 and parts[1].startswith("http"):
            try:
                dur = float(parts[2])
            except ValueError:
                dur = None
            return parts[0], parts[1], dur
    return None


class Library:
    """Resolves songs in background threads so the next one is ready in advance."""
    def __init__(self, queries, suffix):
        self.q, self.suffix, self.res, self.ev = queries, suffix, {}, {}

    def start(self, i):
        if i in self.ev:
            return
        self.ev[i] = threading.Event()
        threading.Thread(target=self._work, args=(i,), daemon=True).start()

    def _work(self, i):
        self.res[i] = resolve(self.q[i], self.suffix)
        self.ev[i].set()

    def ready(self, i):
        return i in self.ev and self.ev[i].is_set()


class MPV:
    """One mpv process per song, controlled over its JSON IPC socket."""
    def __init__(self, url, volume):
        self.path = os.path.join(tempfile.gettempdir(), "ytplay-%d-%d.sock" % (os.getpid(), int(time.time() * 1000)))
        self.proc = subprocess.Popen(
            ["mpv", "--no-video", "--no-terminal", "--force-window=no", "--ytdl-format=bestaudio/best",
             "--volume=%d" % volume, "--input-ipc-server=" + self.path, url],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sock, self.buf, self.rid, self.t0 = None, b"", 0, time.time()

    def _connect(self):
        if self.sock or self.proc.poll() is not None:
            return
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(self.path)
            s.settimeout(0.25)
            self.sock = s
        except OSError:
            pass

    def cmd(self, *args):
        self._connect()
        if not self.sock:
            return None
        self.rid += 1
        try:
            self.sock.sendall((json.dumps({"command": list(args), "request_id": self.rid}) + "\n").encode())
            end = time.time() + 0.6
            while time.time() < end:
                while b"\n" in self.buf:
                    line, self.buf = self.buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("request_id") == self.rid:
                        return msg.get("data")
                try:
                    chunk = self.sock.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    return None
                self.buf += chunk
        except OSError:
            return None
        return None

    def get(self, prop):
        return self.cmd("get_property", prop)

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        try:
            if self.alive():
                self.proc.terminate()
                self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            if self.sock:
                self.sock.close()
            os.remove(self.path)
        except OSError:
            pass


def mmss(s):
    s = int(max(0, s or 0))
    return "%02d:%02d" % (s // 60, s % 60)


def clip(s, w):
    return s if len(s) <= w else s[:max(1, w - 1)] + "…"


class Player:
    def __init__(self, queries, args):
        self.q, self.n = queries, len(queries)
        self.lib = Library(queries, args.suffix)
        self.vol, self.shuffle, self.loop = args.volume, args.shuffle, args.loop
        self.order, self.pos = list(range(self.n)), 0
        if self.shuffle:
            random.shuffle(self.order)
        self.mpv, self.title, self.dur, self.t = None, "", None, 0.0
        self.paused, self.status, self.heard = False, {}, False

    # ---- navigation -------------------------------------------------------
    def stop_current(self):
        if self.mpv:
            self.mpv.close()
            self.mpv = None

    def advance(self, delta):
        new = self.pos + delta
        if new >= self.n or new < 0:
            if self.loop:
                new %= self.n
            elif delta > 0:
                return False
            else:
                new = 0
        self.pos = new
        return True

    def reshuffle(self):
        cur = self.order[self.pos]
        if self.shuffle:
            rest = [i for i in range(self.n) if i != cur]
            random.shuffle(rest)
            self.order, self.pos = [cur] + rest, 0
        else:
            self.order, self.pos = list(range(self.n)), cur

    # ---- screen -----------------------------------------------------------
    def draw(self):
        cols, rows = shutil.get_terminal_size((80, 24))
        cur = self.order[self.pos]
        L = ["", " %s♪  ytplay%s  %s%s%s" % (fg(PINK) + BOLD, RST, fg(GREY),
             "shuffle:%s  loop:%s  vol:%d" % ("on" if self.shuffle else "off", "on" if self.loop else "off", self.vol), RST), ""]
        room = max(3, rows - 11)
        start = max(0, min(self.n - room, self.pos - room // 2)) if self.n > room else 0
        for p in range(start, min(self.n, start + room)):
            i = self.order[p]
            note = self.status.get(i, "")
            if i == cur:
                L.append("  %s▶ %d  %s%s" % (fg(GOLD) + BOLD, i + 1, clip(self.q[i], cols - 14), RST))
            else:
                L.append("  %s  %d  %s%s" % (fg(GREY), i + 1, clip(self.q[i], cols - 14), (" (%s)" % note) if note else "") + RST)
        L.append("")
        if self.mpv and self.heard:
            L.append("  %s%s%s" % (fg(PINK), clip(self.title, cols - 6), RST))
            total = self.dur or 0
            width = max(10, min(40, cols - 30))
            frac = min(1.0, self.t / total) if total else 0
            k = int(frac * width)
            bar = "━" * k + "●" + "─" * (width - k)
            L.append("  %s%s%s  %s / %s  %s" % (fg(GOLD), bar, RST, mmss(self.t), mmss(total) if total else "--:--",
                                                "❚❚" if self.paused else "▶"))
        elif self.mpv:
            L.append("  %sloading %s ...%s" % (fg(BLUE), clip(self.q[cur], cols - 20), RST))
        else:
            L.append("  %ssearching for %s ...%s" % (fg(BLUE), clip(self.q[cur], cols - 22), RST))
            L.append("")
        L += ["", "  %sspace pause · n next · b back · ←/→ seek · ↑/↓ volume · s shuffle · l loop · q quit%s" % (fg(GREY), RST)]
        sys.stdout.write("\033[H" + "".join(x + "\033[K\n" for x in L) + "\033[J")
        sys.stdout.flush()

    # ---- keys -------------------------------------------------------------
    def handle(self, k):
        """Returns True to quit."""
        if k in (b"q", b"\x03"):
            return True
        if k == b"n":
            self.stop_current()
            return not self.advance(1)
        if k == b"b":
            if self.mpv and self.t > 5:
                self.mpv.cmd("seek", 0, "absolute")
            else:
                self.stop_current()
                self.advance(-1)
        elif k == b" " and self.mpv:
            self.mpv.cmd("cycle", "pause")
        elif k == b"\x1b[C" and self.mpv:
            self.mpv.cmd("seek", 10, "relative")
        elif k == b"\x1b[D" and self.mpv:
            self.mpv.cmd("seek", -10, "relative")
        elif k in (b"\x1b[A", b"+", b"="):
            self.set_volume(5)
        elif k in (b"\x1b[B", b"-"):
            self.set_volume(-5)
        elif k == b"s":
            self.shuffle = not self.shuffle
            self.reshuffle()
        elif k == b"l":
            self.loop = not self.loop
        elif k.isdigit() and k != b"0" and int(k) <= self.n:
            self.stop_current()
            self.pos = self.order.index(int(k) - 1)
        return False

    def set_volume(self, d):
        self.vol = max(0, min(130, self.vol + d))
        if self.mpv:
            self.mpv.cmd("set_property", "volume", self.vol)

    def read_key(self, timeout):
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return os.read(sys.stdin.fileno(), 8) if r else b""

    # ---- main loop --------------------------------------------------------
    def run(self):
        interactive = sys.stdin.isatty() and termios is not None
        old = None
        if interactive:
            old = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        sys.stdout.write("\033[?25l\033[2J")
        try:
            while True:
                i = self.order[self.pos]
                self.lib.start(i)
                if self.mpv is None:
                    if self.lib.ready(i):
                        res = self.lib.res[i]
                        if res is None:
                            self.status[i] = "not found"
                            if not self.advance(1):
                                return
                            continue
                        self.title, url, self.dur = res
                        self.mpv, self.heard, self.t, self.paused = MPV(url, self.vol), False, 0.0, False
                        self.lib.start(self.order[(self.pos + 1) % self.n])      # pre-fetch next
                else:
                    if not self.mpv.alive():
                        failed = not self.heard
                        self.stop_current()
                        if failed:
                            self.status[i] = "could not play"
                        if not self.advance(1):
                            return
                        continue
                    t = self.mpv.get("time-pos")
                    if t is not None:
                        self.heard, self.t = True, float(t)
                        d = self.mpv.get("duration")
                        self.dur = float(d) if d else self.dur
                        self.paused = bool(self.mpv.get("pause"))
                self.draw()
                key = self.read_key(0.35) if interactive else (time.sleep(0.35) or b"")
                if key and self.handle(key):
                    return
        finally:
            self.stop_current()
            if old:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
            sys.stdout.write("\033[?25h" + RST + "\n")


def main():
    ap = argparse.ArgumentParser(description="Stream a playlist file through yt-dlp + mpv.")
    ap.add_argument("playlist", nargs="?", default="imp.txt", help="text file, one song per line (default: imp.txt)")
    ap.add_argument("-s", "--shuffle", action="store_true")
    ap.add_argument("-l", "--loop", action="store_true", help="repeat the playlist forever")
    ap.add_argument("--suffix", default=" song", help="added to each search (default: ' song')")
    ap.add_argument("--volume", type=int, default=80)
    args = ap.parse_args()
    missing = [x for x in ("mpv", "yt-dlp") if not shutil.which(x)]
    if missing:
        print("Missing: %s\nInstall with:  sudo pacman -S %s" % (", ".join(missing), " ".join(missing)))
        return 1
    try:
        queries = load_playlist(args.playlist)
    except OSError as e:
        print("Can't read %s: %s" % (args.playlist, e))
        return 1
    if not queries:
        print("%s has no songs in it." % args.playlist)
        return 1
    Player(queries, args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

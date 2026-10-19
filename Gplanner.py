#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIMS - a Year / Month / Day aim planner that lives entirely in your terminal.

Run:    python3 aims.py              (add --ascii for plain-ASCII glyphs)
Data:   ~/.aimplanner/aims.json      (override the folder with AIMS_HOME=/dir)
Keys:   press ? inside the app for the full reference.

Features: 3-level planning (year > month > day), tick/untick, subtasks with
auto-complete, priorities, tags, notes, parent linking (day -> month -> year),
recurring habits, rollover of unfinished aims, quick daily tasks (D) + routines (O),
statistics + streaks, markdown export, atomic saves, rotating backups,
unlimited-ish undo/redo.
"""
import calendar
import copy
import curses
import datetime as dt
import json
import locale
import os
import shutil
import sys
import textwrap
import time
import uuid

APP_DIR = os.environ.get("AIMS_HOME", os.path.expanduser("~/.aimplanner"))
DATA_FILE = os.path.join(APP_DIR, "aims.json")
BACKUP_DIR = os.path.join(APP_DIR, "backups")
EXPORT_DIR = os.path.join(APP_DIR, "exports")

LEVELS = ("year", "month", "day")
PRIO_LABEL = {1: "H", 2: "M", 3: "L"}
PRIO_WORD = {1: "high", 2: "medium", 3: "low"}
SORTS = ("pending-first", "priority", "alphabetical", "created")
FILTERS = ("all", "pending", "done")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTH_ABBR = [calendar.month_abbr[i] for i in range(1, 13)]
TICK_ON, TICK_OFF = "[x]", "[ ]"

FULL, EMPTY, CHECK, CUR, ELL = "#", ".", "v", ">", "~"


def setup_glyphs(ascii_mode):
    global FULL, EMPTY, CHECK, CUR, ELL
    if ascii_mode:
        FULL, EMPTY, CHECK, CUR, ELL = "#", ".", "v", ">", "~"
    else:
        FULL, EMPTY, CHECK, CUR, ELL = "\u2588", "\u2591", "\u2713", "\u25b6", "\u2026"


# --------------------------------------------------------------------------
# date helpers
# --------------------------------------------------------------------------
def key_for(level, d):
    if level == "year":
        return "%04d" % d.year
    if level == "month":
        return "%04d-%02d" % (d.year, d.month)
    return d.isoformat()


def date_from_key(level, key):
    p = key.split("-")
    if level == "year":
        return dt.date(int(p[0]), 1, 1)
    if level == "month":
        return dt.date(int(p[0]), int(p[1]), 1)
    return dt.date(int(p[0]), int(p[1]), int(p[2]))


def label_for(level, d):
    if level == "year":
        return "Year %d" % d.year
    if level == "month":
        return d.strftime("%B %Y")
    return d.strftime("%A, %d %B %Y")


def shift(d, unit, n):
    try:
        if unit == "day":
            return d + dt.timedelta(days=n)
        if unit == "month":
            y, m = divmod(d.year * 12 + (d.month - 1) + n, 12)
            return dt.date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))
        y = d.year + n
        return dt.date(y, d.month, min(d.day, calendar.monthrange(y, d.month)[1]))
    except (ValueError, OverflowError):
        return d


def parse_target(text, level, base):
    """Parse 'today', '+3', '-1', '2027', '11', '2026-11', '25', '10-25', full dates."""
    t = text.strip().lower()
    if not t:
        return None
    try:
        if t in ("t", "today", "now"):
            return dt.date.today()
        if t[0] in "+-" and t[1:].isdigit():
            return shift(base, level, int(t))
        nums = [int(p) for p in t.replace("/", "-").split("-")]
        if len(nums) == 3:
            return dt.date(nums[0], nums[1], nums[2])
        if level == "year" and len(nums) == 1:
            return dt.date(nums[0], 1, 1)
        if level == "month":
            if len(nums) == 1:
                return dt.date(base.year, nums[0], 1)
            if len(nums) == 2:
                return dt.date(nums[0], nums[1], 1)
        if level == "day":
            if len(nums) == 1:
                return dt.date(base.year, base.month, nums[0])
            if len(nums) == 2:
                return dt.date(base.year, nums[0], nums[1])
    except (ValueError, OverflowError):
        return None
    return None


def parse_days(text):
    t = text.strip().lower()
    if t in ("", "daily", "everyday", "all"):
        return list(range(7))
    if t == "weekdays":
        return list(range(5))
    if t == "weekends":
        return [5, 6]
    out = set()
    for p in t.replace(" ", ",").split(","):
        if not p:
            continue
        names = [n for n in DAY_NAMES if n.startswith(p[:3])]
        if not names:
            return None
        out.add(DAY_NAMES.index(names[0]))
    return sorted(out) or None


def fmt_days(days):
    if len(days) == 7:
        return "daily"
    if days == [0, 1, 2, 3, 4]:
        return "weekdays"
    if days == [5, 6]:
        return "weekends"
    return ",".join(DAY_NAMES[i] for i in days)


def parse_prio(text, default):
    t = text.strip().lower()
    return {"1": 1, "h": 1, "2": 2, "m": 2, "3": 3, "l": 3}.get(t, default)


def parse_tags(text):
    out = []
    for p in text.replace(",", " ").split():
        p = p.strip("#").lower()
        if p and p not in out:
            out.append(p)
    return out


def counts(items):
    return sum(1 for a in items if a["done"]), len(items)


def pct(d, t):
    return int(round(100.0 * d / t)) if t else 0


def bar(d, t, width):
    n = int(round(width * d / t)) if t else 0
    return FULL * n + EMPTY * (width - n)


def clip(s, n):
    if n <= 0:
        return ""
    return s if len(s) <= n else s[: max(0, n - 1)] + ELL


# --------------------------------------------------------------------------
# data store
# --------------------------------------------------------------------------
class Store:
    def __init__(self):
        self.aims = []
        self.habits = []
        self.dailies = []
        self.routines = []
        self.undo_stack = []
        self.redo_stack = []
        self.load()
        self.backup()

    # persistence ---------------------------------------------------------
    def load(self):
        if not os.path.exists(DATA_FILE):
            return
        f = open(DATA_FILE, "r", encoding="utf-8")
        try:
            raw = f.read()
        finally:
            f.close()
        try:
            data = json.loads(raw)
            self.aims = list(data.get("aims", []))
            self.habits = list(data.get("habits", []))
        except (ValueError, AttributeError):
            os.replace(DATA_FILE, DATA_FILE + ".corrupt-%d" % int(time.time()))
            self.aims, self.habits = [], []

    def save(self):
        os.makedirs(APP_DIR, exist_ok=True)
        tmp = DATA_FILE + ".tmp"
        f = open(tmp, "w", encoding="utf-8")
        try:
            json.dump({"version": 1, "aims": self.aims, "habits": self.habits},
                      f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        finally:
            f.close()
        os.replace(tmp, DATA_FILE)

    def backup(self):
        if not os.path.exists(DATA_FILE):
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        name = "aims-%s.json" % dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DATA_FILE, os.path.join(BACKUP_DIR, name))
        olds = sorted(n for n in os.listdir(BACKUP_DIR) if n.startswith("aims-"))
        for n in olds[:-10]:
            os.remove(os.path.join(BACKUP_DIR, n))

    # undo / redo ---------------------------------------------------------
    def _snap(self):
        return copy.deepcopy(self.aims), copy.deepcopy(self.habits)

    def checkpoint(self):
        self.undo_stack.append(self._snap())
        del self.undo_stack[:-100]
        self.redo_stack = []

    def undo(self):
        if not self.undo_stack:
            return False
        self.redo_stack.append(self._snap())
        self.aims, self.habits = self.undo_stack.pop()
        self.save()
        return True

    def redo(self):
        if not self.redo_stack:
            return False
        self.undo_stack.append(self._snap())
        self.aims, self.habits = self.redo_stack.pop()
        self.save()
        return True

    # queries -------------------------------------------------------------
    def get(self, aid):
        for a in self.aims:
            if a["id"] == aid:
                return a
        return None

    def scope(self, level, key):
        return [a for a in self.aims if a["level"] == level and a["key"] == key]

    def within(self, level, prefix):
        return [a for a in self.aims if a["level"] == level and a["key"].startswith(prefix)]

    def children(self, aid):
        return [a for a in self.aims if a.get("parent") == aid]

    def day_map(self):
        m = {}
        for a in self.aims:
            if a["level"] == "day":
                v = m.setdefault(a["key"], [0, 0])
                v[1] += 1
                if a["done"]:
                    v[0] += 1
        return m

    def streaks(self):
        m = self.day_map()

        def complete(d):
            v = m.get(d.isoformat())
            return bool(v) and v[1] > 0 and v[0] == v[1]

        d = dt.date.today()
        if not complete(d):
            d -= dt.timedelta(days=1)
        cur = 0
        while complete(d) and cur < 100000:
            cur += 1
            d -= dt.timedelta(days=1)
        best = run = 0
        prev = None
        for k in sorted(m):
            day = dt.date.fromisoformat(k)
            if complete(day):
                run = run + 1 if prev is not None and (day - prev).days == 1 else 1
                prev = day
                best = max(best, run)
            else:
                prev, run = None, 0
        return cur, best

    # mutations -----------------------------------------------------------
    def add(self, title, level, key, prio=2, tags=None, notes="", parent=None,
            habit=None, record=True):
        if record:
            self.checkpoint()
        a = {"id": uuid.uuid4().hex[:8], "title": title, "level": level, "key": key,
             "prio": prio, "tags": tags or [], "notes": notes, "done": False,
             "done_at": None, "subs": [], "parent": parent, "habit": habit,
             "created": time.time()}
        self.aims.append(a)
        if record:
            self.save()
        return a

    def update(self, aid, **fields):
        a = self.get(aid)
        if a is None:
            return
        self.checkpoint()
        a.update(fields)
        self.save()

    @staticmethod
    def _set_done(a, state):
        a["done"] = state
        a["done_at"] = dt.datetime.now().isoformat(timespec="seconds") if state else None

    def toggle(self, aid):
        a = self.get(aid)
        if a is None:
            return
        self.checkpoint()
        state = not a["done"]
        self._set_done(a, state)
        for s in a["subs"]:
            s["d"] = state
        self.save()

    def set_many(self, ids, state):
        self.checkpoint()
        for aid in ids:
            a = self.get(aid)
            if a is not None:
                self._set_done(a, state)
                for s in a["subs"]:
                    s["d"] = state
        self.save()

    def _sync_subs(self, a):
        if a["subs"]:
            all_done = all(s["d"] for s in a["subs"])
            if all_done != a["done"]:
                self._set_done(a, all_done)

    def toggle_sub(self, aid, i):
        a = self.get(aid)
        if a is None or not 0 <= i < len(a["subs"]):
            return
        self.checkpoint()
        a["subs"][i]["d"] = not a["subs"][i]["d"]
        self._sync_subs(a)
        self.save()

    def add_sub(self, aid, text):
        a = self.get(aid)
        if a is None:
            return
        self.checkpoint()
        a["subs"].append({"t": text, "d": False})
        self._sync_subs(a)
        self.save()

    def rename_sub(self, aid, i, text):
        a = self.get(aid)
        if a is None or not 0 <= i < len(a["subs"]):
            return
        self.checkpoint()
        a["subs"][i]["t"] = text
        self.save()

    def delete_sub(self, aid, i):
        a = self.get(aid)
        if a is None or not 0 <= i < len(a["subs"]):
            return
        self.checkpoint()
        del a["subs"][i]
        self._sync_subs(a)
        self.save()

    def delete(self, aid):
        a = self.get(aid)
        if a is None:
            return
        self.checkpoint()
        if a.get("habit"):
            for h in self.habits:
                if h["id"] == a["habit"] and a["key"] not in h["skipped"]:
                    h["skipped"].append(a["key"])
        for c in self.aims:
            if c.get("parent") == aid:
                c["parent"] = None
        self.aims = [x for x in self.aims if x["id"] != aid]
        self.save()

    def rollover(self, level, from_key, to_key):
        self.checkpoint()
        n = 0
        for a in self.aims:
            if (a["level"] == level and a["key"] == from_key
                    and not a["done"] and not a.get("habit")):
                a["key"] = to_key
                a["parent"] = None
                n += 1
        self.save()
        return n

    def duplicate(self, aid, new_key):
        a = self.get(aid)
        if a is None:
            return None
        self.checkpoint()
        n = self.add(a["title"], a["level"], new_key, a["prio"], list(a["tags"]),
                     a["notes"], None, None, record=False)
        n["subs"] = [{"t": s["t"], "d": False} for s in a["subs"]]
        self.save()
        return n

    # habits --------------------------------------------------------------
    def add_habit(self, title, days, prio, tags, start):
        self.checkpoint()
        h = {"id": uuid.uuid4().hex[:8], "title": title, "days": sorted(days),
             "prio": prio, "tags": tags, "start": start.isoformat(), "skipped": []}
        self.habits.append(h)
        self.save()
        return h

    def delete_habit(self, hid):
        self.checkpoint()
        today = dt.date.today().isoformat()
        self.aims = [a for a in self.aims
                     if not (a.get("habit") == hid and a["key"] >= today and not a["done"])]
        self.habits = [h for h in self.habits if h["id"] != hid]
        self.save()

    def materialize(self, day):
        iso = day.isoformat()
        created = False
        for h in self.habits:
            if day.weekday() not in h["days"] or iso < h["start"] or iso in h["skipped"]:
                continue
            if any(a.get("habit") == h["id"] and a["key"] == iso for a in self.aims):
                continue
            self.add(h["title"], "day", iso, h["prio"], list(h["tags"]), "", None,
                     h["id"], record=False)
            created = True
        if created:
            self.save()
        return created

    # export --------------------------------------------------------------
    def export_markdown(self, level, cur):
        prefix = key_for(level, cur)
        items = [a for a in self.aims if a["key"].startswith(prefix)]
        items.sort(key=lambda a: (LEVELS.index(a["level"]), a["key"], a["prio"], a["created"]))
        out = ["# Aims for %s" % prefix,
               "_exported %s_" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"), ""]
        last = None
        for a in items:
            grp = (a["level"], a["key"])
            if grp != last:
                out += ["", "## %s %s" % (a["level"].upper(), a["key"])]
                last = grp
            tags = (" " + " ".join("#" + t for t in a["tags"])) if a["tags"] else ""
            out.append("- [%s] **%s** (%s)%s" % ("x" if a["done"] else " ", a["title"],
                                                 PRIO_WORD[a["prio"]], tags))
            if a["notes"]:
                out.append("  - note: %s" % a["notes"])
            for s in a["subs"]:
                out.append("  - [%s] %s" % ("x" if s["d"] else " ", s["t"]))
        os.makedirs(EXPORT_DIR, exist_ok=True)
        path = os.path.join(EXPORT_DIR, "aims-%s.md" % prefix)
        f = open(path, "w", encoding="utf-8")
        try:
            f.write("\n".join(out) + "\n")
        finally:
            f.close()
        return path


HELP = [
    "#NAVIGATION",
    "  1 2 3 / Tab / Shift-Tab    switch Year / Month / Day view",
    "  Left / Right               move the date cursor (year view: months, else days)",
    "  [ ]  or  PgUp / PgDn       jump a whole unit (year / month / week)",
    "  z / Z                      zoom in (year>month>day) / zoom out",
    "  t   go to today            g   go to a date (2026-12-25, +3, 11, today)",
    "  Up / Down / j / k          select an aim      Home / End   first / last",
    "#AIMS",
    "  a   add aim                e   edit title / priority / tags / notes",
    "  Space or x   tick / untick *   tick (or untick) everything shown",
    "  Enter   open details: subtasks (auto-complete the aim), notes, links",
    "  s   quick-add a subtask    p   cycle priority    d / Del   delete",
    "  l   link to a parent aim (day>month>year)",
    "  m   move to another period    c   copy to another period",
    "  R   roll unfinished aims over from the previous period",
    "#VIEW TOOLS",
    "  f   filter all/pending/done    o   cycle sort    T   filter by tag",
    "  /   search everything         S   statistics, streaks, 14-day chart",
    "  H   recurring habits           E   export this period to markdown",
    "#SYSTEM",
    "  u   undo      U   redo      ?   this help      q   quit (always autosaved)",
    "",
    "  data: %s" % DATA_FILE,
]


# --------------------------------------------------------------------------
# curses application
# --------------------------------------------------------------------------
class App:
    def __init__(self, scr, store):
        self.scr = scr
        self.store = store
        self.view = "day"
        self.cur = dt.date.today()
        self.sel = 0
        self.top = 0
        self.filt = 0
        self.tag = None
        self.sort = 0
        self.msg = None

    # setup / io ----------------------------------------------------------
    def setup(self):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED,
                               curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_BLUE), 1):
            curses.init_pair(i, c, bg)
        self.col = {"ok": curses.color_pair(1), "warn": curses.color_pair(2),
                    "bad": curses.color_pair(3),
                    "head": curses.color_pair(4) | curses.A_BOLD,
                    "tag": curses.color_pair(5), "info": curses.color_pair(6),
                    "dim": curses.A_DIM}
        self.scr.keypad(True)

    def put(self, y, x, s, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        s = s[: max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass

    def getkey(self):
        while True:
            try:
                return self.scr.get_wch()
            except curses.error:
                continue

    def flash(self, text, kind="info"):
        self.msg = (text, time.time() + 5, self.col.get(kind, 0))

    def prompt(self, label, default=""):
        buf = list(default)
        pos = len(buf)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                h, w = self.scr.getmaxyx()
                y = h - 2
                self.put(y, 0, " " * (w - 1))
                avail = max(1, w - 2 - len(label))
                start = pos - avail + 1 if pos >= avail else 0
                self.put(y, 0, label, self.col["head"])
                self.put(y, len(label), "".join(buf)[start:start + avail])
                try:
                    self.scr.move(y, min(w - 1, len(label) + pos - start))
                except curses.error:
                    pass
                self.scr.refresh()
                k = self.getkey()
                if k in ("\n", "\r") or k == curses.KEY_ENTER:
                    return "".join(buf).strip()
                if k == "\x1b":
                    return None
                if k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if pos > 0:
                        del buf[pos - 1]
                        pos -= 1
                elif k == curses.KEY_DC:
                    if pos < len(buf):
                        del buf[pos]
                elif k == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                elif k == curses.KEY_RIGHT:
                    pos = min(len(buf), pos + 1)
                elif k in (curses.KEY_HOME, "\x01"):
                    pos = 0
                elif k in (curses.KEY_END, "\x05"):
                    pos = len(buf)
                elif k == "\x15":
                    buf, pos = [], 0
                elif isinstance(k, str) and k.isprintable():
                    buf.insert(pos, k)
                    pos += 1
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def confirm(self, text):
        h, w = self.scr.getmaxyx()
        self.put(h - 2, 0, " " * (w - 1))
        self.put(h - 2, 0, text + " [y/N] ", self.col["warn"] | curses.A_BOLD)
        self.scr.refresh()
        return self.getkey() in ("y", "Y")

    def pick(self, title, options, start=0):
        sel, top = start, 0
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.put(0, 1, title, self.col["head"])
            rows = max(1, h - 4)
            if sel < top:
                top = sel
            if sel >= top + rows:
                top = sel - rows + 1
            for i in range(rows):
                idx = top + i
                if idx >= len(options):
                    break
                on = idx == sel
                self.put(2 + i, 2, (CUR + " " if on else "  ") + clip(options[idx], w - 6),
                         curses.A_REVERSE if on else 0)
            self.put(h - 1, 1, "Up/Down choose   Enter select   Esc cancel", self.col["dim"])
            self.scr.refresh()
            k = self.getkey()
            if k in (curses.KEY_UP, "k"):
                sel = max(0, sel - 1)
            elif k in (curses.KEY_DOWN, "j"):
                sel = min(len(options) - 1, sel + 1)
            elif k in ("\n", "\r", curses.KEY_ENTER):
                return sel
            elif k in ("\x1b", "q"):
                return None

    def pager(self, title, lines):
        top = 0
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.put(0, 1, title, self.col["head"] | curses.A_REVERSE)
            rows = max(1, h - 3)
            top = max(0, min(top, max(0, len(lines) - rows)))
            for i in range(rows):
                if top + i >= len(lines):
                    break
                text, attr = lines[top + i]
                self.put(2 + i, 0, text, attr)
            self.put(h - 1, 1, "Up/Down/PgUp/PgDn scroll   q / Esc / Enter close", self.col["dim"])
            self.scr.refresh()
            k = self.getkey()
            if k in (curses.KEY_UP, "k"):
                top -= 1
            elif k in (curses.KEY_DOWN, "j"):
                top += 1
            elif k == curses.KEY_PPAGE:
                top -= rows
            elif k == curses.KEY_NPAGE:
                top += rows
            elif k in ("\x1b", "q", "\n", "\r", curses.KEY_ENTER):
                return

    # state helpers -------------------------------------------------------
    def visible(self):
        items = self.store.scope(self.view, key_for(self.view, self.cur))
        if self.filt == 1:
            items = [a for a in items if not a["done"]]
        elif self.filt == 2:
            items = [a for a in items if a["done"]]
        if self.tag:
            items = [a for a in items if self.tag in a["tags"]]
        if self.sort == 0:
            return sorted(items, key=lambda a: (a["done"], a["prio"], a["created"]))
        if self.sort == 1:
            return sorted(items, key=lambda a: (a["prio"], a["done"], a["created"]))
        if self.sort == 2:
            return sorted(items, key=lambda a: a["title"].lower())
        return sorted(items, key=lambda a: a["created"])

    def select_id(self, aid):
        for i, a in enumerate(self.visible()):
            if a["id"] == aid:
                self.sel = i
                return

    def after_nav(self):
        self.sel = 0
        self.top = 0
        if self.view == "day":
            self.store.materialize(self.cur)

    def set_view(self, level):
        self.view = level
        self.after_nav()

    def goto_aim(self, a):
        self.view = a["level"]
        self.cur = date_from_key(a["level"], a["key"])
        self.filt, self.tag = 0, None
        self.after_nav()
        self.select_id(a["id"])

    # drawing -------------------------------------------------------------
    def year_cols(self, w):
        return max(1, min(3, w // 28))

    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        c = self.col
        if h < 14 or w < 60:
            self.put(0, 0, "Terminal too small: need 60x14 (have %dx%d)." % (w, h), c["bad"])
            self.put(1, 0, "Resize the window, or press q to quit.")
            scr.refresh()
            return
        today = dt.date.today()
        self.put(0, 1, " AIMS ", c["head"] | curses.A_REVERSE)
        x = 9
        for i, lv in enumerate(LEVELS):
            txt = " %d %s " % (i + 1, lv.title())
            self.put(0, x, txt, (curses.A_REVERSE | curses.A_BOLD) if lv == self.view
                     else c["dim"])
            x += len(txt) + 1
        tl = "today: " + today.strftime("%a %d %b %Y")
        self.put(0, w - len(tl) - 1, tl, c["dim"])

        scope = self.store.scope(self.view, key_for(self.view, self.cur))
        d, t = counts(scope)
        label = label_for(self.view, self.cur)
        cs, bs = self.store.streaks()
        info = "%s %d/%d (%d%%)  streak %dd (best %dd)" % (bar(d, t, 14), d, t, pct(d, t), cs, bs)
        if len(label) + len(info) + 4 > w:
            info = "%s %d/%d" % (bar(d, t, 10), d, t)
        self.put(1, 1, label, c["head"])
        self.put(1, w - len(info) - 1, info, c["ok"] if t and d == t else 0)

        oh = {"year": -(-12 // self.year_cols(w)), "month": 7, "day": 4}[self.view]
        if h - (2 + oh) - 5 < 5:
            oh = 0
        if oh:
            {"year": self.ov_year, "month": self.ov_month, "day": self.ov_day}[self.view](2, w)
        ly = 2 + oh + (1 if oh else 0)

        items = self.visible()
        n = len(items)
        self.sel = max(0, min(self.sel, n - 1))
        rows = (h - 2) - (ly + 1)
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        self.top = max(0, self.top)
        head = " %s AIMS (%d)  filter:%s  sort:%s%s " % (
            self.view.upper(), n, FILTERS[self.filt], SORTS[self.sort],
            ("  tag:#" + self.tag) if self.tag else "")
        if n > rows:
            head += " [%d-%d of %d]" % (self.top + 1, min(n, self.top + rows), n)
        self.put(ly, 0, head + " " * w, c["head"] | curses.A_UNDERLINE)
        if n == 0:
            self.put(ly + 2, 3, "(nothing here - press 'a' to add an aim, '?' for help)", c["dim"])
        for i in range(rows):
            idx = self.top + i
            if idx >= n:
                break
            self.draw_item(ly + 1 + i, w, items[idx], idx == self.sel)

        if self.msg and time.time() < self.msg[1]:
            self.put(h - 2, 1, self.msg[0], self.msg[2] | curses.A_BOLD)
        elif n and items[self.sel]["notes"]:
            self.put(h - 2, 1, "note: " + clip(items[self.sel]["notes"], w - 10), c["dim"])
        help_line = ("a add  space tick  enter open  e edit  d del  m move  R rollover  "
                     "z zoom  / find  S stats  H habits  ? help  q quit")
        self.put(h - 1, 1, help_line, curses.A_REVERSE)
        scr.refresh()

    def draw_item(self, y, w, a, selected):
        c = self.col
        rev = curses.A_REVERSE if selected else 0
        self.put(y, 0, " " * (w - 1), rev)
        self.put(y, 1, CUR if selected else " ", curses.A_BOLD | rev)
        self.put(y, 3, TICK_ON if a["done"] else TICK_OFF, (c["ok"] if a["done"] else 0) | rev)
        pc = {1: c["bad"], 2: c["warn"], 3: c["info"]}[a["prio"]]
        self.put(y, 7, PRIO_LABEL[a["prio"]], pc | curses.A_BOLD | rev)
        meta = []
        if a["tags"]:
            meta.append(" ".join("#" + t for t in a["tags"]))
        if a["subs"]:
            meta.append("[%d/%d]" % (sum(1 for s in a["subs"] if s["d"]), len(a["subs"])))
        kids = self.store.children(a["id"])
        if kids:
            kd, kt = counts(kids)
            meta.append("<%d/%d>" % (kd, kt))
        if a.get("habit"):
            meta.append("rpt")
        if a.get("parent"):
            p = self.store.get(a["parent"])
            if p:
                meta.append("^" + clip(p["title"], 14))
        meta_s = "  ".join(meta)
        avail = w - 1 - 9 - (len(meta_s) + 2 if meta_s else 0)
        if avail < 12:
            meta_s, avail = "", w - 11
        tattr = ((c["ok"] | curses.A_DIM) if a["done"]
                 else (curses.A_BOLD if a["prio"] == 1 else 0))
        self.put(y, 9, clip(a["title"], avail), tattr | rev)
        if meta_s:
            self.put(y, w - 1 - len(meta_s), meta_s, c["tag"] | rev)

    def ov_year(self, y, w):
        yr = self.cur.year
        tot = {m: [0, 0] for m in range(1, 13)}
        for a in self.store.aims:
            if a["level"] in ("month", "day") and a["key"].startswith("%04d-" % yr):
                m = int(a["key"][5:7])
                tot[m][1] += 1
                if a["done"]:
                    tot[m][0] += 1
        cols = self.year_cols(w)
        cw = w // cols
        today = dt.date.today()
        for m in range(1, 13):
            r, cidx = divmod(m - 1, cols)
            d, t = tot[m]
            txt = "%s %s %2d/%-3d" % (MONTH_ABBR[m - 1], bar(d, t, 8), d, t)
            attr = self.col["ok"] if t and d == t else (0 if t else self.col["dim"])
            if yr == today.year and m == today.month:
                attr |= curses.A_BOLD | curses.A_UNDERLINE
            if m == self.cur.month:
                attr |= curses.A_REVERSE
            self.put(y + r, cidx * cw + 1, txt, attr)

    def ov_month(self, y, w):
        c = self.col
        dm = self.store.day_map()
        weeks = calendar.Calendar(0).monthdatescalendar(self.cur.year, self.cur.month)
        self.put(y, 2, "".join("%-5s" % n for n in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")),
                 c["head"])
        today = dt.date.today()
        for r, wk in enumerate(weeks):
            for ci, d in enumerate(wk):
                if d.month != self.cur.month:
                    continue
                v = dm.get(d.isoformat())
                mark, attr = " ", c["dim"]
                if v and v[1]:
                    if v[0] == v[1]:
                        mark, attr = CHECK, c["ok"]
                    else:
                        mark, attr = "~", c["warn"]
                if d == today:
                    attr |= curses.A_BOLD | curses.A_UNDERLINE
                if d == self.cur:
                    attr |= curses.A_REVERSE
                self.put(y + 1 + r, 2 + ci * 5, "%2d%s" % (d.day, mark), attr)
        if w < 72:
            return
        px = 40
        ym = key_for("month", self.cur)
        md, mt = counts(self.store.scope("month", ym))
        dd, dt_ = counts(self.store.within("day", ym))
        planned = [(k, v) for k, v in dm.items() if k.startswith(ym) and v[1]]
        perfect = sum(1 for k, v in planned if v[0] == v[1])
        self.put(y, px, "MONTH SNAPSHOT", c["head"])
        self.put(y + 1, px, "Month aims  %s %d/%d" % (bar(md, mt, 12), md, mt))
        self.put(y + 2, px, "Daily tasks %s %d/%d" % (bar(dd, dt_, 12), dd, dt_))
        self.put(y + 3, px, "Perfect days %d of %d planned" % (perfect, len(planned)))
        if planned:
            k, v = max(planned, key=lambda kv: kv[1][1])
            self.put(y + 4, px, "Busiest day  %s (%d aims)" % (k[8:], v[1]))
        dv = dm.get(self.cur.isoformat(), [0, 0])
        self.put(y + 5, px, "Cursor %s: %d/%d" % (self.cur.strftime("%a %d"), dv[0], dv[1]))
        self.put(y + 6, px, "%s all done   ~ partial   blank = empty   (z = zoom to day)"
                 % CHECK, c["dim"])

    def ov_day(self, y, w):
        c = self.col
        dm = self.store.day_map()
        monday = self.cur - dt.timedelta(days=self.cur.weekday())
        cw = w // 7
        today = dt.date.today()
        for i in range(7):
            d = monday + dt.timedelta(days=i)
            v = dm.get(d.isoformat(), [0, 0])
            attr = curses.A_BOLD
            if d == today:
                attr |= curses.A_UNDERLINE
            if d == self.cur:
                attr |= curses.A_REVERSE
            self.put(y, i * cw + 1, "%s %02d" % (DAY_NAMES[i].title(), d.day), attr)
            if v[1]:
                done = v[0] == v[1]
                self.put(y + 1, i * cw + 1,
                         "%s %d/%d" % (CHECK if done else "~", v[0], v[1]),
                         c["ok"] if done else c["warn"])
            else:
                self.put(y + 1, i * cw + 1, "-", c["dim"])
        md, mt = counts(self.store.scope("month", key_for("month", self.cur)))
        yd, yt = counts(self.store.scope("year", key_for("year", self.cur)))
        self.put(y + 2, 1, "Month aims %s %d/%d" % (bar(md, mt, 10), md, mt), c["info"])
        self.put(y + 2, w // 2, "Year aims %s %d/%d" % (bar(yd, yt, 10), yd, yt), c["info"])

    # commands ------------------------------------------------------------
    def parent_candidates(self):
        up = {"day": "month", "month": "year"}.get(self.view)
        if not up:
            return []
        return self.store.scope(up, key_for(up, self.cur))

    def pick_parent(self, current=None):
        cands = self.parent_candidates()
        if not cands:
            return current, False
        opts = ["(no parent)"] + [("%s %s" % (TICK_ON if p["done"] else TICK_OFF, p["title"]))
                                   for p in cands]
        i = self.pick("Link to a %s aim" % {"day": "month", "month": "year"}[self.view], opts)
        if i is None:
            return current, False
        return (None if i == 0 else cands[i - 1]["id"]), True

    def cmd_add(self):
        title = self.prompt("Aim title: ")
        if not title:
            return
        prio = parse_prio(self.prompt("Priority 1=high 2=med 3=low [2]: ") or "2", 2)
        tags = parse_tags(self.prompt("Tags (space/comma separated): ") or "")
        notes = self.prompt("Notes (optional): ") or ""
        parent, _ = self.pick_parent()
        a = self.store.add(title, self.view, key_for(self.view, self.cur), prio, tags, notes, parent)
        self.select_id(a["id"])
        self.flash("Added: " + title, "ok")

    def cmd_edit(self, a):
        title = self.prompt("Title: ", a["title"])
        if not title:
            return
        prio = parse_prio(self.prompt("Priority 1-3: ", str(a["prio"])) or "", a["prio"])
        tags = parse_tags(self.prompt("Tags: ", " ".join(a["tags"])) or "")
        notes = self.prompt("Notes: ", a["notes"])
        self.store.update(a["id"], title=title, prio=prio, tags=tags,
                          notes=a["notes"] if notes is None else notes)
        self.flash("Updated.", "ok")

    def cmd_move(self, a, copy_mode=False):
        verb = "Copy" if copy_mode else "Move"
        base = date_from_key(a["level"], a["key"])
        txt = self.prompt("%s to (%s, +n, -n, today): " % (verb, {
            "year": "2027", "month": "2026-11 or 11", "day": "2026-10-25 or 25"}[a["level"]]))
        if not txt:
            return
        d = parse_target(txt, a["level"], base)
        if d is None:
            self.flash("Couldn't understand that date.", "bad")
            return
        key = key_for(a["level"], d)
        if copy_mode:
            self.store.duplicate(a["id"], key)
            self.flash("Copied to %s." % key, "ok")
        else:
            self.store.update(a["id"], key=key, parent=None)
            self.flash("Moved to %s." % key, "ok")

    def cmd_goto(self):
        txt = self.prompt("Go to (2026-12-25, 25, 11-03, +7, today): ")
        if not txt:
            return
        d = parse_target(txt, self.view, self.cur)
        if d is None:
            self.flash("Couldn't understand that date.", "bad")
            return
        self.cur = d
        self.after_nav()

    def cmd_search(self):
        q = self.prompt("Search: ")
        if not q:
            return
        ql = q.lower()
        res = []
        for a in self.store.aims:
            hay = " ".join([a["title"], a["notes"], " ".join(a["tags"]),
                            " ".join(s["t"] for s in a["subs"])]).lower()
            if ql in hay:
                res.append(a)
        res.sort(key=lambda a: (a["key"], LEVELS.index(a["level"]), a["prio"]))
        if not res:
            self.flash("No matches for '%s'." % q, "warn")
            return
        sel = top = 0
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.put(0, 1, " SEARCH: %s  (%d hits) " % (q, len(res)), self.col["head"] | curses.A_REVERSE)
            rows = max(1, h - 3)
            sel = max(0, min(sel, len(res) - 1))
            top = min(max(top, sel - rows + 1), sel)
            for i in range(rows):
                if top + i >= len(res):
                    break
                a = res[top + i]
                on = top + i == sel
                line = "%s %s  %-10s %-5s %s" % (CUR if on else " ", TICK_ON if a["done"] else TICK_OFF,
                                                 a["key"], a["level"], a["title"])
                self.put(2 + i, 1, clip(line, w - 3), curses.A_REVERSE if on else 0)
            self.put(h - 1, 1, "Up/Down   Space tick   Enter jump to it   Esc back", self.col["dim"])
            self.scr.refresh()
            k = self.getkey()
            if k in (curses.KEY_UP, "k"):
                sel -= 1
            elif k in (curses.KEY_DOWN, "j"):
                sel += 1
            elif k == " ":
                self.store.toggle(res[sel]["id"])
            elif k in ("\n", "\r", curses.KEY_ENTER):
                self.goto_aim(res[sel])
                return
            elif k in ("\x1b", "q"):
                return

    def cmd_stats(self):
        c = self.col
        s = self.store
        L = []

        def line(text="", attr=0):
            L.append((text, attr))

        today = dt.date.today()
        dm = s.day_map()
        line("OVERALL", c["head"])
        d, t = counts(s.aims)
        line("  all aims    %s %d/%d (%d%%)" % (bar(d, t, 24), d, t, pct(d, t)))
        for lv in LEVELS:
            d, t = counts([a for a in s.aims if a["level"] == lv])
            line("  %-10s  %s %d/%d (%d%%)" % (lv + " aims", bar(d, t, 24), d, t, pct(d, t)))
        cs, bs = s.streaks()
        line("  current streak: %d day(s)     best streak: %d day(s)" % (cs, bs), c["ok"])
        for span in (7, 30):
            dd = tt = 0
            for i in range(span):
                v = dm.get((today - dt.timedelta(days=i)).isoformat())
                if v:
                    dd += v[0]
                    tt += v[1]
            line("  last %2d days: %d/%d done (%d%%)" % (span, dd, tt, pct(dd, tt)))
        line()
        line("LAST 14 DAYS", c["head"])
        for i in range(13, -1, -1):
            day = today - dt.timedelta(days=i)
            v = dm.get(day.isoformat(), [0, 0])
            line("  %s %s %d/%d" % (day.strftime("%a %d %b"), bar(v[0], v[1], 24), v[0], v[1]),
                 c["ok"] if v[1] and v[0] == v[1] else 0)
        line()
        line("BY PRIORITY", c["head"])
        for p in (1, 2, 3):
            d, t = counts([a for a in s.aims if a["prio"] == p])
            line("  %-7s %s %d/%d" % (PRIO_WORD[p], bar(d, t, 24), d, t))
        tags = {}
        for a in s.aims:
            for tg in a["tags"]:
                v = tags.setdefault(tg, [0, 0])
                v[1] += 1
                v[0] += 1 if a["done"] else 0
        if tags:
            line()
            line("TOP TAGS", c["head"])
            for tg, v in sorted(tags.items(), key=lambda kv: -kv[1][1])[:8]:
                line("  #%-12s %s %d/%d" % (tg, bar(v[0], v[1], 20), v[0], v[1]))
        od = [a for a in s.aims if not a["done"] and (
            (a["level"] == "day" and a["key"] < today.isoformat()) or
            (a["level"] == "month" and a["key"] < key_for("month", today)) or
            (a["level"] == "year" and a["key"] < key_for("year", today)))]
        line()
        line("OVERDUE (unfinished, in the past): %d" % len(od), c["bad"] if od else c["ok"])
        for a in sorted(od, key=lambda a: a["key"])[:10]:
            line("  %-10s %s" % (a["key"], a["title"]), c["warn"])
        self.pager(" STATISTICS ", L)

    def cmd_habits(self):
        sel = 0
        while True:
            hs = self.store.habits
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.put(0, 1, " RECURRING HABITS (auto-added to matching days) ",
                     self.col["head"] | curses.A_REVERSE)
            if not hs:
                self.put(2, 2, "(none yet - press 'a' to add one)", self.col["dim"])
            sel = max(0, min(sel, len(hs) - 1))
            for i, hb in enumerate(hs[: max(1, h - 4)]):
                txt = "%s %s  %s  [%s]  since %s" % (CUR if i == sel else " ", PRIO_LABEL[hb["prio"]],
                                                     hb["title"], fmt_days(hb["days"]), hb["start"])
                self.put(2 + i, 1, clip(txt, w - 3), curses.A_REVERSE if i == sel else 0)
            self.put(h - 1, 1, "a add   d delete (also removes future unticked copies)   Esc back",
                     self.col["dim"])
            self.scr.refresh()
            k = self.getkey()
            if k in (curses.KEY_UP, "k"):
                sel -= 1
            elif k in (curses.KEY_DOWN, "j"):
                sel += 1
            elif k == "a":
                title = self.prompt("Habit title: ")
                if not title:
                    continue
                days = parse_days(self.prompt("Days (daily / weekdays / weekends / mon,wed,fri): ") or "")
                if days is None:
                    self.flash("Bad day list.", "bad")
                    continue
                prio = parse_prio(self.prompt("Priority 1-3 [2]: ") or "2", 2)
                tags = parse_tags(self.prompt("Tags: ") or "")
                self.store.add_habit(title, days, prio, tags, dt.date.today())
                self.store.materialize(dt.date.today())
            elif k in ("d", curses.KEY_DC) and hs:
                if self.confirm("Delete habit '%s'?" % hs[sel]["title"]):
                    self.store.delete_habit(hs[sel]["id"])
            elif k in ("\x1b", "q"):
                self.store.materialize(self.cur)
                return

    def open_detail(self, aid):
        s = self.store
        ss = 0
        c = self.col
        while True:
            a = s.get(aid)
            if a is None:
                return
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            y = [0]

            def row(text, attr=0, x=1):
                if y[0] < h - 2:
                    self.put(y[0], x, clip(text, w - x - 1), attr)
                y[0] += 1

            row("%s %s" % (TICK_ON if a["done"] else TICK_OFF, a["title"]),
                (c["ok"] if a["done"] else c["head"]) | curses.A_BOLD)
            row("%s aim | %s | priority %s | tags: %s" % (
                a["level"], a["key"], PRIO_WORD[a["prio"]],
                " ".join("#" + t for t in a["tags"]) or "-"), c["dim"])
            if a.get("parent") and s.get(a["parent"]):
                row("parent: " + s.get(a["parent"])["title"], c["info"])
            if a.get("habit"):
                row("recurring habit instance", c["info"])
            row("completed: %s" % (a["done_at"] or "not yet"), c["dim"])
            row()
            row("NOTES", c["head"])
            for ln in (textwrap.wrap(a["notes"], max(10, w - 6)) if a["notes"] else ["(none - press e)"]):
                row(ln, 0, 3)
            row()
            sd = sum(1 for x in a["subs"] if x["d"])
            row("SUBTASKS %d/%d  %s" % (sd, len(a["subs"]), bar(sd, len(a["subs"]), 16)), c["head"])
            maxrows = max(1, h - 2 - y[0] - 4)
            off = max(0, ss - maxrows + 1)
            for i, sub in enumerate(a["subs"][off: off + maxrows], off):
                on = i == ss
                self.put(y[0], 3, "%s %s %s" % (CUR if on else " ", TICK_ON if sub["d"] else TICK_OFF,
                                                clip(sub["t"], w - 14)),
                         ((c["ok"] | curses.A_DIM) if sub["d"] else 0) | (curses.A_REVERSE if on else 0))
                y[0] += 1
            if not a["subs"]:
                row("(none - press a to add one)", c["dim"], 3)
            kids = s.children(aid)
            if kids:
                row()
                kd, kt = counts(kids)
                row("LINKED CHILD AIMS %d/%d" % (kd, kt), c["head"])
                for kid in kids[:6]:
                    row("%s %s (%s)" % (TICK_ON if kid["done"] else TICK_OFF, kid["title"], kid["key"]), 0, 3)
            self.put(h - 1, 1, "Space tick sub  a add  r rename  d del  t tick aim  e notes  Esc back",
                     curses.A_REVERSE)
            self.scr.refresh()
            k = self.getkey()
            n = len(a["subs"])
            if k in ("\x1b", "q", "\n", "\r", curses.KEY_ENTER):
                return
            elif k in (curses.KEY_DOWN, "j") and n:
                ss = min(n - 1, ss + 1)
            elif k in (curses.KEY_UP, "k") and n:
                ss = max(0, ss - 1)
            elif k == " " and n:
                s.toggle_sub(aid, ss)
            elif k == "a":
                txt = self.prompt("New subtask: ")
                if txt:
                    s.add_sub(aid, txt)
                    ss = len(s.get(aid)["subs"]) - 1
            elif k == "r" and n:
                txt = self.prompt("Rename: ", a["subs"][ss]["t"])
                if txt:
                    s.rename_sub(aid, ss, txt)
            elif k in ("d", curses.KEY_DC) and n:
                if self.confirm("Delete subtask?"):
                    s.delete_sub(aid, ss)
                    ss = max(0, min(ss, len(s.get(aid)["subs"]) - 1))
            elif k == "t":
                s.toggle(aid)
            elif k == "e":
                txt = self.prompt("Notes: ", a["notes"])
                if txt is not None:
                    s.update(aid, notes=txt)

    # key handling --------------------------------------------------------
    def handle(self, k):
        s = self.store
        items = self.visible()
        n = len(items)
        a = items[self.sel] if 0 <= self.sel < n else None
        if k == curses.KEY_RESIZE:
            return False
        if k == "q":
            return True
        if k in ("1", "2", "3"):
            self.set_view(LEVELS[int(k) - 1])
        elif k == "\t":
            self.set_view(LEVELS[(LEVELS.index(self.view) + 1) % 3])
        elif k == curses.KEY_BTAB:
            self.set_view(LEVELS[(LEVELS.index(self.view) - 1) % 3])
        elif k == "z":
            if self.view == "day":
                self.flash("Already at day level.", "warn")
            else:
                self.set_view(LEVELS[LEVELS.index(self.view) + 1])
        elif k == "Z":
            if self.view == "year":
                self.flash("Already at year level.", "warn")
            else:
                self.set_view(LEVELS[LEVELS.index(self.view) - 1])
        elif k in (curses.KEY_LEFT, curses.KEY_RIGHT):
            step = -1 if k == curses.KEY_LEFT else 1
            self.cur = shift(self.cur, "month" if self.view == "year" else "day", step)
            self.after_nav()
        elif k in ("[", "]", curses.KEY_PPAGE, curses.KEY_NPAGE):
            step = -1 if k in ("[", curses.KEY_PPAGE) else 1
            self.cur = shift(self.cur, "day", 7 * step) if self.view == "day" \
                else shift(self.cur, self.view, step)
            self.after_nav()
        elif k == "t":
            self.cur = dt.date.today()
            self.after_nav()
        elif k == "g":
            self.cmd_goto()
        elif k in (curses.KEY_DOWN, "j"):
            self.sel = min(max(0, n - 1), self.sel + 1)
        elif k in (curses.KEY_UP, "k"):
            self.sel = max(0, self.sel - 1)
        elif k == curses.KEY_HOME:
            self.sel = 0
        elif k == curses.KEY_END:
            self.sel = max(0, n - 1)
        elif k in (" ", "x") and a:
            s.toggle(a["id"])
            self.select_id(a["id"])
            sc = s.scope(self.view, key_for(self.view, self.cur))
            d, t = counts(sc)
            if t and d == t:
                self.flash("Everything in this %s is done. Nice work!" % self.view, "ok")
        elif k == "*" and n:
            state = any(not x["done"] for x in items)
            s.set_many([x["id"] for x in items], state)
            self.flash("%s %d aims." % ("Ticked" if state else "Unticked", n), "ok")
        elif k in ("\n", "\r", curses.KEY_ENTER) and a:
            self.open_detail(a["id"])
            self.select_id(a["id"])
        elif k == "a":
            self.cmd_add()
        elif k == "e" and a:
            self.cmd_edit(a)
        elif k in ("d", curses.KEY_DC) and a:
            if self.confirm("Delete '%s'?" % clip(a["title"], 40)):
                s.delete(a["id"])
                self.flash("Deleted (u to undo).", "warn")
        elif k == "p" and a:
            s.update(a["id"], prio=a["prio"] % 3 + 1)
            self.select_id(a["id"])
        elif k == "s" and a:
            txt = self.prompt("New subtask: ")
            if txt:
                s.add_sub(a["id"], txt)
                self.select_id(a["id"])
        elif k == "l" and a:
            saved = self.parent_candidates()
            if not saved:
                self.flash("No aims at the level above to link to.", "warn")
            else:
                parent, changed = self.pick_parent(a.get("parent"))
                if changed:
                    s.update(a["id"], parent=parent)
        elif k == "m" and a:
            self.cmd_move(a)
        elif k == "c" and a:
            self.cmd_move(a, copy_mode=True)
        elif k == "R":
            prev = key_for(self.view, shift(self.cur, self.view, -1))
            cnt = s.rollover(self.view, prev, key_for(self.view, self.cur))
            self.flash("Rolled %d unfinished aim(s) over from %s." % (cnt, prev),
                       "ok" if cnt else "warn")
        elif k == "f":
            self.filt = (self.filt + 1) % 3
            self.sel = self.top = 0
        elif k == "o":
            self.sort = (self.sort + 1) % len(SORTS)
            self.flash("Sort: " + SORTS[self.sort])
        elif k == "T":
            tg = self.prompt("Filter by tag (blank clears): ")
            if tg is not None:
                self.tag = tg.strip("#").lower() or None
                self.sel = self.top = 0
        elif k == "/":
            self.cmd_search()
        elif k == "S":
            self.cmd_stats()
        elif k == "H":
            self.cmd_habits()
        elif k == "E":
            path = s.export_markdown(self.view, self.cur)
            self.flash("Exported: " + path, "ok")
        elif k == "u":
            self.flash("Undone." if s.undo() else "Nothing to undo.", "info")
            s.materialize(self.cur) if self.view == "day" else None
        elif k == "U":
            self.flash("Redone." if s.redo() else "Nothing to redo.", "info")
        elif k == "?":
            self.pager(" HELP ", [(t[1:], self.col["head"]) if t.startswith("#") else (t, 0)
                                  for t in HELP])
        return False

    def run(self):
        self.setup()
        self.flash("Welcome! Press ? for help, a to add your first aim.", "info")
        self.after_nav()
        while True:
            self.draw()
            if self.handle(self.getkey()):
                break


def main():
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__)
        return
    locale.setlocale(locale.LC_ALL, "")
    enc = (locale.getpreferredencoding(False) or "").lower()
    setup_glyphs("--ascii" in sys.argv or "utf" not in enc)
    os.environ.setdefault("ESCDELAY", "25")
    store = Store()
    store.materialize(dt.date.today())
    try:
        curses.wrapper(lambda scr: App(scr, store).run())
    except KeyboardInterrupt:
        pass
    print("Aims saved in", DATA_FILE)


if __name__ == "__main__":
    main()

"""Fish Flash screen watcher.

Flashes your screen when a fish bites, with no sound needed.

How it works:
  * The FishFlash addon shows a tiny magenta square in the top-left corner of WoW while you're
    channeling Fishing. This program only looks for bites while that square is showing.
  * When a cast starts, it compares pictures of the area where the bobber lands (from before and
    after the bobber lands) to find the bobber.
  * It then watches a small square around the bobber. Normal bobbing barely changes it; when a
    fish bites, the bobber plunges and splashes and the change jumps. That's when it flashes.

It only reads the screen and draws a see-through flash on top. It never touches the game.

Usage (Windows, Python 3.8+, no extra packages):
    py fishflash_watcher.py              watch and flash
    py fishflash_watcher.py --calibrate  drag a box around where your bobber lands (recommended)
    py fishflash_watcher.py --settings   choose the alert: flash or solid colour, colour, strength, text
    py fishflash_watcher.py --test       show the alert once
    py fishflash_watcher.py --verbose    print movement numbers and save a picture of each lock-on
"""

import argparse
import ctypes
import json
import os
import queue
import re
import signal
import struct
import sys
import threading
import time
from ctypes import wintypes
from operator import sub

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "fishflash_watcher.json")
SNAP_DIR = os.path.join(HERE, "snapshots")
LOG_PATH = os.path.join(HERE, "watcher.log")

DEFAULTS = {
    "box": None,            # [x, y, w, h] screen pixels where the bobber lands; None = middle of the WoW screen
    "fps": 30,
    "first_grab": 0.3,      # s after the cast starts: "before" picture (bobber still in the air)
    "settled_grab": 1.6,    # bobber has landed
    "motion_grab": 2.0,     # second look, to rule out things that keep moving (water, your character)
    "baseline_until": 2.6,  # learn how much the bobber normally bobs until here, then start watching
    "ratio": 4.0,           # a bite is this many times more movement than normal bobbing...
    "min_energy": 3.0,      # ...and at least this much (average color change per pixel, 0-255)
    "hits": 2,              # frames in a row that look like a bite
    "min_red": 6,           # the bobber's red top must cover this many pixels to be used
    "red_dip": 0.7,         # movement burst counts only if the red top dropped below 70% of normal...
    "red_gone": 0.35,       # ...and below 35% (bobber pulled under) counts on its own
    # what happens on a bite (change these in "3 - Settings.bat")
    "alert_mode": "flash",  # "flash" = pulse the screen, "solid" = tint it and keep it tinted
    "flash_color": "#33ccff",
    "flash_alpha": 0.55,    # how strong the colour is, 0-1
    "flash_count": 3,       # flash mode: number of pulses
    "pulse_seconds": 0.5,   # flash mode: length of one pulse
    "solid_seconds": 0,     # solid mode: seconds to stay tinted; 0 = until you reel in (the cast ends)
    "show_text": True,
    "alert_text": "FISH ON!",
    "text_color": "#ffffff",
}

DS = 4  # the bobber search area is grabbed at 1/DS size
BS = 4  # block size (in downscaled pixels) when looking for the bobber


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001 - a broken config shouldn't stop the watcher
        print(f"Couldn't read {CONFIG_PATH} ({e}); using defaults.")
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Win32
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels on scaled displays
except Exception:  # noqa: BLE001
    try:
        user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass

HANDLE = ctypes.c_void_p
for fn, res, args in [
    (user32.GetDC, HANDLE, [HANDLE]),
    (user32.ReleaseDC, ctypes.c_int, [HANDLE, HANDLE]),
    (user32.GetParent, HANDLE, [HANDLE]),
    (user32.GetWindowLongW, ctypes.c_long, [HANDLE, ctypes.c_int]),
    (user32.SetWindowLongW, ctypes.c_long, [HANDLE, ctypes.c_int, ctypes.c_long]),
    (user32.SetWindowDisplayAffinity, wintypes.BOOL, [HANDLE, wintypes.DWORD]),
    (user32.MonitorFromPoint, HANDLE, [wintypes.POINT, wintypes.DWORD]),
    (user32.GetMonitorInfoW, wintypes.BOOL, [HANDLE, ctypes.c_void_p]),
    (gdi32.CreateCompatibleDC, HANDLE, [HANDLE]),
    (gdi32.CreateCompatibleBitmap, HANDLE, [HANDLE, ctypes.c_int, ctypes.c_int]),
    (gdi32.SelectObject, HANDLE, [HANDLE, HANDLE]),
    (gdi32.BitBlt, wintypes.BOOL, [HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   HANDLE, ctypes.c_int, ctypes.c_int, wintypes.DWORD]),
    (gdi32.StretchBlt, wintypes.BOOL, [HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.DWORD]),
    (gdi32.SetStretchBltMode, ctypes.c_int, [HANDLE, ctypes.c_int]),
    (gdi32.SetBrushOrgEx, wintypes.BOOL, [HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]),
    (gdi32.GetDIBits, ctypes.c_int, [HANDLE, HANDLE, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                     ctypes.c_void_p, wintypes.UINT]),
]:
    fn.restype = res
    fn.argtypes = args

SRCCOPY = 0x00CC0020
HALFTONE = 4


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def virtual_screen():
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def monitor_rect(x, y):
    """(x, y, w, h) of the monitor containing the point."""
    hmon = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)  # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcMonitor
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def candidate_corners():
    """Where the marker usually is: the top-left of WoW's game area and of every monitor."""
    pts = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HANDLE, ctypes.c_void_p)
    buf = ctypes.create_unicode_buffer(256)

    def on_window(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            user32.GetClassNameW(hwnd, buf, 256)
            cls = buf.value
            user32.GetWindowTextW(hwnd, buf, 256)
            if cls in ("GxWindowClass", "waApplication Window") or "World of Warcraft" in buf.value:
                p = wintypes.POINT(0, 0)
                user32.ClientToScreen(hwnd, ctypes.byref(p))
                pts.append((p.x, p.y))
        return True

    MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HANDLE, HANDLE, ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)

    def on_monitor(_hmon, _hdc, rect, _):
        pts.append((rect.contents.left, rect.contents.top))
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(on_window), None)
        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(on_monitor), None)
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(pts))


def cursor_pos():
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


class Screen:
    """Grabs screen pixels as top-down BGRA bytes."""

    def __init__(self):
        self.hdc = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(self.hdc)
        self.cache = {}

    def _target(self, w, h):
        t = self.cache.get((w, h))
        if t is None:
            if len(self.cache) > 16:
                self.cache.clear()  # (bitmaps leak here, but only after many window/size changes)
            bmp = gdi32.CreateCompatibleBitmap(self.hdc, w, h)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h  # top-down
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            t = (bmp, ctypes.create_string_buffer(w * h * 4), bmi)
            self.cache[(w, h)] = t
        return t

    def grab(self, x, y, w, h, out_w=None, out_h=None):
        out_w, out_h = out_w or w, out_h or h
        bmp, buf, bmi = self._target(out_w, out_h)
        old = gdi32.SelectObject(self.mem, bmp)
        if (out_w, out_h) == (w, h):
            gdi32.BitBlt(self.mem, 0, 0, w, h, self.hdc, x, y, SRCCOPY)
        else:
            gdi32.SetStretchBltMode(self.mem, HALFTONE)
            gdi32.SetBrushOrgEx(self.mem, 0, 0, None)
            gdi32.StretchBlt(self.mem, 0, 0, out_w, out_h, self.hdc, x, y, w, h, SRCCOPY)
        gdi32.SelectObject(self.mem, old)
        gdi32.GetDIBits(self.mem, bmp, 0, out_h, buf, ctypes.byref(bmi), 0)
        return buf.raw


# ---------------------------------------------------------------------------
# Image helpers (plain Python on small images)
# ---------------------------------------------------------------------------
# three magenta pixels in a row, allowing for a little color drift (BGRA byte order)
MARKER_RE = re.compile(rb"(?:[\xe0-\xff][\x00-\x20][\xe0-\xff].){3}", re.S)


def is_marker(b, g, r):
    return b >= 0xE0 and g <= 0x20 and r >= 0xE0


def find_marker_in(buf, w, h):
    """Top-left area of a solid magenta square at least 3x3 pixels, as (x, y) inside it."""
    pos = 0
    while True:
        m = MARKER_RE.search(buf, pos)
        if not m:
            return None
        i = m.start()
        if i % 4:
            pos = i + 1
            continue
        p = i // 4
        x, y = p % w, p // w
        if x <= w - 3 and y <= h - 3 and all(
                is_marker(*buf[((y + dy) * w + x + 1) * 4:((y + dy) * w + x + 1) * 4 + 3]) for dy in (1, 2)):
            return (x + 1, y + 2)
        pos = i + 4


def block_sums(a, b, w, h, bs):
    """Sum of |a - b| over bs x bs blocks of two BGRA images."""
    d = list(map(abs, map(sub, a, b)))
    bw, bh = w // bs, h // bs
    out = [[0] * bw for _ in range(bh)]
    stride, span = w * 4, bs * 4
    for y in range(bh * bs):
        o = out[y // bs]
        base = y * stride
        for bx in range(bw):
            s = base + bx * span
            o[bx] += sum(d[s:s + span])
    return out


def box_sum(grid, r):
    """Sum over the (2r+1) x (2r+1) neighbourhood of every cell (clipped at the edges)."""
    h, w = len(grid), len(grid[0]) if grid else 0
    ii = [[0] * (w + 1) for _ in range(h + 1)]
    for y in range(h):
        run = 0
        for x in range(w):
            run += grid[y][x]
            ii[y + 1][x + 1] = ii[y][x + 1] + run
    out = [[0] * w for _ in range(h)]
    for y in range(h):
        y0, y1 = max(0, y - r), min(h, y + r + 1)
        for x in range(w):
            x0, x1 = max(0, x - r), min(w, x + r + 1)
            out[y][x] = ii[y1][x1] - ii[y0][x1] - ii[y1][x0] + ii[y0][x0]
    return out


def is_bobber_red(b, g, r, loose=False):
    """The bobber's red top, judged by hue rather than brightness so it still counts when it's dark.

    Water is bluer than it is red, wood and skin are brown/orange (green close to red), so neither passes.
    `loose` is the second try, for fog, dusk and murky water (Theramore, for one), where the top is
    muted to a dull brick colour like (59, 35, 31): red only has to beat green by half.
    """
    if loose:
        return r >= 30 and 2 * r >= 3 * g and 10 * r >= 14 * b
    return r >= 30 and r >= 2 * g and 2 * r >= 3 * b


def chroma(b, g, r):
    """Colour without brightness: each channel's share of the total."""
    t = r + g + b or 1
    return r / t, g / t, b / t


def typical_chroma(buf):
    """The usual colour of the calibrated box, which is mostly water: per-channel median of a sample."""
    rs, gs, bs = [], [], []
    for i in range(0, len(buf) - 3, 4 * 7):
        r, g, b = chroma(buf[i], buf[i + 1], buf[i + 2])
        rs.append(r); gs.append(g); bs.append(b)
    if not rs:
        return None
    m = len(rs) // 2
    return sorted(rs)[m], sorted(gs)[m], sorted(bs)[m]


def is_water(b, g, r, loose=False, typical=None):
    """Water around the bobber: at least as blue as it is red.

    `loose` also counts green-led colours, for swamp and marsh water, and anything close in colour to
    `typical` (the box's usual colour), for golden or brown water like Dustwallow Marsh at sunset.
    Wood, ground and skin are red-led browns, far from blue or green water, so they still fail there.
    """
    if b >= r or (loose and g >= r):
        return True
    if typical is None or r + g + b < 30:
        return False
    cr, cg, cb = chroma(b, g, r)
    return abs(cr - typical[0]) + abs(cg - typical[1]) + abs(cb - typical[2]) <= 0.2


def red_count(buf, loose=False):
    n = 0
    for i in range(0, len(buf) - 3, 4):
        if is_bobber_red(buf[i], buf[i + 1], buf[i + 2], loose):
            n += 1
    return n


def locate_red_bobber(before, landed, later, w, h, scale=1.0):
    """Find the bobber's red top: a small red spot on water that appeared during the cast and then held still.

    Other red things that move during the cast (a player or pet on a dock, someone's clothes, your
    own hands) are bigger, sit on wood or ground rather than water, or keep moving, so they lose.
    Tries the strict colour rules first and only loosens them if nothing passes, so dull orange
    things (gear, lanterns) can't beat a properly red bobber.
    `scale` is how many pixels of this image make one pixel at 1/4 size (2 when grabbed at 1/2 size).
    Returns ((x, y) in this image, loose), or None if nothing qualifies.
    """
    for loose in (False, True):
        found = _locate_red(before, landed, later, w, h, scale, loose)
        if found:
            return found, loose
    return None


def _locate_red(before, landed, later, w, h, scale, loose):
    red = set()
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            i = row + x * 4
            b, g, r = landed[i], landed[i + 1], landed[i + 2]
            if is_bobber_red(b, g, r, loose):
                ob, og, orr = before[i], before[i + 1], before[i + 2]
                if abs(r - orr) + abs(g - og) + abs(b - ob) >= 25:
                    red.add((x, y))
    if not red:
        return None

    gap = max(2, round(2 * scale))
    left = set(red)
    groups = []
    while left:
        stack = [left.pop()]
        group = []
        while stack:
            x, y = stack.pop()
            group.append((x, y))
            for dx in range(-gap, gap + 1):
                for dy in range(-gap, gap + 1):
                    p = (x + dx, y + dy)
                    if p in left:
                        left.remove(p)
                        stack.append(p)
        groups.append(group)

    typical = typical_chroma(landed) if loose else None
    max_side, max_n, ring = 12 * scale, 60 * scale * scale, max(4, round(5 * scale))
    best, best_score = None, 0.0
    for group in groups:
        xs = [p[0] for p in group]
        ys = [p[1] for p in group]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        if len(group) < 2 or x1 - x0 > max_side or y1 - y0 > max_side or len(group) > max_n:
            continue  # a stray pixel, or far bigger than a bobber
        # still red a moment later? (hands, players and pets keep moving; the bobber floats)
        still = sum(1 for x, y in group
                    if is_bobber_red(*later[(y * w + x) * 4:(y * w + x) * 4 + 3], loose))
        if still < 0.4 * len(group):
            continue
        # how much of the ring around it looks like water
        water = total = 0
        for y in range(max(0, y0 - ring), min(h, y1 + ring + 1)):
            for x in range(max(0, x0 - ring), min(w, x1 + ring + 1)):
                if x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1:
                    continue
                i = (y * w + x) * 4
                total += 1
                if is_water(landed[i], landed[i + 1], landed[i + 2], loose, typical):
                    water += 1
        frac = water / total if total else 0.0
        if frac < 0.75:  # bobbers measured 0.81-0.96; people and pets on a dock 0.67 at most
            continue
        score = frac + 0.01 * min(len(group), 8 * scale * scale)
        if score > best_score:
            best_score = score
            best = (sum(xs) / len(xs) + 0.5, sum(ys) / len(ys) + 1.5 * scale)
    return best


def locate_bobber(before, landed, later, w, h):
    """Find something small that appeared (before -> landed) and then held still (landed -> later).

    Returns (x, y) in the downscaled image, or None. Big areas that changed (a loot window, the
    whole water surface) score low because only a small spot standing out from its surroundings counts.
    """
    appeared = block_sums(before, landed, w, h, BS)
    moving = block_sums(landed, later, w, h, BS)
    if not appeared or not appeared[0]:
        return None
    score = [[a - 2 * m for a, m in zip(ra, rm)] for ra, rm in zip(appeared, moving)]
    s3, s7 = box_sum(score, 1), box_sum(score, 3)
    a3 = box_sum(appeared, 1)
    best, best_xy = None, None
    for y, row in enumerate(s3):
        for x, v in enumerate(row):
            peak = v - (s7[y][x] - v) * 9 / 40
            if best is None or peak > best:
                best, best_xy = peak, (x, y)
    bx, by = best_xy
    # must be a real change: average at least 4 levels per byte over the 3x3 blocks
    if a3[by][bx] / (9 * BS * BS * 4) < 4 or best <= 0:
        return None
    return ((bx + 0.5) * BS, (by + 0.5) * BS)


def energy(a, b):
    """Average color change per channel between two same-size BGRA images (0-255)."""
    return sum(map(abs, map(sub, a, b))) / (len(a) * 0.75)


def save_bmp(path, buf, w, h, rect=None):
    data = bytearray(buf)
    if rect:
        x0, y0, x1, y1 = [int(v) for v in rect]
        for x in range(max(0, x0), min(w, x1 + 1)):
            for y in (y0, y1):
                if 0 <= y < h:
                    data[(y * w + x) * 4:(y * w + x) * 4 + 3] = b"\x00\x00\xff"
        for y in range(max(0, y0), min(h, y1 + 1)):
            for x in (x0, x1):
                if 0 <= x < w:
                    data[(y * w + x) * 4:(y * w + x) * 4 + 3] = b"\x00\x00\xff"
    header = struct.pack("<2sIHHI", b"BM", 54 + len(data), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, len(data), 2835, 2835, 0, 0)
    with open(path, "wb") as f:
        f.write(header + info + bytes(data))


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------
class Watcher:
    def __init__(self, cfg, screen, on_bite, verbose=False, snapshots=False, log=print, clock=time.monotonic,
                 log_to_file=True, on_end=None):
        self.cfg = cfg
        self.screen = screen
        self.on_bite = on_bite
        self.on_end = on_end or (lambda: None)
        self.verbose = verbose
        self.snapshots = snapshots
        self._out = log
        self.logfile = None
        if log_to_file:
            try:
                if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2_000_000:
                    os.replace(LOG_PATH, LOG_PATH + ".old")
                self.logfile = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
                self.logfile.write(f"\n=== watcher started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            except OSError:
                self.logfile = None
        self.clock = clock
        self.marker = None     # (x, y) screen point inside the addon's magenta square
        self.monitor = None
        self.last_scan = -1e9
        self.last_full_scan = -1e9
        self.started = clock()
        self.warned = False
        self.state = "idle"
        self.off_count = 0
        self.last_print = 0

    def log(self, msg, quiet=False):
        """Print (unless quiet) and always keep a copy in watcher.log for tuning."""
        if not quiet:
            self._out(msg)
        if self.logfile:
            self.logfile.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

    # -- marker --
    def scan_for_marker(self, now):
        """Look in the usual corners (cheap); look at every screen only every 10 s (slow)."""
        self.last_scan = now
        pt, how = None, None
        for cx, cy in candidate_corners():
            buf = self.screen.grab(cx, cy, 64, 64)
            hit = find_marker_in(buf, 64, 64)
            if hit:
                pt, how = (cx + hit[0], cy + hit[1]), "quick"
                break
        if pt is None and now - self.last_full_scan > 10:
            self.last_full_scan = now
            vx, vy, vw, vh = virtual_screen()
            buf = self.screen.grab(vx, vy, vw, vh)
            hit = find_marker_in(buf, vw, vh)
            if hit:
                pt, how = (vx + hit[0], vy + hit[1]), "full"
        if pt and pt != self.marker:
            self.marker = pt
            self.monitor = monitor_rect(*pt)
            self.log(f"Found the Fish Flash marker at {pt}. Watching for casts.")
        return how

    def marker_on(self):
        if not self.marker:
            return False
        px = self.screen.grab(self.marker[0], self.marker[1], 1, 1)
        return is_marker(px[0], px[1], px[2])

    # -- areas --
    def bobber_box(self):
        if self.cfg.get("box"):
            return [int(v) for v in self.cfg["box"]]
        mx, my, mw, mh = self.monitor
        return [mx + int(mw * 0.2), my + int(mh * 0.1), int(mw * 0.6), int(mh * 0.45)]

    def grab_box(self):
        x, y, w, h = self.box
        return self.screen.grab(x, y, w, h, self.bw, self.bh)

    # -- main step --
    def step(self):
        now = self.clock()
        cfg = self.cfg
        on = self.marker_on()
        if not on and self.state == "idle" and now - self.last_scan > 0.5:
            how = self.scan_for_marker(now)
            if how and self.marker_on():
                if how == "quick":  # spotted within half a second of the cast starting
                    self.start_cast(now - 0.25)
                else:  # the slow scan may be many seconds late: the bobber has already landed
                    self.log("(Watching from your next cast.)")
                    self.state, self.t0, self.off_count = "lost", now, 0
                return
            if not self.marker and not self.warned and now - self.started > 30:
                self.warned = True
                self.log("Haven't seen the marker yet. Is the FishFlash addon enabled? "
                         "Type /fishflash marker in WoW to show it for 10 seconds.")

        if self.state == "idle":
            if on:
                self.start_cast(now)
            return
        if not on:
            self.off_count += 1
            if self.off_count >= 3:  # ~0.1 s of no marker: the cast is over
                if self.state != "idle":
                    self.log("Cast ended.", quiet=not self.verbose)
                    self.on_end()
                self.state = "idle"
            return
        self.off_count = 0

        t = now - self.t0
        if self.state == "cast":
            if self.before is None and t >= cfg["first_grab"]:
                self.before = self.grab_box()
            elif self.before is not None and self.landed is None and t >= cfg["settled_grab"]:
                self.landed = self.grab_box()
            elif self.landed is not None and t >= cfg["motion_grab"]:
                self.lock_on(self.grab_box())
        elif self.state == "track":
            self.track(t)

    def start_cast(self, now):
        self.state = "cast"
        self.t0 = now
        self.off_count = 0
        self.before = self.landed = None
        self.box = self.bobber_box()
        self.bw, self.bh = max(BS * 3, self.box[2] // DS), max(BS * 3, self.box[3] // DS)
        self.log("Cast started.", quiet=not self.verbose)

    def lock_on(self, later):
        found = locate_red_bobber(self.before, self.landed, later, self.bw, self.bh)
        self.loose = False
        how = "red top"
        if found:
            found, self.loose = found
            if self.loose:
                how = "red top (dull light)"
        else:  # a bobber toy without red, or very dark water: fall back to "what appeared"
            found = locate_bobber(self.before, self.landed, later, self.bw, self.bh)
            how = "movement"
        if not found:
            self.log("Couldn't find the bobber this cast (try --calibrate so it only looks where it lands).")
            self.state = "lost"
            return
        x, y, w, h = self.box
        cx, cy = x + found[0] * DS, y + found[1] * DS
        size = max(40, round(self.monitor[3] / 17))
        mx, my, mw, mh = self.monitor
        px = int(min(max(cx - size / 2, mx), mx + mw - size))
        py = int(min(max(cy - size / 2, my), my + mh - size))
        self.patch = (px, py, size, size)
        self.prev = self.screen.grab(*self.patch)
        self.samples, self.red_samples, self.baseline, self.red_base, self.hits = [], [], None, None, 0
        self.state = "track"
        self.log(f"Bobber found around ({int(cx)}, {int(cy)}) by its {how}.")
        if self.snapshots:
            os.makedirs(SNAP_DIR, exist_ok=True)
            r = ((px - x) / DS, (py - y) / DS, (px + size - x) / DS, (py + size - y) / DS)
            path = os.path.join(SNAP_DIR, f"cast_{int(time.time()) % 100000}.bmp")
            save_bmp(path, self.landed, self.bw, self.bh, r)
            self.log(f"Saved {path} (red box = where it's watching).")

    def track(self, t):
        """A bite = a burst of movement while the red top dips out of sight."""
        cfg = self.cfg
        cur = self.screen.grab(*self.patch)
        e = energy(self.prev, cur)
        rc = red_count(cur, self.loose)
        self.prev = cur
        if t < cfg["baseline_until"]:
            self.samples.append(e)
            self.red_samples.append(rc)
            return
        if self.baseline is None:
            self.baseline = max(sum(self.samples) / len(self.samples) if self.samples else 1.0, 0.3)
            self.red_base = sum(self.red_samples) / len(self.red_samples) if self.red_samples else 0
            self.log(f"  normal movement {self.baseline:.2f}, red pixels {self.red_base:.0f}")
        threshold = max(cfg["ratio"] * self.baseline, cfg["min_energy"])
        use_red = self.red_base >= cfg["min_red"]
        red_frac = rc / self.red_base if use_red else 1.0
        if use_red:
            bite = (e >= threshold and red_frac <= cfg["red_dip"]) or red_frac <= cfg["red_gone"]
        else:
            bite = e >= threshold
        if bite:
            self.hits += 1
        else:
            self.hits = 0
            if e < threshold:
                self.baseline = max(0.95 * self.baseline + 0.05 * e, 0.3)
            if use_red and red_frac > cfg["red_dip"]:
                self.red_base = 0.95 * self.red_base + 0.05 * rc
        if self.hits or e >= threshold or red_frac <= cfg["red_dip"] or self.clock() - self.last_print > 0.5:
            self.last_print = self.clock()
            self.log(f"  {t:5.1f}s movement {e:5.2f} (flash at {threshold:5.2f})  red {rc:3d}/{self.red_base:3.0f}"
                     + ("  <-" if bite else ""), quiet=not self.verbose)
        if self.hits >= cfg["hits"]:
            self.log(f"FISH ON! ({t:.1f}s into the cast, movement {e:.1f}, red {rc}/{self.red_base:.0f})")
            self.state = "done"
            self.on_bite(self.monitor)
            if self.snapshots:
                os.makedirs(SNAP_DIR, exist_ok=True)
                path = os.path.join(SNAP_DIR, f"bite_{int(time.time()) % 100000}.bmp")
                save_bmp(path, cur, self.patch[2], self.patch[3])

    def run(self, stop):
        period = 1.0 / max(5, self.cfg["fps"])
        while not stop.is_set():
            t = time.perf_counter()
            try:
                self.step()
            except Exception as ex:  # noqa: BLE001 - keep watching after a bad frame
                self.log(f"Error: {ex}")
                self.state = "idle"
                time.sleep(1)
            time.sleep(max(0.0, period - (time.perf_counter() - t)))


# ---------------------------------------------------------------------------
# Flash overlay and calibration (tkinter)
# ---------------------------------------------------------------------------
def make_click_through(win):
    win.update_idletasks()
    hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
    GWL_EXSTYLE = -20
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= 0x00080000 | 0x00000020 | 0x00000080 | 0x08000000  # layered, transparent, toolwindow, noactivate
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    user32.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE: keep the flash out of our own grabs


class Flasher:
    """The see-through, click-through overlay: pulses (flash mode) or stays tinted (solid mode)."""

    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg
        self.windows = {}
        self.gen = 0        # bumping this cancels whatever animation or timer is running
        self.active = None  # the window currently showing

    def _window(self, mon):
        import tkinter as tk
        win = self.windows.get(mon)
        if win is None:
            x, y, w, h = mon
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.geometry(f"{w}x{h}+{x}+{y}")
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.0)
            win.label = tk.Label(win, font=("Segoe UI", -max(24, h // 9), "bold"))
            make_click_through(win)
            self.windows[mon] = win
        cfg = self.cfg
        win.configure(bg=cfg["flash_color"])
        win.label.configure(text=cfg["alert_text"], fg=cfg["text_color"], bg=cfg["flash_color"])
        if cfg["show_text"] and cfg["alert_text"]:
            win.label.place(relx=0.5, rely=0.35, anchor="center")
        else:
            win.label.place_forget()
        return win

    def alert(self, mon):
        self.clear()
        gen = self.gen
        win = self._window(mon)
        win.attributes("-topmost", True)
        self.active = win
        cfg = self.cfg
        peak = max(0.05, min(1.0, float(cfg["flash_alpha"])))

        if cfg["alert_mode"] == "solid":
            win.attributes("-alpha", peak)
            if float(cfg["solid_seconds"]) > 0:
                self.root.after(int(float(cfg["solid_seconds"]) * 1000),
                                lambda: gen == self.gen and self.clear())
            return

        step = 30
        n = max(4, int(float(cfg["pulse_seconds"]) * 1000 / step))
        up = max(1, n // 4)
        down = n - up
        frames = []
        for _ in range(max(1, int(cfg["flash_count"]))):
            frames += [peak * i / up for i in range(1, up + 1)] + [peak * i / down for i in range(down - 1, -1, -1)]

        def tick(i=0):
            if gen != self.gen:
                return
            if i >= len(frames):
                self.clear()
                return
            win.attributes("-alpha", frames[i])
            self.root.after(step, tick, i + 1)
        tick()

    def cast_ended(self):
        """You reeled in (or the cast ran out): end a solid tint that waits for that."""
        if self.cfg["alert_mode"] == "solid" and float(self.cfg["solid_seconds"]) <= 0:
            self.clear()

    def clear(self):
        self.gen += 1
        if self.active is not None:
            self.active.attributes("-alpha", 0.0)
            self.active = None


def settings_window(cfg):
    """Small window to pick how the alert looks, with a preview button."""
    import tkinter as tk
    from tkinter import colorchooser, ttk

    root = tk.Tk()
    root.title("Fish Flash settings")
    root.resizable(False, False)
    flasher = Flasher(root, cfg)
    pad = {"padx": 8, "pady": 4}

    mode = tk.StringVar(value=cfg["alert_mode"])
    alpha = tk.IntVar(value=round(float(cfg["flash_alpha"]) * 100))
    count = tk.IntVar(value=int(cfg["flash_count"]))
    pulse = tk.DoubleVar(value=float(cfg["pulse_seconds"]))
    solid = tk.IntVar(value=int(float(cfg["solid_seconds"])))
    show_text = tk.BooleanVar(value=bool(cfg["show_text"]))
    text = tk.StringVar(value=cfg["alert_text"])
    colors = {"flash_color": cfg["flash_color"], "text_color": cfg["text_color"]}

    def num(var, default):
        try:
            return var.get()
        except tk.TclError:  # half-typed spinbox
            return default

    def apply():
        cfg.update({
            "alert_mode": mode.get(),
            "flash_alpha": alpha.get() / 100,
            "flash_count": max(1, int(num(count, 3))),
            "pulse_seconds": round(pulse.get(), 2),
            "solid_seconds": max(0, int(num(solid, 0))),
            "show_text": show_text.get(),
            "alert_text": text.get(),
            **colors,
        })

    frm = ttk.Frame(root, padding=10)
    frm.grid()
    rows = {}  # name -> (label widget, input widget, row)

    def add(name, label, widget):
        r = len(rows)
        lab = ttk.Label(frm, text=label) if isinstance(label, str) else label
        lab.grid(row=r, column=0, sticky="w", **pad)
        widget.grid(row=r, column=1, sticky="w", **pad)
        rows[name] = (lab, widget, r)

    def color_button(key, title):
        b = tk.Button(frm, width=10, bg=colors[key], activebackground=colors[key], relief="groove")

        def pick():
            c = colorchooser.askcolor(color=colors[key], parent=root, title=title)[1]
            if c:
                colors[key] = c
                b.configure(bg=c, activebackground=c)
        b.configure(command=pick)
        return b

    add("mode1", "On a bite:", ttk.Radiobutton(frm, text="Flash the screen", variable=mode, value="flash",
                                                command=lambda: refresh()))
    add("mode2", "", ttk.Radiobutton(frm, text="Turn the screen a colour", variable=mode, value="solid",
                                     command=lambda: refresh()))
    add("color", "Colour", color_button("flash_color", "Alert colour"))
    add("alpha", "Strength (%)", tk.Scale(frm, from_=10, to=100, orient="horizontal", variable=alpha, length=200))
    add("count", "Number of flashes", ttk.Spinbox(frm, from_=1, to=20, textvariable=count, width=6))
    add("pulse", "Flash length (s)", tk.Scale(frm, from_=0.2, to=2.0, resolution=0.1, orient="horizontal",
                                              variable=pulse, length=200))
    add("solid", "Stay coloured for (s)", ttk.Spinbox(frm, from_=0, to=60, textvariable=solid, width=6))
    add("hint", "", ttk.Label(frm, text="0 = until you reel in", foreground="#666"))
    add("text", ttk.Checkbutton(frm, text="Show text", variable=show_text), ttk.Entry(frm, textvariable=text, width=22))
    add("tcolor", "Text colour", color_button("text_color", "Text colour"))

    def refresh():
        flash = mode.get() == "flash"
        for name in ("count", "pulse", "solid", "hint"):
            lab, widget, _r = rows[name]
            on = flash if name in ("count", "pulse") else not flash
            for w in (lab, widget):
                if on:
                    w.grid()
                else:
                    w.grid_remove()
    refresh()

    def preview():
        apply()
        root.update_idletasks()
        flasher.alert(monitor_rect(root.winfo_rootx() + 10, root.winfo_rooty() + 10))
        if cfg["alert_mode"] == "solid" and float(cfg["solid_seconds"]) <= 0:
            gen = flasher.gen
            root.after(2000, lambda: gen == flasher.gen and flasher.clear())  # stand-in for reeling in

    status = ttk.Label(frm, text="", foreground="#2a7")

    def save():
        apply()
        save_config(cfg)
        status.configure(text="Saved. A running watcher picks this up by itself.")

    btns = ttk.Frame(frm)
    btns.grid(row=len(rows), column=0, columnspan=2, pady=(10, 0))
    ttk.Button(btns, text="Preview", command=preview).pack(side="left", padx=4)
    ttk.Button(btns, text="Save", command=save).pack(side="left", padx=4)
    ttk.Button(btns, text="Close", command=root.destroy).pack(side="left", padx=4)
    status.grid(row=len(rows) + 1, column=0, columnspan=2, pady=(6, 0))
    root.mainloop()


def calibrate(cfg):
    import tkinter as tk
    mx, my, mw, mh = monitor_rect(*cursor_pos())
    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{mw}x{mh}+{mx}+{my}")
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.35)
    c = tk.Canvas(root, bg="black", highlightthickness=0, cursor="crosshair")
    c.pack(fill="both", expand=True)
    c.create_text(mw // 2, 60, fill="white", font=("Segoe UI", 20, "bold"),
                  text="Drag a box over the water where your bobber lands.\n"
                       "Keep your character out of it. Enter = save, Esc = cancel.", justify="center")
    state = {"start": None, "rect": None, "box": None}

    def press(e):
        state["start"] = (e.x, e.y)
        if state["rect"]:
            c.delete(state["rect"])
        state["rect"] = c.create_rectangle(e.x, e.y, e.x, e.y, outline="#33ccff", width=3)

    def drag(e):
        x0, y0 = state["start"]
        c.coords(state["rect"], x0, y0, e.x, e.y)
        state["box"] = (min(x0, e.x), min(y0, e.y), abs(e.x - x0), abs(e.y - y0))

    def save(_e=None):
        b = state["box"]
        if b and b[2] >= 40 and b[3] >= 40:
            cfg["box"] = [mx + b[0], my + b[1], b[2], b[3]]
            save_config(cfg)
            print(f"Saved bobber area {cfg['box']} to {CONFIG_PATH}")
        else:
            print("Box too small, nothing saved.")
        root.destroy()

    c.bind("<ButtonPress-1>", press)
    c.bind("<B1-Motion>", drag)
    root.bind("<Return>", save)
    root.bind("<Escape>", lambda _e: root.destroy())
    root.focus_force()
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="Flash the screen when a fish bites in WoW.")
    ap.add_argument("--calibrate", action="store_true", help="drag a box around where your bobber lands")
    ap.add_argument("--reset-box", action="store_true", help="forget the calibrated box")
    ap.add_argument("--test", action="store_true", help="show the flash once and quit")
    ap.add_argument("--settings", action="store_true", help="choose the alert colour, style and text")
    ap.add_argument("--verbose", "-v", action="store_true", help="print movement numbers and save lock-on pictures")
    args = ap.parse_args()

    cfg = load_config()
    if args.calibrate:
        calibrate(cfg)
        return
    if args.settings:
        settings_window(cfg)
        return
    if args.reset_box:
        cfg["box"] = None
        save_config(cfg)
        print("Bobber area reset to the middle of the screen.")
        return

    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    flasher = Flasher(root, cfg)

    if args.test:
        flasher.alert(monitor_rect(*cursor_pos()))
        root.after(int(float(cfg["flash_count"]) * float(cfg["pulse_seconds"]) * 1000) + 2500, root.destroy)
        root.mainloop()
        return

    bites = queue.Queue()
    stop = threading.Event()

    def worker():
        Watcher(cfg, Screen(), lambda mon: bites.put(("bite", mon)), verbose=args.verbose,
                snapshots=args.verbose, on_end=lambda: bites.put(("end", None))).run(stop)

    threading.Thread(target=worker, daemon=True).start()

    def mtime():
        try:
            return os.path.getmtime(CONFIG_PATH)
        except OSError:
            return None
    seen = {"mtime": mtime(), "next": 0}

    def poll():
        try:
            while True:
                kind, mon = bites.get_nowait()
                if kind == "bite":
                    flasher.alert(mon)
                else:
                    flasher.cast_ended()
        except queue.Empty:
            pass
        now = time.monotonic()
        if now >= seen["next"]:  # pick up changes saved from the settings window
            seen["next"] = now + 1
            m = mtime()
            if m != seen["mtime"]:
                seen["mtime"] = m
                cfg.update(load_config())
                print("Settings reloaded.")
        root.after(30, poll)

    signal.signal(signal.SIGINT, lambda *_: root.destroy())
    print("Fish Flash watcher running. Cast Fishing in WoW. Ctrl+C to quit.")
    if not cfg.get("box"):
        print("Tip: run with --calibrate once to mark where your bobber lands; it makes lock-on more reliable.")
    poll()
    root.mainloop()
    stop.set()


if __name__ == "__main__":
    sys.exit(main())

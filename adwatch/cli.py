#!/usr/bin/env python3
"""
adwatch - automate the ads in Android games, over ADB.

Runs anywhere adb can reach the phone: a PC, or the phone itself (Termux + wireless
debugging to localhost). Each game gets a small JSON profile; buttons are found by
matching small pictures of them that you crop from your own screen.

Run it with no command for the interactive menu. GAME is a game's name (or the path
to its profile file). Commands:
  run    GAME             run the ad loop (active: taps the button that starts an ad)
  watch  GAME             passive: wait for ads that appear by themselves and close them
  test   GAME             screenshot once and report match scores for every template
  snap   [OUT.png]        save a full screenshot (for making templates)
  crop   IN X Y W H OUT   cut a template out of a screenshot
  new    NAME [PACKAGE]   create profiles/NAME.json (auto-detects the game)
  grab   NAME LABEL       drag a box on a live screenshot -> saves the template
                          (--step/--then-ad add it to the loop, --close for ad X buttons)
  stamp  W H              tag existing templates with the screen size they came from
  keepalive run|watch GAME ...  run unattended: restarts after crashes, waits for fixes
  tidy                    remove duplicate pictures, unused template folders, old debug images
  get-adb                 download Google's adb tool if it isn't installed
  pack                    (maintainers) copy your saved ad buttons into the bundled starter set
  step   NAME KIND ...    append watch_ad / wait N / key NAME / tap_xy X Y
"""
import argparse
import glob
import json
import os
import random
import re
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

try:
    import cv2
    import numpy as np
except ImportError:          # the menu's "Check my setup" explains how to install them
    cv2 = np = None

__version__ = "0.2.0"

PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    """Where games, saved buttons and settings are kept:
    ADWATCH_HOME if set; the project folder when run straight from a download or clone
    (python adwatch.py); ~/.adwatch when installed with pip / pipx."""
    env = os.environ.get("ADWATCH_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    parts = PKG_DIR.replace("\\", "/").split("/")
    if "site-packages" in parts or "dist-packages" in parts:
        return os.path.join(os.path.expanduser("~"), ".adwatch")
    return os.path.dirname(PKG_DIR)


DATA_DIR = _data_dir()
STARTER_DIR = os.path.join(PKG_DIR, "starter")     # ad close buttons that ship with adwatch
STATE_FILE = os.path.join(DATA_DIR, ".adwatch_state.json")
ADB_EXE = "adb.exe" if os.name == "nt" else "adb"


def _find_adb():
    """ADB env var, else adb on PATH, else the copy `get-adb` downloaded."""
    import shutil
    if os.environ.get("ADB"):
        return os.environ["ADB"]
    local = os.path.join(DATA_DIR, "platform-tools", ADB_EXE)
    return shutil.which("adb") or (local if os.path.exists(local) else "adb")


ADB = _find_adb()
SERIAL = os.environ.get("ADWATCH_SERIAL")  # e.g. localhost:37123 if several devices


def adb_available():
    import shutil
    return bool(shutil.which(ADB))


LOG_FILE = os.path.join(DATA_DIR, "logs", "adwatch.log")
CONTROL_DIR = os.path.join(DATA_DIR, "control")      # files dropped here steer a running adwatch
RESTART_CODE = 75                                    # "start me again" (keepalive re-launches at once)


VERBOSE = bool(os.environ.get("ADWATCH_VERBOSE"))      # -v: also print the debug log to the terminal
DEBUG_LOG = os.path.join(DATA_DIR, "logs", "debug.log")
_dbg = {"fh": None}


def dlog(msg):
    """Detailed debug line: always appended to logs/debug.log (rotated at 5 MB), and printed
    too with -v. Meant for working out afterwards why something happened."""
    now = time.time()
    line = f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int(now % 1 * 1000):03d} {msg}"
    if VERBOSE:
        print("  \033[2m" + line + "\033[0m" if sys.stdout.isatty() else "  " + line, flush=True)
    try:
        if _dbg["fh"] is None:
            os.makedirs(os.path.dirname(DEBUG_LOG), exist_ok=True)
            if os.path.exists(DEBUG_LOG) and os.path.getsize(DEBUG_LOG) > 5_000_000:
                os.replace(DEBUG_LOG, DEBUG_LOG + ".1")
            _dbg["fh"] = open(DEBUG_LOG, "a", buffering=1)
            _dbg["fh"].write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()} =====\n")
        _dbg["fh"].write(time.strftime("%Y-%m-%d ", time.localtime(now)) + line + "\n")
        if _dbg["fh"].tell() > 5_000_000:
            _dbg["fh"].close()
            _dbg["fh"] = None
    except OSError:
        pass


def make_log():
    """log() for run/watch: prints, and appends to logs/adwatch.log so it can be read later."""
    def log(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        dlog("LOG " + str(msg))
        try:
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            with open(LOG_FILE, "a") as f:
                f.write(time.strftime("%Y-%m-%d ") + line + "\n")
        except OSError:
            pass
    return log


def snapshot(reason, log=None):
    """Save what the phone shows to debug/ when something goes wrong (keeps the newest 40)."""
    try:
        dbg = os.path.join(DATA_DIR, "debug")
        os.makedirs(dbg, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S") + "_" + re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-") + ".png"
        cv2.imwrite(os.path.join(dbg, name), screenshot())
        if log:
            log(f"  saved the screen to debug/{name}")
        old = sorted(glob.glob(os.path.join(dbg, "2*_*.png")))
        for p in old[:-40]:
            os.remove(p)
    except (Exception, SystemExit):   # a snapshot must never break the run (adb gone, disk full...)
        pass


def control_signal():
    """'stop' or 'restart' if one of those files was dropped into control/ (consumed on read)."""
    for name in ("stop", "restart"):
        p = os.path.join(CONTROL_DIR, name)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
            return name
    return None


def obey_control(log):
    sig = control_signal()
    if sig == "stop":
        log("stop requested - finishing")
        sys.exit(0)
    if sig == "restart":
        log("restart requested - restarting with the latest code and settings")
        sys.exit(RESTART_CODE)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass

# Packages that mean an ad kicked us out of the game (store page, browser...).
IGNORED_FOREGROUND = {"com.android.systemui", "android"}


# ---------------------------------------------------------------- adb helpers
NO_ADB = ("adb (the tool that talks to the phone) isn't installed. Run `adwatch get-adb` to download "
          "it, or install Android platform-tools yourself.")
LAST_ERR = ""


def adb(*args, binary=False, timeout=30):
    global LAST_ERR
    cmd = [ADB] + (["-s", SERIAL] if SERIAL else []) + list(args)
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        LAST_ERR = "adb timed out"
        dlog(f"adb {' '.join(args)[:120]} -> TIMEOUT after {timeout}s")
        return b"" if binary else ""
    except FileNotFoundError:
        sys.exit(NO_ADB)
    LAST_ERR = r.stderr.decode(errors="replace").strip()
    dlog(f"adb {' '.join(args)[:120]} -> rc {r.returncode}, {len(r.stdout)} bytes, {(time.time() - t0) * 1000:.0f} ms"
         + (f", stderr: {LAST_ERR[:200]}" if LAST_ERR else ""))
    return r.stdout if binary else r.stdout.decode(errors="replace")


def shell(cmd, **kw):
    return adb("shell", cmd, **kw)


def _decode(data):
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None


class VideoFeed:
    """Read the screen from scrcpy's video stream instead of adb screenshots (milliseconds
    instead of about half a second). Linux: scrcpy writes to a v4l2loopback 'webcam':
        sudo modprobe v4l2loopback video_nr=9 card_label=phone exclusive_caps=1
        scrcpy --v4l2-sink=/dev/video9 --video-bit-rate=16M --hwdec=disabled
    then run adwatch with --feed /dev/video9."""

    def __init__(self, device):
        import threading
        self.device = device
        self.cap = cv2.VideoCapture(device, cv2.CAP_V4L2) if str(device).startswith("/dev/") else cv2.VideoCapture(device)
        if not self.cap.isOpened():
            raise RuntimeError(f"could not open {device} - is scrcpy running with --v4l2-sink={device}?")
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.frame, self.t, self.alive = None, 0.0, True
        self.size = None                                # the phone's real screen size, for taps
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()
        if self.get(timeout=3.0) is None:
            self.close()
            raise RuntimeError(f"{device} opened but sent no frames - is scrcpy running?")

    def _loop(self):
        fails = 0
        while self.alive:
            ok, f = self.cap.read()
            if not ok:
                fails += 1
                if fails > 100:
                    self.alive = False
                time.sleep(0.02)
                continue
            fails = 0
            with self.lock:
                self.frame, self.t = f, time.time()

    def get(self, newer_than=0.0, timeout=0.3):
        """The first frame after `newer_than`. scrcpy only sends frames when the screen changes,
        so if none arrives within `timeout` the latest one is still current."""
        deadline = time.time() + timeout
        while time.time() < deadline and self.alive:
            with self.lock:
                if self.frame is not None and self.t > newer_than:
                    return self._fit(self.frame)
            time.sleep(0.005)
        with self.lock:
            return self._fit(self.frame) if self.alive and self.frame is not None else None

    def _fit(self, f):
        """Scale the frame to the phone's real resolution so tap positions line up
        (scrcpy may shrink it, and rounds sizes to a multiple of 8)."""
        if self.size is None:
            return f.copy()
        w, h = self.size
        if (f.shape[1] > f.shape[0]) != (w > h):        # game turned to landscape
            w, h = h, w
        return f.copy() if f.shape[1] == w and f.shape[0] == h else cv2.resize(f, (w, h))

    def close(self):
        self.alive = False
        self.cap.release()


SCREEN_METHOD = os.environ.get("ADWATCH_SCREEN", "auto")   # auto | png | raw
_SCREEN = {"method": None}


def _grab_png():
    return _decode(adb("exec-out", "screencap", "-p", binary=True))


def _grab_raw():
    """Uncompressed screencap: no PNG encoding on the phone (often the slow part), but about
    10 MB to transfer, so it wins over USB and can lose over Wi-Fi."""
    data = adb("exec-out", "screencap", binary=True)
    if len(data) < 16:
        return None
    w, h, fmt = struct.unpack_from("<III", data, 0)
    n = w * h * 4
    hdr = len(data) - n
    if w <= 0 or h <= 0 or hdr not in (12, 16):      # 16 on Android 9+ (adds a colour-space field)
        return None
    px = np.frombuffer(data, np.uint8, count=n, offset=hdr).reshape(h, w, 4)
    if fmt in (1, 2):                                 # RGBA_8888 / RGBX_8888
        return cv2.cvtColor(px, cv2.COLOR_RGBA2BGR)
    if fmt == 5:                                      # BGRA_8888
        return cv2.cvtColor(px, cv2.COLOR_BGRA2BGR)
    return None


def _adb_screenshot():
    """adb screenshot with whichever format is faster on this phone and connection
    (measured once with --screen auto, the default)."""
    m = _SCREEN["method"] or SCREEN_METHOD
    if m == "auto":
        times = {}
        for name, fn in (("raw", _grab_raw), ("png", _grab_png)):
            best = None
            for _ in range(2):
                t0 = time.time()
                ok = fn() is not None
                dt = time.time() - t0
                if ok:
                    best = dt if best is None else min(best, dt)
            if best is not None:
                times[name] = best
        m = min(times, key=times.get) if times else "png"
        _SCREEN["method"] = m
        msg = "screenshots: " + ", ".join(f"{k} {v * 1000:.0f} ms" for k, v in times.items()) + f" -> using {m}"
        dlog(msg)
        if len(times) == 2:
            print(time.strftime("%H:%M:%S"), msg, flush=True)
    _SCREEN["method"] = m
    img = _grab_raw() if m == "raw" else _grab_png()
    if img is None and m == "raw":                    # this phone's raw format isn't understood
        img = _grab_png()
        if img is not None:
            _SCREEN["method"] = "png"
            dlog("screen: raw screencap unreadable -> png from now on")
    return img


FEED = None          # a VideoFeed when --feed is used
LAST_ACTION = 0.0    # when we last touched the phone; the feed waits for a frame after it


def open_feed(device, log=print):
    """Switch screenshots to the video feed; keep using adb screenshots if it can't start."""
    global FEED
    try:
        feed = VideoFeed(device)
        feed.size = screen_size()
        FEED = feed
        log(f"reading the screen from the video feed {device}")
    except Exception as e:
        log(f"video feed not used ({e}); using adb screenshots")


def screenshot():
    global FEED
    t0 = time.time()
    if FEED is not None:
        f = FEED.get(newer_than=LAST_ACTION + 0.05)
        if f is not None:
            dlog(f"screen: feed frame {f.shape[1]}x{f.shape[0]}, waited {(time.time() - t0) * 1000:.0f} ms, "
                 f"frame age {(time.time() - FEED.t) * 1000:.0f} ms")
            return f
        print("the video feed stopped - switching to adb screenshots", flush=True)
        dlog("screen: video feed stopped -> adb")
        FEED = None
    img = _adb_screenshot()
    if img is not None:
        dlog(f"screen: adb screencap ({_SCREEN['method']}) {img.shape[1]}x{img.shape[0]} in {(time.time() - t0) * 1000:.0f} ms")
        return img
    err1 = LAST_ERR
    # Fallback: some phone/adb combos mangle exec-out. Save on the phone, then pull.
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "adwatch_shot.png")
    adb("shell", "screencap", "-p", "/sdcard/adwatch_shot.png")
    adb("pull", "/sdcard/adwatch_shot.png", tmp)
    img = cv2.imread(tmp)
    if img is not None:
        return img
    devices = "\n".join(f"{d['serial']}  {d['model']}" for d in list_devices()) or "(none ready)"
    raise RuntimeError(
        "screencap failed.\n"
        f"  adb said: {err1 or LAST_ERR or '(nothing)'}\n"
        f"  devices:\n    " + devices.replace("\n", "\n    ") + "\n"
        "  Fixes: exactly ONE device must show 'device' (not 'offline'/'unauthorized'). "
        "With several, pick one: python adwatch.py -s <serial> ...   "
        "(a wireless phone can be listed twice - use only the ip:port one; "
        "'adb disconnect' the extras)."
    )


LAST_FRAME = None    # the colour frame behind the latest gray_screen()
_FRAME_T = [0.0]     # when it was taken


def gray_screen():
    global LAST_FRAME
    LAST_FRAME = screenshot()
    _FRAME_T[0] = time.time()
    return cv2.cvtColor(LAST_FRAME, cv2.COLOR_BGR2GRAY)


def progress_bar(frame):
    """Length (0-1 of the screen width) of a coloured bar running from the left along the
    bottom of the screen - the progress line many video ads show - or None.
    Rows must be solidly coloured from the left edge; two neighbouring rows must agree, so
    text and buttons don't count. A bar only means "still playing" when its length changes."""
    if frame is None or frame.ndim != 3:
        return None
    h, w = frame.shape[:2]
    band = frame[int(h * 0.94):]
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    colored = (hsv[..., 1] > 70) & (hsv[..., 2] > 80)
    lengths = []
    for row in colored:
        start = int(np.argmax(row)) if row.any() else w
        if start > w * 0.03:
            lengths.append(0)
            continue
        off = np.flatnonzero(~row[start:])
        lengths.append((off[0] if off.size else w - start) + start)
    best = 0.0
    for i, (a, b) in enumerate(zip(lengths, lengths[1:])):
        # a long, even line: two rows the same length, and one colour all the way along
        if a > w * 0.08 and abs(a - b) <= w * 0.005:
            hue = hsv[i, :a, 0].astype(np.float32)
            if hue.std() < 12:
                best = max(best, min(a, b) / w)
    return best or None


class FastInput:
    """Taps, presses and keys through ONE adb shell kept open, using `cmd input` where the
    phone has it (Android 11+). `adb shell input tap` starts a new adb process and a small
    Java program for every tap (about 0.3-0.5 s); this way a tap takes tens of milliseconds."""

    def __init__(self):
        import queue
        import threading
        self.queue = queue
        cmd = [ADB] + (["-s", SERIAL] if SERIAL else []) + ["shell"]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.q = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self.n = 0
        out = self.run("cmd input 2>&1 | head -20", timeout=8)
        if out is None:
            raise RuntimeError("the adb shell didn't answer")
        self.prog = "cmd input" if re.search(r"\btap\b", out) and "Can't find service" not in out else "input"
        dlog(f"fast input: persistent adb shell, using '{self.prog}'")

    def _reader(self):
        for line in self.p.stdout:
            self.q.put(line)
        self.q.put(None)

    def run(self, command, timeout=5):
        """Run a command in the open shell and wait for it to finish; None if the shell is gone."""
        self.n += 1
        marker = f"__adwatch_{self.n}__"
        try:
            self.p.stdin.write(f"{command}; echo {marker}\n")
            self.p.stdin.flush()
        except (OSError, ValueError):
            return None
        out, end = [], time.time() + timeout
        while time.time() < end:
            try:
                line = self.q.get(timeout=max(0.01, end - time.time()))
            except self.queue.Empty:
                break
            if line is None:
                return None
            if marker in line:
                return "".join(out)
            out.append(line)
        return None

    def close(self):
        try:
            self.p.stdin.close()
            self.p.terminate()
        except Exception:
            pass


FAST_INPUT = os.environ.get("ADWATCH_FAST_INPUT", "1") != "0"
_FAST = {"obj": None, "failed": False}


def input_cmd(args):
    """Send `input <args>` to the phone - through the fast shell when possible."""
    if FAST_INPUT and not _FAST["failed"]:
        try:
            if _FAST["obj"] is None:
                _FAST["obj"] = FastInput()
            t0 = time.time()
            if _FAST["obj"].run(f"{_FAST['obj'].prog} {args}") is not None:
                dlog(f"input {args} ({(time.time() - t0) * 1000:.0f} ms, fast)")
                return
        except Exception as e:
            dlog(f"fast input unavailable: {e}")
        _FAST["failed"] = True                         # fall back to one adb call per tap from now on
        if _FAST["obj"] is not None:
            _FAST["obj"].close()
        dlog("fast input: switched off, using plain adb shell input")
    shell(f"input {args}")


def quick_shell(command):
    """Run a short command on the phone through the open fast-input shell (no new adb process,
    about half the time); falls back to a normal adb shell call."""
    if FAST_INPUT and not _FAST["failed"]:
        try:
            if _FAST["obj"] is None:
                _FAST["obj"] = FastInput()
            t0 = time.time()
            out = _FAST["obj"].run(command, timeout=6)
            if out is not None:
                dlog(f"quick shell {command[:50]} -> {len(out)} bytes, {(time.time() - t0) * 1000:.0f} ms")
                return out
            dlog("quick shell: no answer - using plain adb calls from now on")
        except Exception as e:
            dlog(f"quick shell unavailable: {e}")
        _FAST["failed"] = True
        if _FAST["obj"] is not None:
            _FAST["obj"].close()
    return shell(command)


def tap(x, y, jitter=6):
    global LAST_ACTION
    x += random.randint(-jitter, jitter)
    y += random.randint(-jitter, jitter)
    dlog(f"TAP {x},{y}")
    input_cmd(f"tap {x} {y}")
    LAST_ACTION = time.time()


def press(x, y, ms=120, jitter=3):
    """A tap held for `ms` milliseconds. Some ad players ignore adb's instant taps
    (pressed and released in the same moment); a short hold reads like a finger."""
    global LAST_ACTION
    x += random.randint(-jitter, jitter)
    y += random.randint(-jitter, jitter)
    dlog(f"PRESS {x},{y} for {int(ms)} ms")
    input_cmd(f"swipe {x} {y} {x} {y} {int(ms)}")
    LAST_ACTION = time.time()


def swipe(x1, y1, x2, y2, ms=250):
    """A finger drag from (x1, y1) to (x2, y2), kept on the screen."""
    global LAST_ACTION
    W, H = screen_size()
    x2, y2 = min(max(x2, 5), W - 5), min(max(y2, 5), H - 5)
    dlog(f"SWIPE {x1},{y1} -> {x2},{y2} in {int(ms)} ms")
    input_cmd(f"swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}")
    LAST_ACTION = time.time()


def template_box(path, screen_short):
    """Size (w, h) in real screen pixels of a template as it appears on this screen.
    `screen_short` is the screen's shorter side (its width in portrait, height in landscape)."""
    t = load_template(path, screen_short)
    f = screen_short / MATCH_W
    return int(t.shape[1] * f), int(t.shape[0] * f)


def outside_point(hit, screen_wh, override=None, pick=0):
    """A spot to tap that is outside a matched pop-up (so the tap dismisses it instead of
    pressing anything on it): beside the pop-up at the same height, on whichever side has
    room, kept out of the screen's edge strips. Returns screen pixels."""
    W, H = screen_wh
    if override:
        fx, fy, _ = safe_point(*override)
        return int(fx * W), int(fy * H)
    cx, cy, path = hit[1], hit[2], hit[3]
    bw, bh = template_box(path, min(W, H))
    left, right, top, bottom = cx - bw * 0.6, cx + bw * 0.6, cy - bh * 0.6, cy + bh * 0.6
    spots = []
    for fx, fy in ((0.12, cy / H), (0.88, cy / H), (0.5, min(0.85, (cy + bh * 1.2) / H)), (0.5, 0.5)):
        fx, fy, _ = safe_point(fx, fy)
        x, y = fx * W, fy * H
        if not (left <= x <= right and top <= y <= bottom):
            spots.append((int(x), int(y)))
    return spots[pick % len(spots)] if spots else (int(0.5 * W), int(0.5 * H))


def safe_point(fx, fy):
    """Keep a tap out of the top and bottom 7% of the screen, where Android's status bar and
    gesture areas can swallow taps during full-screen ads. Returns (fx, fy, moved)."""
    ny = min(max(fy, 0.07), 0.93)
    return fx, ny, ny != fy


def screen_size():
    """(width, height) of the screen as it is right now - swapped while an ad is in landscape."""
    if LAST_FRAME is not None and time.time() - _FRAME_T[0] < 5:
        h, w = LAST_FRAME.shape[:2]
        return w, h
    nums = re.findall(r"(\d+)x(\d+)", shell("wm size"))
    if not nums:
        h, w = screenshot().shape[:2]
        return w, h
    return tuple(map(int, nums[-1]))   # last entry is the override size if one is set


def key(name):
    global LAST_ACTION
    dlog(f"KEY {name}")
    input_cmd(f"keyevent {name}")
    LAST_ACTION = time.time()


FOCUS_RE = re.compile(r"([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/([A-Za-z0-9_.$]+)")


def focus():
    """(package, activity) of what is in front, e.g.
    ('com.some.game', 'com.google.android.gms.ads.AdActivity'). (None, None) if unknown."""
    out = quick_shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
    m = FOCUS_RE.search(out)
    if not m:
        return None, None
    pkg, act = m.group(1), m.group(2)
    if act.startswith("."):          # short form: pkg/.MainActivity
        act = pkg + act
    dlog(f"focus: {pkg} / {act.rsplit('.', 1)[-1]}")
    return pkg, act


def foreground():
    return focus()[0]


# Full-screen ad screens are separate Android "activities" that ad SDKs add to the game.
# These prefixes/names are the common ones. They are only used to word the log and to avoid
# learning an ad as the game's own screen: in watch mode ANY screen of the game that isn't
# one of its known normal ("home") activities is treated as an ad.
AD_ACTIVITY_PREFIXES = (
    "com.google.android.gms.ads", "com.unity3d.services.ads", "com.unity3d.ads", "com.applovin",
    "com.ironsource", "com.supersonic", "com.vungle", "com.facebook.ads", "com.bytedance",
    "com.mbridge", "com.mintegral", "com.chartboost", "com.inmobi", "com.adcolony", "com.fyber",
    "com.tapjoy", "com.moloco", "com.smaato", "com.ogury", "io.bidmachine", "com.yandex.mobile.ads",
    "com.my.target", "com.hyprmx", "com.digitalturbine", "com.amazon.device.ads", "com.mopub",
    "com.startapp", "com.appodeal",
)
AD_CLASS_RE = re.compile(
    r"^(Ads?([A-Z]\w*)?Activity|\w*(Interstitial|RewardedVideo|RewardVideo|FullscreenAd|FullScreenAd|AudienceNetwork)\w*)$")


def foreign_ad(pkg, act):
    """True when an app OTHER than the game is showing an ad on the game's behalf. Meta's ads
    ("Ads served by Meta") run inside the Facebook app: com.facebook.katana /
    ...AudienceNetworkRemoteActivity. Decided by the screen's name, so opening the Facebook app
    yourself (its normal screens) never counts."""
    return bool(pkg) and bool(act) and pkg != "com.android.vending" and is_ad_activity(act)


def app_chooser(pkg, act):
    """True for Android's own "Open with" sheet (Play Store / Galaxy Store / ...), which a tap
    on an ad's store link brings up on phones with more than one store. It belongs to the
    system ("android"), so it would otherwise pass for the game still being in front."""
    return pkg == "android" and bool(act) and bool(re.search(r"Resolver|Chooser", act))


def is_ad_activity(activity, extra=()):
    if not activity:
        return False
    last = activity.rsplit(".", 1)[-1]
    return (activity.startswith(AD_ACTIVITY_PREFIXES) or bool(AD_CLASS_RE.match(last))
            or any(e.lower() in activity.lower() for e in extra))


def launch(pkg):
    shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")


def nap(seconds, spread=0.25):
    time.sleep(max(0.1, seconds * random.uniform(1 - spread, 1 + spread)))


# ------------------------------------------------------------- getting adb
PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-{}.zip"
PLATFORM_TOOLS_PAGE = "https://developer.android.com/tools/releases/platform-tools"


def install_adb(say=print):
    """Download Google's SDK Platform-Tools (which contains adb) into the data folder and
    switch to it. Only called after the user has agreed; raises RuntimeError with a
    readable reason on failure."""
    global ADB
    import shutil
    import tempfile
    import urllib.request
    import zipfile
    plat = ("windows" if os.name == "nt" else "darwin" if sys.platform == "darwin"
            else "linux" if sys.platform.startswith("linux") else None)
    if "ANDROID_ROOT" in os.environ:                    # running on a phone (Termux)
        raise RuntimeError("In Termux, install adb with:  pkg install android-tools")
    if plat is None:
        raise RuntimeError(f"There is no ready-made adb for this system. See {PLATFORM_TOOLS_PAGE}")
    url = os.environ.get("ADWATCH_PLATFORM_TOOLS_URL") or PLATFORM_TOOLS_URL.format(plat)  # env: mirror
    os.makedirs(DATA_DIR, exist_ok=True)
    root = os.path.realpath(DATA_DIR)
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    say(f" downloading {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            total, done, shown = int(r.headers.get("Content-Length") or 0), 0, -1
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                pct = done * 100 // total if total else -1
                if pct >= shown + 10:
                    shown = pct
                    say(f"   {pct}%")
        with zipfile.ZipFile(tmp) as z:
            for name in z.namelist():                   # nothing may land outside the data folder
                if not os.path.realpath(os.path.join(root, name)).startswith(root + os.sep):
                    raise RuntimeError("The download contained an unexpected file; nothing was installed.")
            z.extractall(root)
    except (OSError, zipfile.BadZipFile) as e:
        raise RuntimeError(f"The download failed ({e}). You can get it by hand from {PLATFORM_TOOLS_PAGE}")
    finally:
        os.remove(tmp)
    folder = os.path.join(root, "platform-tools")
    exe = os.path.join(folder, ADB_EXE)
    if not os.path.exists(exe):
        raise RuntimeError(f"The download didn't contain adb. Get it by hand from {PLATFORM_TOOLS_PAGE}")
    if os.name != "nt":
        os.chmod(exe, 0o755)                            # zip extraction drops the executable bit
    try:
        works = subprocess.run([exe, "version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        works = False
    if not works:                                       # e.g. Google's Linux build is x86-64 only
        shutil.rmtree(folder, ignore_errors=True)
        raise RuntimeError("The downloaded adb doesn't run on this computer. Install it with your "
                           "system's package manager instead (the package is usually 'android-tools' or 'adb').")
    ADB = exe
    say(f" adb installed in {folder}")
    return exe


def cmd_get_adb(args=None):
    if adb_available() and not getattr(args, "force", False):
        print(f"adb is already installed: {ADB}")
        return
    print("This downloads Google's SDK Platform-Tools, which is provided under the Android SDK\n"
          f"licence: {PLATFORM_TOOLS_PAGE}")
    try:
        install_adb()
    except RuntimeError as e:
        sys.exit(str(e))


# ------------------------------------------------------- device auto-detection
IP_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+:\d+$")


def list_devices():
    try:
        out = subprocess.run([ADB, "devices", "-l"], capture_output=True).stdout.decode(errors="replace")
    except FileNotFoundError:          # adb isn't installed
        return []
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devs.append({"serial": parts[0], "model": info.get("model", "?"), "device": info.get("device", "?")})
    # A wireless phone is often listed twice (mDNS 'adb-xxx' name + ip:port): keep one.
    keep = []
    for d in devs:
        twin = [o for o in devs if o is not d and (o["model"], o["device"]) == (d["model"], d["device"])]
        if d["serial"].startswith("adb-") and any(not o["serial"].startswith("adb-") for o in twin):
            continue
        keep.append(d)
    return keep


def choose_device():
    """Pick the phone to control: -s/ADWATCH_SERIAL, else the only one, else ask."""
    global SERIAL
    if SERIAL:
        return
    if not adb_available():
        sys.exit(NO_ADB)
    devs = list_devices()
    if not devs:
        sys.exit("no phone found. Run 'adb devices -l' - it should show one line ending in 'device'.")
    if len(devs) == 1:
        d = devs[0]
    elif sys.stdin.isatty():
        print("Several phones connected:")
        for i, d in enumerate(devs, 1):
            print(f"  {i}) {d['model']}  ({d['serial']})")
        while True:
            pick = input("Which one? ").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(devs):
                d = devs[int(pick) - 1]
                break
    else:
        sys.exit("several phones connected - choose one with -s SERIAL:\n  " +
                 "\n  ".join(f"{d['serial']}  {d['model']}" for d in devs))
    SERIAL = d["serial"]
    print(f"using {d['model']} ({d['serial']})")


# ---------------------------------------------------------- template matching
_cache = {}


MATCH_W = 540          # everything is compared with the screen's SHORT side at this size, whatever the
                       # phone - so a landscape ad is not shrunk to fit its long side into 540 pixels
_warned = set()


def template_ref_width(path):
    """Short side of the screen a template was captured on (sidecar written by grab/crop):
    its width for a picture cut from a portrait screen, its height for a landscape one."""
    try:
        with open(path + ".json") as f:
            meta = json.load(f)
        return min(meta["width"], meta.get("height") or meta["width"])
    except (OSError, KeyError, ValueError, TypeError):
        return None


def load_template(path, screen_w):
    """Template as grayscale, scaled to MATCH_W space. `screen_w` is the current screen's short side."""
    key = (path, screen_w)
    if key not in _cache:
        t = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if t is None:
            raise FileNotFoundError(f"template not found or unreadable: {path}")
        ref_w = template_ref_width(path)
        if ref_w is None:
            ref_w = screen_w
            if path not in _warned:
                _warned.add(path)
                print(f"  note: {os.path.basename(path)} has no screen-size info, assuming it was "
                      f"captured at this phone's width ({screen_w}px). "
                      f"For templates from another phone run: adwatch.py stamp WIDTH HEIGHT")
        k = MATCH_W / ref_w
        if abs(k - 1) > 0.01:
            t = cv2.resize(t, None, fx=k, fy=k, interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)
        _cache[key] = t
    return _cache[key]


def expand(patterns, base, quiet=False):
    if isinstance(patterns, str):
        patterns = [patterns]
    out = []
    for p in patterns or []:
        matches = sorted(glob.glob(os.path.join(base, p)))
        if not matches and not quiet:
            print(f"  ! no files match {p}", file=sys.stderr)
        out += matches
    return out


def _grad(g):
    """Gradient magnitude: ignores brightness changes like the in-game shine sweep."""
    g = cv2.GaussianBlur(g, (3, 3), 0)
    return cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))


def _flat(integ, x, y, tw, th, floor):
    """True if the screen window at (x, y) has (almost) no texture (blank / black)."""
    s, sq = integ
    n = th * tw
    ws = s[y + th, x + tw] - s[y, x + tw] - s[y + th, x] + s[y, x]
    wq = sq[y + th, x + tw] - sq[y, x + tw] - sq[y + th, x] + sq[y, x]
    return max(wq / n - (ws / n) ** 2, 0.0) < floor ** 2


def _match(img, integ, t):
    """Best normalized correlation of template t inside img -> (score, top-left).
    Windows with (almost) no texture are ignored: OpenCV can report a perfect 1.0 for
    a blank/black area, which would make the script tap on black ad frames.
    Fast path: only the best few spots are checked for texture; the whole-map check runs
    only when the screen has large blank areas that keep winning."""
    th, tw = t.shape
    res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
    cv2.patchNaNs(res, 0.0)
    floor = max(2.0, 0.25 * float(t.std()))
    orig = None
    for _ in range(6):
        _, score, _, loc = cv2.minMaxLoc(res)
        if score <= 0:
            return 0.0, loc
        x, y = loc
        if score <= 1.0001 and not _flat(integ, x, y, tw, th, floor):
            return score, loc
        if orig is None:
            orig = res.copy()
        res[max(0, y - th):y + th + 1, max(0, x - tw):x + tw + 1] = 0.0   # blank spot: look elsewhere
    # A big blank area keeps winning: mask every blank window (slower, exact).
    res = np.nan_to_num(orig, nan=0.0, posinf=0.0, neginf=0.0)
    res[res > 1.0001] = 0.0
    s, sq = integ
    n = th * tw
    wsum = s[th:, tw:] - s[:-th, tw:] - s[th:, :-tw] + s[:-th, :-tw]
    wsq = sq[th:, tw:] - sq[:-th, tw:] - sq[th:, :-tw] + sq[:-th, :-tw]
    var = np.maximum(wsq / n - (wsum / n) ** 2, 0)
    res[var < floor ** 2] = 0.0
    _, score, _, loc = cv2.minMaxLoc(res)
    return score, loc


EDGE_BAND = 0.45      # edge matching only runs when the plain match lands between this and the target
_hits = {}            # template path -> last time it matched (tried first next time)


COARSE_MIN = 32      # templates at least this big (at match width) are searched at half size first
COARSE_FLOOR = 0.40  # a half-size score below this means "not on screen"


def _match_top(img, integ, t, k=2):
    """Up to k best (score, top-left) spots for t, far enough apart, skipping blank windows."""
    th, tw = t.shape
    res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
    cv2.patchNaNs(res, 0.0)
    floor = max(2.0, 0.25 * float(t.std()))
    out = []
    for _ in range(k + 6):
        _, score, _, loc = cv2.minMaxLoc(res)
        if score <= 0 or len(out) >= k:
            break
        x, y = loc
        if score <= 1.0001 and not _flat(integ, x, y, tw, th, floor):
            out.append((score, loc))
        res[max(0, y - th):y + th + 1, max(0, x - tw):x + tw + 1] = 0.0
    return out


def _refine(small, small_g, t, tg, loc2, use_edge):
    """Full-size match of t in a small window around a half-size hit at loc2."""
    th, tw = t.shape
    mx, my = max(6, tw // 4), max(6, th // 4)
    x0, y0 = max(0, loc2[0] * 2 - mx), max(0, loc2[1] * 2 - my)
    x1, y1 = min(small.shape[1], loc2[0] * 2 + tw + mx), min(small.shape[0], loc2[1] * 2 + th + my)
    roi = small[y0:y1, x0:x1]
    if roi.shape[0] < th or roi.shape[1] < tw:
        return 0.0, (x0, y0)
    score, loc = _match(roi, cv2.integral2(roi), t)
    if use_edge(score) and tg is not None:
        rg = small_g[y0:y1, x0:x1]
        s2, l2 = _match(rg, cv2.integral2(rg), tg)
        if s2 > score:
            score, loc = s2, l2
    return score, (loc[0] + x0, loc[1] + y0)


def score_all(gray, paths, stop_at=None, thorough=False):
    """Return [(score, cx, cy, path)] (cx, cy in real screen pixels), best first.
    Score = better of plain grayscale matching and edge matching. Edge matching (for
    shimmering or glowing buttons) only runs when the plain score is promising but short
    of `stop_at` (or always with thorough=True, used by `test`). With stop_at, stops at
    the first template that reaches it."""
    h, w = gray.shape[:2]
    short = min(w, h)
    f = short / MATCH_W
    small = cv2.resize(gray, (max(1, round(w / f)), max(1, round(h / f))), interpolation=cv2.INTER_AREA)
    small_g = _grad(small)
    integ, integ_g = cv2.integral2(small), cv2.integral2(small_g)
    results = []
    high = stop_at if stop_at is not None else 0.97
    half = half_integ = None
    use_edge = (lambda sc: thorough or EDGE_BAND <= sc < high)
    for p in paths:
        t = load_template(p, short)
        if t.shape[0] > small.shape[0] or t.shape[1] > small.shape[1]:
            continue
        gkey = (p, short, "grad")
        if gkey not in _cache:
            g = _grad(t)
            _cache[gkey] = g if g.std() > 1e-3 else None
        tg = _cache[gkey]
        th, tw = t.shape
        if not thorough and min(th, tw) >= COARSE_MIN:
            # Search a half-size copy of the screen (about 4x less work), then confirm the best
            # spots at full size. Small templates (tiny X buttons) skip this and use full size.
            if half is None:
                half = cv2.resize(small, (small.shape[1] // 2, small.shape[0] // 2), interpolation=cv2.INTER_AREA)
                half_integ = cv2.integral2(half)
            hkey = (p, short, "half")
            if hkey not in _cache:
                _cache[hkey] = cv2.resize(t, (tw // 2, th // 2), interpolation=cv2.INTER_AREA)
            cands = _match_top(half, half_integ, _cache[hkey], k=2)
            if not cands or cands[0][0] < COARSE_FLOOR:
                score, loc = (cands[0][0], (cands[0][1][0] * 2, cands[0][1][1] * 2)) if cands else (0.0, (0, 0))
            else:
                score, loc = -1.0, (0, 0)
                for _, l2 in cands:
                    sc, lc = _refine(small, small_g, t, tg, l2, use_edge)
                    if sc > score:
                        score, loc = sc, lc
        else:
            score, loc = _match(small, integ, t)
            if use_edge(score) and tg is not None:
                s2, loc2 = _match(small_g, integ_g, tg)
                if s2 > score:
                    score, loc = s2, loc2
        results.append((score, int((loc[0] + tw / 2) * f), int((loc[1] + th / 2) * f), p))
        if stop_at is not None and score >= stop_at:
            break
    return sorted(results, reverse=True)


def find(gray, paths, threshold, what=""):
    """Best template at or above threshold, else None. Templates that matched recently are
    tried first, and a very confident match ends the search early."""
    t0 = time.time()
    order = sorted(paths, key=lambda p: -_hits.get(p, 0.0))
    ranked = score_all(gray, order, stop_at=max(threshold, 0.92))
    best = ranked[0] if ranked else None
    hit = best if best and best[0] >= threshold else None
    if hit:
        _hits[hit[3]] = time.time()
    dlog(f"find {what or '?'}: {len(paths)} templates, checked {len(ranked)}, "
         + (f"best {os.path.basename(best[3])} {best[0]:.2f} at {best[1]},{best[2]}" if best else "none")
         + f" (need {threshold:.2f}) -> {'HIT' if hit else 'miss'} in {(time.time() - t0) * 1000:.0f} ms")
    return hit


# ------------------------------------------------ ad buttons: where, what size, which spots lie
# Real close / skip buttons and store badges sit near the screen's edges: in a band along the
# top or bottom, or a strip down either side. Away from the edges a "match" is usually a
# look-alike inside the ad's video (a dark ringed X matched dark footage, and the tap opened the
# store), so there a picture has to match almost perfectly.
EDGE_TOP, EDGE_BOTTOM, EDGE_SIDE = 0.20, 0.18, 0.12
MID_SCREEN_MIN = 0.96
EXTRA_SCALES = (0.8, 0.9, 1.12, 1.25)   # other sizes tried when nothing matched at the saved size
NEAR_MISS = 0.80                         # a score this close, in the same spot look after look, counts


def in_edge_zone(x, y, w, h):
    return y <= h * EDGE_TOP or y >= h * (1 - EDGE_BOTTOM) or x <= w * EDGE_SIDE or x >= w * (1 - EDGE_SIDE)


def spot_of(x, y, w, h):
    """A coarse place on the screen (an 8x8 grid, per orientation), for remembering where a
    picture turned out to be a decoy."""
    return f"{'L' if w > h else 'P'}{min(7, max(0, int(8 * x / w)))},{min(7, max(0, int(8 * y / h)))}"


def _scaled(path, short, k):
    key = (path, short, "x", k)
    if key not in _cache:
        t = load_template(path, short)
        _cache[key] = cv2.resize(t, None, fx=k, fy=k, interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)
    return _cache[key]


def _match_zones(small, integ, t, ok):
    """Best (score, top-left) of t near the edges of small (the bands and strips of
    in_edge_zone, each widened by the template's size), centre passing ok(score, cx, cy)."""
    sh, sw = small.shape[:2]
    th, tw = t.shape
    top, bot, side = int(sh * EDGE_TOP), int(sh * (1 - EDGE_BOTTOM)), int(sw * EDGE_SIDE)
    rects = [(0, 0, sw, top + th), (0, bot - th, sw, sh),
             (0, top - th, side + tw, bot + th), (sw - side - tw, top - th, sw, bot + th)]
    best = (0.0, None)
    for x0, y0, x1, y1 in rects:
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(sw, x1), min(sh, y1)
        crop = small[y0:y1, x0:x1]
        sub = tuple(a[y0:y1 + 1, x0:x1 + 1] for a in integ)     # an integral image slices cleanly
        sc, loc = _match_where(crop, sub, t, lambda s_, cx, cy: ok(s_, cx + x0, cy + y0))
        if loc is not None and sc > best[0]:
            best = (sc, (loc[0] + x0, loc[1] + y0))
    return best


def _match_where(small, integ, t, ok):
    """Best (score, top-left) of t in small whose centre passes ok(score, cx, cy); (0, None) if none."""
    th, tw = t.shape
    if th > small.shape[0] or tw > small.shape[1] or min(th, tw) < 5:
        return 0.0, None
    res = cv2.matchTemplate(small, t, cv2.TM_CCOEFF_NORMED)
    cv2.patchNaNs(res, 0.0)
    res[res > 1.0001] = 0.0
    floor = max(2.0, 0.25 * float(t.std()))
    for _ in range(16):
        _, score, _, loc = cv2.minMaxLoc(res)
        if score <= 0.3:
            break
        x, y = loc
        if ok(score, x + tw / 2, y + th / 2) and not _flat(integ, x, y, tw, th, floor):
            return score, loc
        res[max(0, y - th // 2):y + th // 2 + 1, max(0, x - tw // 2):x + tw // 2 + 1] = 0.0
    return 0.0, None


def find_button(gray, paths, thr, what="", blocked=(), near=NEAR_MISS, scales=EXTRA_SCALES, second_look=True):
    """An ad button (close, skip, store badge) on screen -> (hit, near_misses).
    hit = (score, x, y, path) or None; near_misses = [(score, x, y, path)] that scored at least
    `near` without reaching `thr`, in usable spots.
    Compared with find(): a match away from the screen's edges must reach MID_SCREEN_MIN;
    `blocked` holds (picture name, spot) pairs that opened the store before and are skipped
    (the same picture elsewhere is still fine); and when nothing matches at the saved size, the
    closest pictures are tried a little smaller and larger - an X from another ad is rarely
    exactly the size of the one that was saved."""
    if gray is None or not paths:               # no picture (test stand-ins): plain find
        return (find(gray, paths, thr, what) if paths else None), []
    t0 = time.time()
    H, W = gray.shape[:2]

    def usable(score, x, y, path):
        return ((os.path.basename(path), spot_of(x, y, W, H)) not in blocked
                and (in_edge_zone(x, y, W, H) or score >= MID_SCREEN_MIN))

    order = sorted(paths, key=lambda p: -_hits.get(p, 0.0))
    ranked = score_all(gray, order, stop_at=0.97)
    hit = next((r for r in ranked if r[0] >= thr and usable(*r)), None)
    if hit is None and len(ranked) < len(order):    # stopped early on a match it can't use: score the rest
        seen = {r[3] for r in ranked}
        ranked = sorted(ranked + score_all(gray, [p for p in order if p not in seen]), reverse=True)
        hit = next((r for r in ranked if r[0] >= thr and usable(*r)), None)
    how = ""
    if hit is None and second_look:
        # Second look at the closest few: at other sizes, and (for a picture whose best spot was
        # ruled out) its best usable spot at the saved size.
        short = min(W, H)
        f = short / MATCH_W
        small = cv2.resize(gray, (max(1, round(W / f)), max(1, round(H / f))), interpolation=cv2.INTER_AREA)
        integ = cv2.integral2(small)
        best = None
        for r in [r for r in ranked if r[0] >= 0.3][:4]:
            p = r[3]
            for k in ((1.0,) if not usable(*r) else ()) + tuple(scales):
                t = _scaled(p, short, k) if k != 1.0 else load_template(p, short)
                sc, loc = _match_zones(small, integ, t, lambda s, cx, cy: usable(s, cx * f, cy * f, p))
                if loc is not None and (best is None or sc > best[0]):
                    th, tw = t.shape
                    best = (sc, int((loc[0] + tw / 2) * f), int((loc[1] + th / 2) * f), p, k)
        if best is not None:
            ranked = sorted(ranked + [best[:4]], reverse=True)
            if best[0] >= thr:
                hit, how = best[:4], f" at {best[4]:.2f}x size"
    nears = [r for r in ranked if near <= r[0] < thr and usable(*r)] if hit is None else []
    if hit:
        _hits[hit[3]] = time.time()
    top = ranked[0] if ranked else None
    dlog(f"find {what or 'button'}: {len(paths)} pictures"
         + (f", best {os.path.basename(top[3])} {top[0]:.2f} at {top[1]},{top[2]}" if top else "")
         + (f" -> HIT {os.path.basename(hit[3])} {hit[0]:.2f} at {hit[1]},{hit[2]}{how}" if hit else
            f" -> miss (need {thr:.2f}" + (f"; near: " + ", ".join(f"{os.path.basename(n[3])} {n[0]:.2f}" for n in nears[:3])
                                            if nears else "") + ")")
         + f" in {(time.time() - t0) * 1000:.0f} ms")
    return hit, nears


# ------------------------------------------------ is the ad still playing?
def picture_motion(prev, gray):
    """How the middle of the picture changed since the last look -> (small copy, changed cells).
    The middle 76% is cut into a 4x6 grid; a cell 'changed' when over 8% of its pixels did.
    A video changes many cells; a pointing hand, a sparkle or a pulsing button only one or
    two, which is animation on a screen that is waiting, not a video still running."""
    gh, gw = gray.shape[:2]
    small = cv2.resize(gray[int(gh * 0.12):int(gh * 0.88), int(gw * 0.1):int(gw * 0.9)], (108, 208),
                       interpolation=cv2.INTER_AREA)
    if prev is None or prev.shape != small.shape:
        return small, None
    diff = cv2.absdiff(small, prev) > 25
    cells = sum(1 for i in range(6) for j in range(4)
                if diff[i * 34:(i + 1) * 34, j * 27:(j + 1) * 27].mean() > 0.08)
    return small, cells


class CountdownRing:
    """A small circle in a corner with a number counting down inside it (the 'ad ends in 5'
    ring some ads show instead of a progress bar). Seen as: a circle found in the same place on
    two looks in a row, whose inside changed while the area just around it did not (so it is
    the number ticking, not video behind it)."""

    CORNERS = ((0, 0, 0.25, 0.13), (0.75, 0, 1, 0.13), (0, 0.87, 0.25, 1), (0.75, 0.87, 1, 1))

    def __init__(self):
        self.prev = []            # [(x, y, r, inside, around)] from the last look
        self.last_tick = -1e9
        self.where = None

    @staticmethod
    def _patch(g, x, y, r, k):
        h, w = g.shape[:2]
        rr = max(2, int(r * k))
        return g[max(0, y - rr):min(h, y + rr), max(0, x - rr):min(w, x + rr)]

    def update(self, gray, now):
        """Returns True while a countdown is ticking (seen within the last 4 seconds)."""
        h, w = gray.shape[:2]
        short = min(w, h)
        found = []
        for x0, y0, x1, y1 in self.CORNERS:
            X0, Y0, X1, Y1 = int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)
            crop = cv2.GaussianBlur(gray[Y0:Y1, X0:X1], (3, 3), 0)
            if crop.size == 0:
                continue
            cs = cv2.HoughCircles(crop, cv2.HOUGH_GRADIENT, dp=1, minDist=max(8, short // 40), param1=120, param2=22,
                                  minRadius=max(4, int(short * 0.015)), maxRadius=max(6, int(short * 0.06)))
            for cx, cy, r in (cs[0][:4] if cs is not None else []):
                x, y, r = int(X0 + cx), int(Y0 + cy), int(r)
                inside = self._patch(gray, x, y, r, 0.6).astype(np.int16)
                around = gray[max(0, y - int(r * 1.9)):y + int(r * 1.9), max(0, x - int(r * 1.9)):x + int(r * 1.9)].astype(np.int16)
                found.append((x, y, r, inside, around))
        ticking = False
        for x, y, r, inside, around in found:
            for px, py, pr, pin, paround in self.prev:
                if abs(px - x) <= 3 and abs(py - y) <= 3 and abs(pr - r) <= 2 \
                        and pin.shape == inside.shape and paround.shape == around.shape:
                    changed_in = float((np.abs(inside - pin) > 40).mean())
                    changed_around = float((np.abs(around - paround) > 40).mean())
                    if changed_in > 0.06 and changed_around < changed_in * 0.5:
                        ticking, self.where = True, (x, y)
        self.prev = found
        if ticking:
            self.last_tick = now
        return now - self.last_tick < 4


def same_screen(a, b, need=0.95):
    """True if two frames show (nearly) the same picture - e.g. the game's screen from before the
    ad, which means the game is back even if none of its saved pictures can be seen."""
    if a is None or b is None or a.shape[:2] != b.shape[:2]:
        return False
    sa = cv2.resize(a if a.ndim == 2 else cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), (54, 120), interpolation=cv2.INTER_AREA)
    sb = cv2.resize(b if b.ndim == 2 else cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), (54, 120), interpolation=cv2.INTER_AREA)
    if sa.std() < 3 or sb.std() < 3:
        return False
    return float(cv2.matchTemplate(sa, sb, cv2.TM_CCOEFF_NORMED)[0][0]) >= need


GAME_FRAME = {"frame": None, "t": 0.0}    # the game's screen just before the ad button was tapped


# ------------------------------------------------ remembered decoys and ad statistics
DECOY_FILE = os.path.join(DATA_DIR, "decoys.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")


def _read_json(path, default):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        dlog(f"could not write {os.path.basename(path)}: {e}")


def load_decoys():
    """Decoys found in earlier runs: {"close": {"close_30.png@P3,5": times}, "ui": {"label": times}}."""
    if not DECOYS.get("_loaded"):
        saved = _read_json(DECOY_FILE, {})
        for kind in ("close", "ui"):
            for k, n in (saved.get(kind) or {}).items():
                if isinstance(n, int):
                    DECOYS[kind][k] = max(DECOYS[kind].get(k, 0), n)
        DECOYS["_loaded"] = True


def save_decoys():
    _write_json(DECOY_FILE, {"close": DECOYS["close"], "ui": DECOYS["ui"]})


_PHONE_NAME = {}


def phone_label():
    """'Pixel 9 Pro XL (R58M123ABCD)' - for the statistics."""
    key = SERIAL or "default"
    if key not in _PHONE_NAME:
        try:
            model = shell("getprop ro.product.model").strip()
        except (Exception, SystemExit):
            model = ""
        _PHONE_NAME[key] = f"{model} ({key})" if model and SERIAL else (model or key)
    return _PHONE_NAME[key]


def record_ad(pkg, outcome, seconds):
    """Count one ad in stats.json: per game, per phone and in total. outcome: 'watched' or 'gave_up'."""
    try:
        stats = _read_json(STATS_FILE, {})
        day = time.strftime("%Y-%m-%d")
        for group, name in (("games", pkg or "?"), ("phones", phone_label()), ("total", None)):
            box = stats.setdefault(group, {})
            if name is not None:
                box = box.setdefault(name, {})
            box[outcome] = box.get(outcome, 0) + 1
            box["seconds"] = round(box.get("seconds", 0) + max(0.0, seconds))
            if outcome == "watched":
                days = box.setdefault("days", {})
                days[day] = days.get(day, 0) + 1
                for old in sorted(days)[:-60]:          # keep two months of daily counts
                    del days[old]
        stats.setdefault("since", day)
        _write_json(STATS_FILE, stats)
    except Exception as e:                              # statistics must never stop a run
        dlog(f"stats: not recorded ({e})")


# ------------------------------------------------ accessibility-tree fallback
CLOSE_RE = re.compile(
    r"(^|[^a-z])(close|skip|dismiss|no thanks|reward granted)([^a-z]|$)|^\s*[x×✕✖]\s*$", re.I
)
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
LOSE_REWARD_RE = re.compile(r"lose (your |the )?reward|close (the )?(ad|video)\s*\?", re.I)
RESUME_RE = re.compile(r"^(resume|resume video|continue watching|keep watching|back to (the )?(ad|video))$", re.I)


STORE_PACKAGES = ("com.android.vending", "com.sec.android.app.samsungapps")


def ui_nodes():
    """The phone's screen tree as a list of nodes, or None if it couldn't be read."""
    # Delete the old dump first and only read it if this dump succeeded: otherwise a failed dump
    # (common during video) left the PREVIOUS screen's file, and we tapped buttons that were gone.
    t0 = time.time()
    xml = shell("rm -f /sdcard/adwatch_ui.xml; uiautomator dump /sdcard/adwatch_ui.xml >/dev/null 2>&1 "
                "&& cat /sdcard/adwatch_ui.xml; rm -f /sdcard/adwatch_ui.xml", timeout=15)
    start = xml.find("<")
    if start < 0:
        dlog(f"ui tree: no dump ({(time.time() - t0) * 1000:.0f} ms) - screen probably animating")
        return None
    try:
        return list(ET.fromstring(xml[start:]).iter("node"))
    except ET.ParseError:
        return None


def store_sheet_open(nodes=None):
    """True if the Play Store (or Galaxy Store) is showing as a sheet over the ad. Such a sheet
    doesn't take over the screen, so Android still reports the ad as what's in front."""
    if nodes is None:
        nodes = ui_nodes()
    return bool(nodes) and any(n.get("package") in STORE_PACKAGES for n in nodes)


def ui_close_button(skip=()):
    """Look for a close/skip control in the UI hierarchy. Often fails during
    video playback (uiautomator can't get an idle state) - that's fine.
    Returns (x, y, label[, kind]); kind "resume" = the lose-your-reward box's Resume,
    "store" = a Play Store sheet is open over the ad (nothing to tap here: go back from it)."""
    t0 = time.time()
    nodes = ui_nodes()
    if nodes is None:
        return None
    if store_sheet_open(nodes):
        dlog("ui tree: a Play Store sheet is open over the ad")
        return (0, 0, "Play Store sheet", "store")
    # Only the game's and the ad's own buttons count (never another app's).
    nodes = [n for n in nodes if n.get("package") not in STORE_PACKAGES]
    # Google's "Close Ad? - You will lose your reward - CLOSE / RESUME" box: the X was tapped
    # too early. RESUME keeps the reward; the caller waits before trying to close again.
    words = " ".join(" ".join(n.get(k, "") for k in ("text", "content-desc")) for n in nodes)
    if LOSE_REWARD_RE.search(words):
        for n in nodes:
            t = (n.get("text") or n.get("content-desc") or "").strip()
            m = BOUNDS_RE.match(n.get("bounds", ""))
            if m and RESUME_RE.match(t):
                x1, y1, x2, y2 = map(int, m.groups())
                dlog(f"ui tree: 'lose your reward' box - '{t}' at {(x1 + x2) // 2},{(y1 + y2) // 2}")
                return ((x1 + x2) // 2, (y1 + y2) // 2, t, "resume")
        dlog("ui tree: 'lose your reward' box without a Resume button - not tapping its Close")
        return None
    for n in nodes:
        label = " ".join(n.get(k, "") for k in ("text", "content-desc", "resource-id"))
        if label.strip() in skip:
            continue
        # A close word in a sentence or a question ("Close Ad?") is a title, not a button.
        if not any(CLOSE_RE.search(v) and not v.strip().endswith("?") and len(v.split()) <= 3
                   for v in (n.get("text", ""), n.get("content-desc", ""), n.get("resource-id", "")) if v.strip()):
            continue
        m = BOUNDS_RE.match(n.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 - x1 < 5 or y2 - y1 < 5:
            continue
        cx = (x1 + x2) // 2
        if re.search(r"reward granted", label, re.I) and x2 - x1 > 2 * (y2 - y1):
            cx = x2 - (y2 - y1) // 2            # the "Reward granted  X" pill: its X is at the right end
        dlog(f"ui tree: '{label.strip()[:60]}' at {cx},{(y1 + y2) // 2} ({(time.time() - t0) * 1000:.0f} ms)")
        return (cx, (y1 + y2) // 2, label.strip())
    dlog(f"ui tree: nothing labelled close/skip ({(time.time() - t0) * 1000:.0f} ms)")
    return None


def restart_game(pkg, log, wait=15):
    """Force-stop the game and open it again - the way out of an ad that won't let go."""
    log("  restarting the game to get out of it")
    shell(f"am force-stop {pkg}")
    nap(2, 0)
    launch(pkg)
    nap(wait, 0.1)


def left_game_within(pkg, seconds):
    """True as soon as something other than the game (or its ad) comes to the front (e.g. the store)."""
    end = time.time() + seconds
    while time.time() < end:
        fg, act = focus()
        if fg and fg != pkg and fg not in IGNORED_FOREGROUND and not foreign_ad(fg, act):
            return True
        nap(0.4, 0)
    return False


def is_launcher(pkg):
    return bool(pkg) and ("launcher" in pkg or pkg.endswith(".home"))


def return_to_game(pkg, log, max_presses=5):
    """Press BACK (as many times as it takes) until the game is in front again.
    Only relaunches the game if BACK dumped us on the home screen."""
    for i in range(max_presses):
        key("KEYCODE_BACK")
        nap(1.5, 0.15)
        fg, act = focus()
        if fg == pkg or foreign_ad(fg, act):
            log(f"  back in the game after {i + 1} BACK press(es)")
            return True
        if is_launcher(fg):
            log("  ended up on the home screen, relaunching the game")
            break
    launch(pkg)
    nap(5)
    return foreground() == pkg


# --------------------------------------------------------------- ad handling
COMMON_DIR = os.path.join(DATA_DIR, "templates", "common")


_digests = {}


def _digest(path):
    """Content hash of a file (cached per path + modification time)."""
    import hashlib
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return path
    if key not in _digests:
        with open(path, "rb") as f:
            _digests[key] = hashlib.sha1(f.read()).hexdigest()
    return _digests[key]


def unique_files(paths):
    """Drop files whose contents duplicate an earlier one (e.g. close_3.png and the same picture
    copied in from the starter set): each picture is only compared once per frame."""
    seen, out = set(), []
    for p in paths:
        d = _digest(p)
        if d not in seen:
            seen.add(d)
            out.append(p)
    return out


def with_common(patterns, base, common_glob):
    """The profile's own templates PLUS everything in templates/common matching
    common_glob - so close/stuck buttons saved once work in every game. Duplicates
    (identical pictures under different names) are dropped."""
    paths = expand(patterns, base, quiet=True)
    paths += glob.glob(os.path.join(COMMON_DIR, common_glob))
    return unique_files(sorted(set(os.path.abspath(p) for p in paths)))


def select_point(img, title):
    """Tk picker: click one spot. Returns (x_fraction, y_fraction) or None."""
    import base64
    import tkinter as tk
    h, w = img.shape[:2]
    root = tk.Tk()
    root.title(title)
    scale = int(root.winfo_screenheight() * 0.82) / h
    view = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    vh, vw = view.shape[:2]
    photo = tk.PhotoImage(data=base64.b64encode(cv2.imencode(".png", view)[1].tobytes()))
    canvas = tk.Canvas(root, width=vw, height=vh, highlightthickness=0, cursor="crosshair")
    canvas.pack(side="top")
    canvas.create_image(0, 0, anchor="nw", image=photo)
    st = {"pt": None, "mark": None, "result": None}

    def click(e):
        st["pt"] = (e.x, e.y)
        if st["mark"]:
            canvas.delete(st["mark"])
        st["mark"] = canvas.create_oval(e.x - 8, e.y - 8, e.x + 8, e.y + 8, outline="#ff2d55", width=3)

    def accept(_e=None):
        if st["pt"]:
            st["result"] = (st["pt"][0] / vw, st["pt"][1] / vh)
            root.destroy()

    bar = tk.Frame(root)
    bar.pack(side="bottom", fill="x")
    canvas.bind("<Button-1>", click)
    root.bind("<Return>", accept)
    root.bind("<Escape>", lambda e: root.destroy())
    tk.Button(bar, text="Use this point (Enter)", command=accept).pack(side="left", padx=6, pady=4)
    tk.Button(bar, text="Cancel (Esc)", command=root.destroy).pack(side="left", pady=4)
    tk.Label(bar, text="Click a spot that is safe to tap (won't open the store)").pack(side="left", padx=10)
    root.lift(); root.focus_force()
    root.mainloop()
    return st["result"]


def save_shared(kind, img, shot):
    """Save a template into the shared pool (templates/common) as close_N / stuck_N."""
    os.makedirs(COMMON_DIR, exist_ok=True)
    n = 1
    while os.path.exists(os.path.join(COMMON_DIR, f"{kind}_{n}.png")):
        n += 1
    out = os.path.join(COMMON_DIR, f"{kind}_{n}.png")
    cv2.imwrite(out, img)
    write_meta(out, shot)
    return out


def teach(cfg, base, log):
    """Pause the run so the user can save a new close/store button from the live screen.
    Returns 'continue' or 'skip'."""
    print("\n=== paused: no known button for this ad ===")
    print("Get the button showing on the phone first (tap the ad screen if it faded), then pick:")
    while True:
        try:
            c = input("  [x] save close button   [k] save a skip / 'reward granted' button (safe any time)\n"
                      "  [g] save Google Play/store button   [p] save a pop-up to tap away\n"
                      "  [w] set wake-tap point   [c] continue   [s] skip this ad (BACK)   [q] quit > ").strip().lower()
        except EOFError:
            return "continue"
        if c in ("x", "g", "p", "k"):
            shot = screenshot()
            what = {"x": "close button", "g": "Google Play / store button", "p": "pop-up (box the whole pop-up)",
                    "k": "skip / reward-granted button"}[c]
            img = select_box(shot, what)
            if img is None:
                print("  cancelled")
                continue
            print("  saved", save_shared({"x": "close", "g": "stuck", "p": "popup", "k": "skip"}[c], img, shot))
        elif c == "w":
            pt = select_point(screenshot(), "wake-tap point")
            path = cfg.get("_profile_path")
            if pt and path:
                with open(path) as f:
                    raw = json.load(f)
                raw["wake_tap"] = [round(pt[0], 3), round(pt[1], 3)]
                if safe_point(*pt)[2]:
                    print("  note: that spot is in the screen's top/bottom edge strip, where Android can "
                          "swallow taps; adwatch will press a little further in. Pick a spot in the ad's "
                          "middle area to avoid that.")
                save_profile(path, raw)
                cfg["wake_tap"] = raw["wake_tap"]
                print(f"  wake tap saved: {raw['wake_tap']} (fractions of the screen)")
        elif c == "c":
            return "continue"
        elif c == "s":
            return "skip"
        elif c == "q":
            raise KeyboardInterrupt


def sync_starter():
    """Copy the ad close buttons / store badges that ship with adwatch into the shared pool.
    Each one is copied once: if you delete one you don't like, it stays deleted."""
    import shutil
    if not os.path.isdir(STARTER_DIR):
        return 0
    state = load_state()
    seen = set(state.get("starter_seen", []))
    added = 0
    have = {_digest(p) for p in glob.glob(os.path.join(COMMON_DIR, "*.png"))}
    for src in sorted(glob.glob(os.path.join(STARTER_DIR, "*.png"))):
        name = os.path.basename(src)
        if name in seen or not re.match(r"^(close|stuck|popup|skip|resume)_", name) or not os.path.exists(src + ".json"):
            continue
        seen.add(name)
        dst = os.path.join(COMMON_DIR, name)
        if not os.path.exists(dst) and _digest(src) not in have:     # skip pictures you already have
            os.makedirs(COMMON_DIR, exist_ok=True)
            shutil.copyfile(src, dst)
            shutil.copyfile(src + ".json", dst + ".json")
            added += 1
    if seen != set(state.get("starter_seen", [])):
        save_state(dict(load_state(), starter_seen=sorted(seen)))
    return added


def cmd_pack(args):
    """Maintainers: copy your saved ad close buttons and store badges into the starter set
    that ships with adwatch (adwatch/starter/), so new users start with them."""
    import hashlib
    import shutil
    src_dir = os.path.abspath(os.path.expanduser(args.source)) if args.source else COMMON_DIR
    dst_dir = os.path.abspath(os.path.expanduser(args.to)) if args.to else STARTER_DIR
    os.makedirs(dst_dir, exist_ok=True)
    added = already = no_size = 0
    for kind in ("close", "stuck", "popup", "skip", "resume"):
        for src in sorted(glob.glob(os.path.join(src_dir, f"{kind}_*.png"))):
            if not os.path.exists(src + ".json"):       # without its screen size it can't be rescaled
                no_size += 1
                print(f"  skipped {os.path.basename(src)}: no screen-size file (run `adwatch stamp W H` first)")
                continue
            with open(src, "rb") as f:
                digest = hashlib.sha1(f.read()).hexdigest()[:10]
            dst = os.path.join(dst_dir, f"{kind}_pack_{digest}.png")   # name = content: no clashes, no duplicates
            if os.path.exists(dst):
                already += 1
                continue
            shutil.copyfile(src, dst)
            shutil.copyfile(src + ".json", dst + ".json")
            added += 1
    print(f"starter set at {dst_dir}: added {added}, already there {already}"
          + (f", skipped {no_size}" if no_size else ""))


def game_markers(cfg, base):
    """Templates only visible when the GAME (not an ad) is on screen: explicit in_game
    templates, the button that opens the pop-up, and the button that starts the ad."""
    marks = expand(cfg.get("back_in_game_templates", []), base, quiet=True)
    marks += expand(cfg.get("open_templates", []), base, quiet=True)
    for step in cfg.get("steps", []):
        if "tap" in step:
            marks += expand(step["tap"], base, quiet=True)
            break
    return sorted(set(marks))


# ------------------------------------------------ the game's own pop-ups ("misc buttons")
# Level-up notices, daily rewards, "OK"s - things the GAME puts up that stop it until someone
# deals with them. Each is saved per game with what to do about it.
MISC_ACTIONS = {
    "tap": "tap it",
    "beside": "tap beside it (it goes away when you tap elsewhere)",
    "swipe-up": "swipe it up", "swipe-down": "swipe it down",
    "swipe-left": "swipe it left", "swipe-right": "swipe it right",
    "back": "press BACK",
}
_MISC_DONE = []        # when misc actions happened (at most misc_max_per_minute)


def misc_buttons(cfg, base):
    """{picture path: action} for the profile's "misc_buttons"."""
    out = {}
    for m in cfg.get("misc_buttons", []):
        if isinstance(m, str):
            m = {"file": m}
        action = m.get("action", "tap") if m.get("action") in MISC_ACTIONS else "tap"
        for p in expand(m.get("file", ""), base, quiet=True):
            out[p] = action
    return out


def clear_misc(cfg, base, log, gray=None):
    """If one of the game's own pop-ups is on screen, deal with it as saved. Returns True if it did.
    Takes no screenshot (and does nothing) for a game with no misc buttons."""
    items = misc_buttons(cfg, base)
    if not items:
        return False
    now = time.time()
    _MISC_DONE[:] = [t for t in _MISC_DONE if now - t < 60]
    if len(_MISC_DONE) >= cfg.get("misc_max_per_minute", 8):
        dlog("misc: limit for this minute reached - leaving pop-ups alone for now")
        return False
    if gray is None:
        gray = gray_screen()
    hit = find(gray, list(items), cfg.get("misc_threshold", cfg.get("threshold", 0.85)), "misc")
    if not hit:
        return False
    action = items[hit[3]]
    if gray is not None:
        H, W = gray.shape[:2]
    else:
        W, H = screen_size()
    x, y = hit[1], hit[2]
    log(f"  game pop-up {os.path.basename(hit[3])} ({hit[0]:.2f}) - {MISC_ACTIONS[action].split(' (')[0]}")
    if action == "beside":
        bx, by = outside_point(hit, (W, H))
        press(bx, by, 120, jitter=4)
    elif action.startswith("swipe-"):
        d = action.split("-", 1)[1]
        dist = int(0.35 * (H if d in ("up", "down") else W))
        dx, dy = {"up": (0, -dist), "down": (0, dist), "left": (-dist, 0), "right": (dist, 0)}[d]
        swipe(x, y, x + dx, y + dy, 250)
    elif action == "back":
        key("KEYCODE_BACK")
    else:
        tap(x, y, jitter=3)
    _MISC_DONE.append(time.time())
    nap(1.2)
    return True


def recover(cfg, base, log, attempts=6):
    """After a failed step: get back to the game screen. Returns True when the
    game marker is visible. Tries close buttons first, BACK only sparingly."""
    pkg = cfg["package"]
    thr = cfg.get("threshold", 0.85)
    close_thr = cfg.get("close_threshold", thr)
    marks = game_markers(cfg, base)
    close = with_common(cfg.get("close_templates", []), base, "close_*.png")
    backs = 0
    for _ in range(attempts):
        fg, act = focus()
        if fg and fg != pkg and fg not in IGNORED_FOREGROUND and not foreign_ad(fg, act):
            return_to_game(pkg, log, cfg.get("max_back_presses", 5))
            continue
        gray = gray_screen()
        if marks and find(gray, marks, thr):
            return True
        if clear_misc(cfg, base, log, gray):
            continue
        hit = find(gray, close, close_thr) if close else None
        if hit:
            log(f"  recover: tapping {os.path.basename(hit[3])} ({hit[0]:.2f})")
            tap(hit[1], hit[2], jitter=2)
            nap(2)
            continue
        ui = ui_close_button() if cfg.get("ui_fallback", True) else None
        if ui:
            log(f"  recover: close via UI tree: {ui[2][:40]!r}")
            tap(ui[0], ui[1], jitter=2)
            nap(2)
            continue
        if backs >= 2:
            break
        backs += 1
        log("  recover: pressing BACK")
        key("KEYCODE_BACK")
        nap(2)
    log("  recover: could not get back to a known game screen")
    return False


DECOYS = {"ui": {}, "close": {}}   # "picture@spot" / label -> number of ads in which tapping it opened the store
LAST_AD_END = {"why": None}        # why the last watch_ad gave up: "timeout" or "store-loop"


def watch_ad(cfg, base, log, home=None):
    """Get one ad closed. Active mode (home=None): returns True/False, and "the ad is over"
    means a game marker image is visible again (or the screen is the game's from before the ad).
    Passive mode (home = set of the game's own activities, from the watch command): the ad is
    over as soon as one of those activities is in front again. Returns "closed" (we closed it),
    "gone" (it went away without us), None (the user left the game) or False (gave up).

    Every look, in this order:
      1. left the game (store / browser)? -> BACK to it, and blame what was tapped last: that
         picture is not used again IN THAT SPOT (remembered across runs after two ads); a
         breather follows before anything risky.
      2. the game is back? -> done.
      3. is the ad still playing? One answer from all the evidence: a progress bar that is still
         growing, a countdown ring ticking in a corner, the picture still changing (a video,
         not just a small animation), a picture that never moved (a playable or an end card is
         waiting, not playing).
      4. skip / "reward granted" buttons (skip_*.png): tapped a couple of seconds in, whatever
         the ad is doing - they are always safe.
      5. pop-ups to tap beside, survey answers.
      6. close pictures, from the earliest close time on, unless a bar or ring says the video is
         still running: near the screen's edges, at a few sizes; a near-miss that sits in the
         same spot look after look is tapped too. Then the phone's own Close/Skip labels.
      7. the store badge (remembered where it was if it has faded since), then BACK, then give up -
         sooner for an ad that has visibly ended and offers nothing to tap."""
    pkg = cfg["package"]
    passive = home is not None
    thr = cfg.get("threshold", 0.85)
    close_thr = cfg.get("close_threshold", thr)
    # Buttons that are safe to tap at any moment (skip, "reward granted"): skip_*.png, plus close
    # pictures named in the profile's skip_templates. Their duplicates leave the close list.
    skip_names = set(cfg.get("skip_templates", []))
    all_close = with_common(cfg.get("close_templates", []), base, "close_*.png")
    skips = unique_files(with_common([], base, "skip_*.png") + [p for p in all_close if os.path.basename(p) in skip_names])
    skip_digests = {_digest(p) for p in skips}
    close = [p for p in all_close if _digest(p) not in skip_digests]
    back_in_game = [] if passive else game_markers(cfg, base)
    has_misc = bool(cfg.get("misc_buttons"))
    game_frame = None
    if not passive:
        game_frame = cfg.get("_game_frame")
        if game_frame is None and GAME_FRAME["frame"] is not None and time.time() - GAME_FRAME["t"] < 30:
            game_frame = GAME_FRAME["frame"]
    # Passive ads are interstitials (no reward to lose), so there's no need to sit through them.
    min_s = cfg.get("passive_min_seconds", 5) if passive else cfg.get("ad_min_seconds", 15)
    max_s = cfg.get("ad_max_seconds", 90)
    hard_max = cfg.get("ad_hard_max_seconds", 240)     # whatever the progress bar says
    bar_close = cfg.get("bar_close_fraction", 0.75)    # active mode: no close taps while a moving bar is below this
    still_need = cfg.get("still_before_close", 3) or 3  # seconds without video motion that mean "it has ended"
    done_min = None if passive else cfg.get("finished_min_seconds")   # earliest close once the ad is seen to have ended
    skip_after = cfg.get("skip_after_seconds", 2)
    no_exit_s = cfg.get("no_exit_seconds", 20)         # an ad that has ended and offers nothing for this long: BACK
    near_looks = cfg.get("near_miss_looks", 3)
    max_trips = cfg.get("max_store_trips", 5)
    poll = cfg.get("poll_seconds", 0.5 if FEED is not None else 1)
    use_ui = cfg.get("ui_fallback", True)
    ui_every = cfg.get("ui_check_seconds", 6)   # the UI-tree dump takes seconds: don't run it every loop
    last_ui, ui_tried = 0.0, {}
    # Ads with no other exit (must visit the store, then BACK, before the X appears):
    stuck = with_common(cfg.get("stuck_tap_templates", []), base, "stuck_*.png")
    stuck_s = cfg.get("stuck_seconds", 30)
    stuck_thr = cfg.get("stuck_threshold", min(thr, 0.8))   # the badge is often caught mid-fade
    press_ms = cfg.get("press_ms", 120)
    max_stuck = cfg.get("max_stuck_taps", 2)
    stuck_taps, last_stuck, badge_seen, badge_near = 0, 0.0, None, {}
    last_badge, badge_now = -1e9, False   # badge_now: press a visible badge right away (after a store trip)
    popups = with_common(cfg.get("popup_templates", []), base, "popup_*.png")
    pop_taps, last_pop, pop_pick = 0, 0.0, 0
    # Things an ad needs tapped to finish (a survey's answer and its Done button): tapped in
    # name order whenever one is on screen, before any close button is considered.
    answers = with_common(cfg.get("answer_templates", []), base, "answer_*.png")
    answer_taps, max_answers = 0, cfg.get("max_answer_taps", 12)
    wake = cfg.get("wake_tap")            # [x, y] fractions; a tap that makes a faded badge reappear
    wake_taps, last_wake, wake_blocked = 0, 0.0, False
    teach_on = bool(cfg.get("_teach"))
    next_teach = cfg.get("teach_after", 45)
    store_trips, hold_until, last_act = 0, 0.0, None
    load_decoys()
    blocked_ui, blocked_spots = set(), set()
    bar_hist, last_moving, said_playing, bar_at, bar_seen_moving = [], 0.0, False, None, False
    ring, said_ring = CountdownRing(), False
    prev_small, looks, video_looks, last_video, said_ended = None, 0, 0, 0.0, False
    near_seen, near_tapped, game_looks = {}, set(), 0
    # Google's "Close Ad? You will lose your reward" box, as a picture (resume_*.png) for when
    # the phone's screen tree can't be read; see also ui_close_button.
    resumes = with_common([], base, "resume_*.png")
    reward_wait = cfg.get("reward_wait_seconds", 30)
    reward_hold = 0.0
    back_presses, fast_backs = 0, 0
    closed_once = False
    LAST_AD_END["why"] = None

    def decoy_spots():
        return blocked_spots | {tuple(k.rsplit("@", 1)) for k, n in DECOYS["close"].items() if n >= 2 and "@" in k}

    def acted(kind, detail="", where=None):
        nonlocal last_act
        last_act = (kind, detail, time.time(), where)

    def blame(kind, detail, where):
        """Something we tapped opened the store: don't tap it again on this ad (there)."""
        nonlocal wake_blocked, pop_pick
        if kind == "wake":
            wake_blocked = True
            log("  the wake press opened the store - not using it again on this ad")
        elif kind == "ui":
            blocked_ui.add(detail)
            DECOYS["ui"][detail] = DECOYS["ui"].get(detail, 0) + 1
            save_decoys()
            log(f"  {detail[:40]!r} is a decoy (it opens the store) - skipping it from now on")
        elif kind in ("close", "skip"):
            name = os.path.basename(detail)
            key = (name, where or "?")
            blocked_spots.add(key)
            DECOYS["close"][f"{name}@{key[1]}"] = DECOYS["close"].get(f"{name}@{key[1]}", 0) + 1
            save_decoys()
            log(f"  close picture {name} opened the store here - not using it in that spot on this ad")
        elif kind == "popup":
            pop_pick += 1                                   # tap a different spot beside it next time

    def finish(result, why=None):
        LAST_AD_END["why"] = why
        if result is not None:
            record_ad(pkg, "gave_up" if result is False else "watched", time.time() - start)
        return result

    def return_from_sheet():
        """Back out of a Play Store sheet shown over the ad: the badge's intended trip."""
        nonlocal badge_now, last_act
        for _ in range(3):
            key("KEYCODE_BACK")
            nap(1.5)
            if not (use_ui and store_sheet_open()):
                break
        near_seen.clear()
        badge_now = False
        last_act = ("returned", "", time.time(), None)

    def keep_reward(x, y, how):
        """The ad warned that closing now loses the reward: RESUME, then leave it alone a while."""
        nonlocal reward_hold, hold_until, min_s
        log(f"  'Close ad? You will lose your reward' - tapping {how} and waiting {reward_wait}s before "
            "trying to close it again")
        tap(x, y, jitter=2)
        acted("resume")
        reward_hold = time.time() + reward_wait
        hold_until = max(hold_until, reward_hold)
        near_seen.clear()
        key_name = "passive_min_seconds" if passive else "ad_min_seconds"
        if cfg.get(key_name, 5 if passive else 15) < reward_wait:
            cfg[key_name] = reward_wait               # this game's ads are reward ads: close them later
            path = cfg.get("_profile_path")
            if path:
                try:
                    with open(path) as f:
                        raw = json.load(f)
                    raw[key_name] = reward_wait
                    save_profile(path, raw)
                    log(f"  this game shows reward ads: from now on its ads aren't closed before {reward_wait}s "
                        f"({key_name} in its profile)")
                except (OSError, ValueError):
                    pass
        nap(2)

    def tap_button(hit, kind, gray, note=""):
        nonlocal closed_once
        H, W = (gray.shape[:2] if gray is not None else screen_size()[::-1])
        log(f"  close button {os.path.basename(hit[3])} ({hit[0]:.2f}){note}")
        tap(hit[1], hit[2], jitter=2)
        acted(kind, hit[3], spot_of(hit[1], hit[2], W, H))
        closed_once = True
        near_seen.clear()
        nap(2)

    start = time.time()
    dlog(f"ad: start ({'passive' if passive else 'active'}) close={len(close)} skip={len(skips)} stuck={len(stuck)} "
         f"popups={len(popups)} markers={len(back_in_game)} game_frame={game_frame is not None} min={min_s}s "
         f"stuck_after={stuck_s}s no_exit={no_exit_s}s max={max_s}s hard_max={hard_max}s poll={poll}s")
    nap(2)

    while True:
        now = time.time()
        elapsed = now - start
        quiet = now - max(start, last_moving)   # time since the ad last showed progress
        dlog(f"ad: {elapsed:.1f}s (quiet {quiet:.1f}s) back={back_presses} badge={stuck_taps} wake={wake_taps} "
             f"trips={store_trips} hold={max(0, hold_until - now):.0f}s closed_once={closed_once}")

        if elapsed >= hard_max:
            log(f"  the ad has run for {elapsed:.0f}s - giving up on it")
            snapshot("hard-timeout", log)
            return finish(False, "timeout")

        # An ad click-through opened the store / a browser: go back to the game.
        fg, act = focus()
        if fg and fg != pkg and foreign_ad(fg, act):
            dlog(f"ad: shown by another app ({fg} / {act.rsplit('.', 1)[-1]}) - part of the ad")
        elif fg and fg != pkg and (fg not in IGNORED_FOREGROUND or app_chooser(fg, act)):
            if passive and is_launcher(fg):
                log("  you went to the home screen - leaving this ad alone")
                return finish(None)
            store_trips += 1
            cause = last_act if last_act and time.time() - last_act[2] < 10 else None
            if time.time() - last_badge < 20:              # the badge was pressed: this is the intended trip
                cause = ("badge", "", last_badge, None)
            how = {"wake": "the wake press", "ui": "a labelled button", "close": "a close picture", "skip": "a skip button",
                   "popup": "the tap beside a pop-up", "badge": "the store badge"}.get(cause[0]) if cause else None
            log(f"  left the game ({fg}){' after ' + how if how else ''}, pressing BACK to return")
            if cause and cause[0] != "badge":
                blame(cause[0], cause[1], cause[3])
            return_to_game(pkg, log, cfg.get("max_back_presses", 5))
            near_seen.clear()
            # Some ads only show their way out after the badge's own trip: after any other trip,
            # a badge that is showing is pressed straight away (no waiting for stuck_seconds).
            badge_now = bool(stuck) and not (cause and cause[0] == "badge")
            closed_once = False  # back on the same ad: what was tapped didn't close it
            min_s = 0  # the ad was clicked through; look for its X right away
            last_act = ("returned", "", time.time(), None)    # (counts as activity for the give-up clock)
            if store_trips >= max_trips:
                log(f"  {store_trips} trips to the store and the ad still isn't done - giving up on it")
                snapshot("store-loop", log)
                return finish(False, "store-loop")
            if not (cause and cause[0] in ("badge", "wake")):   # badge = the way out; wake = a step towards it
                pause_s = min(10 * store_trips, 40)
                hold_until = time.time() + pause_s
                log(f"  letting the ad play for {pause_s}s before trying anything else")
            continue

        if fg != pkg:
            dlog(f"ad: front app is {fg} (not the game)")
        if passive and fg == pkg and act in home:
            nap(1, 0.1)                       # make sure it isn't just a flicker between screens
            p2, a2 = focus()
            if p2 == pkg and a2 in home:
                acted_any = closed_once or back_presses or stuck_taps
                log(f"  ad closed after {elapsed:.0f}s" if acted_any
                    else f"  it went away without any help after {elapsed:.0f}s")
                return finish("closed" if acted_any else "gone")
            continue

        gray = gray_screen()
        H, W = gray.shape[:2] if gray is not None else screen_size()[::-1]
        looks += 1

        # ---- is the ad still playing? ------------------------------------------------------
        frac = progress_bar(LAST_FRAME)
        if frac is not None:
            bar_at = frac
            bar_hist = [(t, f) for t, f in bar_hist if now - t < 8] + [(now, frac)]
            fs = [f for _, f in bar_hist]
            if len(fs) >= 2 and max(fs) - min(fs) >= 0.004 and frac < 0.995:
                last_moving = now
                bar_seen_moving = True
        bar_running = bool(last_moving) and now - last_moving < 4
        ticking = ring.update(gray, now) if gray is not None else False
        if ticking:
            last_moving = now
            if not said_ring:
                log(f"  countdown ring in the corner{' at %d,%d' % ring.where if ring.where else ''} - waiting for it")
                said_ring = True
        if gray is not None:
            prev_small, cells = picture_motion(prev_small, gray)
            if cells is not None:
                dlog(f"ad: {cells} of 24 picture cells changed since the last look")
                if cells >= 4:
                    video_looks += 1
                    last_video = now
        playing = bar_running or ticking
        if playing and not said_playing:
            log("  ad still playing (" + (f"progress bar at {frac:.0%}" if bar_running and frac is not None else
                                          "countdown ring") + ") - holding off until it finishes")
        elif said_playing and not playing:
            dlog("ad: progress bar / countdown stopped")
        said_playing = playing
        # Has it visibly ended? A bar that ran and stopped (or reached the end), a video that
        # played and then held still, or a picture that never moved at all.
        ended = not playing and (
            (bar_seen_moving and (bar_at or 0) >= 0.97) or
            (bar_seen_moving and now - last_moving >= still_need) or
            (video_looks >= 2 and now - last_video >= still_need) or
            (gray is not None and looks >= 3 and video_looks == 0 and not bar_seen_moving))
        if ended and not said_ended:
            dlog("ad: looks finished (" + ("progress bar done" if bar_seen_moving else
                                           "video stopped" if video_looks else "picture never moved") + ")")
            said_ended = True
        risky_ok = not playing and now >= hold_until
        # A rewarded video that is visibly still running: leave its close-looking pictures alone.
        too_early = not passive and ((bar_running and (bar_at or 0) < bar_close) or ticking)
        wait_s = min_s
        if done_min is not None and ended:
            wait_s = min(min_s, done_min)
            if wait_s <= elapsed < min_s:
                dlog(f"ad: looks finished after {elapsed:.0f}s - not waiting out the full {min_s}s")

        # ---- the game is back? ---------------------------------------------------------------
        if back_in_game and elapsed > 4:
            hit = find(gray, back_in_game, thr, "game-marker")
            if hit:
                log(f"  back in game after {elapsed:.0f}s")
                return finish(True)
        if not passive and has_misc and elapsed > 4 and clear_misc(cfg, base, log, gray):
            log(f"  back in game after {elapsed:.0f}s (one of its pop-ups was showing)")
            return finish(True)
        if game_frame is not None and elapsed > 4:
            game_looks = game_looks + 1 if same_screen(gray, game_frame) else 0
            if game_looks >= 2:
                log(f"  back in game after {elapsed:.0f}s (the screen is the game's from before the ad)")
                return finish(True)

        # ---- always-safe buttons: skip, "reward granted" ----------------------------------------
        if skips and elapsed >= skip_after:
            hit, _ = find_button(gray, skips, close_thr, "skip", decoy_spots())
            if hit:
                tap_button(hit, "skip", gray, f" - a skip button, {elapsed:.0f}s in")
                continue

        if resumes:
            hit = find(gray, resumes, close_thr, "lose-reward box")
            if hit:
                keep_reward(hit[1], hit[2], f"{os.path.basename(hit[3])} (Resume)")
                continue

        if popups and pop_taps < 8 and time.time() - last_pop > 2.5:
            pop = find(gray, popups, close_thr, "popup")
            if pop:
                x, y = outside_point(pop, (W, H), cfg.get("dismiss_tap"), pick=pop_pick)
                log(f"  pop-up over the ad ({os.path.basename(pop[3])}) - tapping beside it to dismiss it")
                press(x, y, press_ms, jitter=4)
                acted("popup")
                pop_taps += 1
                last_pop = time.time()
                nap(1.5)
                continue

        if answers and answer_taps < max_answers and not passive:
            ranked = [r for r in score_all(gray, answers) if r[0] >= close_thr] if gray is not None else []
            hit = min(ranked, key=lambda r: os.path.basename(r[3])) if ranked else None
            if hit:
                log(f"  the ad wants an answer - tapping {os.path.basename(hit[3])} ({hit[0]:.2f})")
                tap(hit[1], hit[2], jitter=2)
                acted("answer", hit[3])
                answer_taps += 1
                nap(1.2)
                continue

        # ---- close buttons -------------------------------------------------------------------
        if elapsed < wait_s:
            dlog(f"ad: waiting out the minimum ({wait_s}s) before looking for a close button")
        elif now < reward_hold:
            dlog(f"ad: the ad said closing loses the reward - {reward_hold - now:.0f}s more before closing")
        elif too_early:
            dlog("ad: still playing (progress bar or countdown) - not looking for a close button yet")
        else:
            hit, nears = find_button(gray, close, close_thr, "close", decoy_spots()) if close else (None, [])
            if hit:
                tap_button(hit, "close", gray)
                continue
            # A near-miss in the same spot look after look, on a screen that isn't playing, is the
            # button drawn a little differently - not a look-alike flickering past in a video.
            seen_now = {}
            for n in nears:
                k = (os.path.basename(n[3]), spot_of(n[1], n[2], W, H))
                if k not in seen_now:
                    seen_now[k] = (near_seen.get(k, (0, None))[0] + 1, n)
            near_seen.clear()
            near_seen.update(seen_now)
            sure = [v[1] for k, v in seen_now.items() if v[0] >= near_looks and k not in near_tapped]
            if sure:
                n = max(sure)
                near_tapped.add((os.path.basename(n[3]), spot_of(n[1], n[2], W, H)))   # one try per spot
                tap_button(n, "close", gray, f", in the same spot {near_looks} looks in a row")
                continue
            ui = None
            if use_ui and time.time() - last_ui >= ui_every:
                last_ui = time.time()
                ui = ui_close_button(skip=blocked_ui | {k for k, n in DECOYS["ui"].items() if n >= 2})
                if ui:
                    k = (ui[2], ui[0] // 30, ui[1] // 30)
                    ui_tried[k] = ui_tried.get(k, 0) + 1
                    if ui_tried[k] > 2:                 # tapped twice already and still here: it isn't the way out
                        dlog(f"ad: skipping UI-tree button {ui[2][:40]!r} - tapping it twice didn't help")
                        ui = None
            if ui and len(ui) > 3 and ui[3] == "resume":
                keep_reward(ui[0], ui[1], repr(ui[2]))
                continue
            if ui and len(ui) > 3 and ui[3] == "store":
                log("  a Play Store sheet is open over the ad - pressing BACK to return")
                if time.time() - last_badge >= 20 and last_act and last_act[0] not in ("badge", "returned"):
                    blame(last_act[0], last_act[1], last_act[3])
                return_from_sheet()
                continue
            if ui:
                log(f"  close via UI tree: {ui[2][:50]!r}")
                tap(ui[0], ui[1], jitter=2)
                acted("ui", ui[2])
                closed_once = True
                nap(2)
                continue
            if closed_once and not back_in_game and not passive:
                log(f"  ad dismissed after {elapsed:.0f}s")
                return finish(True)

        if playing or now < hold_until:
            dlog("ad: holding off risky presses (" + ("still playing" if playing else
                 f"breather after a store trip, {hold_until - now:.0f}s left") + ")")

        # ---- the store badge -----------------------------------------------------------------
        # Watched for from the start (every other look, to save time) so that if it fades out
        # before it is needed, adwatch still knows where it was.
        badge_due = stuck and stuck_taps < max_stuck and time.time() - last_stuck > 10 and (
            (risky_ok and quiet >= stuck_s) or (badge_now and badge_seen and time.time() - badge_seen[1] < 3))
        if stuck and elapsed >= 3 and (badge_due or badge_now or looks % 2 == 0):
            seen, bnears = find_button(gray, stuck, stuck_thr, "store-badge", decoy_spots(), second_look=badge_due)
            if not seen:                                    # a faded badge: the same near-miss three looks running
                cur = {}
                for n in bnears:
                    k = (os.path.basename(n[3]), spot_of(n[1], n[2], W, H))
                    cur.setdefault(k, (badge_near.get(k, (0, None))[0] + 1, n))
                badge_near = cur
                seen = next((v[1] for v in cur.values() if v[0] >= near_looks), None)
            if seen:
                if badge_seen is None:
                    dlog(f"ad: store badge {os.path.basename(seen[3])} seen at {seen[1]},{seen[2]} - remembered")
                badge_seen = (seen, time.time())
                if badge_now and stuck_taps < max_stuck and not badge_due:
                    badge_due = True                       # just back from the store and the badge shows: press it
        if badge_due:
            hit = None
            if badge_seen and time.time() - badge_seen[1] < 3:
                hit = badge_seen[0]
            elif wake and not wake_blocked and wake_taps < 15 and time.time() - last_wake > 4:
                fx, fy, moved = safe_point(*wake)
                if moved and not wake_taps:
                    log(f"  (the wake point is in the screen's edge strip, where taps can be swallowed: "
                        f"using {fy:.2f} down instead of {wake[1]:.2f})")
                log("  badge not visible - pressing the wake point to bring it back")
                press(int(fx * W), int(fy * H), press_ms)
                acted("wake")
                wake_taps += 1
                last_wake = time.time()
                # The badge fades in, stays a moment and fades out: look again and again right away.
                look_until = time.time() + 2.5
                while not hit and time.time() < look_until:
                    hit, _ = find_button(gray_screen(), stuck, stuck_thr, "store-badge (after wake)", decoy_spots())
                    if not hit:
                        nap(0.05, 0)
                if not hit and badge_seen:
                    hit = badge_seen[0]
                    log("  the badge didn't come back into view - pressing where it was")
                if not hit:
                    log("  the badge didn't show up after the wake press")
                    continue
            elif badge_seen and time.time() - badge_seen[1] < 180:
                hit = badge_seen[0]
                log(f"  the store badge has faded - pressing where it was ({hit[1]},{hit[2]})")
            if hit:
                log(f"  no close button after {elapsed:.0f}s - pressing {os.path.basename(hit[3])} "
                    f"({hit[0]:.2f}); will come back with BACK")
                press(hit[1], hit[2], press_ms, jitter=2)
                acted("badge", hit[3])
                stuck_taps += 1
                last_stuck = time.time()
                last_badge = time.time()
                badge_now = False
                if left_game_within(pkg, 5):
                    continue                                   # the store opened: handled at the top
                if use_ui and store_sheet_open():
                    # The store opened as a sheet over the ad (Android still says the ad is in front).
                    log("  the store opened over the ad - pressing BACK to return")
                    return_from_sheet()
                    continue
                again, _ = find_button(gray_screen(), [hit[3]], stuck_thr, "store-badge (still there?)", decoy_spots())
                fg2, _a = focus()
                if again and fg2 == pkg:
                    log("  the press didn't open the store - pressing again, dead centre and a bit longer")
                    press(again[1], again[2], press_ms + 100, jitter=0)
                    acted("badge", hit[3])
                    last_badge = time.time()
                continue

        if teach_on and quiet >= next_teach:
            log(f"  no known button after {elapsed:.0f}s - pausing so you can teach it")
            action = teach(cfg, base, log)
            all_close = with_common(cfg.get("close_templates", []), base, "close_*.png")
            skips = unique_files(with_common([], base, "skip_*.png") + [p for p in all_close if os.path.basename(p) in skip_names])
            skip_digests = {_digest(p) for p in skips}
            close = [p for p in all_close if _digest(p) not in skip_digests]
            stuck = with_common(cfg.get("stuck_tap_templates", []), base, "stuck_*.png")
            popups = with_common(cfg.get("popup_templates", []), base, "popup_*.png")
            wake = cfg.get("wake_tap")
            resume_at = max(min_s, stuck_s)          # pick up in the "looking for a button" phase
            start = time.time() - resume_at
            last_moving = 0.0
            next_teach = resume_at + cfg.get("teach_again", 25)
            if action == "skip":
                key("KEYCODE_BACK")
                nap(2)
            continue

        # ---- nothing to tap: BACK, then give up ------------------------------------------------
        # An ad that has visibly ended and offers nothing for no_exit_seconds gets BACK straight
        # away (twice, then it is given up on - the ad loop then restarts the game); one that
        # gives no sign either way waits until ad_max_seconds without progress.
        last_try = max(start + wait_s, last_moving, last_act[2] if last_act and last_act[0] != "back" else 0.0)
        badge_pending = stuck and badge_seen and stuck_taps == 0
        fast = ended and now - last_try >= no_exit_s and not badge_pending
        if risky_ok and (quiet >= max_s or fast):
            if back_presses >= (2 if fast and quiet < max_s else 3):
                log("  ad wouldn't close - giving up on this one")
                snapshot("gave-up", log)
                return finish(False, "timeout")
            if back_presses == 0:
                snapshot("no-close-button", log)   # the ad as it looked with no known close button
            log("  timeout, pressing BACK" if not fast or quiet >= max_s else
                f"  the ad has ended and shows nothing to tap ({now - last_try:.0f}s) - pressing BACK")
            key("KEYCODE_BACK")
            acted("back")
            back_presses += 1
            nap(3)
            continue

        watching_badge = stuck and quiet >= stuck_s and stuck_taps < max_stuck and risky_ok
        nap(0.5 if watching_badge else poll, 0.1)   # the badge only flashes up briefly


def run_step(step, cfg, base, log):
    thr = step.get("threshold", cfg.get("threshold", 0.85))

    if step.get("watch_ad"):
        return watch_ad(cfg, base, log)

    if "tap" in step:
        paths = expand(step["tap"], base)
        # The first tap step (the ad button) lives in a pop-up: if it isn't showing, tap the
        # profile's opener button ("open_templates") to bring the pop-up up. Set "open": false
        # on a step to turn this off.
        first_tap = next((st for st in cfg["steps"] if "tap" in st), None)
        open_paths = []
        if step.get("open", step is first_tap):
            open_paths = expand(cfg.get("open_templates", []), base, quiet=True)
        open_taps, t0 = 0, time.time()
        deadline = time.time() + step.get("timeout", 10)
        best, last = 0.0, None
        while time.time() < deadline:
            last = gray_screen()
            ranked = score_all(last, paths, stop_at=max(thr, 0.92))
            dlog(f"step {os.path.basename(str(step['tap']))}: best "
                 + (f"{os.path.basename(ranked[0][3])} {ranked[0][0]:.2f}" if ranked else "none") + f" (need {thr:.2f})")
            now_best = ranked[0][0] if ranked else 0.0
            best = max(best, now_best)
            if ranked and now_best >= thr:
                hit = ranked[0]
                log(f"  tap {os.path.basename(hit[3])} ({hit[0]:.2f})")
                GAME_FRAME.update(frame=last, t=time.time())   # the game, for spotting its return after an ad
                tap(hit[1], hit[2])
                nap(step.get("after", 1.5))
                return True
            # A game pop-up (level up, daily reward...) covering the button: deal with it first.
            if now_best < thr and time.time() - t0 > 1.0 and clear_misc(cfg, base, log, last):
                deadline = max(deadline, time.time() + 6)
                continue
            # Only open when the target is clearly absent (a near-miss means the pop-up
            # is already open, and tapping the opener again could close it).
            if open_paths and open_taps < 2 and now_best < 0.6 and time.time() - t0 > 1.5:
                oh = find(last, open_paths, thr)
                if oh:
                    log(f"  boost not showing - tapping opener {os.path.basename(oh[3])} ({oh[0]:.2f})")
                    tap(oh[1], oh[2])
                    open_taps += 1
                    deadline = max(deadline, time.time() + 6)
                    nap(2.0)
                    continue
            nap(0.5, 0.1)
        if not step.get("optional"):
            log(f"  didn't find {step['tap']} (best score {best:.2f}, need {thr:.2f})")
            if last is not None:
                dbg = os.path.join(DATA_DIR, "debug")
                os.makedirs(dbg, exist_ok=True)
                cv2.imwrite(os.path.join(dbg, "last_not_found.png"), last)
                cv2.imwrite(os.path.join(dbg, time.strftime("%Y%m%d-%H%M%S") + "_not-found.png"), last)
                log(f"  saved what the phone showed to debug/last_not_found.png")
        return bool(step.get("optional"))

    if "tap_xy" in step:
        x, y = step["tap_xy"]
        if isinstance(x, float) and isinstance(y, float) and x <= 1 and y <= 1:
            sw, sh = screen_size()   # fractions of the screen work on any phone
            x, y = int(x * sw), int(y * sh)
        tap(x, y)
        nap(step.get("after", 1.5))
        return True

    if "key" in step:
        key(step["key"])
        nap(step.get("after", 1))
        return True

    if "wait" in step:
        nap(step["wait"], 0.1)
        return True

    raise ValueError(f"unknown step: {step}")


# ------------------------------------------------------------------ keep the computer awake
KEEP_AWAKE = None    # None = the saved choice (on unless switched off); True/False = --keep-awake / --no-keep-awake


def keep_awake_wanted():
    if KEEP_AWAKE is not None:
        return KEEP_AWAKE
    if os.environ.get("ADWATCH_KEEP_AWAKE") in ("0", "1"):
        return os.environ["ADWATCH_KEEP_AWAKE"] == "1"
    return bool(load_state().get("keep_awake", True))


class KeepAwake:
    """Stops the computer from going to sleep while adwatch runs (the screen may still turn off).
    Linux: a systemd-inhibit lock (sleep + idle) held by a small helper that ends by itself the
    moment adwatch does, even after a crash. macOS: caffeinate. Windows: SetThreadExecutionState."""

    def __init__(self, why):
        self.why, self.proc, self.win, self.how = why, None, False, None

    def start(self):
        import shutil
        me = str(os.getpid())
        try:
            if sys.platform.startswith("linux"):
                inhibit = shutil.which("systemd-inhibit")
                if not inhibit:
                    return "systemd-inhibit isn't available (not a systemd system?)"
                hold = ["tail", f"--pid={me}", "-f", "/dev/null"] if shutil.which("tail") else ["sleep", "infinity"]
                self.proc = subprocess.Popen([inhibit, "--what=sleep:idle", "--who=adwatch", f"--why={self.why}",
                                              "--mode=block", *hold],
                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self.how = "systemd-inhibit"
            elif sys.platform == "darwin":
                if not shutil.which("caffeinate"):
                    return "caffeinate isn't available"
                self.proc = subprocess.Popen(["caffeinate", "-i", "-w", me], stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self.how = "caffeinate"
            elif sys.platform == "win32":
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                if not ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                    return "Windows refused the request"
                self.win, self.how = True, "Windows power request"
                return None
            else:
                return f"not supported on {sys.platform}"
        except OSError as e:
            return str(e)
        try:                                     # refused straight away (e.g. no permission)?
            self.proc.wait(timeout=0.5)
            err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()
            self.proc = None
            return err or "the helper stopped straight away"
        except subprocess.TimeoutExpired:
            return None

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        if self.win:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:
                pass
            self.win = False


def keeps_awake(kind):
    """Decorator for run / watch / keepalive: hold a keep-awake lock for as long as they run."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(args):
            if not keep_awake_wanted() or os.environ.get("ADWATCH_AWAKE_HELD") == "1":
                return fn(args)
            game = os.path.splitext(os.path.basename(str(getattr(args, "profile", "") or "")))[0]
            lock = KeepAwake(f"adwatch {kind}" + (f" ({game})" if game else ""))
            problem = lock.start()
            if problem:
                print(time.strftime("%H:%M:%S"), f"couldn't keep the computer awake: {problem}", flush=True)
                dlog(f"keep awake: failed - {problem}")
            else:
                print(time.strftime("%H:%M:%S"), f"keeping this computer awake while adwatch runs ({lock.how}; "
                      "turn off in Settings or with --no-keep-awake)", flush=True)
                dlog(f"keep awake: on ({lock.how})")
            try:
                return fn(args)
            finally:
                lock.stop()
                if not problem:
                    dlog("keep awake: released")
        return wrapper
    return deco


def session_header(cfg, kind):
    """One block in the debug log describing the setup, so a log can be read on its own."""
    try:
        props = shell("getprop ro.product.model; getprop ro.build.version.release; wm size; wm density").split()
    except (Exception, SystemExit):         # logging must never stop a run
        props = []
    dlog(f"SESSION {kind}: adwatch {__version__}, python {sys.version.split()[0]}, opencv {cv2.__version__}, "
         f"{sys.platform}; adb {ADB}; phone {SERIAL or '(default)'} {' '.join(props)}; "
         f"screen source {'feed ' + FEED.device if FEED is not None else 'adb screencap'}")
    keep = {k: v for k, v in cfg.items() if not k.startswith("_") and k not in ("steps",)}
    dlog(f"SESSION profile {os.path.basename(cfg.get('_profile_path', '?'))}: {json.dumps(keep)[:900]}")
    dlog(f"SESSION steps: {json.dumps(cfg.get('steps', []))[:600]}")


@keeps_awake("ad loop")
def cmd_run(args):
    args.profile = resolve_profile(args.profile)
    with open(args.profile) as f:
        cfg = json.load(f)
    base = os.path.dirname(os.path.abspath(args.profile))
    cfg["_profile_path"] = os.path.abspath(args.profile)
    cfg["_teach"] = bool(args.teach) and sys.stdin.isatty()
    if args.teach and not cfg["_teach"]:
        print("--teach needs an interactive terminal; ignoring it")
    cycles = args.cycles or cfg.get("cycles", 10)
    max_fail = cfg.get("max_consecutive_failures", 3)
    cooldown = cfg.get("cooldown_seconds", 5)

    log = make_log()
    session_header(cfg, "run")

    if foreground() != cfg["package"]:
        log(f"launching {cfg['package']}")
        launch(cfg["package"])
        nap(8)

    ok = fails = restarts = 0
    stopped = False
    restart_ok = cfg.get("restart_game_when_stuck", True)
    max_restarts = cfg.get("max_restarts", 5)
    for i in range(1, cycles + 1):
        obey_control(log)
        LAST_AD_END["why"] = None
        log(f"cycle {i}/{cycles}")
        clear_misc(cfg, base, log)
        success = True
        for step in cfg["steps"]:
            if not run_step(step, cfg, base, log):
                success = False
                break
        if success:
            ok += 1
            fails = 0
        else:
            fails += 1
            stuck_ad = LAST_AD_END["why"] in ("timeout", "store-loop")
            if stuck_ad or not recover(cfg, base, log):        # dismiss whatever we're stuck on
                if restart_ok and restarts < max_restarts:
                    restarts += 1
                    restart_game(cfg["package"], log, cfg.get("restart_wait_seconds", 15))
            if fails >= max_fail:
                log(f"{fails} failures in a row - stopping")
                snapshot("stopped", log)
                stopped = True
                break
        nap(cooldown)
    log(f"done: {ok} successful cycle(s); {game_total(cfg['package'])} ads watched in this game so far")
    if stopped:
        sys.exit(2)


# ------------------------------------------------------------------ stats
def _game_names():
    """package -> the name(s) its profiles go by, e.g. 'idleminer'."""
    names = {}
    for prof in glob.glob(os.path.join(DATA_DIR, "profiles", "*.json")):
        try:
            with open(prof) as f:
                pkg = json.load(f).get("package")
        except (OSError, ValueError):
            continue
        if pkg:
            names.setdefault(pkg, []).append(os.path.splitext(os.path.basename(prof))[0])
    return names


def _duration(seconds):
    m = int(seconds) // 60
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def game_total(pkg):
    """All-time count of ads watched in this game (for the end-of-run line)."""
    return (_read_json(STATS_FILE, {}).get("games", {}).get(pkg) or {}).get("watched", 0)


def stats_text():
    stats = _read_json(STATS_FILE, {})
    total = stats.get("total") or {}
    if not total:
        return "No ads counted yet. Every ad adwatch gets through is counted from now on, per game and per phone."
    today = time.strftime("%Y-%m-%d")
    names = _game_names()

    def row(label, box):
        w, g = box.get("watched", 0), box.get("gave_up", 0)
        avg = f", about {box.get('seconds', 0) / max(1, w + g):.0f}s each" if w + g else ""
        return (f"  {label:<34} {w:>6} watched   {box.get('days', {}).get(today, 0):>4} today"
                + (f"   {g} given up" if g else "") + avg)

    out = [f"Ads since {stats.get('since', '?')}  (time spent on ads: {_duration(total.get('seconds', 0))})", "",
           row("All games, all phones", total), "", " By game:"]
    for pkg, box in sorted((stats.get("games") or {}).items(), key=lambda kv: -kv[1].get("watched", 0)):
        label = ", ".join(names.get(pkg, [])) or pkg
        out.append(row(label[:34], box))
    out += ["", " By phone:"]
    for phone, box in sorted((stats.get("phones") or {}).items(), key=lambda kv: -kv[1].get("watched", 0)):
        out.append(row(phone[:34], box))
    return "\n".join(out)


def cmd_stats(args=None):
    print(stats_text())


# ------------------------------------------------------------------ tidy
def cmd_tidy(args):
    """Housekeeping: duplicate pictures in the shared pool, template folders no game uses,
    old debug screenshots. Lists everything first; deletes only after you say yes (or --yes)."""
    import shutil
    doomed = []                                         # (path, why)
    # 1. the same picture saved twice in templates/common (keep your own name over a starter copy)
    by_digest = {}
    # (a skip_ copy of a close_ picture wins: it was re-marked as safe to tap at any time)
    pics = sorted(glob.glob(os.path.join(COMMON_DIR, "*.png")),
                  key=lambda p: (not os.path.basename(p).startswith("skip_"), "_pack_" in p, p))
    for p in pics:
        by_digest.setdefault(_digest(p), []).append(p)
    for group in by_digest.values():
        for dup in group[1:]:
            doomed.append((dup, f"same picture as {os.path.basename(group[0])}"))
    # 2. template folders that no game profile mentions
    used = set()
    for prof in glob.glob(os.path.join(DATA_DIR, "profiles", "*.json")):
        used |= set(re.findall(r"templates/([^/\"]+)/", open(prof).read()))
    for d in sorted(glob.glob(os.path.join(DATA_DIR, "templates", "*"))):
        name = os.path.basename(d)
        if os.path.isdir(d) and name != "common" and name not in used:
            doomed.append((d, "folder not used by any game"))
    # 3. old debug screenshots (keep the newest 20)
    shots = sorted(glob.glob(os.path.join(DATA_DIR, "debug", "*.png")))
    for p in shots[:-20]:
        doomed.append((p, "old debug screenshot"))
    if not doomed:
        print("nothing to tidy")
        return
    print("Would remove:")
    for p, why in doomed:
        print(f"  {os.path.relpath(p, DATA_DIR)}   ({why})")
    if not args.yes:
        if not sys.stdin.isatty():
            print("run again with --yes to remove them")
            return
        if input(f"Remove these {len(doomed)} item(s)? (y/N) ").strip().lower() not in ("y", "yes"):
            print("left everything as it was")
            return
    for p, _ in doomed:
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            for f in (p, p + ".json"):
                if os.path.exists(f):
                    os.remove(f)
    print(f"removed {len(doomed)} item(s)")


# ------------------------------------------------------------------ keepalive
@keeps_awake("keepalive")
def cmd_keepalive(args):
    """Run `run`/`watch` unattended: restart it after crashes, and after it stops on
    problems wait for control/restart (dropped by whoever fixed it) and start again.
    Every start loads the code fresh, so fixes made in the meantime take effect."""
    rest = list(args.rest)
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest or rest[0] not in ("run", "watch"):
        sys.exit("usage: adwatch keepalive run GAME [-n N]   (or: keepalive watch GAME ...)")
    log = make_log()
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [os.path.dirname(PKG_DIR), os.environ.get("PYTHONPATH")])))
    if SERIAL:
        env["ADWATCH_SERIAL"] = SERIAL
    if args.feed:
        env["ADWATCH_FEED"] = args.feed
    if VERBOSE:
        env["ADWATCH_VERBOSE"] = "1"
    env["ADWATCH_SCREEN"] = SCREEN_METHOD
    if not FAST_INPUT:
        env["ADWATCH_FAST_INPUT"] = "0"
    if keep_awake_wanted():
        env["ADWATCH_AWAKE_HELD"] = "1"               # this keepalive holds the lock for them
    os.makedirs(CONTROL_DIR, exist_ok=True)
    control_signal()                                   # ignore stale signals from earlier runs
    crashes = 0
    log(f"[keepalive] starting: adwatch {' '.join(rest)}")
    while True:
        code = subprocess.run([sys.executable, "-m", "adwatch", *rest], env=env, stdin=subprocess.DEVNULL).returncode
        if code == 0:
            log("[keepalive] finished")
            return
        if code == RESTART_CODE:
            continue
        if code in (-2, 130):                          # Ctrl+C
            log("[keepalive] stopped by Ctrl+C")
            return
        if code != 2 and crashes < 3:                  # a crash (adb hiccup etc.): try again a few times
            crashes += 1
            log(f"[keepalive] exited with code {code}; trying again in 60s ({crashes}/3)")
            time.sleep(60)
            continue
        log(f"[keepalive] stopped (code {code}). Waiting up to {args.wait_hours:g}h for control/restart "
            "(drop an empty file called 'restart' into the control folder), or 'stop' to quit.")
        end = time.time() + args.wait_hours * 3600
        sig = None
        while time.time() < end and not sig:
            time.sleep(10)
            sig = control_signal()
        if sig != "restart":
            log("[keepalive] " + ("stop requested" if sig == "stop" else "no restart signal - giving up"))
            return
        crashes = 0
        log("[keepalive] restart signal received - starting again")


# ------------------------------------------------------------------ passive mode
def learn_home(cfg, log, force=False):
    """The game's own normal activities. Learned once and stored in the profile."""
    pkg = cfg["package"]
    home = set(cfg.get("home_activities", []))
    if home and not force:
        return home
    if sys.stdin.isatty():
        print("Watch mode needs to know what the game's normal screen is (one-time setup).")
        input("Put the game on its normal screen, with no ad showing, then press Enter... ")
    p, a = focus()
    if p != pkg:
        sys.exit(f"the game isn't in front (saw {p or 'nothing'}). Open it on its normal screen and run this again.")
    if is_ad_activity(a, cfg.get("ad_activities", [])):
        sys.exit(f"that looks like an ad screen ({a}). Close the ad and run this again.")
    home.add(a)
    path = cfg.get("_profile_path")
    if path:
        with open(path) as f:
            raw = json.load(f)
        raw["home_activities"] = sorted(home)
        save_profile(path, raw)
    cfg["home_activities"] = sorted(home)
    log(f"learned the game's normal screen: {a}")
    return home


@keeps_awake("watching for ads")
def cmd_watch(args):
    """Passive mode: do nothing until an ad takes over the screen, then close it."""
    args.profile = resolve_profile(args.profile)
    with open(args.profile) as f:
        cfg = json.load(f)
    base = os.path.dirname(os.path.abspath(args.profile))
    cfg["_profile_path"] = os.path.abspath(args.profile)
    cfg["_teach"] = bool(args.teach) and sys.stdin.isatty()
    if args.teach and not cfg["_teach"]:
        print("--teach needs an interactive terminal; ignoring it")
    pkg = cfg["package"]
    extra = cfg.get("ad_activities", [])
    unknown_is_ad = cfg.get("unknown_activity_is_ad", True)
    idle = cfg.get("watch_poll_seconds", 1.5)
    thr = cfg.get("threshold", 0.85)
    close_thr = cfg.get("close_threshold", thr)

    log = make_log()
    session_header(cfg, "watch")

    home = learn_home(cfg, log, force=args.learn)
    close = with_common(cfg.get("close_templates", []), base, "close_*.png")
    log(f"watching {pkg} - play as normal, Ctrl+C to stop"
        + (" (also scanning the game screen for ad close buttons)" if args.scan else ""))

    misc_every = cfg.get("misc_check_seconds", 5)
    last_misc = 0.0
    if cfg.get("misc_buttons"):
        log(f"also dealing with the game's own pop-ups ({len(misc_buttons(cfg, base))} saved), every {misc_every}s")
    seen = closed = 0
    armed, away, last_hit, noted = True, False, None, None
    game_seen = time.time()
    deadline = time.time() + args.minutes * 60 if args.minutes else None
    try:
        while True:
            obey_control(log)
            if deadline and time.time() > deadline:
                log("time is up")
                break
            if args.count and closed >= args.count:
                break
            p, a = focus()
            if p == pkg:
                game_seen = time.time()

            if p != pkg and foreign_ad(p, a) and time.time() - game_seen < 15:
                # e.g. Meta's ads, shown by the Facebook app on top of the game
                seen += 1
                log(f"ad #{seen} detected ({a.rsplit('.', 1)[-1]}, shown by {p})")
                away = False
                res = watch_ad(cfg, base, log, home=home)
                close = with_common(cfg.get("close_templates", []), base, "close_*.png")
                if res == "closed":
                    closed += 1
                elif res != "gone":
                    armed = False
                    if res is False:
                        log("  couldn't close this one - close it by hand; watching resumes when the game is back")
                game_seen = time.time()
                nap(1)
                continue

            if p != pkg:                       # another app, home screen, phone asleep...
                if not away:
                    log("game not in front - waiting (not touching anything)")
                    away = True
                nap(idle * 2, 0.1)
                continue
            if away:
                log("game is back - watching")
                away = False

            if a in home:                      # the game's own screen
                armed = True
                frame = None
                if cfg.get("misc_buttons") and time.time() - last_misc >= misc_every:
                    last_misc = time.time()
                    frame = gray_screen()
                    if clear_misc(cfg, base, log, frame):
                        continue
                if args.scan and close:        # ads drawn inside the game's own screen
                    hit = find(frame if frame is not None else gray_screen(), close, close_thr)
                    same = (hit and last_hit and hit[3] == last_hit[3]
                            and abs(hit[1] - last_hit[1]) < 25 and abs(hit[2] - last_hit[2]) < 25)
                    if same:                   # seen twice in the same place: not a passing frame
                        seen += 1
                        closed += 1
                        log(f"ad close button on the game screen: {os.path.basename(hit[3])} "
                            f"({hit[0]:.2f}) - tapping")
                        tap(hit[1], hit[2], jitter=2)
                        last_hit = None
                        nap(2)
                        continue
                    last_hit = hit
                nap(idle, 0.1)
                continue

            # A different screen inside the game: an ad (or something we were told to leave).
            known = is_ad_activity(a, extra)
            if not armed or not (known or unknown_is_ad):
                if noted != a and armed:
                    log(f"unfamiliar screen ({a}) - leaving it alone")
                    noted = a
                nap(idle, 0.1)
                continue
            seen += 1
            log(f"ad #{seen} detected ({a.rsplit('.', 1)[-1]}{'' if known else ', unrecognised screen'})")
            res = watch_ad(cfg, base, log, home=home)
            close = with_common(cfg.get("close_templates", []), base, "close_*.png")  # --teach may add some
            if res == "closed":
                closed += 1
            elif res != "gone":                # gave up, or the user left: don't keep poking it
                armed = False
                if res is False:
                    log("  couldn't close this one - close it by hand; watching resumes when the game is back"
                        + ("" if cfg["_teach"] else " (run with --teach to save its button)"))
            nap(1)
    finally:
        log(f"saw {seen} ad(s), closed {closed}; {game_total(cfg['package'])} ads in this game so far")


# ------------------------------------------------------------ helper commands
def cmd_test(args):
    args.profile = resolve_profile(args.profile)
    with open(args.profile) as f:
        cfg = json.load(f)
    base = os.path.dirname(os.path.abspath(args.profile))
    thr = cfg.get("threshold", 0.85)
    paths = []
    for step in cfg["steps"]:
        if "tap" in step:
            paths += expand(step["tap"], base)
    paths += with_common(cfg.get("close_templates", []), base, "close_*.png")
    paths += with_common(cfg.get("stuck_tap_templates", []), base, "stuck_*.png")
    paths += expand(cfg.get("back_in_game_templates", []), base, quiet=True)
    paths += expand(cfg.get("open_templates", []), base, quiet=True)
    g = gray_screen()
    print(f"foreground: {foreground()}   screen: {g.shape[1]}x{g.shape[0]}")
    for score, x, y, p in score_all(g, sorted(set(paths)), thorough=True):
        mark = "MATCH" if score >= thr else "     "
        print(f"{mark} {score:.3f}  ({x},{y})  {os.path.relpath(p, base)}")


def cmd_snap(args):
    out = args.out or time.strftime("snap_%Y%m%d_%H%M%S.png")
    cv2.imwrite(out, screenshot())
    print(f"saved {out}")


def cmd_crop(args):
    img = cv2.imread(args.src)
    if img is None:
        sys.exit(f"can't read {args.src}")
    crop = img[args.y:args.y + args.h, args.x:args.x + args.w]
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    cv2.imwrite(args.out, crop)
    write_meta(args.out, img)
    print(f"saved {args.out} ({args.w}x{args.h})")


# ------------------------------------------- guided setup (new / grab / step)
def profile_path(name):
    return os.path.join(DATA_DIR, "profiles", name + ".json")


def resolve_profile(arg):
    """Commands take a game's name (`adwatch run idleminer`) or the path to its profile file."""
    if os.path.isfile(arg):
        return os.path.abspath(arg)
    name = arg[:-5] if arg.endswith(".json") else arg
    if os.path.isfile(profile_path(os.path.basename(name))):
        return profile_path(os.path.basename(name))
    have = sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in glob.glob(os.path.join(DATA_DIR, "profiles", "*.json")))
    sys.exit(f"no game called '{arg}'. " + (f"Your games: {', '.join(have)}" if have
             else "Add one with `adwatch new NAME`, or run `adwatch` for the menu."))


def load_profile(name):
    p = profile_path(name)
    if not os.path.exists(p):
        sys.exit(f"no profile '{name}' - create it with: adwatch.py new {name}")
    with open(p) as f:
        return p, json.load(f)


def save_profile(p, cfg):
    with open(p, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def cmd_new(args):
    p = profile_path(args.name)
    if os.path.exists(p):
        sys.exit(f"{p} already exists")
    pkg = args.package
    if not pkg:
        quiet = getattr(args, "quiet", False)
        input(" Open the game on the phone, then press Enter... " if quiet
              else "Open the game on your phone, then press Enter to detect its package... ")
        pkg = foreground()
        if not pkg or pkg in IGNORED_FOREGROUND or "launcher" in pkg:
            sys.exit("Couldn't tell which game is open. Make sure it's on the phone's screen and try again."
                     if quiet else "couldn't detect the game (is it in the foreground?). Pass the package explicitly.")
        print(f" found it: {pkg}" if quiet else f"detected package: {pkg}")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    cfg = {
        "package": pkg, "cycles": 10, "threshold": 0.85,
        "ad_min_seconds": 15, "ad_max_seconds": 90, "poll_seconds": 2,
        "cooldown_seconds": 6, "max_consecutive_failures": 3, "ui_fallback": True,
        "steps": [],
        "close_templates": ["../templates/common/close_*.png"],
        "back_in_game_templates": [f"../templates/{args.name}/in_game_*.png"],
    }
    save_profile(p, cfg)
    if getattr(args, "quiet", False):
        return
    print(f"created {p}\nNext, in order, e.g.:\n"
          f"  adwatch.py grab {args.name} boost --then-ad      (button that starts the ad)\n"
          f"  adwatch.py grab {args.name} collect --step       (optional reward button)\n"
          f"  adwatch.py grab {args.name} ingame --ingame      (something only visible in-game)\n"
          f"  adwatch.py grab {args.name} x --close            (while an ad is showing)")


def select_box_tk(img, title):
    """Tkinter picker: big window, drag a box, Enter/button = save, Esc/C = cancel."""
    import base64
    import tkinter as tk

    h, w = img.shape[:2]
    root = tk.Tk()
    root.title(title)
    view_h = int(os.environ.get("ADWATCH_VIEW_HEIGHT", 0)) or int(root.winfo_screenheight() * 0.82)
    scale = view_h / h
    view = cv2.resize(img, None, fx=scale, fy=scale,
                      interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    vh, vw = view.shape[:2]
    photo = tk.PhotoImage(data=base64.b64encode(cv2.imencode(".png", view)[1].tobytes()))

    bar = tk.Frame(root)
    bar.pack(side="bottom", fill="x")
    canvas = tk.Canvas(root, width=vw, height=vh, highlightthickness=0, cursor="crosshair")
    canvas.pack(side="top")
    canvas.create_image(0, 0, anchor="nw", image=photo)

    st = {"start": None, "rect": None, "box": None, "result": None}
    info = tk.StringVar(value="Drag a box around the button")

    def clamp(v, hi):
        return max(0, min(hi, v))

    def press(e):
        st["start"] = (clamp(e.x, vw), clamp(e.y, vh))
        if st["rect"]:
            canvas.delete(st["rect"])
        st["rect"] = canvas.create_rectangle(*st["start"], *st["start"], outline="#ff2d55", width=2)

    def drag(e):
        if not st["start"]:
            return
        x0, y0 = st["start"]
        x1, y1 = clamp(e.x, vw), clamp(e.y, vh)
        canvas.coords(st["rect"], x0, y0, x1, y1)
        st["box"] = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        info.set(f"{int((st['box'][2]-st['box'][0])/scale)} x {int((st['box'][3]-st['box'][1])/scale)} px")

    def accept(_e=None):
        b = st["box"]
        if b and b[2] - b[0] > 3 and b[3] - b[1] > 3:
            x0, y0, x1, y1 = (int(v / scale) for v in b)
            st["result"] = img[y0:y1, x0:x1]
            root.destroy()
        else:
            info.set("Drag a box first")

    def cancel(_e=None):
        root.destroy()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    for k in ("<Return>", "<KP_Enter>", "<space>"):
        root.bind(k, accept)
    for k in ("<Escape>", "<c>", "<C>"):
        root.bind(k, cancel)
    tk.Button(bar, text="Save (Enter)", command=accept).pack(side="left", padx=6, pady=4)
    tk.Button(bar, text="Cancel (Esc / C)", command=cancel).pack(side="left", pady=4)
    tk.Label(bar, textvariable=info).pack(side="left", padx=10)
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()
    return st["result"]


def select_box_cv(img, title):
    """Fallback if tkinter isn't installed (OpenCV window; may open small)."""
    h, w = img.shape[:2]
    scale = int(os.environ.get("ADWATCH_VIEW_HEIGHT", "1000")) / h
    view = cv2.resize(img, None, fx=scale, fy=scale)
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(title, view.shape[1], view.shape[0])
    x, y, bw, bh = cv2.selectROI(title, view, showCrosshair=False)
    cv2.destroyAllWindows()
    if bw == 0 or bh == 0:
        return None
    return img[int(y / scale):int((y + bh) / scale), int(x / scale):int((x + bw) / scale)]


def select_box(img, title):
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("tkinter not installed - using OpenCV window (install python3-tk for a better one)")
        return select_box_cv(img, title)
    return select_box_tk(img, title)


def write_meta(path, shot):
    with open(path + ".json", "w") as f:
        json.dump({"width": int(shot.shape[1]), "height": int(shot.shape[0])}, f)


def cmd_stamp(args):
    """Record the source screen size for templates made before sizes were tracked."""
    n = 0
    for p in glob.glob(os.path.join(DATA_DIR, "templates", "**", "*.png"), recursive=True):
        if not os.path.exists(p + ".json"):
            with open(p + ".json", "w") as f:
                json.dump({"width": args.width, "height": args.height}, f)
            n += 1
    print(f"stamped {n} template(s) as captured on a {args.width}x{args.height} screen")


def cmd_grab(args):
    p, cfg = load_profile(args.name)
    if args.delay:
        print(f"screenshot in {args.delay}s - get the screen ready...")
        time.sleep(args.delay)
    shot = screenshot()
    img = select_box(shot, f"{args.name}: {args.label}")
    if img is None:
        sys.exit("cancelled")

    root = os.path.dirname(os.path.dirname(p))
    if args.close or args.stuck or getattr(args, "popup", False) or getattr(args, "skip", False):
        kind = ("close" if args.close else "stuck" if args.stuck else "popup" if getattr(args, "popup", False)
                else "skip")
        d = os.path.join(root, "templates", "common")
        n = 1
        while os.path.exists(os.path.join(d, f"{kind}_{n}.png")):
            n += 1
        out = os.path.join(d, f"{kind}_{n}.png")
        rel = None
    else:
        d = os.path.join(root, "templates", args.name)
        prefix = ("in_game_" if args.ingame else "open_" if args.open else
                  "misc_" if getattr(args, "misc", False) else "")
        out = os.path.join(d, f"{prefix}{args.label}.png")
        rel = f"../templates/{args.name}/{prefix}{args.label}.png"
    os.makedirs(d, exist_ok=True)
    cv2.imwrite(out, img)
    write_meta(out, shot)
    print(f"saved {out} ({img.shape[1]}x{img.shape[0]}, from a {shot.shape[1]}x{shot.shape[0]} screen)")
    if args.close and img.shape[1] > 1.5 * img.shape[0]:
        print("WARNING: that box is much wider than tall. The script taps the CENTER of the "
              "box, so make sure the X is in the middle - re-grab tightly around just the X.")

    if getattr(args, "misc", False):
        action = getattr(args, "action", None) or "tap"
        lst = [m for m in cfg.get("misc_buttons", []) if (m.get("file") if isinstance(m, dict) else m) != rel]
        lst.append({"file": rel, "action": action})
        cfg["misc_buttons"] = lst
        save_profile(p, cfg)
        print(f"saved as a game pop-up: whenever it shows up, adwatch will {MISC_ACTIONS[action].split(' (')[0]} "
              f"({len(lst)} saved for {args.name})")
    if args.open:
        glob_ = f"../templates/{args.name}/open_*.png"
        lst = cfg.setdefault("open_templates", [])
        if glob_ not in lst:
            lst.append(glob_)
        save_profile(p, cfg)
        print("added to open_templates: tapped when the boost button isn't showing; it also "
              "counts as 'back in the game' so BACK stops as soon as this button is visible")
    if getattr(args, "skip", False):
        print("saved to the shared pool as a skip button: works in every game, and is tapped as soon as "
              "it shows up, even while the ad is still playing. Only use this for buttons that are always "
              "safe (Skip, >>, 'Reward granted X') - an ordinary X belongs in --close.")
    if getattr(args, "popup", False):
        print("saved to the shared pool: works in every game. Whenever it shows up over an ad, "
              "adwatch taps beside it to make it go away.")
    if args.stuck:
        print("saved to the shared pool: works in every game. It is only tapped when an ad shows "
              "no close button after stuck_seconds; BACK is then pressed until the game returns.")
    if args.step or args.then_ad:
        cfg["steps"].append({"tap": rel, "timeout": 10, "optional": args.optional})
        if args.then_ad:
            cfg["steps"].append({"watch_ad": True})
        save_profile(p, cfg)
        print(f"added to steps ({len(cfg['steps'])} total)")


def cmd_step(args):
    """Add a non-image step: watch_ad | wait N | key NAME | tap_xy X Y"""
    p, cfg = load_profile(args.name)
    kind, vals = args.kind, args.values
    if kind == "watch_ad":
        step = {"watch_ad": True}
    elif kind == "wait":
        step = {"wait": float(vals[0])}
    elif kind == "key":
        step = {"key": vals[0]}
    elif kind == "tap_xy":
        conv = lambda v: float(v) if "." in v else int(v)
        step = {"tap_xy": [conv(vals[0]), conv(vals[1])]}
    else:
        sys.exit("kind must be one of: watch_ad, wait, key, tap_xy")
    cfg["steps"].append(step)
    save_profile(p, cfg)
    print(f"added {step}")


# ============================================================== interactive menu
# `python adwatch.py` with no arguments opens this. It only collects answers and then
# calls the same cmd_* functions the command line uses.
UI_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,30}$")


class Back(Exception):
    """Raised by a prompt when the user answers 'b'."""


def _sty(code, s):
    return f"\033[{code}m{s}\033[0m" if UI_COLOR else str(s)


def bold(s): return _sty("1", s)
def dim(s): return _sty("2", s)
def green(s): return _sty("32", s)
def yellow(s): return _sty("33", s)
def red(s): return _sty("31", s)
def cyan(s): return _sty("36", s)


def ui_clear():
    if UI_COLOR:
        print("\033[2J\033[H", end="")
    else:
        print("\n" + "=" * 64)


def ui_title(text):
    ui_clear()
    print(bold(f" adwatch  |  {text}") if text else bold(" adwatch"))
    print(dim(" " + "-" * 62))


def ui_wrap(text, indent=" "):
    import textwrap
    for para in text.strip().split("\n"):
        print(textwrap.fill(para, 76, initial_indent=indent, subsequent_indent=indent) if para.strip() else "")


def ask(prompt, default=None):
    """One line of input. Enter = default. 'b' = go back."""
    tail = f" [{default}]" if default not in (None, "") else ""
    try:
        v = input(f" {prompt}{tail}: ").strip()
    except EOFError:
        raise KeyboardInterrupt
    if v.lower() == "b":
        raise Back()
    return v if v else ("" if default is None else str(default))


def ask_num(prompt, default, lo, hi, kind=int):
    while True:
        v = ask(prompt, default)
        if v == "" and default is None:
            return None
        try:
            n = kind(v)
        except ValueError:
            print(red(f"   please type a number between {lo} and {hi}"))
            continue
        if lo <= n <= hi:
            return n
        print(red(f"   must be between {lo} and {hi}"))


def ask_yes(prompt, default=False):
    while True:
        v = ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
        if v == "":
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False
        print(red("   please answer y or n"))


def pick(rows, prompt="Choose", allow_quit=False):
    """rows: [(label, hint)]. Prints a numbered list, returns the chosen index.
    'b' (or Enter) raises Back; 'q' raises KeyboardInterrupt when allow_quit."""
    width = max((len(r[0]) for r in rows), default=0)
    for i, (label, hint) in enumerate(rows, 1):
        print(f"  {cyan(f'{i:>2}')}  {label.ljust(width)}   {dim(hint)}")
    print(f"  {cyan(' q')}  Quit" if allow_quit else f"  {cyan(' b')}  Back")
    while True:
        try:
            v = input(f"\n {prompt}: ").strip().lower()
        except EOFError:
            raise KeyboardInterrupt
        if v == "q" and allow_quit:
            raise KeyboardInterrupt
        if v in ("b", "") and not allow_quit:
            raise Back()
        if v.isdigit() and 1 <= int(v) <= len(rows):
            return int(v) - 1
        if v:
            print(red(f"   type a number from 1 to {len(rows)}"))


def pause():
    try:
        input(dim("\n Press Enter to go back to the menu "))
    except EOFError:
        pass


def all_devices():
    """Every phone adb knows about, ready or not: [(serial, state, model)]."""
    try:
        out = subprocess.run([ADB, "devices", "-l"], capture_output=True).stdout.decode(errors="replace")
    except FileNotFoundError:
        return []
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
            rows.append((parts[0], parts[1], info.get("model", "?").replace("_", " ")))
    return rows


def describe_step(step):
    if "tap" in step:
        name = step["tap"] if isinstance(step["tap"], str) else step["tap"][0]
        name = os.path.splitext(os.path.basename(name))[0]
        return f"tap '{name}'" + (" (if it's there)" if step.get("optional") else "")
    if step.get("watch_ad"):
        return "watch the ad and close it"
    if "wait" in step:
        return f"wait {step['wait']:g}s"
    if "key" in step:
        return "press " + step["key"].replace("KEYCODE_", "")
    if "tap_xy" in step:
        return f"tap at {tuple(step['tap_xy'])}"
    return str(step)


CONNECT_HELP = """\
To connect a phone:
1. On the phone: Settings > About phone > tap "Build number" 7 times. This unlocks Developer options.
2. In Developer options turn on "USB debugging", plug the phone into this computer and tap "Allow" on the phone.
3. No cable? Turn on "Wireless debugging" instead, tap "Pair device with pairing code", then on this computer run:  adb pair IP:PORT   and then:  adb connect IP:PORT   (use the address shown on the Wireless debugging screen).
A phone listed as "unauthorized" is waiting for you to tap "Allow" on its screen. "offline" usually means: toggle debugging off and on, then reconnect."""

HELP_TEXT = """\
adwatch presses the buttons for you when a game makes you sit through ads. It controls the phone from this computer and finds buttons by looking for small pictures of them that you save once.

Two ways to use it:
- Run the ad loop: for "watch an ad to get a reward" buttons. It taps the button, waits for the ad, closes it, and repeats.
- Watch for pop-up ads: for ads that appear by themselves. You play; it closes them.

First time with a game:
1. "Games" > add the game (have it open on the phone).
2. "Teach it buttons" > save the button that starts an ad. A window shows the phone's screen; drag a box tightly around the button and press Enter.
3. "Run the ad loop" for 1 ad, and answer yes to pausing. When an ad shows a close button it doesn't know, it stops and lets you save it. Each close button you save works in every game from then on.

If a button isn't found, use "Test" with that button on screen. A score just under the limit means lower "match strictness" in Settings; a very low score means save the picture again.

Automated ad watching is against most games' rules, and ad networks can notice it. Use it on accounts you can afford to lose."""

GRABS = {
    "ad": dict(title="The button that starts an ad", label="ad_button", delay=0, flags=dict(then_ad=True),
               show="Have the game on the screen where that button is visible (for example the \"watch ad for 2x\" button)."),
    "after": dict(title="A button to tap after the ad", label="collect", delay=5, flags=dict(step=True),
                  show="Have the screen that appears after an ad finishes on the phone, with the button showing (for example \"Collect\")."),
    "close": dict(title="An ad's close button (X / Skip)", label="x", delay=8, flags=dict(close=True),
                  show="Get an ad on the phone at the moment its X or Skip button is showing. Box just the X, nothing around it. It's saved for every game."),
    "skip": dict(title="A skip or 'reward granted' button", label="skip", delay=8, flags=dict(skip=True),
                 show="Get an ad on the phone while a button that is ALWAYS safe to tap is showing: Skip, >>, or 'Reward granted X'. Box just the button. It's saved for every game and tapped as soon as it appears, even while the ad still plays. An ordinary X goes under 'Ad close button' instead."),
    "stuck": dict(title="A store badge", label="store", delay=8, flags=dict(stuck=True),
                  show="For ads that only let you out through Google Play: get the ad on screen with its \"Google Play\" badge showing. It's saved for every game and only used when an ad shows no close button."),
    "popup": dict(title="An ad pop-up to tap away", label="popup", delay=8, flags=dict(popup=True),
                  show="Get an ad on the phone while its pop-up is showing (for example an \"Install\" bubble that goes away when you tap beside it). Box the whole pop-up. It's saved for every game; adwatch then taps beside it whenever it appears."),
    "misc": dict(title="A game pop-up to deal with", label="levelup", delay=5, flags=dict(misc=True),
                 show="Have the game on the phone with its pop-up showing (for example a level-up notice with an OK button). Box the button to tap, or for pop-ups you swipe away or tap beside, box something that is always on that pop-up."),
    "open": dict(title="The button that opens the pop-up with the ad button", label="opener", delay=0, flags=dict(open=True),
                 show="Have the game on its main screen with the pop-up closed, so the button that opens it is visible."),
    "ingame": dict(title="Something only visible in the game", label="hud", delay=0, flags=dict(ingame=True),
                   show="Have the game on its normal screen. Pick something that is always there in the game and never in an ad."),
}

SETTINGS = [  # key, default, low, high, type, short name, what it means
    ("threshold", 0.85, 0.5, 0.99, float, "Match strictness",
     "How closely the screen must match a saved picture (0.5 loose - 0.99 exact). Lower it a little if buttons that are on screen aren't found; raise it if the wrong thing gets tapped."),
    ("close_threshold", None, 0.5, 0.99, float, "Match strictness, ad close buttons",
     "The same, but only for ad close buttons. Try 0.75 if an X on a dark overlay isn't found. Empty = use the value above."),
    ("cycles", 10, 1, 1000, int, "Ads per run", "The number 'Run the ad loop' suggests."),
    ("ad_min_seconds", 15, 0, 120, int, "Run: earliest close (seconds)",
     "In the ad loop, an ad is never closed before this many seconds, so the reward isn't lost."),
    ("passive_min_seconds", 5, 0, 120, int, "Watch: earliest close (seconds)",
     "When watching for pop-up ads, an ad is never closed before this many seconds. Raise it to about 35 if you also start reward ads by hand while it's watching."),
    ("ad_max_seconds", 90, 20, 600, int, "Give up after (seconds)",
     "If no close button has been found by now, press BACK instead."),
    ("stuck_seconds", 30, 0, 300, int, "Use store badge after (seconds)",
     "If no close button has shown up by now, tap a saved store badge (for ads you can only leave through Google Play)."),
    ("cooldown_seconds", 5, 0, 600, int, "Run: pause between ads (seconds)", "How long to wait before starting the next ad."),
    ("misc_check_seconds", 5, 1, 300, int, "Watch: look for game pop-ups every (seconds)",
     "When watching, how often to check the game's screen for one of its pop-ups you saved (level up, daily reward...). Each check takes a screenshot."),
]


class Menu:
    def __init__(self):
        if os.name == "nt":
            os.system("")                       # turns on colour support in Windows terminals
        self.state = load_state()
        games = self.games()
        self.game = self.state.get("game") if self.state.get("game") in games else (games[0] if len(games) == 1 else None)
        self.phone = None

    # ---- state
    def save_state(self):
        self.state = dict(load_state(), game=self.game,
                          serial=self.phone["serial"] if self.phone else self.state.get("serial"))
        save_state(self.state)

    def games(self):
        return sorted(os.path.splitext(os.path.basename(p))[0]
                      for p in glob.glob(os.path.join(DATA_DIR, "profiles", "*.json")))

    def cfg(self):
        with open(profile_path(self.game)) as f:
            return json.load(f)

    def set_cfg(self, key, value):
        cfg = self.cfg()
        if value is None:
            cfg.pop(key, None)
        else:
            cfg[key] = value
        save_profile(profile_path(self.game), cfg)

    def act_stats(self):
        ui_title("Ad stats")
        print(stats_text())
        pause()

    def pool(self, kind):
        return len(glob.glob(os.path.join(COMMON_DIR, f"{kind}_*.png")))

    # ---- phone
    def find_phone(self, ask_if_many=True):
        """Settle which phone to use. Returns True when one is selected."""
        global SERIAL
        devs = list_devices()
        want = SERIAL or self.state.get("serial")
        chosen = next((d for d in devs if d["serial"] == want), None)
        if not chosen and len(devs) == 1:
            chosen = devs[0]
        if not chosen and len(devs) > 1 and ask_if_many:
            print("\n More than one phone is connected:")
            try:
                i = pick([(d["model"].replace("_", " "), d["serial"]) for d in devs], "Which phone")
                chosen = devs[i]
            except Back:
                pass
        self.phone = chosen
        SERIAL = chosen["serial"] if chosen else None
        if chosen:
            self.save_state()
        return bool(chosen)

    def offer_adb(self):
        """adb missing: explain and offer to download it. Returns True when adb is usable."""
        if adb_available():
            return True
        print()
        ui_wrap("adwatch talks to the phone with Google's 'adb' tool, and it isn't installed on this "
                f"computer. It can be downloaded now from Google (about 15 MB) into {DATA_DIR}. "
                f"It comes under Google's Android SDK licence: {PLATFORM_TOOLS_PAGE}")
        try:
            if not ask_yes("Download adb now?", True):
                return False
        except Back:
            return False
        ok = self.guarded(install_adb)
        if not ok:
            print(" Linux alternative: install the 'android-tools' (or 'adb') package with your package manager.")
        return ok and adb_available()

    def need(self, phone=True, game=True, vision=True):
        """Check the things an action depends on; explain what's missing."""
        if phone and not adb_available() and not self.offer_adb():
            return False
        if vision and cv2 is None:
            print(red("\n OpenCV isn't installed, so the phone's screen can't be read."))
            print(" Install it with:  pip install opencv-python numpy")
            return False
        if phone and not self.find_phone():
            print(red("\n No phone is connected.") + "  Choose 'Phone' in the menu for how to connect one.")
            return False
        if game and not self.game:
            print(red("\n No game is set up yet.") + "  Choose 'Games' in the menu to add one.")
            return False
        return True

    def guarded(self, fn, *a):
        """Run a command; a Ctrl+C or an error brings us back to the menu instead of quitting."""
        try:
            fn(*a)
            return True
        except KeyboardInterrupt:
            print("\n stopped")
        except SystemExit as e:
            if e.code not in (None, 0):
                print(red(f"\n {e.code}"))
        except Exception as e:                  # never drop a menu user into a traceback
            msg = str(e).strip() or type(e).__name__
            window = "tkinter" in msg.lower() or "display" in msg.lower() or type(e).__name__ in ("TclError", "error")
            if type(e).__name__ != "RuntimeError":      # our own RuntimeErrors are already written for people
                msg = msg.splitlines()[-1][-160:]
            print(red(f"\n Something went wrong: {msg}"))
            if window:
                print(" The window for boxing buttons could not open. See 'Check my setup'.")
        return False

    # ---- screens
    def header(self):
        ui_title("")
        if self.phone:
            print(f" Phone   {bold(self.phone['model'].replace('_', ' '))}  {dim(self.phone['serial'])}")
        elif not adb_available():
            print(f" Phone   " + red("adb isn't installed - see 'Check my setup'"))
        else:
            n = len(list_devices())
            print(f" Phone   " + yellow("more than one connected - choose one under 'Phone'" if n > 1 else "none connected"))
        if self.game:
            cfg = self.cfg()
            steps = cfg.get("steps", [])
            flow = " > ".join(describe_step(s) for s in steps) if steps else dim("not set up (only 'Run the ad loop' needs it)")
            print(f" Game    {bold(self.game)}  {dim(cfg.get('package', ''))}")
            print(f" Loop    {flow}")
        else:
            print(f" Game    " + yellow("none yet - add one under 'Games'"))
        c, s = self.pool("close"), self.pool("stuck")
        line = f"{c} close button{'s' * (c != 1)}, {s} store badge{'s' * (s != 1)} saved (shared by all games)"
        print(f" Ads     " + (yellow(line) if c == 0 else line))
        if cv2 is None:
            print(red(" OpenCV is not installed - see 'Check my setup'"))
        if getattr(self, "notice", None):
            print(green(" " + self.notice))
            self.notice = None
        print()

    def loop(self):
        added = sync_starter()
        if not adb_available() and sys.stdin.isatty():
            ui_title("")
            self.offer_adb()
            pause()
        self.find_phone()
        if added:
            self.notice = f"Added {added} ad button{'s' * (added != 1)} from the starter set."
        items = [
            ("Run the ad loop", "taps the ad button, waits, closes the ad, repeats", self.act_run),
            ("Watch for pop-up ads", "you play; it closes ads that appear by themselves", self.act_watch),
            ("Teach it buttons", "the ad button, ad close buttons, store badges", self.act_buttons),
            ("Test", "check the saved buttons against the phone's screen", self.act_test),
            ("Games", "switch game or add a new one", self.act_games),
            ("Phone", "switch phone, or how to connect one", self.act_phone),
            ("Settings", "timings and match strictness for this game", self.act_settings),
            ("Check my setup", "is everything installed and connected?", self.act_doctor),
            ("Ad stats", "how many ads, per game and per phone", self.act_stats),
            ("Help", "what this does and how to start", self.act_help),
        ]
        while True:
            self.find_phone(ask_if_many=False)
            self.header()
            try:
                i = pick([(a, b) for a, b, _ in items], allow_quit=True)
            except KeyboardInterrupt:
                print()
                return
            try:
                items[i][2]()
            except Back:
                pass
            except KeyboardInterrupt:
                print("\n stopped")
                pause()

    def teach_default(self):
        return self.pool("close") < 3      # a new user has few close buttons: pausing builds the set

    def act_run(self):
        ui_title("Run the ad loop")
        if not self.need():
            return pause()
        cfg = self.cfg()
        steps = cfg.get("steps", [])
        if not any("tap" in s for s in steps):
            print(yellow(" This game has no ad button saved yet."))
            if ask_yes("Save it now?", True):
                self.grab("ad")
            return pause()
        if not any(s.get("watch_ad") for s in steps):
            print(yellow(" Note: the loop has no 'watch the ad' step, so it will only tap buttons."))
        print(" Loop: " + " > ".join(describe_step(s) for s in steps))
        print(dim(" Put the game on the screen with the ad button. Ctrl+C stops it.\n"))
        n = ask_num("How many ads", cfg.get("cycles", 10), 1, 1000)
        teach = ask_yes("Pause on ads it can't close, so I can save their button?", self.teach_default())
        print()
        self.guarded(cmd_run, argparse.Namespace(profile=profile_path(self.game), cycles=n, teach=teach))
        pause()

    def act_watch(self):
        ui_title("Watch for pop-up ads")
        if not self.need():
            return pause()
        ui_wrap("You play as normal. When an ad takes over the screen it gets closed. Nothing is "
                "touched while the game's own screen is showing. Ctrl+C stops it.")
        print()
        how = pick([("Until I stop it", "Ctrl+C"), ("For a number of ads", ""), ("For a number of minutes", "")], "Run")
        count = ask_num("How many ads", 20, 1, 10000) if how == 1 else None
        minutes = ask_num("How many minutes", 60, 1, 1440, float) if how == 2 else None
        scan = ask_yes("Also check inside the game's own screen? (only if ads get missed)", False)
        teach = ask_yes("Pause on ads it can't close, so I can save their button?", self.teach_default())
        print()
        self.guarded(cmd_watch, argparse.Namespace(profile=profile_path(self.game), count=count, minutes=minutes,
                                                   scan=scan, teach=teach, learn=False))
        pause()

    def grab(self, kind):
        g = GRABS[kind]
        ui_title(g["title"])
        if not self.need():
            return
        try:
            import tkinter  # noqa: F401
        except ImportError:
            print(red(" Saving a button opens a window, and that needs Tkinter, which isn't installed."))
            ui_wrap("Linux: sudo apt install python3-tk  /  sudo pacman -S tk  /  sudo dnf install python3-tkinter\n"
                    "Windows and macOS: reinstall Python from python.org (Tkinter is included).")
            return
        ui_wrap(g["show"])
        print()
        flags = dict(step=False, then_ad=False, optional=False, close=False, ingame=False, open=False, stuck=False, popup=False,
                     skip=False, misc=False, action="tap")
        flags.update(g["flags"])
        label = g["label"]
        if kind == "misc":
            print(" What should adwatch do when this pop-up shows up?")
            keys = list(MISC_ACTIONS)
            flags["action"] = keys[pick([(MISC_ACTIONS[k].split(" (")[0].capitalize(),
                                          MISC_ACTIONS[k].split(" (")[1].rstrip(")") if " (" in MISC_ACTIONS[k] else "")
                                         for k in keys], "Do")]
            print()
        if kind in ("ad", "after", "open", "ingame", "misc"):
            while True:
                label = ask("Short name for it", g["label"])
                if NAME_RE.match(label):
                    break
                print(red("   letters, numbers, - and _ only"))
        if kind == "after":
            flags["optional"] = ask_yes("Is this button sometimes missing? (then it's skipped)", True)
        if kind in ("ad", "after"):
            rel = f"../templates/{self.game}/{label}.png"
            if any(s.get("tap") == rel for s in self.cfg().get("steps", [])):
                print(dim(f"   '{label}' is already in the loop: only its picture will be replaced."))
                flags["step"] = flags["then_ad"] = False
        delay = ask_num("Seconds before the screenshot (time to get the phone ready)", g["delay"], 0, 120)
        ui_wrap("\nA window with the phone's screen will open. Drag a box tightly around the button, "
                "then press Enter (Esc cancels).")
        self.guarded(cmd_grab, argparse.Namespace(name=self.game, label=label, delay=delay, **flags))

    def act_buttons(self):
        while True:
            ui_title("Teach it buttons")
            if not self.need(phone=False, vision=False):
                return pause()
            steps = self.cfg().get("steps", [])
            print(f" Game: {bold(self.game)}")
            print(" Loop: " + (" > ".join(describe_step(s) for s in steps) if steps else yellow("empty")))
            misc = self.cfg().get("misc_buttons", [])
            print(f" Game pop-ups: {len(misc) or 'none'}")
            print(f" Shared: {self.pool('close')} close buttons, {self.pool('skip')} skip buttons, "
                  f"{self.pool('stuck')} store badges\n")
            rows = [("Ad button", "the button in the game that starts an ad"),
                    ("Ad close button", "an ad's X - works in every game"),
                    ("Skip button", "Skip, >> or 'Reward granted X': tapped right away"),
                    ("Store badge", "ads you can only leave via Google Play"),
                    ("Ad pop-up to tap away", "e.g. an 'Install' bubble over the ad"),
                    ("Button to tap after the ad", "for example Collect"),
                    ("Game pop-up", "level up, daily reward... tap, swipe or BACK it away"),
                    ("Pop-up opener", "if the ad button is inside a pop-up"),
                    ("In-game marker", "optional: something only the game shows"),
                    ("Edit the loop", "remove a step, add a wait, start over"),
                    ("Forget a game pop-up", "stop dealing with one you saved")]
            i = pick(rows)
            if i == 9:
                try:
                    self.edit_steps()
                except Back:
                    pass
                continue
            if i == 10:
                try:
                    self.forget_misc()
                except Back:
                    pass
                continue
            try:
                self.grab(["ad", "close", "skip", "stuck", "popup", "after", "misc", "open", "ingame"][i])
            except Back:
                continue
            pause()

    def forget_misc(self):
        ui_title("Forget a game pop-up")
        cfg = self.cfg()
        lst = cfg.get("misc_buttons", [])
        if not lst:
            print(dim(" No game pop-ups saved for this game."))
            return pause()
        rows = [(os.path.basename(m["file"] if isinstance(m, dict) else m),
                 MISC_ACTIONS.get(m.get("action", "tap") if isinstance(m, dict) else "tap", "").split(" (")[0])
                for m in lst]
        i = pick(rows, "Forget which")
        del lst[i]
        self.set_cfg("misc_buttons", lst)
        print(green(f"   forgotten: {rows[i][0]}"))
        pause()

    def edit_steps(self):
        while True:
            ui_title("Edit the loop")
            cfg = self.cfg()
            steps = cfg.get("steps", [])
            if steps:
                for n, s in enumerate(steps, 1):
                    print(f"   {n}. {describe_step(s)}")
            else:
                print(yellow("   (empty)"))
            print()
            i = pick([("Remove a step", ""), ("Add a wait at the end", ""), ("Add 'press BACK' at the end", ""),
                      ("Add 'watch the ad' at the end", ""), ("Remove all steps", "the saved pictures are kept")])
            if i == 0 and steps:
                n = ask_num("Which step number (Enter = none)", None, 1, len(steps))
                if n:
                    steps.pop(n - 1)
            elif i == 1:
                steps.append({"wait": ask_num("Seconds", 3, 0.1, 600, float)})
            elif i == 2:
                steps.append({"key": "KEYCODE_BACK"})
            elif i == 3:
                steps.append({"watch_ad": True})
            elif i == 4 and ask_yes("Really remove every step?", False):
                steps = []
            cfg["steps"] = steps
            save_profile(profile_path(self.game), cfg)

    def act_test(self):
        ui_title("Test")
        if not self.need():
            return pause()
        ui_wrap("Each saved picture is compared with what the phone shows right now. MATCH means it "
                "would be found. Only buttons that are on screen at this moment can match.")
        print()
        self.guarded(cmd_test, argparse.Namespace(profile=profile_path(self.game)))
        pause()

    def act_games(self):
        ui_title("Games")
        games = self.games()
        rows = [(g, "current" if g == self.game else "") for g in games] + [("Add a new game", "")]
        i = pick(rows)
        if i < len(games):
            self.game = games[i]
            self.save_state()
            return
        if not self.need(game=False, vision=False):
            return pause()
        while True:
            name = ask("Short name for the game, no spaces (e.g. idleminer)")
            if name in games:
                print(red("   that name is taken"))
            elif NAME_RE.match(name):
                break
            else:
                print(red("   letters, numbers, - and _ only"))
        if not self.guarded(cmd_new, argparse.Namespace(name=name, package=None, quiet=True)):
            return pause()
        self.game = name
        self.save_state()
        print(green(f"\n Added {name}.") + " How do ads show up in this game?")
        how = pick([("I tap a button to watch an ad", "for a reward or boost"),
                    ("Ads pop up by themselves", "between levels or tasks"),
                    ("Both", "")], "Ads")
        if how in (0, 2):
            self.grab("ad")
            print(green("\n Next: 'Run the ad loop' from the menu. Try 1 ad first."))
        if how in (1, 2):
            print(green("\n Pop-up ads: choose 'Watch for pop-up ads' in the menu. No buttons needed."))
        pause()

    def act_phone(self):
        ui_title("Phone")
        devs = all_devices()
        if not devs:
            print(yellow(" No phone found.\n"))
            ui_wrap(CONNECT_HELP)
            return pause()
        ready = list_devices()
        for serial, state, model in devs:
            if state != "device":
                who = serial if model == "?" else f"{model}  {dim(serial)}"
                print(f"   {who}  " + yellow(f"not ready ({state})"))
        if any(st != "device" for _, st, _ in devs):
            print()
        if not ready:
            ui_wrap(CONNECT_HELP)
            return pause()
        rows = [(d["model"].replace("_", " "), d["serial"] + ("   current" if self.phone and d["serial"] == self.phone["serial"] else ""))
                for d in ready] + [("How to connect a phone", "")]
        i = pick(rows)
        if i == len(ready):
            print()
            ui_wrap(CONNECT_HELP)
            return pause()
        global SERIAL
        self.phone = ready[i]
        SERIAL = self.phone["serial"]
        self.save_state()

    def act_settings(self):
        while True:
            ui_title("Settings")
            if not self.need(phone=False, vision=False):
                return pause()
            cfg = self.cfg()
            print(f" Game: {bold(self.game)}\n")
            awake = bool(load_state().get("keep_awake", True))
            rows = [("Keep this computer awake while running", ("on" if awake else "off") + "   (all games)")]
            for key, default, lo, hi, kind, name, text in SETTINGS:
                cur = cfg.get(key, default)
                rows.append((name, "same as above" if cur is None else str(cur)))
            i = pick(rows, "Change which")
            if i == 0:
                save_state(dict(load_state(), keep_awake=not awake))
                print(green(f"   keep awake is now {'off' if awake else 'on'}") + dim(
                    "" if awake else "  (the screen may still turn off; the computer won't sleep)"))
                continue
            key, default, lo, hi, kind, name, text = SETTINGS[i - 1]
            cur = cfg.get(key, default)
            print()
            ui_wrap(text)
            try:
                val = ask_num(f"New value ({lo} to {hi})" + (", Enter to clear" if default is None else ""),
                              None if default is None else cur, lo, hi, kind)
            except Back:
                continue
            self.set_cfg(key, val)

    def act_doctor(self):
        ui_title("Check my setup")
        import shutil

        def row(ok, text, fix=""):
            mark = green("[ok]") if ok else (red("[!!]") if ok is False else yellow("[--]"))
            print(f" {mark} {text}")
            if not ok and fix:
                ui_wrap(fix, indent="        ")

        row(sys.version_info >= (3, 8), f"Python {sys.version.split()[0]}", "Python 3.8 or newer is needed.")
        adb_path = shutil.which(ADB)
        row(bool(adb_path), "adb (talks to the phone)" + (f"  {dim(adb_path)}" if adb_path else " not found"),
            "adwatch can download it for you: answer yes at the end of this check.")
        row(cv2 is not None, "OpenCV + NumPy (reads the screen)" + ("" if cv2 is not None else " not installed"),
            "pip install opencv-python numpy")
        try:
            import tkinter  # noqa: F401
            tk_ok = True
        except ImportError:
            tk_ok = False
        row(tk_ok, "Tkinter (the window for boxing buttons)" + ("" if tk_ok else " not installed"),
            "Linux: sudo apt install python3-tk  /  sudo pacman -S tk  /  sudo dnf install python3-tkinter. "
            "Windows and macOS: it comes with the python.org installer.")
        devs = all_devices() if adb_path else []
        ready = [d for d in devs if d[1] == "device"]
        row(bool(ready), f"Phone connected: {', '.join(d[2] for d in ready)}" if ready else "No phone ready",
            "See 'Phone' in the menu for how to connect one.")
        for serial, state, model in devs:
            if state != "device":
                row(None, f"{serial if model == '?' else model + ' (' + serial + ')'} is '{state}'",
                    "unauthorized: tap Allow on the phone. offline: toggle debugging off and on, then reconnect.")
        row(bool(self.games()), f"Games set up: {', '.join(self.games()) or 'none'}", "Add one under 'Games'.")
        c = self.pool("close")
        row(c > 0 or None, f"Ad close buttons saved: {c}",
            "None yet. Run with 'pause' turned on and save them as ads come up, or use 'Teach it buttons'.")
        print(f"\n Your games and saved buttons are kept in:\n   {DATA_DIR}")
        print(dim(f" adwatch {__version__}"))
        if not adb_path:
            self.offer_adb()
        pause()

    def act_help(self):
        ui_title("Help")
        ui_wrap(HELP_TEXT)
        pause()


def cmd_menu(args=None):
    try:
        Menu().loop()
    except KeyboardInterrupt:
        print()


def main():
    ap = argparse.ArgumentParser(prog="adwatch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--serial", help="adb device serial (see: adb devices)")
    ap.add_argument("--version", action="version", version=f"adwatch {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print the detailed debug log to the terminal too (it always goes to logs/debug.log)")
    ap.add_argument("--screen", choices=["auto", "raw", "png"], default=os.environ.get("ADWATCH_SCREEN", "auto"),
                    help="adb screenshot format when not using --feed (default: auto = time both, use the faster)")
    ap.add_argument("--no-fast-taps", action="store_true",
                    help="send each tap as its own adb command (slower; for phones where fast taps misbehave)")
    aw_grp = ap.add_mutually_exclusive_group()
    aw_grp.add_argument("--keep-awake", dest="keep_awake", action="store_true", default=None,
                        help="stop the computer from sleeping while adwatch runs (the default; see Settings)")
    aw_grp.add_argument("--no-keep-awake", dest="keep_awake", action="store_false",
                        help="let the computer sleep as usual")
    ap.add_argument("--feed", metavar="DEVICE", default=os.environ.get("ADWATCH_FEED"),
                    help="read the screen from scrcpy's video feed, e.g. /dev/video9 (Linux, see README)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("menu", help="interactive menu (also what you get with no command)")
    ga = sub.add_parser("get-adb", help="download Google's adb tool into adwatch's folder")
    ga.add_argument("--force", action="store_true", help="download even if adb is already installed")
    td = sub.add_parser("tidy", help="remove duplicate pictures, unused template folders and old debug images")
    td.add_argument("--yes", action="store_true", help="don't ask, just remove")
    kp = sub.add_parser("keepalive", help="run unattended: restart after crashes, wait for fixes after problems")
    kp.add_argument("--wait-hours", type=float, default=4, help="how long to wait for a fix after it stops (default 4)")
    kp.add_argument("rest", nargs=argparse.REMAINDER, help="run GAME ... | watch GAME ...")
    sub.add_parser("stats", help="how many ads have been watched, per game, per phone and in total")
    pk = sub.add_parser("pack", help="(maintainers) copy saved ad buttons into the bundled starter set")
    pk.add_argument("--from", dest="source", help="folder with close_*.png / stuck_*.png (default: your shared pool)")
    pk.add_argument("--to", help="starter folder (default: the one inside this adwatch)")
    r = sub.add_parser("run"); r.add_argument("profile", metavar="GAME"); r.add_argument("-n", "--cycles", type=int)
    r.add_argument("--teach", action="store_true",
                   help="pause when an ad has no known button so you can save one, then continue")
    w = sub.add_parser("watch", help="passive mode: wait for ads that pop up by themselves and close them")
    w.add_argument("profile", metavar="GAME")
    w.add_argument("-n", "--count", type=int, help="stop after closing this many ads")
    w.add_argument("--minutes", type=float, help="stop after this many minutes")
    w.add_argument("--scan", action="store_true",
                   help="also look for ad close buttons on the game's own screen (for ads that don't open a separate screen)")
    w.add_argument("--teach", action="store_true",
                   help="pause when an ad has no known button so you can save one, then continue")
    w.add_argument("--learn", action="store_true", help="add the current screen as one of the game's normal screens")
    t = sub.add_parser("test"); t.add_argument("profile", metavar="GAME")
    s = sub.add_parser("snap"); s.add_argument("out", nargs="?")
    c = sub.add_parser("crop")
    c.add_argument("src")
    for k in ("x", "y", "w", "h"):
        c.add_argument(k, type=int)
    c.add_argument("out")
    n = sub.add_parser("new", help="create a profile for a game")
    n.add_argument("name"); n.add_argument("package", nargs="?")
    g = sub.add_parser("grab", help="drag a box on a live screenshot to save a template")
    g.add_argument("name"); g.add_argument("label")
    g.add_argument("--step", action="store_true", help="also append a 'tap this' step")
    g.add_argument("--then-ad", action="store_true", help="tap step followed by watch_ad")
    g.add_argument("--optional", action="store_true", help="step may be absent")
    g.add_argument("--close", action="store_true", help="save as an ad close button (shared by all games)")
    g.add_argument("--ingame", action="store_true", help="save as an 'in the game' marker")
    g.add_argument("--open", action="store_true",
                   help="save as the button that opens the pop-up holding the ad button")
    g.add_argument("--popup", action="store_true",
                   help="save as a shared ad pop-up that is dismissed by tapping beside it (e.g. an 'Install' bubble)")
    g.add_argument("--misc", action="store_true",
                   help="save as one of the game's own pop-ups (level up, daily reward...) to deal with when it shows up")
    g.add_argument("--action", choices=list(MISC_ACTIONS), default="tap",
                   help="with --misc: what to do with it (default: tap it)")
    g.add_argument("--skip", action="store_true",
                   help="save as a shared skip / 'reward granted' button: tapped as soon as it appears")
    g.add_argument("--stuck", action="store_true",
                   help="save as a shared ad 'exit via store' button (tapped only if no close button appears)")
    g.add_argument("--delay", type=int, default=0, help="seconds to wait before the screenshot")
    st = sub.add_parser("step", help="append watch_ad / wait / key / tap_xy step")
    st.add_argument("name"); st.add_argument("kind"); st.add_argument("values", nargs="*")
    stp = sub.add_parser("stamp", help="record the screen size older templates were captured on")
    stp.add_argument("width", type=int); stp.add_argument("height", type=int)
    args = ap.parse_args()
    global SERIAL, VERBOSE, SCREEN_METHOD, FAST_INPUT, KEEP_AWAKE
    KEEP_AWAKE = args.keep_awake
    if args.serial:
        SERIAL = args.serial
    if args.verbose:
        VERBOSE = True
    SCREEN_METHOD = args.screen
    if args.no_fast_taps:
        FAST_INPUT = False
    if args.cmd in (None, "menu"):
        return cmd_menu(args)
    if args.cmd == "get-adb":
        return cmd_get_adb(args)
    if args.cmd == "pack":
        return cmd_pack(args)
    if args.cmd == "stats":
        return cmd_stats(args)
    if cv2 is None:
        sys.exit("OpenCV isn't installed. Run:  pip install opencv-python numpy")
    if args.cmd == "keepalive":
        return cmd_keepalive(args)
    if args.cmd == "tidy":
        return cmd_tidy(args)
    if args.cmd in ("run", "watch", "test"):
        added = sync_starter()
        if added:
            print(f"added {added} ad button(s) from the starter set")
    if args.cmd not in ("crop", "stamp"):
        choose_device()
    if args.feed and args.cmd in ("run", "watch", "test", "snap", "grab"):
        open_feed(args.feed)
    try:
        {"run": cmd_run, "watch": cmd_watch, "test": cmd_test, "snap": cmd_snap, "crop": cmd_crop,
         "new": cmd_new, "grab": cmd_grab, "step": cmd_step, "stamp": cmd_stamp}[args.cmd](args)
    except KeyboardInterrupt:
        print("\nstopped")
    except Exception:                                  # keep the full error in the log for later
        import traceback
        if args.cmd in ("run", "watch"):
            make_log()("CRASH\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()

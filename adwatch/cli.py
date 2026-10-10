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
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

try:
    import cv2
    import numpy as np
except ImportError:          # the menu's "Check my setup" explains how to install them
    cv2 = np = None

__version__ = "0.1.0"

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
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        LAST_ERR = "adb timed out"
        return b"" if binary else ""
    except FileNotFoundError:
        sys.exit(NO_ADB)
    LAST_ERR = r.stderr.decode(errors="replace").strip()
    return r.stdout if binary else r.stdout.decode(errors="replace")


def shell(cmd, **kw):
    return adb("shell", cmd, **kw)


def _decode(data):
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None


def screenshot():
    img = _decode(adb("exec-out", "screencap", "-p", binary=True))
    if img is not None:
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


def gray_screen():
    return cv2.cvtColor(screenshot(), cv2.COLOR_BGR2GRAY)


def tap(x, y, jitter=6):
    x += random.randint(-jitter, jitter)
    y += random.randint(-jitter, jitter)
    shell(f"input tap {x} {y}")


def screen_size():
    nums = re.findall(r"(\d+)x(\d+)", shell("wm size"))
    if not nums:
        h, w = screenshot().shape[:2]
        return w, h
    return tuple(map(int, nums[-1]))   # last entry is the override size if one is set


def key(name):
    shell(f"input keyevent {name}")


FOCUS_RE = re.compile(r"([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/([A-Za-z0-9_.$]+)")


def focus():
    """(package, activity) of what is in front, e.g.
    ('com.some.game', 'com.google.android.gms.ads.AdActivity'). (None, None) if unknown."""
    out = shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
    m = FOCUS_RE.search(out)
    if not m:
        return None, None
    pkg, act = m.group(1), m.group(2)
    if act.startswith("."):          # short form: pkg/.MainActivity
        act = pkg + act
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
    r"^(Ads?([A-Z]\w*)?Activity|\w*(Interstitial|RewardedVideo|RewardVideo|FullscreenAd|FullScreenAd)\w*)$")


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


MATCH_W = 540          # everything is compared at this width, whatever the phone
_warned = set()


def template_ref_width(path):
    """Width of the screen a template was captured on (sidecar written by grab/crop)."""
    try:
        with open(path + ".json") as f:
            return json.load(f)["width"]
    except (OSError, KeyError, ValueError):
        return None


def load_template(path, screen_w):
    """Template as grayscale, scaled to MATCH_W space."""
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


def _match(img, integ, t):
    """Best normalized correlation of template t inside img -> (score, top-left).
    Windows with (almost) no texture are ignored: OpenCV can report a perfect 1.0 for
    a blank/black area, which would make the script tap on black ad frames."""
    th, tw = t.shape
    res = np.nan_to_num(cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED), nan=0.0, posinf=0.0, neginf=0.0)
    s, sq = integ
    n = th * tw
    wsum = s[th:, tw:] - s[:-th, tw:] - s[th:, :-tw] + s[:-th, :-tw]
    wsq = sq[th:, tw:] - sq[:-th, tw:] - sq[th:, :-tw] + sq[:-th, :-tw]
    var = np.maximum(wsq / n - (wsum / n) ** 2, 0)
    floor = max(2.0, 0.25 * float(t.std()))
    res[var < floor ** 2] = 0.0
    _, score, _, loc = cv2.minMaxLoc(res)
    return score, loc


def score_all(gray, paths):
    """Return [(score, cx, cy, path)] (cx, cy in real screen pixels), best first.
    Score = better of plain grayscale matching and edge matching."""
    h, w = gray.shape[:2]
    f = w / MATCH_W
    small = cv2.resize(gray, (MATCH_W, max(1, round(h / f))), interpolation=cv2.INTER_AREA)
    small_g = _grad(small)
    integ, integ_g = cv2.integral2(small), cv2.integral2(small_g)
    results = []
    for p in paths:
        t = load_template(p, w)
        if t.shape[0] > small.shape[0] or t.shape[1] > small.shape[1]:
            continue
        score, loc = _match(small, integ, t)
        gkey = (p, w, "grad")
        if gkey not in _cache:
            _cache[gkey] = _grad(t)
        tg = _cache[gkey]
        if tg.std() > 1e-3:
            s2, loc2 = _match(small_g, integ_g, tg)
            if s2 > score:
                score, loc = s2, loc2
        th, tw = t.shape
        results.append((score, int((loc[0] + tw / 2) * f), int((loc[1] + th / 2) * f), p))
    return sorted(results, reverse=True)


def find(gray, paths, threshold):
    for hit in score_all(gray, paths):
        return hit if hit[0] >= threshold else None
    return None


# ------------------------------------------------ accessibility-tree fallback
CLOSE_RE = re.compile(
    r"(^|[^a-z])(close|skip|dismiss|no thanks)([^a-z]|$)|^\s*[x×✕✖]\s*$", re.I
)
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def ui_close_button():
    """Look for a close/skip control in the UI hierarchy. Often fails during
    video playback (uiautomator can't get an idle state) - that's fine."""
    shell("uiautomator dump /sdcard/adwatch_ui.xml >/dev/null 2>&1", timeout=15)
    xml = shell("cat /sdcard/adwatch_ui.xml 2>/dev/null")
    start = xml.find("<")
    if start < 0:
        return None
    try:
        root = ET.fromstring(xml[start:])
    except ET.ParseError:
        return None
    for n in root.iter("node"):
        label = " ".join(n.get(k, "") for k in ("text", "content-desc", "resource-id"))
        if not CLOSE_RE.search(label):
            continue
        m = BOUNDS_RE.match(n.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 - x1 < 5 or y2 - y1 < 5:
            continue
        return ((x1 + x2) // 2, (y1 + y2) // 2, label.strip())
    return None


def is_launcher(pkg):
    return bool(pkg) and ("launcher" in pkg or pkg.endswith(".home"))


def return_to_game(pkg, log, max_presses=5):
    """Press BACK (as many times as it takes) until the game is in front again.
    Only relaunches the game if BACK dumped us on the home screen."""
    for i in range(max_presses):
        key("KEYCODE_BACK")
        nap(1.5, 0.15)
        fg = foreground()
        if fg == pkg:
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


def with_common(patterns, base, common_glob):
    """The profile's own templates PLUS everything in templates/common matching
    common_glob - so close/stuck buttons saved once work in every game."""
    paths = expand(patterns, base, quiet=True)
    paths += glob.glob(os.path.join(COMMON_DIR, common_glob))
    return sorted(set(os.path.abspath(p) for p in paths))


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
            c = input("  [x] save close button   [g] save Google Play/store button   "
                      "[w] set wake-tap point\n  [c] continue   [s] skip this ad (BACK)   [q] quit > ").strip().lower()
        except EOFError:
            return "continue"
        if c in ("x", "g"):
            shot = screenshot()
            img = select_box(shot, "close button" if c == "x" else "Google Play / store button")
            if img is None:
                print("  cancelled")
                continue
            print("  saved", save_shared("close" if c == "x" else "stuck", img, shot))
        elif c == "w":
            pt = select_point(screenshot(), "wake-tap point")
            path = cfg.get("_profile_path")
            if pt and path:
                with open(path) as f:
                    raw = json.load(f)
                raw["wake_tap"] = [round(pt[0], 3), round(pt[1], 3)]
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
    for src in sorted(glob.glob(os.path.join(STARTER_DIR, "*.png"))):
        name = os.path.basename(src)
        if name in seen or not re.match(r"^(close|stuck)_", name) or not os.path.exists(src + ".json"):
            continue
        seen.add(name)
        dst = os.path.join(COMMON_DIR, name)
        if not os.path.exists(dst):
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
    for kind in ("close", "stuck"):
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
        fg = foreground()
        if fg and fg != pkg and fg not in IGNORED_FOREGROUND:
            return_to_game(pkg, log, cfg.get("max_back_presses", 5))
            continue
        gray = gray_screen()
        if marks and find(gray, marks, thr):
            return True
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


def watch_ad(cfg, base, log, home=None):
    """Get one ad closed. Active mode (home=None): returns True/False, and "the ad is over"
    means a game marker image is visible again.
    Passive mode (home = set of the game's own activities, from the watch command): the ad is
    over as soon as one of those activities is in front again. Returns "closed" (we closed it),
    "gone" (it went away without us), None (the user left the game) or False (gave up)."""
    pkg = cfg["package"]
    passive = home is not None
    thr = cfg.get("threshold", 0.85)
    close = with_common(cfg.get("close_templates", []), base, "close_*.png")
    close_thr = cfg.get("close_threshold", thr)
    back_in_game = [] if passive else game_markers(cfg, base)
    # Passive ads are interstitials (no reward to lose), so there's no need to sit through them.
    min_s = cfg.get("passive_min_seconds", 5) if passive else cfg.get("ad_min_seconds", 15)
    max_s = cfg.get("ad_max_seconds", 90)
    poll = cfg.get("poll_seconds", 2)
    use_ui = cfg.get("ui_fallback", True)
    # Ads with no other exit (must visit the store, then BACK, before the X appears):
    stuck = with_common(cfg.get("stuck_tap_templates", []), base, "stuck_*.png")
    stuck_s = cfg.get("stuck_seconds", 30)
    max_stuck = cfg.get("max_stuck_taps", 2)
    stuck_taps, last_stuck = 0, 0.0
    wake = cfg.get("wake_tap")            # [x, y] fractions; a tap that makes a faded badge reappear
    wake_taps, last_wake = 0, 0.0
    teach_on = bool(cfg.get("_teach"))
    next_teach = cfg.get("teach_after", 45)

    start = time.time()
    back_presses = 0
    closed_once = False
    nap(3)

    while True:
        elapsed = time.time() - start

        # An ad click-through opened the store / a browser: go back to the game.
        fg, act = focus()
        if fg and fg != pkg and fg not in IGNORED_FOREGROUND:
            if passive and is_launcher(fg):
                log("  you went to the home screen - leaving this ad alone")
                return None
            log(f"  left the game ({fg}), pressing BACK to return")
            return_to_game(pkg, log, cfg.get("max_back_presses", 5))
            min_s = 0  # the ad was clicked through; look for its X right away
            continue

        if passive and fg == pkg and act in home:
            nap(1, 0.1)                       # make sure it isn't just a flicker between screens
            p2, a2 = focus()
            if p2 == pkg and a2 in home:
                acted = closed_once or back_presses or stuck_taps
                log(f"  ad closed after {elapsed:.0f}s" if acted
                    else f"  it went away without any help after {elapsed:.0f}s")
                return "closed" if acted else "gone"
            continue

        gray = gray_screen()

        if back_in_game and elapsed > 5:
            hit = find(gray, back_in_game, thr)
            if hit:
                log(f"  back in game after {elapsed:.0f}s")
                return True

        if elapsed >= min_s:
            hit = find(gray, close, close_thr) if close else None
            if hit:
                log(f"  close button {os.path.basename(hit[3])} ({hit[0]:.2f})")
                tap(hit[1], hit[2], jitter=2)
                closed_once = True
                nap(2)
                continue
            ui = ui_close_button() if use_ui else None
            if ui:
                log(f"  close via UI tree: {ui[2][:50]!r}")
                tap(ui[0], ui[1], jitter=2)
                closed_once = True
                nap(2)
                continue
            if closed_once and not back_in_game and not passive:
                log(f"  ad dismissed after {elapsed:.0f}s")
                return True

        if stuck and elapsed >= stuck_s and stuck_taps < max_stuck and time.time() - last_stuck > 10:
            hit = find(gray, stuck, thr)
            if hit:
                log(f"  no close button after {elapsed:.0f}s - tapping {os.path.basename(hit[3])} "
                    f"({hit[0]:.2f}); will come back with BACK")
                tap(hit[1], hit[2], jitter=2)
                stuck_taps += 1
                last_stuck = time.time()
                nap(3)
                continue
            elif wake and wake_taps < 8 and time.time() - last_wake > 4:
                sw, sh = screen_size()
                log("  badge not visible - tapping the wake point to bring it back")
                tap(int(wake[0] * sw), int(wake[1] * sh), jitter=4)
                wake_taps += 1
                last_wake = time.time()
                nap(0.7)
                continue

        if teach_on and elapsed >= next_teach:
            log(f"  no known button after {elapsed:.0f}s - pausing so you can teach it")
            action = teach(cfg, base, log)
            close = with_common(cfg.get("close_templates", []), base, "close_*.png")
            stuck = with_common(cfg.get("stuck_tap_templates", []), base, "stuck_*.png")
            wake = cfg.get("wake_tap")
            resume_at = max(min_s, stuck_s)          # pick up in the "looking for a button" phase
            start = time.time() - resume_at
            next_teach = resume_at + cfg.get("teach_again", 25)
            if action == "skip":
                key("KEYCODE_BACK")
                nap(2)
            continue

        if elapsed >= max_s:
            if back_presses >= 3:
                log("  ad wouldn't close - giving up on this one")
                return False
            log("  timeout, pressing BACK")
            key("KEYCODE_BACK")
            back_presses += 1
            nap(3)
            continue

        watching_badge = stuck and elapsed >= stuck_s and stuck_taps < max_stuck
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
            ranked = score_all(last, paths)
            now_best = ranked[0][0] if ranked else 0.0
            best = max(best, now_best)
            if ranked and now_best >= thr:
                hit = ranked[0]
                log(f"  tap {os.path.basename(hit[3])} ({hit[0]:.2f})")
                tap(hit[1], hit[2])
                nap(step.get("after", 1.5))
                return True
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

    def log(msg):
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    if foreground() != cfg["package"]:
        log(f"launching {cfg['package']}")
        launch(cfg["package"])
        nap(8)

    ok = fails = 0
    for i in range(1, cycles + 1):
        log(f"cycle {i}/{cycles}")
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
            recover(cfg, base, log)  # dismiss whatever we're stuck on
            if fails >= max_fail:
                log(f"{fails} failures in a row - stopping")
                break
        nap(cooldown)
    log(f"done: {ok} successful cycle(s)")


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

    def log(msg):
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    home = learn_home(cfg, log, force=args.learn)
    close = with_common(cfg.get("close_templates", []), base, "close_*.png")
    log(f"watching {pkg} - play as normal, Ctrl+C to stop"
        + (" (also scanning the game screen for ad close buttons)" if args.scan else ""))

    seen = closed = 0
    armed, away, last_hit, noted = True, False, None, None
    deadline = time.time() + args.minutes * 60 if args.minutes else None
    try:
        while True:
            if deadline and time.time() > deadline:
                log("time is up")
                break
            if args.count and closed >= args.count:
                break
            p, a = focus()

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
                if args.scan and close:        # ads drawn inside the game's own screen
                    hit = find(gray_screen(), close, close_thr)
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
        log(f"saw {seen} ad(s), closed {closed}")


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
    for score, x, y, p in score_all(g, sorted(set(paths))):
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
    if args.close or args.stuck:
        kind = "close" if args.close else "stuck"
        d = os.path.join(root, "templates", "common")
        n = 1
        while os.path.exists(os.path.join(d, f"{kind}_{n}.png")):
            n += 1
        out = os.path.join(d, f"{kind}_{n}.png")
        rel = None
    else:
        d = os.path.join(root, "templates", args.name)
        prefix = "in_game_" if args.ingame else "open_" if args.open else ""
        out = os.path.join(d, f"{prefix}{args.label}.png")
        rel = f"../templates/{args.name}/{prefix}{args.label}.png"
    os.makedirs(d, exist_ok=True)
    cv2.imwrite(out, img)
    write_meta(out, shot)
    print(f"saved {out} ({img.shape[1]}x{img.shape[0]}, from a {shot.shape[1]}x{shot.shape[0]} screen)")
    if args.close and img.shape[1] > 1.5 * img.shape[0]:
        print("WARNING: that box is much wider than tall. The script taps the CENTER of the "
              "box, so make sure the X is in the middle - re-grab tightly around just the X.")

    if args.open:
        glob_ = f"../templates/{args.name}/open_*.png"
        lst = cfg.setdefault("open_templates", [])
        if glob_ not in lst:
            lst.append(glob_)
        save_profile(p, cfg)
        print("added to open_templates: tapped when the boost button isn't showing; it also "
              "counts as 'back in the game' so BACK stops as soon as this button is visible")
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
    "stuck": dict(title="A store badge", label="store", delay=8, flags=dict(stuck=True),
                  show="For ads that only let you out through Google Play: get the ad on screen with its \"Google Play\" badge showing. It's saved for every game and only used when an ad shows no close button."),
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
        flags = dict(step=False, then_ad=False, optional=False, close=False, ingame=False, open=False, stuck=False)
        flags.update(g["flags"])
        label = g["label"]
        if kind in ("ad", "after", "open", "ingame"):
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
            print(f" Shared: {self.pool('close')} close buttons, {self.pool('stuck')} store badges\n")
            rows = [("Ad button", "the button in the game that starts an ad"),
                    ("Ad close button", "an ad's X or Skip - works in every game"),
                    ("Store badge", "ads you can only leave via Google Play"),
                    ("Button to tap after the ad", "for example Collect"),
                    ("Pop-up opener", "if the ad button is inside a pop-up"),
                    ("In-game marker", "optional: something only the game shows"),
                    ("Edit the loop", "remove a step, add a wait, start over")]
            i = pick(rows)
            if i == 6:
                try:
                    self.edit_steps()
                except Back:
                    pass
                continue
            try:
                self.grab(["ad", "close", "stuck", "after", "open", "ingame"][i])
            except Back:
                continue
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
            rows = []
            for key, default, lo, hi, kind, name, text in SETTINGS:
                cur = cfg.get(key, default)
                rows.append((name, "same as above" if cur is None else str(cur)))
            i = pick(rows, "Change which")
            key, default, lo, hi, kind, name, text = SETTINGS[i]
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
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("menu", help="interactive menu (also what you get with no command)")
    ga = sub.add_parser("get-adb", help="download Google's adb tool into adwatch's folder")
    ga.add_argument("--force", action="store_true", help="download even if adb is already installed")
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
    g.add_argument("--stuck", action="store_true",
                   help="save as a shared ad 'exit via store' button (tapped only if no close button appears)")
    g.add_argument("--delay", type=int, default=0, help="seconds to wait before the screenshot")
    st = sub.add_parser("step", help="append watch_ad / wait / key / tap_xy step")
    st.add_argument("name"); st.add_argument("kind"); st.add_argument("values", nargs="*")
    stp = sub.add_parser("stamp", help="record the screen size older templates were captured on")
    stp.add_argument("width", type=int); stp.add_argument("height", type=int)
    args = ap.parse_args()
    global SERIAL
    if args.serial:
        SERIAL = args.serial
    if args.cmd in (None, "menu"):
        return cmd_menu(args)
    if args.cmd == "get-adb":
        return cmd_get_adb(args)
    if args.cmd == "pack":
        return cmd_pack(args)
    if cv2 is None:
        sys.exit("OpenCV isn't installed. Run:  pip install opencv-python numpy")
    if args.cmd in ("run", "watch", "test"):
        added = sync_starter()
        if added:
            print(f"added {added} ad button(s) from the starter set")
    if args.cmd not in ("crop", "stamp"):
        choose_device()
    try:
        {"run": cmd_run, "watch": cmd_watch, "test": cmd_test, "snap": cmd_snap, "crop": cmd_crop,
         "new": cmd_new, "grab": cmd_grab, "step": cmd_step, "stamp": cmd_stamp}[args.cmd](args)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()

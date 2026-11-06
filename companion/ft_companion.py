#!/usr/bin/env python3
"""Forever Tracker companion.

Reads the in-game strip and the ForeverTracker SavedVariables file that WoW
writes on reload, logout and exit, and forwards them to the tracker backend:
backend_url from config.json (or the LAN address set in game), with the key
from config.json or, for friends, the code entered in game (minimap button).

Packaged as an exe that sits inside the addon folder: double-clicked once, it
finds the game from its own location, starts silently with Windows from then
on, and logs to companion.log next to it.
Standard library only (Pillow for screen capture on Windows).
"""

import argparse
import glob
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import combatlog
import strip

# Packaged by tools/package.py as a Windows exe or a Mac app, with the addon bundled inside.
FROZEN = getattr(sys, "frozen", False)
MAC = sys.platform == "darwin"
if FROZEN and MAC and ".app/Contents/MacOS" in sys.executable:
    # The folder holding ForeverTrackerCompanion.app (normally the addon folder).
    APP_DIR = os.path.dirname(sys.executable.split(".app/Contents/MacOS")[0] + ".app")
else:
    APP_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
# Config and log: next to the program, except the Mac app keeps them in Application
# Support (macOS may run a downloaded app from a read-only copy).
HERE = (os.path.expanduser("~/Library/Application Support/ForeverTracker") if FROZEN and MAC
        else APP_DIR)
ADDON_SRC = (os.path.join(sys._MEIPASS, "addon", "ForeverTracker") if FROZEN
             else os.path.join(APP_DIR, "..", "addon", "ForeverTracker"))
DEFAULT_BACKEND = "https://wow.osrs.dk"
APP_KEY = "ForeverTracker"   # name of the startup and Apps & features entries
EXE_NAME = "ForeverTrackerCompanion"
# Commands run from the windowless exe would otherwise flash a console window.
NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW
SV_NAME = "ForeverTracker.lua"
GAME_PROCESS = re.compile(r"^wow(classic|forever|t|b)?(-64)?\.exe$|^world of warcraft", re.I)


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    if not FROZEN:
        print(line[11:], flush=True)
        return
    # The exe has no console: keep a small log file next to it.
    path = os.path.join(HERE, "companion.log")
    try:
        os.makedirs(HERE, exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def applescript_text(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mac_dialog(text, buttons, error=False):
    """An AppleScript dialog; returns the button clicked, or None if cancelled."""
    icon = "caution" if error else "note"
    script = (f"display dialog {applescript_text(text)} with title \"Forever Tracker\" with icon {icon} "
              f"buttons {{{', '.join(applescript_text(b) for b in buttons)}}} "
              f"default button {applescript_text(buttons[-1])}")
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    m = re.search(r"button returned:(.*)", out.stdout)
    return m.group(1).strip() if m else None


def message_box(text, error=False):
    """A dialog from the windowless app (stdout when run as a script)."""
    if FROZEN and sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Forever Tracker", 0x10 if error else 0x40)
    elif FROZEN and MAC:
        mac_dialog(text, ["OK"], error)
    else:
        print(text)


# --- SavedVariables (Lua table literal) parser ------------------------------

class LuaParser:
    """Parses the subset of Lua that WoW uses to serialise SavedVariables."""

    TOKEN = re.compile(r"""
        (?P<ws>\s+|--[^\n]*)
      | (?P<str>"(?:\\.|[^"\\])*")
      | (?P<num>-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|-?0[xX][0-9a-fA-F]+|-?(?:inf|nan))
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<sym>[{}\[\]=,;])
    """, re.X)

    ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'",
               "a": "\a", "b": "\b", "f": "\f", "v": "\v", "\n": "\n"}

    def __init__(self, text):
        self.tokens = []
        pos = 0
        while pos < len(text):
            m = self.TOKEN.match(text, pos)
            if not m:
                raise ValueError(f"unexpected character at {pos}: {text[pos:pos+20]!r}")
            pos = m.end()
            if m.lastgroup != "ws":
                self.tokens.append((m.lastgroup, m.group()))
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise ValueError(f"expected {value!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def unescape(self, s):
        out, i = [], 0
        while i < len(s):
            c = s[i]
            if c != "\\":
                out.append(c)
                i += 1
                continue
            n = s[i + 1]
            if n.isdigit():
                m = re.match(r"\d{1,3}", s[i + 1:])
                out.append(chr(int(m.group())))
                i += 1 + len(m.group())
            else:
                out.append(self.ESCAPES.get(n, n))
                i += 2
        # The file is read as latin-1 so every char is one byte; WoW writes
        # UTF-8, and decimal escapes are bytes too.
        return "".join(out).encode("latin-1").decode("utf-8", "replace")

    def value(self):
        kind, text = self.take()
        if kind == "str":
            return self.unescape(text[1:-1])
        if kind == "num":
            if "x" in text.lower():
                return int(text, 16)
            f = float(text)
            return int(f) if f.is_integer() and re.fullmatch(r"-?\d+", text) else f
        if kind == "name":
            return {"true": True, "false": False, "nil": None}.get(text, text)
        if text == "{":
            return self.table()
        raise ValueError(f"unexpected token {text!r}")

    def table(self):
        array, mapping = [], {}
        while self.peek()[1] != "}":
            kind, text = self.peek()
            if text == "[":
                self.take("[")
                key = self.value()
                self.take("]")
                self.take("=")
                mapping[key] = self.value()
            elif kind == "name" and self.tokens[self.i + 1][1] == "=":
                self.take()
                self.take("=")
                mapping[text] = self.value()
            else:
                array.append(self.value())
            if self.peek()[1] in (",", ";"):
                self.take()
        self.take("}")
        # Tables keyed 1..n are lists.
        if not array and mapping and all(isinstance(k, int) for k in mapping):
            keys = sorted(mapping)
            if keys == list(range(1, len(keys) + 1)):
                return [mapping[k] for k in keys]
        if array and not mapping:
            return array
        if array:
            for idx, v in enumerate(array, 1):
                mapping.setdefault(idx, v)
        return mapping if mapping or not array else array

    def assignments(self):
        result = {}
        while self.i < len(self.tokens):
            _, name = self.take()
            self.take("=")
            result[name] = self.value()
        return result


def parse_saved_variables(path):
    with open(path, "rb") as f:
        text = f.read().decode("latin-1")
    return LuaParser(text).assignments()


# --- helpers ----------------------------------------------------------------

def find_saved_variables(cfg):
    explicit = cfg.get("saved_variables")
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    root = cfg.get("wow_path", "")
    pattern = os.path.join(root, "_*_", "WTF", "Account", "*", "SavedVariables", SV_NAME)
    matches = glob.glob(pattern)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def game_running():
    """True/False if the WoW client process is running, None if unknown."""
    try:
        if sys.platform.startswith("linux"):
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        argv0 = f.read().split(b"\0", 1)[0].decode("utf-8", "replace")
                except OSError:
                    continue
                if GAME_PROCESS.match(re.split(r"[\\/]", argv0)[-1]):
                    return True
            return False
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=10, **NO_WINDOW).stdout
            names = [line.split('","')[0].strip('"') for line in out.splitlines()]
        else:
            out = subprocess.run(["ps", "-axo", "comm"], capture_output=True,
                                 text=True, timeout=10).stdout
            names = [os.path.basename(line.strip()) for line in out.splitlines()]
        return any(GAME_PROCESS.match(n) for n in names)
    except Exception as exc:  # noqa: BLE001 - detection is best effort
        log(f"process check failed: {exc}")
        return None


def endpoint_url(endpoint):
    """Builds the ingest URL, refusing anything that isn't a private IPv4."""
    m = re.fullmatch(r"(\d+\.\d+\.\d+\.\d+)(?::(\d+))?", str(endpoint or ""))
    if not m:
        return None
    try:
        ip = ipaddress.IPv4Address(m.group(1))
    except ValueError:
        return None
    if not (ip.is_private or ip.is_loopback):
        return None
    return f"http://{ip}:{int(m.group(2) or 8787)}/api/ingest"


def expand_professions(payload):
    """The strip's compact rows [name, rank, max, bonus, secondary] -> dicts
    (an empty list arrives as {})."""
    rows = [r for r in payload if isinstance(r, list) and len(r) >= 5] if isinstance(payload, list) else []
    return [{"name": r[0], "rank": r[1], "max": r[2] or None, "bonus": r[3] or None, "secondary": bool(r[4])}
            for r in rows]


def post(url, token, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "forever-tracker-companion",
        "Authorization": f"Bearer {token}",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


# --- first run and addon install -------------------------------------------------

def is_wow_root(path):
    return bool(path) and os.path.isdir(os.path.join(path, "_forever_"))


def wow_from_location():
    """The game folder when this runs from inside it (e.g. Interface/AddOns/ForeverTracker)."""
    path = APP_DIR
    while True:
        parent = os.path.dirname(path)
        if os.path.basename(path).lower() == "_forever_":
            return parent
        if parent == path:
            return None
        path = parent


def detect_wow_path():
    """The World of Warcraft folder that holds _forever_, or None."""
    candidates = []
    if sys.platform == "win32":
        try:
            import winreg
            for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Blizzard Entertainment\World of Warcraft")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        install = winreg.QueryValueEx(k, "InstallPath")[0].rstrip("\\/")
                except OSError:
                    continue
                # InstallPath points at a flavour folder such as ..._forever_
                candidates += [os.path.dirname(install), install]
        except ImportError:
            pass
        for drive in "CDEFG":
            candidates += [f"{drive}:\\Program Files (x86)\\World of Warcraft",
                           f"{drive}:\\Program Files\\World of Warcraft", f"{drive}:\\World of Warcraft",
                           f"{drive}:\\Games\\World of Warcraft"]
    elif MAC:
        candidates += ["/Applications/World of Warcraft", os.path.expanduser("~/Applications/World of Warcraft")]
    return next((c for c in candidates if is_wow_root(c)), None)


def first_run(path):
    """Finds the game and writes config.json. The code comes from the game."""
    wow = wow_from_location() or detect_wow_path()
    if not wow and not FROZEN:
        while not is_wow_root(wow):
            wow = input("Paste your World of Warcraft folder (the one that contains _forever_): ").strip().strip('"')
    if not wow:
        where = ("World of Warcraft/_forever_/Interface/AddOns" if MAC
                 else "World of Warcraft\\_forever_\\Interface\\AddOns")
        message_box(f"Couldn't find World of Warcraft.\n\nPut the ForeverTracker folder in\n{where}\n"
                    "and open ForeverTrackerCompanion from there.", error=True)
        sys.exit(1)
    cfg = {"wow_path": wow, "backend_url": DEFAULT_BACKEND,
           "poll_seconds": 2, "live_seconds": 1, "heartbeat_seconds": 30}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    log(f"first run: game in {wow}; saved {path}")
    message_box("Forever Tracker is set up and now runs in the background. It starts by itself "
                f"when you log in to {'your Mac' if MAC else 'Windows'}, so you don't need to open it "
                "again.\n\nIn game, click the Forever Tracker button on the minimap and paste the code "
                "you were given.")
    return cfg


def single_instance():
    """False when another copy is already running (packaged app only)."""
    if not FROZEN:
        return True
    if sys.platform == "win32":
        import ctypes
        single_instance.handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\ForeverTrackerCompanion")
        return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    import fcntl
    os.makedirs(HERE, exist_ok=True)
    single_instance.handle = open(os.path.join(HERE, "companion.lock"), "w")
    try:
        fcntl.flock(single_instance.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


LAUNCH_AGENT = os.path.expanduser("~/Library/LaunchAgents/com.forevertracker.companion.plist")


def untranslocate():
    """A downloaded app runs from a random read-only copy ("App Translocation")
    until its quarantine mark is gone; the login item would point at that copy.
    Finds the real addon folder through the game's location, clears the mark
    there and starts the app again from it. False if the folder can't be found."""
    wow = detect_wow_path()
    folder = os.path.join(wow, "_forever_", "Interface", "AddOns", "ForeverTracker") if wow else None
    app = folder and os.path.join(folder, EXE_NAME + ".app")
    if not (app and os.path.isdir(app)):
        return False
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", folder], capture_output=True, timeout=30)
    handle = getattr(single_instance, "handle", None)
    if handle:
        handle.close()  # let the new copy take the single-instance lock
    subprocess.run(["open", "-n", app], capture_output=True, timeout=30)
    log(f"started again from {app} (was running from a temporary copy)")
    return True


def mac_checks():
    """Mac app: leaves macOS's temporary read-only copy (see untranslocate), and
    asks for Screen Recording, which reading the in-game strip needs. Returns
    False when this copy should quit."""
    if "/AppTranslocation/" in sys.executable:
        if not untranslocate():
            message_box("macOS is running Forever Tracker from a temporary copy, and the ForeverTracker "
                        "folder wasn't found in World of Warcraft/_forever_/Interface/AddOns.\n\nPut the "
                        "folder there. Or open Terminal, type: xattr -dr com.apple.quarantine (and a space), "
                        "drag the ForeverTracker folder into the Terminal window, press Return, and open "
                        "ForeverTrackerCompanion again.", error=True)
        return False
    try:
        import Quartz
        if not Quartz.CGPreflightScreenCaptureAccess():
            Quartz.CGRequestScreenCaptureAccess()
            message_box("Forever Tracker needs Screen Recording permission to read your character from "
                        "the game window.\n\nIn System Settings > Privacy & Security > Screen Recording, "
                        "turn on ForeverTrackerCompanion, then open it again.")
            log("asked for the Screen Recording permission")
    except (ImportError, AttributeError) as exc:
        log(f"couldn't check the Screen Recording permission: {exc}")
    return True


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall" + "\\" + APP_KEY


def autostart(cfg):
    """Starts the app at login (per user, no admin), pointing at wherever it lives
    now. Windows also lists it under Settings > Apps for uninstalling."""
    if not FROZEN or cfg.get("autostart") is False:
        return
    if MAC:
        import plistlib
        os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
        with open(LAUNCH_AGENT, "wb") as f:
            plistlib.dump({"Label": "com.forevertracker.companion", "ProgramArguments": [sys.executable],
                           "RunAtLoad": True, "ProcessType": "Interactive"}, f)
        return
    if sys.platform != "win32":
        return
    import winreg
    exe = sys.executable
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, APP_KEY, 0, winreg.REG_SZ, f'"{exe}"')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APPS_KEY) as k:
        for name, value in (("DisplayName", "Forever Tracker"),
                            ("DisplayVersion", addon_version(ADDON_SRC) or ""),
                            ("Publisher", "Forever Tracker"),
                            ("DisplayIcon", exe),
                            ("InstallLocation", APP_DIR),
                            ("UninstallString", f'"{exe}" --uninstall')):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        for name in ("NoModify", "NoRepair"):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, 1)


# --- uninstall ----------------------------------------------------------------

def ask_yes_no(text):
    if FROZEN and sys.platform == "win32":
        import ctypes
        return ctypes.windll.user32.MessageBoxW(None, text, "Forever Tracker", 0x4 | 0x20) == 6  # YES
    if FROZEN and MAC:
        return mac_dialog(text, ["Cancel", "Remove"]) == "Remove"
    return input(text + " [y/N] ").strip().lower().startswith("y")


def remove_registry_entries():
    """Takes the app out of login startup (and, on Windows, Settings > Apps)."""
    if MAC:
        try:
            os.remove(LAUNCH_AGENT)
        except OSError:
            pass
        return
    if sys.platform != "win32":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_KEY)
    except OSError:
        pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, APPS_KEY)
    except OSError:
        pass


def stop_other_instances():
    """Ends the copy running in the background (not this one)."""
    if sys.platform == "win32" and FROZEN:
        subprocess.run(["taskkill", "/F", "/IM", os.path.basename(sys.executable),
                        "/FI", f"PID ne {os.getpid()}"], capture_output=True, timeout=15, **NO_WINDOW)
    elif MAC and FROZEN:
        out = subprocess.run(["pgrep", "-f", "ForeverTrackerCompanion.app/Contents/MacOS"],
                             capture_output=True, text=True, timeout=15).stdout
        for pid in out.split():
            if pid.isdigit() and int(pid) != os.getpid():
                try:
                    os.kill(int(pid), 15)
                except OSError:
                    pass


def saved_variable_files(wow_path):
    pattern = os.path.join(wow_path or "", "_*_", "WTF", "Account", "*", "SavedVariables", SV_NAME)
    return [p for g in (pattern, pattern + ".bak") for p in glob.glob(g)]


def is_addon_folder(path):
    return (os.path.isfile(os.path.join(path, "ForeverTracker.toc"))
            and os.path.basename(os.path.dirname(path)).lower() == "addons")


def addon_folders(wow_path):
    """This exe's own folder when it is the addon, plus the installed addon."""
    found = [APP_DIR] if is_addon_folder(APP_DIR) else []
    installed = os.path.join(wow_path or "", "_forever_", "Interface", "AddOns", "ForeverTracker")
    if is_addon_folder(installed) and os.path.normcase(os.path.abspath(installed)) not in \
            [os.path.normcase(os.path.abspath(f)) for f in found]:
        found.append(installed)
    return found


def delete_folder(path):
    """Deletes a folder; on Windows the one this exe runs from goes a moment after
    it exits (a running exe's file is locked there; macOS doesn't mind)."""
    if (FROZEN and sys.platform == "win32"
            and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(APP_DIR))):
        subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 > nul & rmdir /s /q "{path}"', **NO_WINDOW)
    else:
        shutil.rmtree(path, ignore_errors=True)


def uninstall(cfg, confirm=True):
    """Stops the companion, removes the startup and Apps entries, the addon
    and its saved settings (with the code). Returns True when done."""
    wow = cfg.get("wow_path") or wow_from_location() or detect_wow_path()
    if game_running():
        message_box("Close World of Warcraft first, then uninstall again.\n\n"
                    "(While it runs, the game would save Forever Tracker's settings again.)", error=True)
        return False
    folders = addon_folders(wow)
    if confirm and not ask_yes_no(
            f"Remove Forever Tracker from this {'Mac' if MAC else 'PC'}?\n\nThis stops it, takes it out "
            "of startup, and deletes the addon and its saved settings, including your code."):
        return False
    stop_other_instances()
    remove_registry_entries()
    for path in saved_variable_files(wow):
        try:
            os.remove(path)
        except OSError:
            pass
    for folder in folders:
        delete_folder(folder)
    log("uninstalled")
    if FROZEN and MAC:
        shutil.rmtree(HERE, ignore_errors=True)  # config and log in Application Support
    if confirm:
        message_box("Forever Tracker has been removed.\n\nYour character stays on the website until "
                    "whoever runs it takes it off.")
    return True


def addon_version(folder):
    try:
        with open(os.path.join(folder, "ForeverTracker.toc"), encoding="utf-8") as f:
            m = re.search(r"^## Version:\s*(\S+)", f.read(), re.M)
            return m and m.group(1)
    except OSError:
        return None


def install_addon(cfg):
    """Copies the bundled addon into the game when it's missing or different."""
    if cfg.get("install_addon") is False or not os.path.isdir(ADDON_SRC):
        return
    forever = os.path.join(cfg.get("wow_path", ""), "_forever_")
    if not os.path.isdir(forever):
        log(f"no _forever_ folder in {cfg.get('wow_path')!r}; install the addon by hand")
        return
    dest = os.path.join(forever, "Interface", "AddOns", "ForeverTracker")
    same = os.path.isdir(dest) and all(
        os.path.isfile(os.path.join(dest, name))
        and open(os.path.join(ADDON_SRC, name), "rb").read() == open(os.path.join(dest, name), "rb").read()
        for name in os.listdir(ADDON_SRC))
    if same:
        return
    old = addon_version(dest)
    shutil.copytree(ADDON_SRC, dest, dirs_exist_ok=True)
    log(f"installed the Forever Tracker addon {addon_version(dest)}"
        + (f" (was {old})" if old else "") + f" in {dest}; restart WoW to load it")


# --- main loop ---------------------------------------------------------------

class Companion:
    LIVE_EVERY = 1.0      # seconds between live uploads (config: live_seconds)
    CAPTURE_EVERY = 0.05  # seconds between strip reads
    LIVE_STALE = 3.0      # no valid strip for this long = not live
    GAME_CHECK = 5.0      # seconds between "is WoW running?" checks

    def __init__(self, cfg):
        self.cfg = cfg
        self.poll = cfg.get("poll_seconds", 2)
        self.heartbeat_every = cfg.get("heartbeat_seconds", 30)
        self.live_every = cfg.get("live_seconds", self.LIVE_EVERY)
        self.problem_seen = (None, 0)
        self.path = None
        self.mtime = None
        self.db = None
        self.pending = None
        self.last_heartbeat = 0
        self.was_running = None
        self.last_file_check = 0

        self.live = cfg.get("live", True)
        self.game_up = True       # is WoW running? checked every GAME_CHECK seconds
        self.game_checked = 0
        self.game_up_since = time.time()
        self.capture = None
        self.reasm = strip.Reassembler()
        self.frame = None
        self.frame_at = 0
        self.identity = None
        self.zone = None
        self.quests = None
        self.quests_dirty = False
        self.gear = None
        self.gear_dirty = False
        self.target = None
        self.professions = None
        self.professions_dirty = False
        self.jumps = None
        self.jumps_dirty = False
        self.events = []
        self.last_live_post = 0
        self.last_hidden_post = 0
        self.live_logged = False
        self.combat_log = combatlog.CombatLogTail(cfg["wow_path"]) if cfg.get("wow_path") else None

    @property
    def token(self):
        """config.json's token (the owner), else the code entered in game."""
        return self.cfg.get("token") or self.settings().get("key")

    def settings(self):
        return (self.db or {}).get("settings") or {}

    def sharing(self):
        s = self.settings()
        return bool(s.get("enabled")) and not s.get("appearOffline") and not self.offline_pending()

    def offline_pending(self):
        """The player hasn't chosen yet whether to share time spent appearing offline."""
        return bool((self.db or {}).get("offlineSince"))

    def build_payload(self, running):
        s = self.settings()
        if not s.get("enabled"):
            return None  # tracking off: nothing leaves this machine
        if s.get("appearOffline") or self.offline_pending():
            # Recorded while appearing offline (or not decided yet): stays on this PC.
            return {"kind": "presence", "online": False}
        snap = self.db.get("snapshot")
        if not snap:
            return None
        return {
            "kind": "snapshot",
            "gameRunning": running,
            "snapshot": snap,
            "events": self.db.get("events") or [],
            "positions": self.db.get("positions") or [],
            "xp": self.db.get("xp") or [],
            "quests": self.db.get("quests"),
            "gear": self.db.get("gear"),
            "professions": self.db.get("professions"),
            "jumps": (self.db.get("jumps") or {}).get(f"{snap.get('name')}-{snap.get('realm') or ''}"),
        }

    def ingest_url(self):
        """backend_url (a site on Vercel) wins over the in-game LAN address."""
        base = str(self.cfg.get("backend_url") or "").rstrip("/")
        if base:
            # The token travels with every upload: never over plain HTTP.
            return f"{base}/api/ingest" if base.startswith("https://") else None
        return endpoint_url(self.settings().get("endpoint") or self.cfg.get("endpoint"))

    def problem(self, msg):
        """Logs a repeating problem once, then every 5 minutes while it lasts."""
        last, at = self.problem_seen
        if msg != last or time.time() - at > 300:
            log(msg)
            self.problem_seen = (msg, time.time())

    def character(self):
        """Which character uploads belong to: from the strip, else the saved file."""
        for who in (self.identity, (self.db or {}).get("snapshot")):
            if isinstance(who, dict) and who.get("name"):
                return {"name": who["name"], "realm": who.get("realm")}
        return None

    def send(self, payload):
        url = self.ingest_url()
        if not url:
            log("no usable backend: set backend_url (https://...) in the config, "
                "or a LAN address in game; not sending")
            return True  # don't retry an invalid target
        who = self.character()
        if not who:
            return True  # nothing to attach it to until the game tells us who's playing
        if not self.token:
            self.problem("no code yet: click the Forever Tracker button on the minimap and enter it")
            return True
        try:
            post(url, self.token, {**payload, "character": who})
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self.problem("the site doesn't accept this code; enter it again with the minimap button")
            elif exc.code == 403:
                self.problem(f"{who['name']} is already tracked with someone else's key")
            else:
                self.problem(f"send to {url} failed: {exc}")
            return exc.code in (401, 403)  # retrying won't help
        except (urllib.error.URLError, OSError) as exc:
            self.problem(f"send to {url} failed: {exc}")
            return False

    def check_file(self):
        path = find_saved_variables(self.cfg)
        if not path:
            return
        mtime = os.path.getmtime(path)
        if path == self.path and mtime == self.mtime:
            return
        try:
            self.db = parse_saved_variables(path).get("ForeverTrackerDB") or {}
        except (ValueError, OSError) as exc:
            # The game may still be writing the file; retry next poll.
            log(f"could not parse {path}: {exc}")
            return
        self.path, self.mtime = path, mtime
        running = game_running()
        self.pending = self.build_payload(running)
        log(f"saved variables changed ({path}); "
            + ("sending update" if self.pending else "tracking off, not sending"))

    # --- live strip ---------------------------------------------------------

    def read_strip(self, now):
        if self.capture is None:
            self.capture = strip.make_capture()
        data = self.capture.grab()
        frame = strip.decode(data) if data else None
        if not frame:
            return
        self.frame, self.frame_at = frame, now
        if not self.live_logged:
            log("reading live data from the in-game strip")
            self.live_logged = True
        chunk = frame.pop("chunk", None)
        msg = chunk and self.reasm.add(chunk, now)
        if not msg:
            return
        kind, payload = msg
        if kind == "I":
            self.identity = payload
        elif kind == "Z":
            self.zone = payload
        elif kind == "Q":
            if payload != self.quests:
                self.quests, self.quests_dirty = payload, True
        elif kind == "T":
            self.target = payload or None
        elif kind == "P":
            professions = expand_professions(payload)
            if professions != self.professions:
                self.professions, self.professions_dirty = professions, True
        elif kind == "J":
            n = payload.get("n") if isinstance(payload, dict) else None
            if isinstance(n, int) and n != self.jumps:
                self.jumps, self.jumps_dirty = n, True
        elif kind == "G":
            if payload != self.gear:
                self.gear, self.gear_dirty = payload, True
        elif kind == "E":
            self.events.append(payload)

    def is_live(self, now):
        return self.frame is not None and now - self.frame_at < self.LIVE_STALE

    def post_live(self, now):
        if now - self.last_live_post < self.live_every:
            return
        if not self.ingest_url():
            return  # keep queued events until we know where to send them
        self.last_live_post = now
        flags = self.frame["flags"]
        if flags["hidden"]:
            # Appear offline: only ever say so, nothing else.
            if now - self.last_hidden_post > self.heartbeat_every:
                if self.send({"kind": "presence", "online": False}):
                    self.last_hidden_post = now
            return
        if not flags["sharing"]:
            return
        self.last_hidden_post = 0
        target = dict(self.target) if self.target else None
        if target:
            target.pop("guid", None)  # scrambled by the client; never uploaded
        payload = {"kind": "live", "frame": self.frame, "identity": self.identity,
                   "zone": self.zone, "events": self.events, "target": target}
        # Health/power from the game's combat log, only while the in-game
        # setting is on (the strip says so).
        if self.combat_log and flags.get("combatLog"):
            try:
                self.combat_log.poll()
            except OSError as exc:
                log(f"combat log read failed: {exc}")
            if self.combat_log.vitals:
                payload["vitals"] = self.combat_log.vitals
            surname = self.identity and self.combat_log.surname_for(self.identity.get("name"))
            if surname:
                payload["identity"] = {**self.identity, "surname": surname}
            # The target's health, when the game keeps it secret from the addon.
            seen = target and not target.get("player") and target.get("pct") is None \
                and self.combat_log.foe_health(target.get("name"))
            # A dead one with that name is a previous kill, not a fresh target.
            if seen and (seen["health"] > 0 or target.get("dead")):
                target.update(health=seen["health"], healthMax=seen["healthMax"], healthAt=seen["t"])
        if self.quests_dirty:
            payload["quests"] = self.quests
        if self.gear_dirty:
            payload["gear"] = self.gear
        if self.professions_dirty:
            payload["professions"] = self.professions
        if self.jumps_dirty:
            payload["jumps"] = self.jumps
        if self.send(payload):
            self.events = []
            self.quests_dirty = False
            self.gear_dirty = False
            self.professions_dirty = False
            self.jumps_dirty = False
            self.last_heartbeat = now

    # --- main loop -----------------------------------------------------------

    def tick(self):
        now = time.time()
        # The settings file goes first: it holds the backend address.
        if now - self.last_file_check >= self.poll:
            self.last_file_check = now
            self.check_file()
            if self.pending and self.send(self.pending):
                self.pending = None
                self.last_heartbeat = now

        if self.live and now - self.game_checked >= self.GAME_CHECK:
            self.game_checked = now
            # Unknown (None) counts as running, so capture still works if the check fails.
            up = game_running() is not False
            if up != self.game_up:
                log("World of Warcraft started; watching the screen" if up
                    else "World of Warcraft closed; not watching the screen")
            if up and not self.game_up:
                self.game_up_since = now
            self.game_up = up
            # The game runs but the strip never decodes: say why that usually is.
            if up and now - max(self.game_up_since, self.frame_at) > 120:
                self.problem("WoW is running but the in-game strip can't be read: log in to a character, "
                             "keep WoW on the main screen and not minimised"
                             + (", and allow Screen Recording for ForeverTrackerCompanion in System "
                                "Settings > Privacy & Security" if MAC else ""))

        if self.live and self.game_up:
            try:
                self.read_strip(now)
            except Exception as exc:  # noqa: BLE001 - capture is best effort
                log(f"strip capture failed: {exc}")
                self.capture = None
            if self.is_live(now):
                self.post_live(now)

        if self.db is None or not self.sharing() or self.is_live(now):
            return
        if now - self.last_heartbeat < self.heartbeat_every:
            return
        running = game_running()
        snap = self.db.get("snapshot") or {}
        online = running is not False and snap.get("status") == "online"
        if online or self.was_running:
            if self.send({"kind": "presence", "online": online}):
                self.last_heartbeat = now
                self.was_running = online

    def run(self):
        log("Forever Tracker companion started")
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - keep the watcher alive
                log(f"error: {exc}")
            # Read the strip ~20 times a second while the game runs; otherwise
            # just look at the saved file now and then.
            time.sleep(self.CAPTURE_EVERY if self.live and self.game_up else self.poll)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--dump", action="store_true",
                    help="parse the SavedVariables file once, print the payload and exit")
    ap.add_argument("--setup", action="store_true", help="find the game folder again")
    ap.add_argument("--uninstall", action="store_true",
                    help="stop it, remove it from startup, delete the addon and its saved settings")
    ap.add_argument("--selftest", action="store_true",
                    help="check that the packaged app starts and can set up screen capture, then exit")
    args = ap.parse_args()

    if args.selftest:
        # Used by the release build (CI): no dialogs, no config, no network.
        print(f"Forever Tracker companion {addon_version(ADDON_SRC)} on {sys.platform}, frozen={FROZEN}")
        print(f"bundled addon: {sorted(os.listdir(ADDON_SRC))}")
        capture = strip.make_capture()
        print(f"capture: {type(capture).__name__}")
        try:
            print(f"one grab: {'strip found' if capture.grab() else 'no strip (expected without WoW)'}")
        except Exception as exc:  # noqa: BLE001 - no screen access in CI is fine
            print(f"one grab failed (expected without a screen): {exc}")
        return

    if args.uninstall:
        cfg = {}
        if os.path.isfile(args.config):
            with open(args.config, encoding="utf-8") as f:
                cfg = json.load(f)
        uninstall(cfg)
        return

    if not args.dump and not single_instance():
        message_box("Forever Tracker is already running in the background.")
        return
    if FROZEN and MAC and not mac_checks():
        return
    if args.setup or not os.path.isfile(args.config):
        cfg = first_run(args.config)
    else:
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
        # Installs before 0.6.4 were set up to upload every 2 s; the page now updates every second.
        if cfg.get("live_seconds") == 2:
            cfg["live_seconds"] = 1
            try:
                with open(args.config, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
                log("config: live uploads now every second")
            except OSError as exc:
                log(f"config: couldn't save live_seconds: {exc}")
    install_addon(cfg)
    autostart(cfg)

    if args.dump:
        path = find_saved_variables(cfg)
        if not path:
            sys.exit("ForeverTracker.lua not found; check wow_path / saved_variables")
        c = Companion(cfg)
        c.db = parse_saved_variables(path).get("ForeverTrackerDB") or {}
        print(json.dumps({"file": path, "settings": c.settings(),
                          "payload": c.build_payload(game_running())}, indent=2))
        return

    Companion(cfg).run()


if __name__ == "__main__":
    main()

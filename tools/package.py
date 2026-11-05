#!/usr/bin/env python3
"""Builds dist/ForeverTracker-<version>.zip to hand to friends (Windows).

Inside: ForeverTrackerCompanion.exe (Python and Pillow bundled, the addon
inside it; on first run it finds WoW, installs the addon and asks for the
friend's key), the ForeverTracker addon folder for installing by hand, and
README.txt.

Needs PyInstaller and Pillow in the Python that runs this:
  python -m pip install pyinstaller pillow
  python tools/package.py
"""

import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ADDON = os.path.join(ROOT, "addon", "ForeverTracker")
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
EXE = "ForeverTrackerCompanion"

README = """Forever Tracker - {version}
=========================

Shows your character live on {site} : level, XP, zone and map, quests,
gear and what you're fighting. Your friends can see you there.

You need the key you were given (one per person; don't share it).

Setup (once)
------------
1. Unzip this folder anywhere, e.g. your Documents.
2. Double-click ForeverTrackerCompanion.exe.
   - It finds your World of Warcraft folder (or asks for it).
   - It installs the Forever Tracker addon into WoW.
   - It asks for your key. Paste it and press Enter.
   Windows may warn about an unknown app: More info -> Run anyway.
3. Restart WoW. At character select, AddOns: make sure Forever Tracker is ticked.

Every time you play
-------------------
Start ForeverTrackerCompanion.exe and leave its window open while you play.
Your character shows up on {site} within a few seconds.

In game
-------
/ft opens the options. "Appear offline" hides you right away; untick
"Enable tracking" to stop completely. A tiny strip of coloured pixels in the
top-left corner carries the live data to the companion; leave it visible.
Turning on "Share health from the combat log" also shows your health.

Problems
--------
- "doesn't accept this key": check the key; run the exe with --setup, or
  delete config.json next to it, to enter it again.
- Nothing shows up: keep WoW on your main monitor and not minimised, and
  type /reload in game once.
"""


def version():
    with open(os.path.join(ADDON, "ForeverTracker.toc"), encoding="utf-8") as f:
        return re.search(r"^## Version:\s*(\S+)", f.read(), re.M).group(1)


def site():
    with open(os.path.join(ROOT, "companion", "ft_companion.py"), encoding="utf-8") as f:
        return re.search(r'^DEFAULT_BACKEND = "([^"]+)"', f.read(), re.M).group(1)


def main():
    ver = version()
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--onefile", "--noconfirm", "--clean", "--console",
        "--name", EXE,
        "--distpath", os.path.join(BUILD, "exe"), "--workpath", os.path.join(BUILD, "work"),
        "--specpath", BUILD,
        "--paths", os.path.join(ROOT, "companion"),
        "--add-data", f"{ADDON}{os.pathsep}addon/ForeverTracker",
        "--exclude-module", "Xlib", "--exclude-module", "tkinter",
        os.path.join(ROOT, "companion", "ft_companion.py"),
    ], check=True)

    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, f"ForeverTracker-{ver}.zip")
    top = f"ForeverTracker-{ver}"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(BUILD, "exe", EXE + ".exe"), f"{top}/{EXE}.exe")
        for name in sorted(os.listdir(ADDON)):
            z.write(os.path.join(ADDON, name), f"{top}/ForeverTracker/{name}")
        z.writestr(f"{top}/README.txt", README.format(version=ver, site=site()).replace("\n", "\r\n"))
    shutil.rmtree(os.path.join(BUILD, "work"), ignore_errors=True)
    print(f"\n{out} ({os.path.getsize(out) // 1024} KB)")


if __name__ == "__main__":
    main()

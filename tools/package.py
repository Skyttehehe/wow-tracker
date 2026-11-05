#!/usr/bin/env python3
"""Builds dist/ForeverTracker-<version>.zip to hand to friends (Windows).

The zip is a normal addon folder, ForeverTracker/, that goes into
Interface/AddOns. Next to the addon files it holds ForeverTrackerCompanion.exe
(windowless; Python and Pillow bundled) and README.txt. Double-clicked once,
the exe finds the game from its own location and starts with Windows from then
on; the friend enters their code in game with the minimap button.

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

README = """Forever Tracker {version}
=====================

Shows your character live on {site} : level, XP, zone and map, quests,
gear and what you're fighting.

Setup (once)
------------
1. Put this ForeverTracker folder in
      World of Warcraft\\_forever_\\Interface\\AddOns
   like any other addon.
2. In that folder, double-click ForeverTrackerCompanion.exe.
   Windows may warn about an unknown app: More info -> Run anyway.
   It says it's set up, then runs in the background and starts by itself
   with Windows from now on. You never need to open it again.
3. Start WoW (tick Forever Tracker under AddOns if it isn't), log in, click
   the Forever Tracker button on the minimap and paste the code you were given.

That's it: you show up on {site} within a few seconds whenever you play.

In game
-------
Minimap button: left-click to enter your code, right-click for options,
drag to move it. /ft minimap hides or shows it.
/ft opens the options: "Appear offline" hides you right away; untick
"Enable tracking" to stop completely. "Share health from the combat log"
also shows your health on the site.
A tiny strip of coloured pixels in the top-left corner carries the live data
to the companion; leave it visible.

Problems
--------
- Nothing shows up: keep WoW on your main monitor and not minimised, and
  check that the code is right (minimap button). companion.log in this
  folder says what's going on.
- To stop it starting with Windows: Task Manager -> Startup apps ->
  ForeverTracker -> Disable.
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
        sys.executable, "-m", "PyInstaller", "--onefile", "--noconfirm", "--clean", "--noconsole",
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
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(ADDON)):
            z.write(os.path.join(ADDON, name), f"ForeverTracker/{name}")
        z.write(os.path.join(BUILD, "exe", EXE + ".exe"), f"ForeverTracker/{EXE}.exe")
        z.writestr("ForeverTracker/README.txt", README.format(version=ver, site=site()).replace("\n", "\r\n"))
    shutil.rmtree(os.path.join(BUILD, "work"), ignore_errors=True)
    # The site's footer links to releases/latest/download/ForeverTracker.zip.
    shutil.copyfile(out, os.path.join(DIST, "ForeverTracker.zip"))
    print(f"\n{out} ({os.path.getsize(out) // 1024} KB)")
    print(f"Publish: gh release create v{ver} dist/ForeverTracker.zip {os.path.relpath(out, ROOT)}")


if __name__ == "__main__":
    main()

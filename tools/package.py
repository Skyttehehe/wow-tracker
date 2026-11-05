#!/usr/bin/env python3
"""Builds the package friends download: a normal addon folder, ForeverTracker/,
that goes into Interface/AddOns, with the companion app inside it.

  Windows: dist/ForeverTracker-<version>.zip (+ ForeverTracker.zip)
           ForeverTrackerCompanion.exe, windowless; Python and Pillow bundled.
  macOS:   dist/ForeverTracker-mac-<version>.zip (+ ForeverTracker-mac.zip)
           ForeverTrackerCompanion.app, no Dock icon; Python and pyobjc bundled.

Opened once, the app finds the game from its own location and starts at login
from then on; the friend enters their code in game with the minimap button.
Each platform has to be built on that platform; .github/workflows/release.yml
builds both for every GitHub release.

  Windows: python -m pip install pyinstaller pillow
  macOS:   python3 -m pip install pyinstaller pyobjc-framework-Quartz
  then:    python tools/package.py
"""

import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ADDON = os.path.join(ROOT, "addon", "ForeverTracker")
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
EXE = "ForeverTrackerCompanion"
MAC = sys.platform == "darwin"

README_WINDOWS = """Forever Tracker {version}
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

Uninstall
---------
Close WoW, then either open Windows Settings -> Apps -> Installed apps ->
Forever Tracker -> Uninstall, or double-click "Uninstall Forever Tracker.bat"
in this folder. It stops the companion, takes it out of Windows startup and
deletes this folder and the addon's saved settings (including your code).
"""

README_MAC = """Forever Tracker {version} for Mac
=============================

Shows your character live on {site} : level, XP, zone and map, quests,
gear and what you're fighting.

Setup (once)
------------
1. Put this ForeverTracker folder in
      World of Warcraft/_forever_/Interface/AddOns
   like any other addon (usually in Applications).
2. In that folder, right-click ForeverTrackerCompanion and choose Open, then
   Open again. (macOS blocks apps from unknown developers on a plain
   double-click; this is needed only the first time.)
3. macOS asks to allow Screen Recording: open System Settings > Privacy &
   Security > Screen Recording, turn on ForeverTrackerCompanion, and open it
   again. It reads your character from a tiny strip in the game window.
   It then runs in the background (no Dock icon) and starts by itself when
   you log in. You never need to open it again.
4. Start WoW (tick Forever Tracker under AddOns if it isn't), log in, click
   the Forever Tracker button on the minimap and paste the code you were given.

That's it: you show up on {site} within a few seconds whenever you play.

In game
-------
Minimap button: left-click to enter your code, right-click for options,
drag to move it. /ft minimap hides or shows it. /ft opens the options.
A tiny strip of coloured pixels in the top-left corner of the game window
carries the live data to the companion; leave it visible.

Problems
--------
- Nothing shows up: check Screen Recording (step 3) and the code (minimap
  button). The log is in ~/Library/Application Support/ForeverTracker.
- "Temporary copy" message: move the ForeverTracker folder out of AddOns and
  back in with Finder, then open the app again.

Uninstall
---------
Close WoW, then right-click "Uninstall Forever Tracker.command" in this folder
and choose Open. It stops the companion, removes it from login items and
deletes this folder and the addon's saved settings (including your code).
"""

UNINSTALL_BAT = '@start "" "%~dp0ForeverTrackerCompanion.exe" --uninstall\r\n'
UNINSTALL_COMMAND = """#!/bin/sh
# Removes Forever Tracker (asks first).
"$(dirname "$0")/ForeverTrackerCompanion.app/Contents/MacOS/ForeverTrackerCompanion" --uninstall
"""


def version():
    with open(os.path.join(ADDON, "ForeverTracker.toc"), encoding="utf-8") as f:
        return re.search(r"^## Version:\s*(\S+)", f.read(), re.M).group(1)


def site():
    with open(os.path.join(ROOT, "companion", "ft_companion.py"), encoding="utf-8") as f:
        return re.search(r'^DEFAULT_BACKEND = "([^"]+)"', f.read(), re.M).group(1)


def pyinstaller(*extra):
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noconsole",
        "--name", EXE,
        "--distpath", os.path.join(BUILD, "exe"), "--workpath", os.path.join(BUILD, "work"),
        "--specpath", BUILD,
        "--paths", os.path.join(ROOT, "companion"),
        "--add-data", f"{ADDON}{os.pathsep}addon/ForeverTracker",
        "--exclude-module", "Xlib", "--exclude-module", "tkinter",
        *extra,
        os.path.join(ROOT, "companion", "ft_companion.py"),
    ], check=True)
    shutil.rmtree(os.path.join(BUILD, "work"), ignore_errors=True)


def build_windows(ver):
    pyinstaller("--onefile")
    out = os.path.join(DIST, f"ForeverTracker-{ver}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(ADDON)):
            z.write(os.path.join(ADDON, name), f"ForeverTracker/{name}")
        z.write(os.path.join(BUILD, "exe", EXE + ".exe"), f"ForeverTracker/{EXE}.exe")
        z.writestr("ForeverTracker/README.txt", README_WINDOWS.format(version=ver, site=site()).replace("\n", "\r\n"))
        z.writestr("ForeverTracker/Uninstall Forever Tracker.bat", UNINSTALL_BAT)
    # The site's footer links to releases/latest/download/ForeverTracker.zip.
    shutil.copyfile(out, os.path.join(DIST, "ForeverTracker.zip"))
    return out


def build_mac(ver):
    pyinstaller("--windowed", "--osx-bundle-identifier", "com.forevertracker.companion",
                "--hidden-import", "Quartz")
    app = os.path.join(BUILD, "exe", EXE + ".app")
    # A background app: no Dock icon, no menu bar.
    info = os.path.join(app, "Contents", "Info.plist")
    with open(info, "rb") as f:
        plist = plistlib.load(f)
    plist.update(LSUIElement=True, CFBundleShortVersionString=ver, CFBundleVersion=ver)
    with open(info, "wb") as f:
        plistlib.dump(plist, f)
    # Editing Info.plist breaks PyInstaller's ad-hoc signature; Apple Silicon needs one.
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app], check=True)

    folder = os.path.join(BUILD, "mac", "ForeverTracker")
    shutil.rmtree(os.path.dirname(folder), ignore_errors=True)
    shutil.copytree(ADDON, folder)
    subprocess.run(["ditto", app, os.path.join(folder, EXE + ".app")], check=True)  # keeps links and signature
    with open(os.path.join(folder, "README.txt"), "w", encoding="utf-8") as f:
        f.write(README_MAC.format(version=ver, site=site()))
    command = os.path.join(folder, "Uninstall Forever Tracker.command")
    with open(command, "w", encoding="utf-8") as f:
        f.write(UNINSTALL_COMMAND)
    os.chmod(command, os.stat(command).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    out = os.path.join(DIST, f"ForeverTracker-mac-{ver}.zip")
    if os.path.exists(out):
        os.remove(out)
    subprocess.run(["ditto", "-c", "-k", "--keepParent", folder, out], check=True)
    shutil.copyfile(out, os.path.join(DIST, "ForeverTracker-mac.zip"))
    return out


def main():
    ver = version()
    os.makedirs(DIST, exist_ok=True)
    out = build_mac(ver) if MAC else build_windows(ver)
    print(f"\n{out} ({os.path.getsize(out) // 1024} KB)")


if __name__ == "__main__":
    main()

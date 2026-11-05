"""Follows the game's own combat log (Logs/WoWCombatLog*.txt, advanced
logging on) for the player's current health and power.

The client writes an "advanced" block on most lines describing one unit:
    infoGUID, ownerGUID, currentHP, maxHP, attackPower, spellPower, armor,
    3 more fields, powerType, currentPower, maxPower, powerCost, x, y, ...
Only lines whose unit is the player's own character are used; the log also
contains everyone nearby, and none of that is passed on.
"""

import csv
import glob
import os
import re
import time
from datetime import datetime

# Unit flags: COMBATLOG_OBJECT_AFFILIATION_MINE | COMBATLOG_OBJECT_TYPE_PLAYER
MINE, PLAYER = 0x1, 0x400
GUID = re.compile(r"^[A-Za-z]+-[0-9A-F]+(-[0-9A-F]+)*$")
NO_OWNER = "0000000000000000"
POWER_TYPES = {"0": "MANA", "1": "RAGE", "2": "FOCUS", "3": "ENERGY"}
BACKFILL = 64 * 1024  # bytes read from the end of a log when first opened


def find_log(wow_path):
    logs = glob.glob(os.path.join(wow_path, "_*_", "Logs", "WoWCombatLog*.txt"))
    return max(logs, key=os.path.getmtime) if logs else None


def parse_time(stamp):
    """'11/5/2026 15:48:06.5351' (local time) -> epoch seconds."""
    try:
        date, clock = stamp.split(" ")
        dt = datetime.strptime(f"{date} {clock[:15]}", "%m/%d/%Y %H:%M:%S.%f")
        return dt.timestamp()
    except ValueError:
        return None


class CombatLogTail:
    def __init__(self, wow_path):
        self.wow_path = wow_path
        self.path = None
        self.offset = 0
        self.partial = b""
        self.me = None          # player GUID, learned from "mine + player" source flags
        self.vitals = None      # latest {"t", "health", "healthMax", "power": {...}}
        self.next_check = 0

    def poll(self):
        """Reads whatever the game appended since the last call."""
        now = time.monotonic()
        if now < self.next_check:
            return
        self.next_check = now + 0.5
        path = find_log(self.wow_path)
        if not path:
            return
        size = os.path.getsize(path)
        if path != self.path or size < self.offset:
            self.path, self.partial, self.me = path, b"", None
            self.offset = max(0, size - BACKFILL)
            skip_first = self.offset > 0
        else:
            skip_first = False
        if size == self.offset:
            return
        with open(path, "rb") as f:
            f.seek(self.offset)
            data = f.read(size - self.offset)
        self.offset = size
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()  # last piece may be an incomplete line
        if skip_first and lines:
            lines.pop(0)            # we started mid-line
        for raw in lines:
            self.parse(raw.decode("utf-8", "replace").rstrip("\r"))

    def parse(self, line):
        stamp, sep, body = line.partition("  ")
        if not sep:
            return
        try:
            fields = next(csv.reader([body]))
        except (csv.Error, StopIteration):
            return
        if len(fields) < 4:
            return
        # Learn our GUID from a line where we're the source.
        if self.me is None and fields[1].startswith("Player-"):
            try:
                flags = int(fields[3], 16)
            except ValueError:
                flags = 0
            if flags & MINE and flags & PLAYER:
                self.me = fields[1]
        if self.me is None:
            return
        for i in range(1, len(fields) - 13):
            if fields[i] != self.me or not (fields[i + 1] == NO_OWNER or GUID.match(fields[i + 1])):
                continue
            try:
                hp, hp_max = int(fields[i + 2]), int(fields[i + 3])
                ptype, power, power_max = fields[i + 10], int(fields[i + 11]), int(fields[i + 12])
            except ValueError:
                continue
            t = parse_time(stamp)
            if t is None or hp_max <= 0:
                return
            if self.vitals is None or t >= self.vitals["t"]:
                self.vitals = {
                    "t": round(t, 2), "health": hp, "healthMax": hp_max,
                    "power": {"type": POWER_TYPES.get(ptype), "cur": power, "max": power_max},
                }
            return

#!/usr/bin/env python3
"""Forever Tracker backend.

- POST /api/ingest       companion uploads (bearer key; LAN only unless on Vercel)
- GET  /api/characters   every tracked character, online ones first
- GET  /api/state?c=     latest snapshot + online status of one character
- GET  /api/history?c=   its position trail, XP samples, event log
- GET  /api/stream?c=    Server-Sent Events; fires "update" after each ingest
- /api/admin/keys        friends' upload keys (FT_TOKEN only): GET list, POST
                         {"name"} to create, DELETE ?id= to revoke
- DELETE /api/admin/characters?c=   take a character off the site
- everything else        static frontend

Each character's data is kept apart. FT_TOKEN is the owner's key; friends get
their own codes from FRIEND_KEYS (JSON, e.g. {"Bob": "code1", "Alice": "code2"})
or from /api/admin/keys, and a character belongs to the code that first uploaded it.

Standard library only. Configure with environment variables (see README).
Runs as its own server (SQLite) or as a Vercel function (api/index.py, Redis).
"""

import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import urllib.request
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("FT_TOKEN", "")
DB_PATH = os.environ.get("FT_DB", os.path.join(HERE, "tracker.sqlite3"))
HOST = os.environ.get("FT_HOST", "0.0.0.0")
PORT = int(os.environ.get("FT_PORT", "8787"))
STATIC = os.path.realpath(os.environ.get("FT_STATIC", os.path.join(HERE, "..", "frontend")))
INGEST_NETS = [ipaddress.ip_network(n.strip()) for n in os.environ.get(
    "FT_INGEST_NETS", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8").split(",")]
# On Vercel (or with FT_PUBLIC_INGEST=1) uploads come over the internet and the token is
# the only check. Vercel functions end after each request, so there is no live stream.
SERVERLESS = bool(os.environ.get("VERCEL"))
PUBLIC_INGEST = SERVERLESS or os.environ.get("FT_PUBLIC_INGEST") == "1"


def friend_codes():
    """FRIEND_KEYS: JSON {"Bob": "code", ...} or [{"name": "Bob", "code": "..."}].
    Read case-insensitively, so Friend_Keys works too."""
    raw = next((v for k, v in os.environ.items() if k.upper() in ("FRIEND_KEYS", "FRIEND_KEY")), "")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        print("FRIEND_KEYS is not valid JSON; ignoring it", flush=True)
        return {}
    if isinstance(data, list):
        data = {d.get("name"): d.get("code") for d in data if isinstance(d, dict)}
    if not isinstance(data, dict):
        return {}
    return {str(name): str(code) for name, code in data.items() if name and code}


FRIENDS = friend_codes()
PRESENCE_TIMEOUT = 120   # seconds without a heartbeat before showing offline
MAX_BODY = 2 * 1024 * 1024

# Only these snapshot fields are stored and published.
SNAPSHOT_FIELDS = {
    "t", "status", "name", "surname", "realm", "faction", "race", "class", "classToken", "sex",
    "level", "maxLevel", "xp", "xpMax", "rested", "health", "healthMax", "healthPct", "power",
    "money", "guild", "dead", "ghost", "resting", "combat", "zone", "subzone",
    "mapID", "map", "x", "y", "vitalsAt", "vitalsAssumed",
}
EVENT_FIELDS = {"t", "type", "level", "zone", "subzone", "x", "y", "mapID",
                "questID", "title", "xp", "money"}
IDENTITY_FIELDS = {"name", "surname", "realm", "faction", "race", "class", "classToken", "sex", "guild", "maxLevel"}
ZONE_FIELDS = {"zone", "subzone", "map"}
LIVE_FIELDS = {"level", "xp", "xpMax", "rested", "health", "healthMax", "healthPct", "money", "mapID", "x", "y"}
LIVE_STALE = 10            # seconds; a live frame newer than this beats a file snapshot
FULL_AFTER = int(os.environ.get("FT_FULL_AFTER", "30"))  # seconds out of combat before vitals count as full
POSITION_EVERY = 5         # seconds between stored trail points from live data
XP_EVERY = 30
MAX_QUESTS = 60


def clean_quests(quests):
    """Keeps only the expected quest fields and sane sizes."""
    out = []
    for q in (quests if isinstance(quests, list) else [])[:MAX_QUESTS]:
        if not isinstance(q, dict) or not isinstance(q.get("title"), str):
            continue
        objectives = []
        for o in (q.get("objectives") if isinstance(q.get("objectives"), list) else [])[:12]:
            if isinstance(o, dict) and isinstance(o.get("text"), str):
                objectives.append({"text": o["text"][:200], "done": bool(o.get("done")),
                                   "have": o.get("have") if isinstance(o.get("have"), int) else None,
                                   "need": o.get("need") if isinstance(o.get("need"), int) else None})
        out.append({
            "id": q.get("id") if isinstance(q.get("id"), int) else None,
            "title": q["title"][:200],
            "level": q.get("level") if isinstance(q.get("level"), int) else None,
            "header": q.get("header") if isinstance(q.get("header"), str) else None,
            "complete": bool(q.get("complete")),
            "failed": bool(q.get("failed")),
            "objectives": objectives,
        })
    return out


SUMMARY_FIELDS = ("name", "surname", "realm", "faction", "race", "sex", "class", "classToken", "level",
                  "maxLevel", "zone")
CHARACTER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def slugify(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def character_slug(who):
    """'Fractured' on 'CamWoW EU4 Realm 6 PvP' -> 'fractured-camwow-eu4-realm-6-pvp'."""
    name = who.get("name") if isinstance(who, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise ValueError("no character name")
    base = slugify(name) or hashlib.sha256(name.encode()).hexdigest()[:10]
    realm = slugify(who.get("realm") or "")
    return (f"{base}-{realm}" if realm else base)[:64].strip("-")


def payload_character(payload):
    for who in (payload.get("character"), payload.get("identity"), payload.get("snapshot")):
        if isinstance(who, dict) and who.get("name"):
            return character_slug(who)
    raise ValueError("upload without a character name; update the companion")


def summarize(snap, presence):
    """The character list's entry: who it is and whether it's online."""
    out = {k: snap.get(k) for k in SUMMARY_FIELDS}
    out["online"] = bool(presence.get("online"))
    out["presenceAt"] = presence.get("t") or 0
    return out


TARGET_RANKS = {"elite", "rare", "rareelite", "worldboss"}


def clean_target(t):
    """What the player is fighting. Other players never carry a name."""
    if not isinstance(t, dict) or not t:
        return None

    def num(k, lo, hi):
        v = t.get(k)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi else None

    player = t.get("player") is True
    text = lambda k, n: t[k][:n] if isinstance(t.get(k), str) else None
    return {
        "player": player,
        "name": None if player else text("name", 80),
        "class": text("class", 30) if player else None,
        "level": num("level", -1, 1000),
        "rank": t.get("rank") if t.get("rank") in TARGET_RANKS else None,
        "pct": num("pct", 0, 100),
        "health": num("health", 0, 10**9),
        "healthMax": num("healthMax", 1, 10**9),
        "healthAt": num("healthAt", 0, 10**10),
        "dead": t.get("dead") is True,
    }


GEAR_SLOTS = set(range(1, 20))


def clean_gear(items):
    """Keeps only the expected item fields, one item per equipment slot."""
    out = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or it.get("slot") not in GEAR_SLOTS:
            continue
        num = lambda k: it.get(k) if isinstance(it.get(k), int) and not isinstance(it.get(k), bool) else None
        out[it["slot"]] = {
            "slot": it["slot"],
            "id": num("id"),
            "name": it["name"][:100] if isinstance(it.get("name"), str) else None,
            "quality": num("quality"),
            "ilvl": num("ilvl"),
        }
    return list(out.values())


# --- storage -----------------------------------------------------------------
#
# Store holds the tracking logic; subclasses only store and fetch rows.
# SqliteStore runs on your own server. RedisStore runs on Vercel, where no disk
# outlives a request, against Upstash Redis over its REST API.

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.live_version = 0
        self.history_version = 0
        self.changed = threading.Condition()

    # Storage primitives. get/put/insert_* are only called inside transaction().
    def transaction(self):
        raise NotImplementedError

    def get(self, key):
        raise NotImplementedError

    def put(self, key, value):
        raise NotImplementedError

    def insert_events(self, rows):
        """rows: (t, type, data_json). Returns how many were new."""
        raise NotImplementedError

    def insert_positions(self, rows):
        """rows: (t, mapID, map, x, y)."""
        raise NotImplementedError

    def insert_xp(self, rows):
        """rows: (t, level, xp, xpMax)."""
        raise NotImplementedError

    def read_history(self, limit_positions, limit_events):
        """Returns (positions oldest first, all xp oldest first, event data_json newest first)."""
        raise NotImplementedError

    # Logic.
    def notify(self, history=False):
        with self.changed:
            self.live_version += 1
            if history:
                self.history_version += 1
            self.changed.notify_all()

    def add_events(self, events):
        rows = []
        for e in events or []:
            if isinstance(e, dict) and isinstance(e.get("t"), int) and e.get("type"):
                data = {k: v for k, v in e.items() if k in EVENT_FIELDS}
                rows.append((e["t"], str(e["type"]), json.dumps(data, sort_keys=True)))
        return self.insert_events(rows) if rows else 0

    def ingest_live(self, payload, now, sampler):
        frame = payload.get("frame") or {}
        flags = frame.get("flags") or {}
        snap = self.get("snapshot") or {}
        for part, fields in ((payload.get("identity"), IDENTITY_FIELDS),
                             (payload.get("zone"), ZONE_FIELDS)):
            if isinstance(part, dict):
                snap.update({k: v for k, v in part.items() if k in fields})
        # Missing (secret) values become None so the page shows them as hidden.
        for k in LIVE_FIELDS:
            snap[k] = frame.get(k)
        power = frame.get("power")
        if isinstance(power, dict):
            snap["power"] = {k: power.get(k) for k in ("type", "cur", "max", "pct")}
        # The combat log fills in values the addon can't read (secret).
        vitals = payload.get("vitals")
        snap["vitalsAt"] = None
        if isinstance(vitals, dict) and isinstance(vitals.get("t"), (int, float)):
            if snap.get("health") is None and isinstance(vitals.get("health"), int):
                snap["health"], snap["healthMax"] = vitals["health"], vitals.get("healthMax")
                snap["vitalsAt"] = vitals["t"]
            vp = vitals.get("power") if isinstance(vitals.get("power"), dict) else {}
            power = snap.get("power") or {}
            if power.get("cur") is None and isinstance(vp.get("cur"), int) and vp.get("type") == power.get("type"):
                snap["power"] = {**power, "cur": vp["cur"], "max": vp.get("max") or power.get("max")}
                snap["vitalsAt"] = vitals["t"]
        for k in ("dead", "ghost", "combat", "resting"):
            snap[k] = bool(flags.get(k))
        snap["target"] = clean_target(payload.get("target"))
        self.assume_full(snap, now, frame, sampler)
        snap["status"] = "offline" if flags.get("leaving") else "online"
        snap["t"] = frame.get("t") or now

        history = self.add_events(payload.get("events")) > 0
        if "quests" in payload:
            self.put("quests", {"t": now, "list": clean_quests(payload["quests"])})
            history = True
        if "gear" in payload:
            self.put("gear", {"t": now, "items": clean_gear(payload["gear"])})
        if snap.get("name"):
            self.put("snapshot", snap)
            self.put("updatedAt", now)
            self.put("liveAt", now)
            history |= self.sample_live(snap, now, sampler)
        self.put("presence", {"online": snap["status"] == "online", "t": now})
        return history

    def assume_full(self, snap, now, frame, sampler):
        """After FULL_AFTER seconds out of combat with no newer combat-log
        reading, health and power have regenerated: show them as full (rage
        as empty)."""
        snap["vitalsAssumed"] = False
        if snap["combat"] or sampler.get("combatEndedAt") is None:
            sampler["combatEndedAt"] = None if snap["combat"] else now
            return
        if snap["dead"] or snap["ghost"] or now - sampler["combatEndedAt"] < FULL_AFTER:
            return
        if snap["vitalsAt"] and now - snap["vitalsAt"] < FULL_AFTER:
            return
        power = snap.get("power") or {}
        if power.get("max") and (frame.get("power") or {}).get("cur") is None:
            snap["power"] = {**power, "cur": 0 if power.get("type") == "RAGE" else power["max"]}
            snap["vitalsAssumed"] = True
        if snap.get("healthMax") and frame.get("health") is None:
            snap["health"] = snap["healthMax"]
            snap["vitalsAssumed"] = True
        if snap["vitalsAssumed"]:
            snap["vitalsAt"] = None

    def sample_live(self, s, now, sampler):
        """Adds trail and XP points from live data at a modest rate."""
        added = False
        if s.get("x") is not None and s.get("mapID"):
            last = sampler.get("position")
            moved = (not last or last[1] != s["mapID"]
                     or abs(last[2] - s["x"]) > 0.002 or abs(last[3] - s["y"]) > 0.002)
            if moved and (not last or now - last[0] >= POSITION_EVERY):
                self.insert_positions([(now, s["mapID"], s.get("map"), s["x"], s["y"])])
                sampler["position"] = [now, s["mapID"], s["x"], s["y"]]
                added = True
        if s.get("xp") is not None and s.get("xpMax"):
            last = sampler.get("xp")
            key = [s["level"], s["xp"]]
            if not last or (last[1] != key and (key[0] != last[1][0] or now - last[0] >= XP_EVERY)):
                self.insert_xp([(now, s["level"], s["xp"], s["xpMax"])])
                sampler["xp"] = [now, key]
                added = True
        return added

    def ingest(self, payload):
        kind = payload.get("kind")
        now = int(time.time())
        history = False
        with self.transaction():
            # Sampling state lives in storage: on Vercel each request may be a fresh process.
            sampler = self.get("sampler") or {}
            if kind == "presence":
                self.put("presence", {"online": bool(payload.get("online")), "t": now})
            elif kind == "live":
                history = self.ingest_live(payload, now, sampler)
                self.put("sampler", sampler)
            elif kind == "snapshot":
                snap = {k: v for k, v in (payload.get("snapshot") or {}).items()
                        if k in SNAPSHOT_FIELDS}
                if not snap.get("name"):
                    raise ValueError("snapshot without character name")
                # The saved file has no surname; keep the one live data gave us.
                old = self.get("snapshot") or {}
                if old.get("name") == snap["name"] and old.get("surname"):
                    snap["surname"] = old["surname"]
                # While live data flows it is fresher than any saved file.
                live = now - (self.get("liveAt") or 0) < LIVE_STALE
                if not live:
                    self.put("snapshot", snap)
                    self.put("updatedAt", now)
                    online = snap.get("status") == "online" and payload.get("gameRunning") is not False
                    self.put("presence", {"online": online, "t": now})
                    if payload.get("quests") is not None:
                        self.put("quests", {"t": now, "list": clean_quests(payload["quests"])})
                    if payload.get("gear") is not None:
                        self.put("gear", {"t": now, "items": clean_gear(payload["gear"])})
                history = True
                self.add_events(payload.get("events"))
                self.insert_positions([
                    (p["t"], p.get("mapID"), p.get("map"), p.get("x"), p.get("y"))
                    for p in payload.get("positions") or []
                    if isinstance(p, dict) and isinstance(p.get("t"), int) and "x" in p])
                self.insert_xp([
                    (s["t"], s.get("level"), s.get("xp"), s.get("xpMax"))
                    for s in payload.get("xp") or []
                    if isinstance(s, dict) and isinstance(s.get("t"), int)])
            else:
                raise ValueError(f"unknown kind {kind!r}")
            if history:
                # Tells polling pages when to refetch /api/history.
                self.put("historyAt", now)
            summary = summarize(self.get("snapshot") or {}, self.get("presence") or {})
        self.notify(history)
        return summary

    def state(self, quests=True):
        with self.transaction():
            presence = self.get("presence") or {"online": False, "t": 0}
            snapshot = self.get("snapshot")
            updated = self.get("updatedAt")
            live_at = self.get("liveAt") or 0
            history_at = self.get("historyAt")
            quest_state = self.get("quests") if quests else None
            gear = self.get("gear") if quests else None
        now = int(time.time())
        online = presence["online"] and now - presence["t"] < PRESENCE_TIMEOUT
        state = {
            "serverTime": now,
            "online": online,
            "live": online and now - live_at < LIVE_STALE,
            "presenceAt": presence["t"],
            "updatedAt": updated,
            "historyAt": history_at,
            # Without a stream (Vercel) the page polls instead.
            "stream": not SERVERLESS,
            "snapshot": snapshot,
        }
        if quests:
            state["quests"] = quest_state
            state["gear"] = gear
        return state

    def history(self, limit_positions=500, limit_events=150):
        positions, xp, events = self.read_history(limit_positions, limit_events)
        return {
            "positions": [dict(zip(("t", "mapID", "map", "x", "y"), r)) for r in positions],
            "xp": [dict(zip(("t", "level", "xp", "xpMax"), r)) for r in xp],
            "events": [json.loads(e) for e in events],
        }


class SqliteStore(Store):
    def __init__(self, path):
        super().__init__()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                t INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY (t, type, data));
            CREATE TABLE IF NOT EXISTS positions (
                t INTEGER PRIMARY KEY, mapID INTEGER, map TEXT, x REAL, y REAL);
            CREATE TABLE IF NOT EXISTS xp (
                t INTEGER PRIMARY KEY, level INTEGER, xp INTEGER, xpMax INTEGER);
        """)

    @contextmanager
    def transaction(self):
        with self.lock, self.conn:
            yield

    def get(self, key):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))

    def insert_events(self, rows):
        return sum(self.conn.execute("INSERT OR IGNORE INTO events VALUES (?, ?, ?)", r).rowcount
                   for r in rows)

    def insert_positions(self, rows):
        self.conn.executemany("INSERT OR IGNORE INTO positions VALUES (?, ?, ?, ?, ?)", rows)

    def insert_xp(self, rows):
        self.conn.executemany("INSERT OR IGNORE INTO xp VALUES (?, ?, ?, ?)", rows)

    def read_history(self, limit_positions, limit_events):
        with self.lock:
            positions = self.conn.execute(
                "SELECT t, mapID, map, x, y FROM positions ORDER BY t DESC LIMIT ?",
                (limit_positions,)).fetchall()
            xp = self.conn.execute("SELECT t, level, xp, xpMax FROM xp ORDER BY t").fetchall()
            events = self.conn.execute(
                "SELECT data FROM events ORDER BY t DESC LIMIT ?", (limit_events,)).fetchall()
        return list(reversed(positions)), xp, [r[0] for r in events]

    def move_into(self, dest):
        """Copies everything into `dest` (an empty store) and empties this one."""
        with self.lock:
            self.conn.commit()
            self.conn.backup(dest.conn)
            with self.conn:
                for table in ("kv", "events", "positions", "xp"):
                    self.conn.execute(f"DELETE FROM {table}")


class RedisStore(Store):
    """Upstash Redis through its REST API: one read and one write round trip per upload.

    Keys under a prefix (ft:c:<character>: or ft:root:): kv (hash of JSON
    values), events / positions / xp (sorted sets scored by time; members are
    JSON, so identical rows are stored once)."""

    def __init__(self, url, token, prefix="ft:"):
        super().__init__()
        self.url, self.token = url.rstrip("/"), token
        self.KV, self.EVENTS, self.POSITIONS, self.XP = (
            prefix + k for k in ("kv", "events", "positions", "xp"))
        # Older rows beyond this are dropped.
        self.KEEP = {self.EVENTS: 2000, self.POSITIONS: 20000, self.XP: 20000}
        self.kv = None
        self.dirty = {}
        self.writes = []

    def pipeline(self, commands):
        req = urllib.request.Request(
            self.url + "/pipeline", data=json.dumps(commands).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            replies = json.loads(resp.read())
        for r in replies:
            if "error" in r:
                raise RuntimeError(f"redis: {r['error']}")
        return [r.get("result") for r in replies]

    @contextmanager
    def transaction(self):
        with self.lock:
            flat = self.pipeline([["HGETALL", self.KV]])[0] or []
            self.kv = dict(zip(flat[::2], flat[1::2]))
            self.dirty, self.writes = {}, []
            try:
                yield
                commands = list(self.writes)
                if self.dirty:
                    commands.append(["HSET", self.KV, *[x for kv in self.dirty.items() for x in kv]])
                if commands:
                    self.pipeline(commands)
            finally:
                self.kv, self.dirty, self.writes = None, {}, []

    def get(self, key):
        value = self.kv.get(key)
        return json.loads(value) if value is not None else None

    def put(self, key, value):
        self.kv[key] = self.dirty[key] = json.dumps(value)

    def add_rows(self, key, rows, member):
        if not rows:
            return 0
        self.writes.append(["ZADD", key, "NX", *[x for r in rows for x in (r[0], member(r))]])
        self.writes.append(["ZREMRANGEBYRANK", key, 0, -self.KEEP[key] - 1])
        return len(rows)  # an upper bound; Redis ignores rows it already has

    def insert_events(self, rows):
        return self.add_rows(self.EVENTS, rows, lambda r: r[2])

    def insert_positions(self, rows):
        return self.add_rows(self.POSITIONS, rows, lambda r: json.dumps(list(r)))

    def insert_xp(self, rows):
        return self.add_rows(self.XP, rows, lambda r: json.dumps(list(r)))

    def read_history(self, limit_positions, limit_events):
        positions, xp, events = self.pipeline([
            ["ZRANGE", self.POSITIONS, -limit_positions, -1],
            ["ZRANGE", self.XP, 0, -1],
            ["ZREVRANGE", self.EVENTS, 0, limit_events - 1],
        ])
        return ([json.loads(p) for p in positions or []], [json.loads(x) for x in xp or []],
                events or [])

    def move_into(self, dest):
        """Renames this store's keys to `dest`'s (an empty store)."""
        pairs = [(self.KV, dest.KV), (self.EVENTS, dest.EVENTS),
                 (self.POSITIONS, dest.POSITIONS), (self.XP, dest.XP)]
        exists = self.pipeline([["EXISTS", a] for a, _ in pairs])
        renames = [["RENAME", a, b] for (a, b), e in zip(pairs, exists) if e]
        if renames:
            self.pipeline(renames)


class NotYours(Exception):
    """The character belongs to another key."""


class Tracker:
    """Every character: a root store (keys, owners, character list) plus one
    store per character, opened on demand."""

    def __init__(self, open_store, legacy=None):
        self.open_store = open_store
        self.root = open_store(None)
        self.stores = {}
        self.lock = threading.Lock()
        try:
            self.migrate(legacy)
        except RuntimeError as exc:  # e.g. another instance migrated first; retried next start
            print(f"migration skipped: {exc}", flush=True)

    def store(self, char):
        with self.lock:
            if char not in self.stores:
                self.stores[char] = self.open_store(char)
            return self.stores[char]

    def migrate(self, legacy):
        """Moves single-character data (before characters were kept apart) into
        that character, owned by FT_TOKEN. Runs once."""
        with self.root.transaction():
            if self.root.get("migrated"):
                return
            snap = self.root.get("snapshot") if legacy is None else None
        if legacy is not None:
            with legacy.transaction():
                snap = legacy.get("snapshot")
        char = character_slug(snap) if snap and snap.get("name") else None
        summary = None
        if char:
            dest = self.store(char)
            (legacy or self.root).move_into(dest)
            with dest.transaction():
                summary = summarize(dest.get("snapshot") or {}, dest.get("presence") or {})
        with self.root.transaction():
            if char:
                self.root.put("owners", {**(self.root.get("owners") or {}), char: "owner"})
                self.root.put("index", {**(self.root.get("index") or {}), char: summary})
            self.root.put("migrated", int(time.time()))

    # Keys -------------------------------------------------------------------
    def key_id(self, token):
        """'owner' for FT_TOKEN, a friend's key id, or None. Inside a root transaction."""
        if not token:
            return None
        if TOKEN and hmac.compare_digest(token.encode(), TOKEN.encode()):
            return "owner"
        for name, code in FRIENDS.items():
            if hmac.compare_digest(token.encode(), code.encode()):
                return f"friend:{name}"
        digest = hashlib.sha256(token.encode()).hexdigest()
        return digest[:12] if digest in (self.root.get("keys") or {}) else None

    def add_key(self, name):
        token = secrets.token_hex(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.root.transaction():
            keys = self.root.get("keys") or {}
            keys[digest] = {"name": name, "t": int(time.time())}
            self.root.put("keys", keys)
        return {"id": digest[:12], "name": name, "token": token}

    def list_keys(self):
        with self.root.transaction():
            keys, owners = self.root.get("keys") or {}, self.root.get("owners") or {}
        out = [{"id": "owner", "name": "FT_TOKEN (you)", "t": None}]
        out += [{"id": f"friend:{n}", "name": f"{n} (FRIEND_KEYS)", "t": None} for n in FRIENDS]
        out += [{"id": d[:12], "name": k["name"], "t": k["t"]} for d, k in keys.items()]
        for k in out:
            k["characters"] = sorted(c for c, o in owners.items() if o == k["id"])
        return out

    def remove_key(self, kid):
        with self.root.transaction():
            keys = self.root.get("keys") or {}
            kept = {d: k for d, k in keys.items() if d[:12] != kid}
            self.root.put("keys", kept)
        return len(kept) < len(keys)

    # Characters -------------------------------------------------------------
    def ingest(self, token, payload):
        char = payload_character(payload)
        with self.root.transaction():
            kid = self.key_id(token)
            if kid is None:
                raise PermissionError("bad key")
            owners = self.root.get("owners") or {}
            owner = owners.get(char)
            if owner is None:
                self.root.put("owners", {**owners, char: kid})
            elif owner != kid and kid != "owner":
                raise NotYours(f"{char} is uploaded by someone else's key")
            summary = self.store(char).ingest(payload)
            # The list only needs rewriting when something on it changed.
            index = self.root.get("index") or {}
            old = index.get(char)
            if (old is None or any(old.get(k) != summary.get(k) for k in SUMMARY_FIELDS + ("online",))
                    or summary["presenceAt"] - (old.get("presenceAt") or 0) >= 60):
                self.root.put("index", {**index, char: summary})

    def characters(self):
        with self.root.transaction():
            index = self.root.get("index") or {}
        now = int(time.time())
        out = []
        for char, s in index.items():
            if not s or not s.get("name"):
                continue
            online = bool(s.get("online")) and now - (s.get("presenceAt") or 0) < PRESENCE_TIMEOUT
            out.append({**s, "id": char, "online": online})
        out.sort(key=lambda c: (not c["online"], -(c.get("presenceAt") or 0)))
        return out

    def find(self, char):
        """The store for a listed character (or the most recently active one), else None."""
        if char:
            if not CHARACTER_ID.match(char):
                return None
            with self.root.transaction():
                listed = char in (self.root.get("index") or {})
            return self.store(char) if listed else None
        chars = self.characters()
        return self.store(chars[0]["id"]) if chars else None

    def hide(self, char):
        with self.root.transaction():
            index, owners = self.root.get("index") or {}, self.root.get("owners") or {}
            found = char in index
            index.pop(char, None)
            owners.pop(char, None)
            self.root.put("index", index)
            self.root.put("owners", owners)
        return found


def make_tracker():
    """Redis when a Redis REST URL is configured (Vercel), otherwise SQLite: the
    root at FT_DB and one file per character next to it."""
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return Tracker(lambda c: RedisStore(url, token, "ft:root:" if c is None else f"ft:c:{c}:"),
                       legacy=RedisStore(url, token, "ft:"))
    if SERVERLESS:
        raise RuntimeError("no database: connect an Upstash Redis store to this Vercel project "
                           "(Storage tab), then redeploy")
    folder = os.path.splitext(DB_PATH)[0] + "-characters"

    def open_store(char):
        if char is None:
            return SqliteStore(DB_PATH)
        os.makedirs(folder, exist_ok=True)
        return SqliteStore(os.path.join(folder, char + ".sqlite3"))
    return Tracker(open_store)


TRACKER = None


# --- HTTP ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ForeverTracker/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("FT_ACCESS_LOG"):
            super().log_message(fmt, *args)

    def send_json(self, obj, status=HTTPStatus.OK, cdn_seconds=0):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # On Vercel every viewer polls. A short CDN cache keeps that to about one
        # function call (and one database read) per interval, however many watch.
        if SERVERLESS and cdn_seconds and status == HTTPStatus.OK:
            self.send_header("Cache-Control", f"public, s-maxage={cdn_seconds}")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, msg):
        self.send_json({"error": msg}, status)

    def ingest_allowed(self):
        if PUBLIC_INGEST:
            return True
        # Uploads come straight from the companion. Anything relayed by a
        # reverse proxy (i.e. from the internet) is refused.
        if self.headers.get("X-Forwarded-For") or self.headers.get("Forwarded"):
            return False
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in net for net in INGEST_NETS)

    def bearer(self):
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def is_admin(self):
        return bool(TOKEN) and hmac.compare_digest(self.bearer().encode(), TOKEN.encode())

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise OverflowError("bad body size")
        return json.loads(self.rfile.read(length))

    def query(self, name):
        return (parse_qs(urlparse(self.path).query).get(name) or [""])[0]

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/admin/keys":
            if not self.is_admin():
                return self.error(HTTPStatus.UNAUTHORIZED, "admin only (FT_TOKEN)")
            try:
                name = str(self.read_json().get("name") or "").strip()[:40]
            except (ValueError, OverflowError, AttributeError):
                name = ""
            if not name:
                return self.error(HTTPStatus.BAD_REQUEST, 'send {"name": "..."}')
            return self.send_json(TRACKER.add_key(name))
        if path != "/api/ingest":
            return self.error(HTTPStatus.NOT_FOUND, "not found")
        if not self.ingest_allowed():
            return self.error(HTTPStatus.FORBIDDEN, "ingest is LAN-only")
        try:
            payload = self.read_json()
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            TRACKER.ingest(self.bearer(), payload)
        except OverflowError as exc:
            return self.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
        except NotYours as exc:
            return self.error(HTTPStatus.FORBIDDEN, str(exc))
        except PermissionError:
            return self.error(HTTPStatus.UNAUTHORIZED, "bad key")
        except (ValueError, TypeError, AttributeError) as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        self.send_json({"ok": True})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self.is_admin():
            return self.error(HTTPStatus.UNAUTHORIZED, "admin only (FT_TOKEN)")
        if path == "/api/admin/keys":
            return self.send_json({"removed": TRACKER.remove_key(self.query("id"))})
        if path == "/api/admin/characters":
            return self.send_json({"removed": TRACKER.hide(self.query("c"))})
        return self.error(HTTPStatus.NOT_FOUND, "not found")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/characters":
            return self.send_json(TRACKER.characters(), cdn_seconds=5)
        if url.path == "/api/admin/keys":
            if not self.is_admin():
                return self.error(HTTPStatus.UNAUTHORIZED, "admin only (FT_TOKEN)")
            return self.send_json(TRACKER.list_keys())
        if url.path in ("/api/state", "/api/history", "/api/stream"):
            store = TRACKER.find(self.query("c"))
            if store is None:
                return self.error(HTTPStatus.NOT_FOUND, "no such character")
            if url.path == "/api/state":
                return self.send_json(store.state(), cdn_seconds=2)
            if url.path == "/api/history":
                # The page asks for ?v=<historyAt>, so a new version always misses the cache.
                return self.send_json(store.history(), cdn_seconds=60)
            if not SERVERLESS:
                return self.stream(store)
        if url.path.startswith("/api/"):
            return self.error(HTTPStatus.NOT_FOUND, "not found")
        return self.static(url.path)

    def stream(self, store):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        seen_live, seen_history = store.live_version, store.history_version
        try:
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            while True:
                with store.changed:
                    store.changed.wait_for(lambda: store.live_version != seen_live, timeout=25)
                if store.history_version != seen_history:
                    # Journal, trail or quests changed: the page refetches everything.
                    seen_live, seen_history = store.live_version, store.history_version
                    self.wfile.write(f"event: update\ndata: {seen_history}\n\n".encode())
                elif store.live_version != seen_live:
                    seen_live = store.live_version
                    state = json.dumps(store.state(quests=False), separators=(",", ":"))
                    self.wfile.write(f"event: live\ndata: {state}\n\n".encode())
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.realpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
            return self.error(HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global TRACKER
    if not TOKEN:
        raise SystemExit("set FT_TOKEN (the owner's upload and admin key)")
    TRACKER = make_tracker()
    print(f"Forever Tracker backend on http://{HOST}:{PORT} (static: {STATIC}, db: {DB_PATH})",
          flush=True)
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

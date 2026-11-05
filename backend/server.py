#!/usr/bin/env python3
"""Forever Tracker backend.

- POST /api/ingest   companion uploads (bearer token; LAN only unless on Vercel)
- GET  /api/state    latest snapshot + online status
- GET  /api/history  position trail, XP samples, event log
- GET  /api/stream   Server-Sent Events; fires "update" after each ingest
- everything else    static frontend

Standard library only. Configure with environment variables (see README).
Runs as its own server (SQLite) or as a Vercel function (api/index.py, Redis).
"""

import hmac
import ipaddress
import json
import mimetypes
import os
import sqlite3
import threading
import time
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
        self.notify(history)

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


class RedisStore(Store):
    """Upstash Redis through its REST API: one read and one write round trip per upload.

    Keys: ft:kv (hash of JSON values), ft:events / ft:positions / ft:xp (sorted
    sets scored by time; members are JSON, so identical rows are stored once)."""

    KV, EVENTS, POSITIONS, XP = "ft:kv", "ft:events", "ft:positions", "ft:xp"
    KEEP = {EVENTS: 2000, POSITIONS: 20000, XP: 20000}  # older rows beyond this are dropped

    def __init__(self, url, token):
        super().__init__()
        self.url, self.token = url.rstrip("/"), token
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


def make_store():
    """Redis when a Redis REST URL is configured (Vercel), otherwise SQLite at FT_DB."""
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return RedisStore(url, token)
    if SERVERLESS:
        raise RuntimeError("no database: connect an Upstash Redis store to this Vercel project "
                           "(Storage tab), then redeploy")
    return SqliteStore(DB_PATH)


STORE = None


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

    def do_POST(self):
        if urlparse(self.path).path != "/api/ingest":
            return self.error(HTTPStatus.NOT_FOUND, "not found")
        if not self.ingest_allowed():
            return self.error(HTTPStatus.FORBIDDEN, "ingest is LAN-only")
        if not TOKEN or not hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), f"Bearer {TOKEN}".encode()):
            return self.error(HTTPStatus.UNAUTHORIZED, "bad token")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "bad body size")
        try:
            payload = json.loads(self.rfile.read(length))
            STORE.ingest(payload)
        except (ValueError, TypeError, AttributeError) as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        self.send_json({"ok": True})

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/state":
            return self.send_json(STORE.state(), cdn_seconds=2)
        if url.path == "/api/history":
            # The page asks for ?v=<historyAt>, so a new version always misses the cache.
            return self.send_json(STORE.history(), cdn_seconds=60)
        if url.path == "/api/stream" and not SERVERLESS:
            return self.stream()
        if url.path.startswith("/api/"):
            return self.error(HTTPStatus.NOT_FOUND, "not found")
        return self.static(url.path)

    def stream(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        seen_live, seen_history = STORE.live_version, STORE.history_version
        try:
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            while True:
                with STORE.changed:
                    STORE.changed.wait_for(lambda: STORE.live_version != seen_live, timeout=25)
                if STORE.history_version != seen_history:
                    # Journal, trail or quests changed: the page refetches everything.
                    seen_live, seen_history = STORE.live_version, STORE.history_version
                    self.wfile.write(f"event: update\ndata: {seen_history}\n\n".encode())
                elif STORE.live_version != seen_live:
                    seen_live = STORE.live_version
                    state = json.dumps(STORE.state(quests=False), separators=(",", ":"))
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
    global STORE
    if not TOKEN:
        raise SystemExit("set FT_TOKEN (shared secret for the companion)")
    STORE = make_store()
    print(f"Forever Tracker backend on http://{HOST}:{PORT} (static: {STATIC}, db: {DB_PATH})",
          flush=True)
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

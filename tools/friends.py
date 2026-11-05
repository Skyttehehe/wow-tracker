#!/usr/bin/env python3
"""Manages friends' upload keys on the tracker site (owner only).

Uses backend_url and the owner's key (FT_TOKEN) from companion/config.json.

  python tools/friends.py add "Bob"        create a key for Bob and print it once
  python tools/friends.py list             keys and the characters each uploads
  python tools/friends.py remove <id>      revoke a key (its characters stay listed)
  python tools/friends.py hide <character> take a character off the site
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "..", "companion", "config.json")


def call(cfg, method, path, body=None):
    req = urllib.request.Request(
        cfg["backend_url"].rstrip("/") + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json",
                 "User-Agent": "forever-tracker-admin"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"{exc.code}: {exc.read().decode(errors='replace')}")


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("add", "list", "remove", "hide") or (args[0] != "list" and len(args) < 2):
        sys.exit(__doc__)
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    if not str(cfg.get("backend_url", "")).startswith("https://"):
        sys.exit("set backend_url (https://...) in companion/config.json")

    if args[0] == "add":
        key = call(cfg, "POST", "/api/admin/keys", {"name": " ".join(args[1:])})
        print(f"Key for {key['name']} (id {key['id']}). It is shown only now:\n\n  {key['token']}\n")
    elif args[0] == "list":
        for k in call(cfg, "GET", "/api/admin/keys"):
            chars = ", ".join(k["characters"]) or "no characters yet"
            print(f"{k['id']:<12}  {k['name']:<20}  {chars}")
    elif args[0] == "remove":
        print("removed" if call(cfg, "DELETE", "/api/admin/keys?id=" + urllib.parse.quote(args[1]))["removed"]
              else "no such key")
    else:
        print("hidden" if call(cfg, "DELETE", "/api/admin/characters?c=" + urllib.parse.quote(args[1]))["removed"]
              else "no such character")


if __name__ == "__main__":
    main()

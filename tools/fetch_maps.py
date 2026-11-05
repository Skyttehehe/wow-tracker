#!/usr/bin/env python3
"""Builds frontend/maps/<uiMapID>.jpg from the game's own world map tiles.

Map tables and tile files come from wago.tools for the given client build, so
the images match exactly what the in-game world map shows. Coordinates from
C_Map.GetPlayerMapPosition are normalised to these images.

Usage: python3 tools/fetch_maps.py [--build 1.60.1.70790] [--force]
Needs Pillow (for BLP decoding).
"""

import argparse
import csv
import io
import json
import os
import time
import urllib.request

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "frontend", "maps")
API = "https://wago.tools"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "forever-tracker-mapfetch"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except OSError:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


def table(name, build):
    text = get(f"{API}/db2/{name}/csv?build={build}").decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="1.60.1.70790")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    maps = {r["ID"]: r for r in table("UiMap", args.build)}
    links = table("UiMapXMapArt", args.build)
    arts = {r["ID"]: r for r in table("UiMapArt", args.build)}
    styles = {(r["UiMapArtStyleID"], r["LayerIndex"]): r for r in table("UiMapArtStyleLayer", args.build)}
    tiles = {}
    for t in table("UiMapArtTile", args.build):
        if t["LayerIndex"] == "0":
            tiles.setdefault(t["UiMapArtID"], []).append(t)

    index = {}
    for link in links:
        if link["PhaseID"] != "0":
            continue
        map_id, art_id = link["UiMapID"], link["UiMapArtID"]
        ui = maps.get(map_id)
        art = arts.get(art_id)
        if not ui or not art or art_id not in tiles:
            continue
        style = styles.get((art["UiMapArtStyleID"], "0"))
        if not style:
            continue
        w, h = int(style["LayerWidth"]), int(style["LayerHeight"])
        tw, th = int(style["TileWidth"]), int(style["TileHeight"])
        index[map_id] = {"name": ui["Name_lang"], "type": int(ui["Type"]),
                         "parent": int(ui["ParentUiMapID"]), "width": w, "height": h}
        path = os.path.join(OUT, f"{map_id}.jpg")
        if os.path.exists(path) and not args.force:
            continue

        canvas = Image.new("RGB", (w, h))
        for t in tiles[art_id]:
            blp = get(f"{API}/api/casc/{t['FileDataID']}?version={args.build}")
            tile = Image.open(io.BytesIO(blp)).convert("RGB")
            canvas.paste(tile, (int(t["ColIndex"]) * tw, int(t["RowIndex"]) * th))
            time.sleep(0.1)  # be gentle with wago.tools
        canvas.save(path, quality=85, optimize=True)
        print(f"{map_id:>5} {ui['Name_lang']} ({w}x{h}, {len(tiles[art_id])} tiles)", flush=True)

    with open(os.path.join(OUT, "index.json"), "w") as f:
        json.dump(index, f, indent=1, sort_keys=True)
    print(f"{len(index)} maps in {os.path.realpath(OUT)}")


if __name__ == "__main__":
    main()

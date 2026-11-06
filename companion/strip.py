"""Reads and decodes the ForeverTracker pixel strip (see addon Strip.lua)."""

import json
import sys
import time

CELL_PX = 4
NUM_BYTES = 171
NUM_CELLS = NUM_BYTES // 3
WIDTH = NUM_CELLS * CELL_PX
VERSIONS = (1, 2, 3)  # v2 adds health/power percent (chunks 118 bytes); v3 a character tag (117)

FLAGS = {"sharing": 1, "dead": 2, "ghost": 4, "combat": 8, "resting": 16, "hidden": 32, "leaving": 64,
         "combatLog": 128}
VALID = {"level": 1, "xp": 2, "health": 4, "healthMax": 8, "power": 16, "powerMax": 32, "pos": 64,
         "money": 128, "map": 256, "healthPct": 512, "powerPct": 1024}
POWER_TYPES = {0: "MANA", 1: "RAGE", 2: "FOCUS", 3: "ENERGY"}


def cells_to_bytes(pixels):
    """pixels: list of (r, g, b) sampled at each cell centre."""
    out = bytearray()
    for r, g, b in pixels:
        out += bytes((r, g, b))
    return bytes(out)


def checksum_ok(data):
    a = b = 0
    for byte in data[:NUM_BYTES - 2]:
        a = (a + byte) % 255
        b = (b + a) % 255
    return data[NUM_BYTES - 2] == b and data[NUM_BYTES - 1] == a


def num(data, offset, width):
    return int.from_bytes(data[offset:offset + width], "big")


def decode(data):
    """Returns the decoded frame dict, or None if this isn't a valid strip."""
    if len(data) < NUM_BYTES or data[0:2] != b"FT" or data[2] not in VERSIONS or not checksum_ok(data):
        return None
    flags = data[4]
    valid = num(data, 5, 2)
    has = lambda k: bool(valid & VALID[k])
    v2 = data[2] >= 2
    frame = {
        "counter": data[3],
        "flags": {k: bool(flags & v) for k, v in FLAGS.items()},
        "t": num(data, 41, 4),
        "level": data[7] if has("level") else None,
        "xp": num(data, 9, 3) if has("xp") else None,
        "xpMax": num(data, 12, 3) if has("xp") else None,
        "rested": num(data, 15, 3) if has("xp") else None,
        "health": num(data, 18, 3) if has("health") else None,
        "healthMax": num(data, 21, 3) if has("healthMax") else None,
        "healthPct": data[167] if v2 and has("healthPct") else None,
        "power": {
            "id": data[8],
            "type": POWER_TYPES.get(data[8]),
            "cur": num(data, 24, 3) if has("power") else None,
            "max": num(data, 27, 3) if has("powerMax") else None,
            "pct": data[168] if v2 and has("powerPct") else None,
        },
        "money": num(data, 37, 4) if has("money") else None,
        "mapID": num(data, 30, 3) if has("map") else None,
        "x": round(num(data, 33, 2) / 65535, 4) if has("pos") else None,
        "y": round(num(data, 35, 2) / 65535, 4) if has("pos") else None,
    }
    # v3: which character these numbers belong to (char_tag); 0 = unknown.
    frame["char"] = data[166] if data[2] >= 3 and data[166] else None
    length = data[48]
    if data[45] and 0 < length <= (117 if data[2] >= 3 else 118 if v2 else 120):
        frame["chunk"] = (data[45], data[46], data[47], bytes(data[49:49 + length]))
    return frame


def char_tag(name, realm):
    """The addon's FT.CharTag: 1-255 for a 'Name-Realm', 0 without a name."""
    if not name:
        return 0
    h = 0
    for b in f"{name}-{realm or ''}".encode("utf-8"):
        h = (h * 31 + b) % 65521
    return h % 255 + 1


class Reassembler:
    """Collects message chunks; returns (kind, payload) when one completes.

    Each chunk stays on screen for several frames, so chunks of a message that
    just completed keep arriving; ids completed in the last DONE_WINDOW
    seconds are ignored (ids wrap at 255, far slower than that)."""

    DONE_WINDOW = 10

    def __init__(self):
        self.parts = {}
        self.done = {}

    def add(self, chunk, now):
        msg_id, index, count, data = chunk
        if not (1 <= index <= count):
            return None
        if now - self.done.get(msg_id, -1e9) < self.DONE_WINDOW:
            return None
        entry = self.parts.get(msg_id)
        if entry is None or entry["count"] != count:
            entry = self.parts[msg_id] = {"count": count, "chunks": {}}
        entry["chunks"][index] = data
        if len(entry["chunks"]) < count:
            return None
        raw = b"".join(entry["chunks"][i] for i in range(1, count + 1))
        del self.parts[msg_id]
        self.done[msg_id] = now
        try:
            text = raw.decode("utf-8")
            return text[0], json.loads(text[1:])
        except (UnicodeDecodeError, ValueError, IndexError):
            return None


# --- capture -------------------------------------------------------------------

class X11Capture:
    """Reads the strip straight from the WoW X11 window (works under XWayland,
    and while the window is covered)."""

    def __init__(self):
        from Xlib import X, display
        self.X = X
        self.display = display.Display()
        self.window = None
        self.next_search = 0

    def find_window(self):
        root = self.display.screen().root
        stack = [root]
        while stack:
            win = stack.pop()
            try:
                name = win.get_wm_name()
                if name == "World of Warcraft":
                    geom = win.get_geometry()
                    if geom.width > WIDTH:
                        return win
                stack.extend(win.query_tree().children)
            except Exception:  # noqa: BLE001 - windows vanish mid-walk
                continue
        return None

    def grab(self):
        if self.window is None:
            if time.monotonic() < self.next_search:
                return None
            self.window = self.find_window()
            if self.window is None:
                self.next_search = time.monotonic() + 5
                return None
        try:
            img = self.window.get_image(0, 0, WIDTH, CELL_PX, self.X.ZPixmap, 0xFFFFFFFF)
        except Exception:  # noqa: BLE001 - window closed
            self.window = None
            return None
        raw = img.data
        row = CELL_PX // 2
        stride = WIDTH * 4
        pixels = []
        for i in range(NUM_CELLS):
            o = row * stride + (i * CELL_PX + CELL_PX // 2) * 4
            b, g, r = raw[o], raw[o + 1], raw[o + 2]
            pixels.append((r, g, b))
        return cells_to_bytes(pixels)


class ScreenCapture:
    """Fallback: grabs the top-left of the primary screen (game must be
    fullscreen or its window at the screen origin)."""

    def __init__(self):
        from PIL import ImageGrab
        self.grab_image = ImageGrab.grab

    def grab(self):
        img = self.grab_image(bbox=(0, 0, WIDTH, CELL_PX)).convert("RGB")
        y = CELL_PX // 2
        return cells_to_bytes([img.getpixel((i * CELL_PX + CELL_PX // 2, y)) for i in range(NUM_CELLS)])


def sample_strip(pixel, width, height):
    """Reads the strip from an image whose cells may be larger than CELL_PX
    (Retina screens, or the game rendering at another resolution).

    pixel(x, y) -> (r, g, b). The first cell always holds "F", "T", version,
    so its width gives the cell size; then each cell's centre is sampled.
    Returns the strip bytes, or None if the strip isn't there."""
    if width < 2 or height < 2:
        return None
    first = pixel(1, 1)
    if first[0] != 70 or first[1] != 84 or first[2] not in VERSIONS:
        return None
    size = 1
    while size < width and pixel(size, 1) == first:
        size += 1
    if size < CELL_PX // 2 or NUM_CELLS * size > width or size // 2 >= height:
        return None
    y = size // 2
    return cells_to_bytes([pixel(i * size + size // 2, y) for i in range(NUM_CELLS)])


def channel_order(bitmap_info):
    """(r, g, b) byte offsets in a 32-bit CoreGraphics pixel."""
    alpha_first = (bitmap_info & 0x1F) in (2, 4, 6)   # premultiplied/plain/none-skip "first"
    little = (bitmap_info & 0x7000) == 0x2000          # kCGBitmapByteOrder32Little
    if little:
        return (2, 1, 0) if alpha_first else (3, 2, 1)  # BGRA / ABGR in memory
    return (1, 2, 3) if alpha_first else (0, 1, 2)      # ARGB / RGBA in memory


class MacCapture:
    """macOS: reads the strip from the WoW window itself through CoreGraphics
    (pyobjc), in the window's own colours (macOS colour management would change
    the bytes of a plain screenshot), also while the window is covered.
    Needs the Screen Recording permission."""

    REGION_PT = (2 * WIDTH, 2 * CELL_PX)  # points captured: room for 2x cells

    def __init__(self):
        import Quartz
        self.Q = Quartz
        self.window = None      # (id, x, y, width, height) in points
        self.found_at = 0

    def find_window(self):
        Q = self.Q
        infos = Q.CGWindowListCopyWindowInfo(
            Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements, Q.kCGNullWindowID) or []
        best = None
        for info in infos:
            owner = str(info.get(Q.kCGWindowOwnerName) or "")
            if not owner.lower().startswith("world of warcraft") or int(info.get(Q.kCGWindowLayer, 0)) != 0:
                continue
            b = info.get(Q.kCGWindowBounds) or {}
            w, h = float(b.get("Width", 0)), float(b.get("Height", 0))
            if w > WIDTH and (best is None or w * h > best[3] * best[4]):
                best = (int(info[Q.kCGWindowNumber]), float(b.get("X", 0)), float(b.get("Y", 0)), w, h)
        return best

    def grab(self):
        now = time.monotonic()
        if self.window is None or now - self.found_at > 5:  # the window may move or resize
            self.window, self.found_at = self.find_window(), now
        if self.window is None:
            return None
        Q = self.Q
        wid, x, y, w, h = self.window
        rect = Q.CGRectMake(x, y, min(w, self.REGION_PT[0]), min(h, self.REGION_PT[1]))
        img = Q.CGWindowListCreateImage(rect, Q.kCGWindowListOptionIncludingWindow, wid,
                                        Q.kCGWindowImageBoundsIgnoreFraming)
        if img is None:
            self.window = None
            return None
        width, height = Q.CGImageGetWidth(img), Q.CGImageGetHeight(img)
        if Q.CGImageGetBitsPerPixel(img) != 32:
            return None
        stride = Q.CGImageGetBytesPerRow(img)
        data = bytes(Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(img)))
        r, g, b = channel_order(Q.CGImageGetBitmapInfo(img))

        def pixel(px, py):
            o = py * stride + px * 4
            return data[o + r], data[o + g], data[o + b]
        return sample_strip(pixel, width, height)


def make_capture():
    if sys.platform.startswith("linux"):
        try:
            return X11Capture()
        except Exception as exc:  # noqa: BLE001
            print(f"X11 capture unavailable ({exc}); falling back to screen grab", flush=True)
    if sys.platform == "darwin":
        try:
            return MacCapture()
        except Exception as exc:  # noqa: BLE001
            print(f"window capture unavailable ({exc}); falling back to screen grab", flush=True)
    return ScreenCapture()

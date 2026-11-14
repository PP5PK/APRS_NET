#!/usr/bin/env python3
"""
Generate PKTNET participation certificates (PDF) from a template image.

The visual design lives in a background template (certs/pktnet_template.png);
this script composites the operator callsign, name, event, date and time onto
it. Fonts live in fonts/ next to this script.

Usage:
    pktnet_cert.py --db /var/lib/pktnet/pktnet.db --event 1 --out ./certs
    pktnet_cert.py -c /etc/pktnet/pktnet.conf --callsign PP5ABC

`--names` is an optional CSV mapping "callsign,name" used to print the
operator's name; without it the name area is left blank.

Requires: Pillow (the only non-stdlib dependency).
"""

import argparse
import configparser
import csv
import os
import re
import sqlite3
import sys
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageFilter


# --------------------------------------------------------------------------- #
# Config / database helpers
# --------------------------------------------------------------------------- #

def db_path_from_config(path):
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        sys.exit("Config file not found or unreadable: {}".format(path))
    return cfg.get("db", "path", fallback="/var/lib/pktnet/pktnet.db")


def template_from_config(path):
    cfg = configparser.ConfigParser()
    cfg.read(path)
    return cfg.get("cert", "template", fallback=DEFAULT_TEMPLATE)


def open_db(path):
    if not os.path.exists(path):
        sys.exit("Database not found: {}".format(path))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def get_event(conn, event_id):
    if event_id:
        row = conn.execute("SELECT * FROM events WHERE event_id = ?",
                           (event_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM events ORDER BY start_utc DESC LIMIT 1").fetchone()
    if not row:
        sys.exit("Event not found.")
    return row


def get_checkins(conn, event_id, callsign=None):
    if callsign:
        rows = conn.execute(
            "SELECT * FROM checkins WHERE event_id = ? AND callsign = ? "
            "ORDER BY ts_utc", (event_id, callsign.upper())).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM checkins WHERE event_id = ? ORDER BY ts_utc",
            (event_id,)).fetchall()
    return rows


def load_names(path):
    names = {}
    if not path:
        return names
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2 and row[0].strip():
                names[row[0].strip().upper()] = row[1].strip()
    return names


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #

def fmt_date_br(iso_date):
    """YYYY-MM-DD -> DD/MM/YYYY."""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return iso_date or ""


def fmt_time_utc(iso_ts):
    """ISO 8601 timestamp -> 'HH:MM' (UTC)."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except (ValueError, TypeError, AttributeError):
        return iso_ts or ""


def safe_filename(text):
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)


# --------------------------------------------------------------------------- #
# Template compositing
# --------------------------------------------------------------------------- #

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
DEFAULT_TEMPLATE = "/var/lib/pktnet/certs/pktnet_template.png"

GOLD = (223, 170, 78)
CREAM = (246, 246, 248)
SKY_BLUE = (168, 205, 235)   # operator name - picks up the night-sky backdrop
CORAL = (235, 140, 95)       # Net name - picks up the sunset in the artwork

FONT_CALLSIGN = "Orbitron-Black.ttf"
FONT_NAME = "Playfair-SemiBoldItalic.ttf"
FONT_EVENT = "Montserrat-SemiBold.ttf"
FONT_VALUE = "Montserrat-Medium.ttf"

# The certificate data goes in a small translucent grey box in the bottom-right
# corner, so the background template can be swapped freely - it never has to
# leave room for the text. The box is sized to its content: callsign and
# operator name on the left, caption, event and date/time on the right.
# Every `_size` is a starting font size in pixels that shrinks to fit; the
# box distance from the page edge is physical (cm, the PDF is saved at 150 DPI).
FIXED_TEXT = "Successfully participated in"

LAYOUT = {
    # Distance from the bottom-right corner of the page (cm).
    "box_right_cm": 1.0, "box_bottom_cm": 1.0,

    # Box look: grey fill, alpha 0-255 (128 = 50% transparent), corner radius,
    # inner padding (px) and gold outline alpha (0 = no outline).
    "box_rgb": (70, 70, 75), "box_alpha": 128,
    "box_radius_px": 16, "box_outline_alpha": 170,
    "box_pad_x_px": 28, "box_pad_y_px": 22,

    # Gap between the two columns (px; a thin gold divider is drawn in it)
    # and the widest each column may grow before its text shrinks/wraps.
    "col_gap_px": 36, "divider_alpha": 150,
    "left_max_w_px": 400, "right_max_w_px": 380,

    # Starting font sizes (px): callsign, operator name, fixed text,
    # event name, date/time line.
    "callsign_size": 68, "name_size": 36, "fixed_size": 18,
    "event_size": 28, "value_size": 24,

    # Operator name: smallest size on a single line (below this it wraps to
    # two lines) and smallest size once it is on two lines.
    "name_single_min": 24, "name_wrap_min": 20,

    # Event name: same idea.
    "event_single_min": 18, "event_wrap_min": 15,

    # Text effect (readable on any background): dark outline + drop shadow.
    "outline_rgb": (10, 12, 20), "shadow_alpha": 200,
}


def _font(name, size):
    return ImageFont.truetype(os.path.join(_FONT_DIR, name), int(size))


def _fit(name, text, max_w, start, minsz=20):
    """Largest font (from `start` down) whose text width fits `max_w` px."""
    size = start
    while size > minsz:
        f = _font(name, size)
        box = f.getbbox(text)
        if (box[2] - box[0]) <= max_w:
            return f
        size -= 2
    return _font(name, minsz)


def _fit_or_wrap(name, text, max_w, start, single_min=20, wrap_min=20):
    """Largest single-line font (from `start` down to `single_min`) whose
    text fits `max_w`. If `text` is still too wide at `single_min`, it is
    split at the word boundary that best balances the two halves, and that
    two-line version is fit the same way (shrinking down to `wrap_min` if
    needed). Returns (lines, font), where `lines` has 1 or 2 strings."""

    def width(f, t):
        box = f.getbbox(t)
        return box[2] - box[0]

    size = start
    while size >= single_min:
        f = _font(name, size)
        if width(f, text) <= max_w:
            return [text], f
        size -= 2

    words = text.split()
    if len(words) < 2:
        return [text], _font(name, single_min)

    f0 = _font(name, start)
    best = None
    for i in range(1, len(words)):
        line1, line2 = " ".join(words[:i]), " ".join(words[i:])
        score = max(width(f0, line1), width(f0, line2))
        if best is None or score < best[0]:
            best = (score, line1, line2)
    _, line1, line2 = best

    size = start
    while size >= wrap_min:
        f = _font(name, size)
        if width(f, line1) <= max_w and width(f, line2) <= max_w:
            return [line1, line2], f
        size -= 2
    return [line1, line2], _font(name, wrap_min)


def _base_call(callsign):
    """Base callsign only - the SSID suffix is never shown."""
    return (callsign or "").split("-")[0].upper().strip()


def _fx(font):
    """Outline width (px) and shadow offset/blur for a font size."""
    sw = max(1, int(round(font.size / 16.0)))
    return sw, sw + 1


def _measure(draw, text, font):
    """(width, height) of `text` including its outline."""
    sw, _ = _fx(font)
    l, t, r, b = draw.textbbox((0, 0), text, font=font, stroke_width=sw)
    return r - l, b - t


def _draw_fx(img, text, cx, cy, font, fill):
    """Draw text centred on (cx, cy) with a dark outline and a soft drop
    shadow, so it stays readable on light and dark backgrounds alike."""
    L = LAYOUT
    draw = ImageDraw.Draw(img)
    sw, off = _fx(font)
    l, t, r, b = draw.textbbox((0, 0), text, font=font, stroke_width=sw)
    x, y = cx - (r - l) / 2.0 - l, cy - (b - t) / 2.0 - t
    m = off * 3 + 4                      # margin around the text for the blur
    box = (int(x + l - m), int(y + t - m), int(x + r + m), int(y + b + m))
    layer = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(
        (x - box[0] + off, y - box[1] + off), text, font=font,
        fill=(0, 0, 0, L["shadow_alpha"]), stroke_width=sw,
        stroke_fill=(0, 0, 0, L["shadow_alpha"]))
    layer = layer.filter(ImageFilter.GaussianBlur(off))
    base = img.crop(box).convert("RGBA")
    img.paste(Image.alpha_composite(base, layer).convert("RGB"), box[:2])
    ImageDraw.Draw(img).text((x, y), text, font=font, fill=fill,
                             stroke_width=sw,
                             stroke_fill=tuple(L["outline_rgb"]))
    return img


def _cm(value):
    """Centimetres -> pixels at the 150 DPI the PDF is saved with."""
    return int(round(value * 150.0 / 2.54))


class _Column(object):
    """A vertical stack of centred text entries (one or two lines each)."""

    GAP = 8

    def __init__(self, draw):
        self.draw = draw
        self.entries = []        # (lines, font, fill, line heights, height)

    def add(self, lines, font, fill):
        hs = [_measure(self.draw, t, font)[1] for t in lines]
        h = sum(hs) + font.size * 0.2 * (len(lines) - 1)
        self.entries.append((lines, font, fill, hs, h))

    @property
    def width(self):
        w = 0
        for lines, font, _, _, _ in self.entries:
            for t in lines:
                w = max(w, _measure(self.draw, t, font)[0])
        return w

    @property
    def height(self):
        return (sum(e[4] for e in self.entries)
                + self.GAP * max(len(self.entries) - 1, 0))

    def paint(self, img, cx, cy):
        y = cy - self.height / 2.0
        for lines, font, fill, hs, h in self.entries:
            top = y
            for t, th in zip(lines, hs):
                img = _draw_fx(img, t, cx, top + th / 2.0, font, fill)
                top += th + font.size * 0.2
            y += h + self.GAP
        return img


def draw_certificate(path, ctx):
    """Composite the certificate data onto the template and save to `path`
    (PDF if the path ends in .pdf, otherwise inferred from the extension).

    A translucent grey box in the bottom-right corner holds two columns:
    callsign + operator name on the left; caption, event name and
    date/time on the right. The box is sized to fit its content."""
    template = ctx.get("template") or DEFAULT_TEMPLATE
    img = Image.open(template).convert("RGB")
    W, H = img.size
    L = LAYOUT
    draw = ImageDraw.Draw(img)

    left, right = _Column(draw), _Column(draw)

    callsign = _base_call(ctx.get("callsign", ""))
    if callsign:
        f = _fit(FONT_CALLSIGN, callsign, L["left_max_w_px"],
                 L["callsign_size"])
        left.add([callsign], f, GOLD)

    name = (ctx.get("op_name") or "").strip()
    if name:
        lines, f = _fit_or_wrap(FONT_NAME, name, L["left_max_w_px"],
                                L["name_size"], L["name_single_min"],
                                L["name_wrap_min"])
        left.add(lines, f, SKY_BLUE)

    f = _fit(FONT_VALUE, FIXED_TEXT, L["right_max_w_px"], L["fixed_size"])
    right.add([FIXED_TEXT], f, CREAM)

    event = (ctx.get("event_name") or "").strip()
    if event:
        lines, f = _fit_or_wrap(FONT_EVENT, event, L["right_max_w_px"],
                                L["event_size"], L["event_single_min"],
                                L["event_wrap_min"])
        right.add(lines, f, CORAL)

    date_txt = (ctx.get("date_br") or "").strip()
    time_txt = (ctx.get("checkin_time") or "").strip()
    if time_txt and not time_txt.lower().endswith("z"):
        time_txt += "z"
    when = "  •  ".join(t for t in (date_txt, time_txt) if t)
    if when:
        f = _fit(FONT_VALUE, when, L["right_max_w_px"], L["value_size"])
        right.add([when], f, CREAM)

    # Size the box to its content and anchor it to the bottom-right corner.
    cols = [c for c in (left, right) if c.entries]
    gap = L["col_gap_px"] if len(cols) == 2 else 0
    bw = sum(c.width for c in cols) + gap + 2 * L["box_pad_x_px"]
    bh = max(c.height for c in cols) + 2 * L["box_pad_y_px"]
    x0 = W - _cm(L["box_right_cm"]) - bw
    y0 = H - _cm(L["box_bottom_cm"]) - bh

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(
        [x0, y0, x0 + bw, y0 + bh], radius=L["box_radius_px"],
        fill=tuple(L["box_rgb"]) + (L["box_alpha"],),
        outline=(GOLD + (L["box_outline_alpha"],)
                 if L["box_outline_alpha"] else None), width=2)
    if len(cols) == 2 and L["divider_alpha"]:
        dx = x0 + L["box_pad_x_px"] + left.width + gap / 2.0
        od.line([(dx, y0 + L["box_pad_y_px"]),
                 (dx, y0 + bh - L["box_pad_y_px"])],
                fill=GOLD + (L["divider_alpha"],), width=2)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    cy = y0 + bh / 2.0
    x = x0 + L["box_pad_x_px"]
    for c in cols:
        img = c.paint(img, x + c.width / 2.0, cy)
        x += c.width + gap

    if path.lower().endswith(".pdf"):
        img.save(path, "PDF", resolution=150.0)
    else:
        img.save(path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="Generate PKTNET participation certificates (PDF).")
    ap.add_argument("-c", "--config",
                    help="pktnet config file (for the database and template)")
    ap.add_argument("--db", help="path to the SQLite database "
                                 "(overrides the config value)")
    ap.add_argument("--event", type=int,
                    help="event id (defaults to the most recent event)")
    ap.add_argument("--callsign", help="generate for a single operator only")
    ap.add_argument("--names",
                    help="optional CSV 'callsign,name' for operator names")
    ap.add_argument("--out", default="./certs",
                    help="output directory (default: ./certs)")
    ap.add_argument("--template",
                    help="certificate template image "
                         "(default: from config, else {})".format(
                             DEFAULT_TEMPLATE))
    args = ap.parse_args()

    if args.db:
        db_path = args.db
    elif args.config:
        db_path = db_path_from_config(args.config)
    else:
        sys.exit("Provide --db or --config to locate the database.")

    template = (args.template
                or (template_from_config(args.config) if args.config
                    else DEFAULT_TEMPLATE))

    conn = open_db(db_path)
    event = get_event(conn, args.event)
    checkins = get_checkins(conn, event["event_id"], args.callsign)
    if not checkins:
        sys.exit("No check-ins found for event #{}.".format(event["event_id"]))

    names = load_names(args.names)
    os.makedirs(args.out, exist_ok=True)
    date_br = fmt_date_br(event["event_date"])

    made = 0
    for row in checkins:
        call = row["callsign"]
        ctx = {
            "template": template,
            "net_call": event["net_call"],
            "event_name": event["name"],
            "date_br": date_br,
            "callsign": call,
            "op_name": names.get(_base_call(call), ""),
            "checkin_time": fmt_time_utc(row["ts_utc"]),
        }
        fname = "{}_ev{}_{}.pdf".format(
            safe_filename(event["net_call"]), event["event_id"],
            safe_filename(_base_call(call)))
        out_path = os.path.join(args.out, fname)
        draw_certificate(out_path, ctx)
        made += 1
        print("  {} -> {}".format(call, out_path))

    conn.close()
    print("Generated {} certificate(s) for event #{} ({}).".format(
        made, event["event_id"], event["name"]))


if __name__ == "__main__":
    main()

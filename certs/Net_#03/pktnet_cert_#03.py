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

FONT_CALLSIGN = "Orbitron-Black.ttf"
FONT_NAME = "Playfair-SemiBoldItalic.ttf"
FONT_EVENT = "Montserrat-SemiBold.ttf"
FONT_VALUE = "Montserrat-Medium.ttf"

# The settings below refer to Net #03 - Dia das Criancas.
# Field placement as fractions of the template width/height. This template's
# central frame is a sub-region of pktnet_template_#03.png (2000x1344), not
# the full canvas, so these fractions are tuned to that frame's position,
# not inherited from the 1248x832 base template.
# _cy = Vertical position, lower value goes up, higher value goes down
# _size = font size.
FIXED_TEXT = "Participou com sucesso de"

LAYOUT = {
    "cx": 0.500,             # horizontal centre of the card frame
    # callsign raised 1cm from the previous draft. name_cy gives a true
    # 1cm clear gap below the callsign's rendered bbox (was 1.5cm, brought
    # up 0.5cm - callsign ~113px tall at 156pt, name ~40px tall at 48pt -
    # half-heights of each subtracted so the GAP, not the centre-to-centre
    # distance, is 1cm).
    # fixed_cy is anchored to event_cy (not to name_cy): a true 7mm clear
    # gap above the event name's rendered bbox (fixed text ~29px tall at
    # 30pt, event text ~28px tall at 34pt - half-heights subtracted so the
    # GAP is 7mm). Decoupling it from the name leaves the space between
    # name_cy and fixed_cy free, which is exactly where a long operator
    # name wraps onto a second line (see _fit_or_wrap / name_single_min /
    # name_wrap_min below) without colliding with the fixed caption.
    # event name set so its rendered text leaves a true 1cm clear gap above
    # the date/time boxes (boxes top edge at y~918, event text ~28px tall
    # at 34pt -> half-height 14px subtracted so the GAP is 1cm).
    "callsign_cy": 0.4000, "name_cy": 0.5009, "fixed_cy": 0.5767,
    "event_cy": 0.6287, "value_cy": 0.713,
    # date_x/time_x are now the horizontal CENTRE of the free space to the
    # right of each box's icon (not a left-aligned start).
    "date_x": 0.4401, "time_x": 0.6011,
    "callsign_maxw": 0.446, "name_maxw": 0.315, "fixed_maxw": 0.388,
    "event_maxw": 0.315, "date_maxw": 0.090, "time_maxw": 0.094,
    # callsign_size is calibrated so a 7-character callsign fills
    # callsign_maxw; name_size matches the Net #02 certificate.
    "callsign_size": 156, "name_size": 48, "fixed_size": 30,
    "event_size": 34, "value_size": 32,
    # Operator name: shrink from name_size down to name_single_min while it
    # still fits on one line; below that, wrap it onto two lines instead of
    # shrinking further (down to name_wrap_min if needed for the long half).
    "name_single_min": 32, "name_wrap_min": 24,
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


def _draw_center(img, text, cx, cy, font, fill, glow=False):
    """Draw text centred on (cx, cy). With glow, a soft coloured halo is
    composited behind it. Returns the (possibly new) image."""
    draw = ImageDraw.Draw(img)
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    x, y = cx - (r - l) / 2 - l, cy - (b - t) / 2 - t
    if glow:
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((x, y), text, font=font, fill=fill + (160,))
        layer = layer.filter(ImageFilter.GaussianBlur(7))
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        draw = ImageDraw.Draw(img)
    draw.text((x, y), text, font=font, fill=fill)
    return img


def _draw_left(img, text, x, cy, font, fill):
    """Draw text left-aligned at x, vertically centred on cy."""
    draw = ImageDraw.Draw(img)
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    draw.text((x, cy - (b - t) / 2 - t), text, font=font, fill=fill)


def draw_certificate(path, ctx):
    """Composite the dynamic fields onto the template and save to `path`
    (PDF if the path ends in .pdf, otherwise inferred from the extension)."""
    template = ctx.get("template") or DEFAULT_TEMPLATE
    img = Image.open(template).convert("RGB")
    W, H = img.size
    L = LAYOUT
    cx = L["cx"] * W

    callsign = _base_call(ctx.get("callsign", ""))
    if callsign:
        f = _fit(FONT_CALLSIGN, callsign, L["callsign_maxw"] * W,
                 L["callsign_size"])
        img = _draw_center(img, callsign, cx, L["callsign_cy"] * H, f, GOLD,
                           glow=True)

    name = (ctx.get("op_name") or "").strip()
    if name:
        lines, f = _fit_or_wrap(FONT_NAME, name, L["name_maxw"] * W,
                                 L["name_size"], L["name_single_min"],
                                 L["name_wrap_min"])
        if len(lines) == 1:
            img = _draw_center(img, lines[0], cx, L["name_cy"] * H, f, CREAM)
        else:
            draw = ImageDraw.Draw(img)
            b1 = draw.textbbox((0, 0), lines[0], font=f)
            b2 = draw.textbbox((0, 0), lines[1], font=f)
            h1, h2 = b1[3] - b1[1], b2[3] - b2[1]
            line_gap = f.size * 0.25
            block_h = h1 + line_gap + h2
            name_cy_px = L["name_cy"] * H
            cy1 = name_cy_px - block_h / 2 + h1 / 2
            cy2 = name_cy_px + block_h / 2 - h2 / 2
            img = _draw_center(img, lines[0], cx, cy1, f, CREAM)
            img = _draw_center(img, lines[1], cx, cy2, f, CREAM)

    # Fixed caption - same wording on every certificate from this event.
    f = _fit(FONT_VALUE, FIXED_TEXT, L["fixed_maxw"] * W, L["fixed_size"])
    img = _draw_center(img, FIXED_TEXT, cx, L["fixed_cy"] * H, f, CREAM)

    event = (ctx.get("event_name") or "").strip()
    if event:
        f = _fit(FONT_EVENT, event, L["event_maxw"] * W, L["event_size"])
        img = _draw_center(img, event, cx, L["event_cy"] * H, f, GOLD)

    date_txt = (ctx.get("date_br") or "").strip()
    if date_txt:
        f = _fit(FONT_VALUE, date_txt, L["date_maxw"] * W, L["value_size"])
        img = _draw_center(img, date_txt, L["date_x"] * W, L["value_cy"] * H,
                           f, CREAM)
    time_txt = (ctx.get("checkin_time") or "").strip()
    if time_txt:
        if not time_txt.lower().endswith("z"):
            time_txt += "z"
        f = _fit(FONT_VALUE, time_txt, L["time_maxw"] * W, L["value_size"])
        img = _draw_center(img, time_txt, L["time_x"] * W, L["value_cy"] * H,
                           f, CREAM)

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

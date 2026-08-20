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

# Field placement as fractions of the template width/height. Tuned to
# pktnet_template.png (1248x832); it scales with any same-proportion template.
LAYOUT = {
    "cx": 0.497,            # horizontal centre of the card frame
    "callsign_cy": 0.330, "name_cy": 0.458, "event_cy": 0.590,
    "value_cy": 0.680, "date_x": 0.352, "time_x": 0.628,
    "callsign_maxw": 0.60, "name_maxw": 0.58, "event_maxw": 0.62,
    "callsign_size": 108, "name_size": 56, "event_size": 36, "value_size": 24,
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
        f = _fit(FONT_NAME, name, L["name_maxw"] * W, L["name_size"])
        img = _draw_center(img, name, cx, L["name_cy"] * H, f, CREAM)

    event = (ctx.get("event_name") or "").strip()
    if event:
        f = _fit(FONT_EVENT, event, L["event_maxw"] * W, L["event_size"])
        img = _draw_center(img, event, cx, L["event_cy"] * H, f, GOLD)

    fval = _font(FONT_VALUE, L["value_size"])
    date_txt = (ctx.get("date_br") or "").strip()
    if date_txt:
        _draw_left(img, date_txt, L["date_x"] * W, L["value_cy"] * H, fval,
                   CREAM)
    time_txt = (ctx.get("checkin_time") or "").strip()
    if time_txt:
        if not time_txt.lower().endswith("z"):
            time_txt += "z"
        _draw_left(img, time_txt, L["time_x"] * W, L["value_cy"] * H, fval,
                   CREAM)

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

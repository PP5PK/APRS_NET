#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PKTNET - participation certificate generator
============================================

Reads the SQLite database populated by pktnet_bot.py and produces one
participation certificate (PDF) per operator for a given event, showing the
event name, date and the operator's check-in time.

Palette is colourblind-safe (deep blue + amber, no red/green).

Requires: reportlab (the only non-stdlib dependency).

Usage
-----
    pktnet_cert.py -c /etc/pktnet/pktnet.conf --event 1 --out ./certs
    pktnet_cert.py --db /var/lib/pktnet/pktnet.db --event 1 --names ops.csv
    pktnet_cert.py -c /etc/pktnet/pktnet.conf --callsign PP5ABC-7

`--names` is an optional CSV mapping "callsign,name" used to print the
operator's name under the callsign. Callsigns not found fall back to callsign
only.

73 de PP5PK.
"""

import argparse
import configparser
import csv
import math
import os
import random
import re
import sqlite3
import sys
from datetime import datetime, timezone

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

# --------------------------------------------------------------------------- #
# Palette - blue + amber base (both are flag colours) for legibility,
# green used only as a decorative accent. No red/green contrasts.
# --------------------------------------------------------------------------- #

NAVY_TOP = HexColor("#081726")   # dark sky (globe / flag night)
NAVY = HexColor("#0F3055")       # main navy
NAVY_MID = HexColor("#15436E")   # lighter centre
PANEL = HexColor("#0C2949")      # pill / panel fill
AMBER = HexColor("#EAA31A")      # amber - headings, callsign, frame
AMBER_LT = HexColor("#F6C560")   # light amber
GREEN = HexColor("#1F9A57")      # flag green - decorative accents only
WHITE = HexColor("#F5F8FC")      # primary text
MUTED = HexColor("#9FB6D2")      # secondary text
STAR = HexColor("#FFFFFF")       # star field


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #

def db_path_from_config(path):
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        sys.exit("Config file not found or unreadable: {}".format(path))
    return cfg.get("db", "path", fallback="/var/lib/pktnet/pktnet.db")


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
    except ValueError:
        return iso_date


def fmt_time_utc(iso_ts):
    """ISO 8601 timestamp -> 'HH:MM' (UTC)."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except ValueError:
        return iso_ts


def safe_filename(text):
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)


def _load_image(path):
    """Return an ImageReader for `path`, or None if missing/unreadable."""
    if not path or not os.path.exists(path):
        return None
    try:
        return ImageReader(path)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Certificate drawing
# --------------------------------------------------------------------------- #

def _draw_star(c, x, y, r, color, alpha=1.0):
    c.setFillColor(color)
    c.setFillAlpha(alpha)
    pts = []
    for i in range(10):
        ang = math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.4
        pts.append((x + rad * math.cos(ang), y + rad * math.sin(ang)))
    p = c.beginPath()
    p.moveTo(*pts[0])
    for px, py in pts[1:]:
        p.lineTo(px, py)
    p.close()
    c.drawPath(p, fill=1, stroke=0)
    c.setFillAlpha(1)


def _draw_globe(c, cx, cy, r):
    """Faint wireframe globe: APRS-worldwide + flag celestial-sphere motif."""
    c.saveState()
    c.setStrokeColor(HexColor("#3E6FA0"))
    c.setStrokeAlpha(0.16)
    c.setLineWidth(0.8)
    c.circle(cx, cy, r, stroke=1, fill=0)
    for k in range(1, 4):                       # parallels
        dy = r * k / 4.0
        hw = math.sqrt(max(r * r - dy * dy, 0))
        for yy in (cy + dy, cy - dy):
            c.ellipse(cx - hw, yy - 2.2, cx + hw, yy + 2.2, stroke=1, fill=0)
    c.ellipse(cx - r, cy - 2.6, cx + r, cy + 2.6, stroke=1, fill=0)  # equator
    for k in range(1, 4):                       # meridians
        hw = r * k / 4.0
        c.ellipse(cx - hw, cy - r, cx + hw, cy + r, stroke=1, fill=0)
    c.ellipse(cx - 0.6, cy - r, cx + 0.6, cy + r, stroke=1, fill=0)
    c.restoreState()


def _corner_bracket(c, x, y, dx, dy, size, color, lw=2.4):
    c.setStrokeColor(color)
    c.setLineWidth(lw)
    c.line(x, y, x + dx * size, y)
    c.line(x, y, x, y + dy * size)


def _calendar_icon(c, x, y, s, color):
    c.saveState()
    c.setStrokeColor(color)
    c.setLineWidth(1.4)
    c.roundRect(x, y, s, s * 0.86, 1.5, stroke=1, fill=0)
    c.line(x, y + s * 0.62, x + s, y + s * 0.62)
    c.line(x + s * 0.28, y + s * 0.86, x + s * 0.28, y + s)
    c.line(x + s * 0.72, y + s * 0.86, x + s * 0.72, y + s)
    c.restoreState()


def _clock_icon(c, x, y, s, color):
    c.saveState()
    c.setStrokeColor(color)
    c.setLineWidth(1.4)
    r = s / 2.0
    c.circle(x + r, y + r, r, stroke=1, fill=0)
    c.line(x + r, y + r, x + r, y + r + r * 0.55)
    c.line(x + r, y + r, x + r + r * 0.45, y + r)
    c.restoreState()


def _chip(c, x, y, w, h, label, value, icon):
    c.setStrokeColor(AMBER)
    c.setStrokeAlpha(0.55)
    c.setLineWidth(1.2)
    c.roundRect(x, y, w, h, 4, stroke=1, fill=0)
    c.setStrokeAlpha(1)
    icon(c, x + 7 * mm, y + h / 2 - 4.2 * mm, 8.4 * mm, AMBER)
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8)
    c.drawString(x + 21 * mm, y + h - 6 * mm, label)
    c.setFillColor(WHITE)
    c.setFont("Courier-Bold", 15)
    c.drawString(x + 21 * mm, y + 4.5 * mm, value)


def draw_certificate(path, ctx):
    """Render a single Brazil/APRS themed certificate page to `path`."""
    page = landscape(A4)
    width, height = page
    cx = width / 2.0
    c = canvas.Canvas(path, pagesize=page)

    # Navy background gradient
    c.radialGradient(cx, height * 0.58, width * 0.75,
                     (NAVY_MID, NAVY, NAVY_TOP), (0.0, 0.55, 1.0))

    # Globe motif
    _draw_globe(c, cx, height * 0.55, 82 * mm)

    # Star field (deterministic)
    rng = random.Random(2026)
    for _ in range(70):
        sx = rng.uniform(20 * mm, width - 20 * mm)
        sy = rng.uniform(20 * mm, height - 20 * mm)
        c.setFillColor(STAR)
        c.setFillAlpha(rng.uniform(0.15, 0.7))
        c.circle(sx, sy, rng.uniform(0.3, 1.1), stroke=0, fill=1)
    c.setFillAlpha(1)
    for sx, sy, sr in [(cx - 96 * mm, height - 45 * mm, 2.6),
                       (cx + 98 * mm, height - 60 * mm, 2.0),
                       (cx - 92 * mm, 44 * mm, 1.8)]:
        _draw_star(c, sx, sy, sr, AMBER_LT, 0.85)

    # Stylised radio (left side, raised and roughly vertically centred so it
    # stays clear of the title, the signature area and the date/time boxes)
    radio = ctx.get("radio")
    if radio is not None:
        iw, ih = radio.getSize()
        w_img = 84 * mm
        h_img = w_img * ih / iw
        c.drawImage(radio, 15 * mm, 84 * mm, width=w_img, height=h_img,
                    mask="auto", preserveAspectRatio=True, anchor="sw")

    # Green outer corner accents (decorative only)
    c.setStrokeColor(GREEN)
    c.setLineWidth(3)
    m = 8 * mm
    for (x, y, dx, dy) in [(m, m, 1, 1), (width - m, m, -1, 1),
                           (m, height - m, 1, -1),
                           (width - m, height - m, -1, -1)]:
        c.line(x, y, x + dx * 26 * mm, y)
        c.line(x, y, x, y + dy * 26 * mm)

    # Amber tech frame + corner brackets
    c.setStrokeColor(AMBER)
    c.setLineWidth(1.6)
    c.roundRect(13 * mm, 13 * mm, width - 26 * mm, height - 26 * mm, 6,
                stroke=1, fill=0)
    for (x, y, dx, dy) in [(13 * mm, 13 * mm, 1, 1),
                           (width - 13 * mm, 13 * mm, -1, 1),
                           (13 * mm, height - 13 * mm, 1, -1),
                           (width - 13 * mm, height - 13 * mm, -1, -1)]:
        _corner_bracket(c, x, y, dx, dy, 12 * mm, AMBER_LT)

    # Top tag
    c.setFillColor(AMBER)
    c.setFont("Courier-Bold", 12)
    c.drawCentredString(cx, height - 30 * mm,
                        "{}  \u2022  APRS NET  \u2022  BRAZIL".format(ctx["net_call"]))

    # Title (English)
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 34)
    c.drawCentredString(cx, height - 47 * mm, "CERTIFICATE OF PARTICIPATION")

    c.setStrokeColor(AMBER)
    c.setLineWidth(1.5)
    c.line(cx - 52 * mm, height - 53 * mm, cx + 52 * mm, height - 53 * mm)

    c.setFillColor(MUTED)
    c.setFont("Helvetica-Oblique", 11.5)
    c.drawCentredString(cx, height - 63 * mm, "This is to certify that")

    # Callsign hero
    c.setFillColor(AMBER)
    c.setFont("Courier-Bold", 42)
    c.drawCentredString(cx, height - 84 * mm, ctx["callsign"])

    line_y = height - 84 * mm
    if ctx.get("op_name"):
        c.setFillColor(WHITE)
        c.setFont("Helvetica-Oblique", 14)
        c.drawCentredString(cx, height - 92 * mm, ctx["op_name"])
        line_y = height - 92 * mm

    # Body
    c.setFillColor(WHITE)
    c.setFont("Helvetica", 12.5)
    c.drawCentredString(cx, line_y - 12 * mm,
                        "has successfully participated in {}".format(ctx["event_name"]))
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Oblique", 10)
    c.drawCentredString(cx, line_y - 18 * mm,
                        "Connecting the amateur radio community through APRS")

    # Date / time chips
    chip_w, chip_h, gap = 78 * mm, 18 * mm, 12 * mm
    x0 = cx - (chip_w * 2 + gap) / 2
    _chip(c, x0, 64 * mm, chip_w, chip_h, "DATE",
          ctx["date_br"], _calendar_icon)
    _chip(c, x0 + chip_w + gap, 64 * mm, chip_w, chip_h, "TIME (UTC)",
          ctx["checkin_time"] + "z", _clock_icon)

    # Motto pill (amateur radio)
    pill_w, pill_h = 84 * mm, 8.5 * mm
    c.setStrokeColor(AMBER)
    c.setFillColor(PANEL)
    c.setLineWidth(1.2)
    c.roundRect(cx - pill_w / 2, 48 * mm, pill_w, pill_h, pill_h / 2,
                stroke=1, fill=1)
    c.setFillColor(AMBER_LT)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(cx, 50.4 * mm, "CQ CQ CQ  \u2022  CALLING THE WORLD")

    # Footer: issuer (left) / website (centre) / seal (right)
    base_y = 22 * mm
    c.setStrokeColor(WHITE)
    c.setStrokeAlpha(0.5)
    c.setLineWidth(0.8)
    c.line(cx - 90 * mm, base_y + 4 * mm, cx - 50 * mm, base_y + 4 * mm)
    c.setStrokeAlpha(1)
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8)
    c.drawCentredString(cx - 70 * mm, base_y + 6 * mm, "ISSUED BY")
    c.setFillColor(WHITE)
    c.setFont("Courier-Bold", 12)
    c.drawCentredString(cx - 70 * mm, base_y - 1 * mm, ctx["org"])

    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8)
    c.drawCentredString(cx - 10 * mm, base_y + 6 * mm, "WEBSITE")
    c.setFillColor(WHITE)
    c.setFont("Courier-Bold", 12)
    c.drawCentredString(cx - 10 * mm, base_y - 1 * mm, ctx["site"])

    seal_x, seal_y, r = cx + 66 * mm, 31 * mm, 14 * mm
    c.setStrokeColor(AMBER)
    c.setLineWidth(2)
    c.circle(seal_x, seal_y, r, stroke=1, fill=0)
    c.setStrokeColor(GREEN)
    c.setLineWidth(1)
    c.circle(seal_x, seal_y, r - 2.4 * mm, stroke=1, fill=0)
    c.setFillColor(AMBER)
    c.setFont("Courier-Bold", 11)
    c.drawCentredString(seal_x, seal_y + 1.4 * mm, ctx["net_call"])
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7)
    c.drawCentredString(seal_x, seal_y - 4.4 * mm, ctx["year"])

    c.showPage()
    c.save()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="Generate PKTNET participation certificates (PDF).")
    ap.add_argument("-c", "--config",
                    help="pktnet config file (to read the database path)")
    ap.add_argument("--db", help="path to the SQLite database "
                                 "(overrides the config value)")
    ap.add_argument("--event", type=int,
                    help="event id (defaults to the most recent event)")
    ap.add_argument("--callsign",
                    help="generate for a single operator only")
    ap.add_argument("--names",
                    help="optional CSV 'callsign,name' to print operator names")
    ap.add_argument("--out", default="./certs",
                    help="output directory (default: ./certs)")
    ap.add_argument("--org", default="PP5PK", help="issuer / organiser")
    ap.add_argument("--site", default="pp5pk.net", help="issuer website")
    ap.add_argument("--radio",
                    help="background radio PNG (default: pktnet_radio.png "
                         "next to this script; pass '' to disable)")
    args = ap.parse_args()

    if args.db:
        db_path = args.db
    elif args.config:
        db_path = db_path_from_config(args.config)
    else:
        sys.exit("Provide --db or --config to locate the database.")

    conn = open_db(db_path)
    event = get_event(conn, args.event)
    checkins = get_checkins(conn, event["event_id"], args.callsign)
    if not checkins:
        sys.exit("No check-ins found for event #{}.".format(event["event_id"]))

    names = load_names(args.names)
    os.makedirs(args.out, exist_ok=True)

    date_br = fmt_date_br(event["event_date"])
    year = (event["event_date"] or "")[:4]

    # Optional stylised radio background (drawn faintly behind the text).
    if args.radio is not None:
        radio_path = args.radio
    else:
        radio_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "pktnet_radio.png")
    radio = _load_image(radio_path)

    made = 0
    for row in checkins:
        call = row["callsign"]
        ctx = {
            "net_call": event["net_call"],
            "event_name": event["name"],
            "date_br": date_br,
            "year": year,
            "callsign": call,
            "op_name": names.get(call.upper(), ""),
            "checkin_time": fmt_time_utc(row["ts_utc"]),
            "org": args.org,
            "site": args.site,
            "radio": radio,
        }
        fname = "{}_ev{}_{}.pdf".format(
            safe_filename(event["net_call"]), event["event_id"],
            safe_filename(call))
        out_path = os.path.join(args.out, fname)
        draw_certificate(out_path, ctx)
        made += 1
        print("  {} -> {}".format(call, out_path))

    conn.close()
    print("Generated {} certificate(s) for event #{} ({}).".format(
        made, event["event_id"], event["name"]))


if __name__ == "__main__":
    main()

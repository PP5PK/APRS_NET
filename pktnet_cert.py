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
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

# --------------------------------------------------------------------------- #
# Colourblind-safe palette (blue / amber)
# --------------------------------------------------------------------------- #

BLUE = HexColor("#12395B")      # deep blue - borders, title
BLUE_LT = HexColor("#2E6CA4")   # lighter blue - callsign
AMBER = HexColor("#E39A12")     # amber - accent lines, seal
AMBER_LT = HexColor("#F2C14E")  # light amber
INK = HexColor("#2B2B2B")       # body text
MUTED = HexColor("#6B6B6B")     # secondary text
CREAM = HexColor("#FBF7EF")     # background


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


# --------------------------------------------------------------------------- #
# Certificate drawing
# --------------------------------------------------------------------------- #

def draw_certificate(path, ctx):
    """Render a single certificate page to `path`."""
    page = landscape(A4)
    width, height = page
    cx = width / 2.0
    c = canvas.Canvas(path, pagesize=page)

    # Background
    c.setFillColor(CREAM)
    c.rect(0, 0, width, height, stroke=0, fill=1)

    # Outer blue frame
    c.setStrokeColor(BLUE)
    c.setLineWidth(3)
    c.rect(14 * mm, 14 * mm, width - 28 * mm, height - 28 * mm, stroke=1, fill=0)

    # Inner amber frame
    c.setStrokeColor(AMBER)
    c.setLineWidth(1.2)
    c.rect(17 * mm, 17 * mm, width - 34 * mm, height - 34 * mm, stroke=1, fill=0)

    # Net tag (top, monospace-like)
    c.setFillColor(BLUE_LT)
    c.setFont("Courier-Bold", 13)
    c.drawCentredString(cx, height - 34 * mm, ctx["net_call"] + "  \u2022  APRS NET")

    # Title
    c.setFillColor(BLUE)
    c.setFont("Helvetica-Bold", 30)
    c.drawCentredString(cx, height - 48 * mm, "CERTIFICADO DE PARTICIPA\u00c7\u00c3O")

    # Amber divider under the title
    c.setStrokeColor(AMBER)
    c.setLineWidth(2)
    c.line(cx - 55 * mm, height - 52 * mm, cx + 55 * mm, height - 52 * mm)

    # Lead line
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 13)
    c.drawCentredString(cx, height - 64 * mm, "Certificamos que a esta\u00e7\u00e3o")

    # Callsign (hero)
    c.setFillColor(BLUE_LT)
    c.setFont("Helvetica-Bold", 40)
    c.drawCentredString(cx, height - 82 * mm, ctx["callsign"])

    # Operator name (optional)
    line_y = height - 82 * mm
    if ctx.get("op_name"):
        c.setFillColor(INK)
        c.setFont("Helvetica-Oblique", 15)
        c.drawCentredString(cx, height - 90 * mm, ctx["op_name"])
        line_y = height - 90 * mm

    # Body sentence
    c.setFillColor(INK)
    c.setFont("Helvetica", 13.5)
    body = ("participou da {event}, realizada em {date},"
            .format(event=ctx["event_name"], date=ctx["date_br"]))
    c.drawCentredString(cx, line_y - 12 * mm, body)
    c.drawCentredString(
        cx, line_y - 19 * mm,
        "com check-in registrado \u00e0s {t} UTC.".format(t=ctx["checkin_time"]))

    # Signature block (left) + issue info (right)
    base_y = 30 * mm
    c.setStrokeColor(BLUE)
    c.setLineWidth(1)
    c.line(cx - 78 * mm, base_y, cx - 28 * mm, base_y)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(cx - 53 * mm, base_y - 5 * mm, ctx["org"])
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 9.5)
    c.drawCentredString(cx - 53 * mm, base_y - 10 * mm, "Organiza\u00e7\u00e3o  \u2022  " + ctx["site"])

    # Seal (right)
    seal_x, seal_y, r = cx + 55 * mm, base_y + 6 * mm, 15 * mm
    c.setStrokeColor(AMBER)
    c.setLineWidth(2)
    c.circle(seal_x, seal_y, r, stroke=1, fill=0)
    c.setStrokeColor(AMBER_LT)
    c.setLineWidth(1)
    c.circle(seal_x, seal_y, r - 2.2 * mm, stroke=1, fill=0)
    c.setFillColor(BLUE)
    c.setFont("Courier-Bold", 11)
    c.drawCentredString(seal_x, seal_y + 1.5 * mm, ctx["net_call"])
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7.5)
    c.drawCentredString(seal_x, seal_y - 4 * mm, ctx["year"])

    # Footer note
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 8)
    c.drawCentredString(
        cx, 20 * mm,
        "Emitido em {} UTC".format(
            datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M")))

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

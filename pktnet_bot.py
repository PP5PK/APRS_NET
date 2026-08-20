#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PKTNET - APRS Net check-in bot
================================

A small daemon that runs an APRS "net" in the style of #APRSThursday.

How it works
------------
* Users send an APRS message addressed to a special net callsign (e.g. PKTNET).
* The bot connects to APRS-IS with a verified login (e.g. PP5PK-3) and a
  group-message filter ("g/PKTNET") so it receives every message addressed to
  the net callsign, regardless of where it originated (RF or Internet).
* For each incoming message the bot:
    1. sends an APRS ACK (if the message carried a line number);
    2. records the check-in in a local SQLite database (one per operator per
       event);
    3. replies with a short confirmation that includes the operator's callsign.
* Outgoing ACKs and replies are injected with the NET callsign as the source,
  so the user sees the conversation coming from PKTNET. The verified login
  (PP5PK-3) is what authorises the injection.

Only the Python standard library is used (socket, sqlite3, configparser, ...).

Subcommands
-----------
    pktnet_bot.py run                       Run the daemon.
    pktnet_bot.py addevent NAME START END   Register a net event window (UTC).
    pktnet_bot.py events                    List registered events.
    pktnet_bot.py checkins [EVENT_ID]       List check-ins (latest event default).

73 - design built for PP5PK.
"""

import argparse
import configparser
import logging
import os
import re
import select
import signal
import smtplib
import socket
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import formataddr

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG_PATH = "/etc/pktnet/pktnet.conf"
APRS_MAX_TEXT = 67           # APRS message text hard limit (characters)
# Chars reserved for a "[i/n]: " part prefix when a reply spans several messages.
PART_RESERVE = 8
SOFTWARE_NAME = "PKTNET"
SOFTWARE_VERS = "1.0"

LOG = logging.getLogger("pktnet")

# Matches an APRS message ACK/REJ payload, e.g. "ack042" or "rej07".
ACK_RE = re.compile(r"^(ack|rej)([0-9A-Za-z]{1,5})$")

# Remote-control commands. Keys are the normalised message text (upper-case,
# surrounding brackets/spaces stripped); values are the internal action names.
COMMAND_ALIASES = {
    # public (any participant)
    "HELP": "help",
    "STATUS": "status",
    "LAST": "last",
    "TIME": "time",
    "ME": "me",
    "RESEND": "resend",
    # admin only
    "USERS": "users",
    "START": "start",
    "STOP": "stop", "END": "stop",
    "PAUSE": "pause",
    "RESTART": "restart", "RESUME": "restart",
}

PUBLIC_ACTIONS = {"help", "status", "last", "time", "me", "resend"}
ADMIN_ACTIONS = {"users", "start", "stop", "pause", "restart"}

# Group-chat room commands (messages addressed to the room callsign).
ROOM_COMMAND_ALIASES = {
    "JOIN": "join", "IN": "join",
    "LEAVE": "leave", "QRT": "leave", "OUT": "leave",
    "WHO": "who",
    "HELP": "help", "?": "help",
}

# Command names shown by HELP, per permission group.
HELP_PUBLIC = ["HELP", "STATUS", "LAST", "TIME", "ME", "RESEND"]
HELP_ADMIN = ["USERS", "START", "STOP", "PAUSE", "RESTART"]


def base_call(call):
    """Return the base callsign without its SSID (PP5MFA-7 -> PP5MFA)."""
    return call.split("-", 1)[0].upper().strip()


def _parse_calls(raw):
    """Parse a comma/space separated callsign list into a set of base calls."""
    parts = re.split(r"[,\s]+", raw.strip())
    return {base_call(p) for p in parts if p}


def _iso_to_dt(iso):
    """Parse a stored ISO 8601 timestamp into a tz-aware UTC datetime."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt_duration(td):
    """Human-friendly remaining time, e.g. '2h05m' or '18m'."""
    secs = int(td.total_seconds())
    if secs <= 0:
        return "ending now"
    hours, rem = divmod(secs, 3600)
    minutes = rem // 60
    return "{}h{:02d}m".format(hours, minutes) if hours else "{}m".format(minutes)


def parse_command(text):
    """Parse a remote-control message into (action, argument).

    The command is the first word (case-insensitive, optional [brackets]); the
    rest is an optional argument with its own surrounding brackets stripped.
    Returns (None, "") when the first word is not a known command.
    Examples:
      "[STATUS]"                 -> ("status", "")
      "START [Rede da Serra]"-> ("start", "Rede da Serra")
      "start minha rede"    -> ("start", "minha rede")
    """
    parts = text.strip().split(None, 1)
    if not parts:
        return None, ""
    action = COMMAND_ALIASES.get(parts[0].strip("[]").strip().upper())
    if action is None:
        return None, ""
    arg = parts[1].strip().strip("[]").strip() if len(parts) > 1 else ""
    return action, arg


# Lowercase connective particles kept in lower case inside names (unless first).
NAME_PARTICLES = {
    "de", "del", "della", "di", "do", "dos", "das", "da", "du", "van", "von",
    "der", "den", "la", "le", "lo", "los", "e", "y",
}


def name_case(raw):
    """Title-case a personal name, keeping connective particles lower case.

    The first word is always capitalised; recognised particles elsewhere stay
    lower case. Applies to hyphenated parts too (Jean-Pierre). Runs even on
    user-typed names, since fixing case on a radio keypad is awkward.
    """
    words = raw.split()
    out = []
    for i, word in enumerate(words):
        subs = word.split("-")
        cased = []
        for j, s in enumerate(subs):
            if not s:
                cased.append(s)
                continue
            low = s.lower()
            if i == 0 and j == 0:
                cased.append(low[:1].upper() + low[1:])
            elif low in NAME_PARTICLES:
                cased.append(low)
            else:
                cased.append(low[:1].upper() + low[1:])
        out.append("-".join(cased))
    return " ".join(out)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def load_config(path):
    """Load and validate the INI configuration file."""
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        LOG.error("Config file not found or unreadable: %s", path)
        sys.exit(2)

    out = {
        "server": cfg.get("aprsis", "server", fallback="rotate.aprs2.net"),
        "port": cfg.getint("aprsis", "port", fallback=14580),
        "login_call": cfg.get("aprsis", "login_call", fallback="").upper().strip(),
        "passcode": cfg.get("aprsis", "passcode", fallback="").strip(),
        "net_call": cfg.get("aprsis", "net_call", fallback="").upper().strip(),

        "require_active_event": cfg.getboolean("net", "require_active_event",
                                               fallback=True),
        "confirm_text": cfg.get("net", "confirm_text",
                                fallback="Check-in OK {time}z. 73 de PP5PK"),
        "dup_text": cfg.get("net", "dup_text",
                            fallback="Already registered {time}z. 73 de PP5PK"),
        "closed_text": cfg.get("net", "closed_text",
                               fallback="PKTNET not active. 73 de PP5PK"),
        "paused_text": cfg.get("net", "paused_text",
                               fallback="PKTNET under maintenance, try again "
                                        "in a few minutes."),
        "admin_calls": _parse_calls(cfg.get("net", "admin_calls", fallback="")),
        "checkin_keyword": cfg.get("net", "checkin_keyword",
                                   fallback="CHECK").upper().strip(),
        "checkin_hint": cfg.get("net", "checkin_hint",
                                fallback="Send CHECK to join the net. 73 de "
                                         "PP5PK"),

        # Group chat room (optional). Empty room_call disables the room.
        "room_call": cfg.get("room", "room_call", fallback="").upper().strip(),
        "room_timeout_min": cfg.getint("room", "timeout_min", fallback=60),
        "room_max": cfg.getint("room", "max_members", fallback=30),
        "room_min_interval": cfg.getint("room", "min_interval", fallback=3),

        "max_retries": cfg.getint("messaging", "max_retries", fallback=3),
        "retry_interval": cfg.getint("messaging", "retry_interval", fallback=30),
        "reply_delay": cfg.getfloat("messaging", "reply_delay", fallback=1.5),
        "keepalive_interval": cfg.getint("messaging", "keepalive_interval",
                                         fallback=20),
        "rx_timeout": cfg.getint("messaging", "rx_timeout", fallback=90),

        "db_path": cfg.get("db", "path", fallback="/var/lib/pktnet/pktnet.db"),

        # Certificate flow (optional). enable=false keeps it off.
        "cert_enable": cfg.getboolean("cert", "enable", fallback=False),
        "cert_dir": cfg.get("cert", "dir", fallback="/var/lib/pktnet/certs"),
        "cert_users_db": cfg.get("cert", "users_db",
                                 fallback="/var/lib/pktnet/certs/users.db"),
        "cert_flow_timeout_min": cfg.getint("cert", "flow_timeout_min",
                                            fallback=10),
        "cert_template": cfg.get("cert", "template",
                                 fallback="/var/lib/pktnet/certs/"
                                          "pktnet_template.png"),

        # Email delivery of certificates (SMTP, e.g. Brevo). Off by default.
        "email_enable": cfg.getboolean("email", "enable", fallback=False),
        "email_host": cfg.get("email", "host",
                              fallback="smtp-relay.brevo.com"),
        "email_port": cfg.getint("email", "port", fallback=587),
        "email_user": cfg.get("email", "user", fallback=""),
        "email_password": cfg.get("email", "password", fallback=""),
        "email_from": cfg.get("email", "from", fallback=""),
        "email_from_name": cfg.get("email", "from_name", fallback="PKTNET"),
        "email_reply_to": cfg.get("email", "reply_to", fallback="").strip(),
        "email_subject": cfg.get("email", "subject",
                                 fallback="Your PKTNET participation "
                                          "certificate"),
        "email_body": cfg.get("email", "body", fallback=(
            "Hello {name},\n\n"
            "Attached is your certificate for taking part in the PKTNET "
            "amateur radio net.\n\n"
            "Your email address was used only to deliver this certificate "
            "and is not shared with anyone.\n\n"
            "73 de PP5PK")),
    }

    if not out["login_call"] or not out["passcode"] or not out["net_call"]:
        LOG.error("login_call, passcode and net_call are all required in %s", path)
        sys.exit(2)
    return out


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

def init_db(path):
    """Open the SQLite database, creating the schema on first run."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            event_date TEXT    NOT NULL,          -- YYYY-MM-DD (UTC)
            start_utc  TEXT    NOT NULL,          -- ISO 8601 UTC
            end_utc    TEXT    NOT NULL,          -- ISO 8601 UTC
            net_call   TEXT    NOT NULL,
            status     TEXT    NOT NULL DEFAULT 'open'  -- open | paused | closed
        );

        CREATE TABLE IF NOT EXISTS checkins (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id  INTEGER NOT NULL REFERENCES events(event_id),
            callsign  TEXT    NOT NULL,
            ts_utc    TEXT    NOT NULL,           -- ISO 8601 UTC
            message   TEXT,
            UNIQUE(event_id, callsign)
        );

        CREATE TABLE IF NOT EXISTS room_members (
            callsign      TEXT PRIMARY KEY,
            joined_utc    TEXT NOT NULL,
            last_seen_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cert_flow (
            callsign     TEXT PRIMARY KEY,   -- source call currently in a flow
            event_id     INTEGER,
            state        TEXT NOT NULL,      -- reuse|await_email|confirm_name|await_name
            email        TEXT,
            name_cand    TEXT,
            updated_utc  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cert_contacts (
            callsign     TEXT PRIMARY KEY,   -- base call
            email        TEXT,
            name         TEXT,
            updated_utc  TEXT NOT NULL
        );
        """
    )
    # Migration: add 'status' to pre-existing events tables.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "status" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN status TEXT NOT NULL "
                     "DEFAULT 'open'")
    conn.commit()
    return conn


def get_active_event(conn, now_iso):
    """Return the active (non-closed) event whose window contains now_iso."""
    cur = conn.execute(
        "SELECT * FROM events "
        "WHERE status != 'closed' AND start_utc <= ? AND end_utc >= ? "
        "ORDER BY start_utc DESC LIMIT 1",
        (now_iso, now_iso),
    )
    return cur.fetchone()


def record_checkin(conn, event_id, callsign, ts_iso, message):
    """Insert a check-in. Return True if new, False if it was a duplicate."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO checkins (event_id, callsign, ts_utc, message) "
        "VALUES (?, ?, ?, ?)",
        (event_id, callsign, ts_iso, message),
    )
    conn.commit()
    return cur.rowcount == 1


def count_checkins(conn, event_id):
    row = conn.execute(
        "SELECT COUNT(*) FROM checkins WHERE event_id = ?", (event_id,)
    ).fetchone()
    return row[0] if row else 0


def list_checkin_calls(conn, event_id):
    rows = conn.execute(
        "SELECT callsign FROM checkins WHERE event_id = ? ORDER BY ts_utc",
        (event_id,),
    ).fetchall()
    return [r["callsign"] for r in rows]


def end_event(conn, event_id, now_iso):
    """Close an event now (status=closed, end_utc=now). Return the row or None."""
    row = conn.execute("SELECT * FROM events WHERE event_id = ?",
                       (event_id,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE events SET end_utc = ?, status = 'closed' "
                 "WHERE event_id = ?", (now_iso, event_id))
    conn.commit()
    return row


def set_event_status(conn, event_id, status):
    conn.execute("UPDATE events SET status = ? WHERE event_id = ?",
                 (status, event_id))
    conn.commit()


def last_checkin_calls(conn, event_id, limit=5):
    """Most recent check-in callsigns first."""
    rows = conn.execute(
        "SELECT callsign FROM checkins WHERE event_id = ? "
        "ORDER BY ts_utc DESC LIMIT ?", (event_id, limit),
    ).fetchall()
    return [r["callsign"] for r in rows]


def checkins_by_base(conn, event_id, base):
    """Return [(callsign, ts_utc), ...] for every SSID of `base` in the event."""
    rows = conn.execute(
        "SELECT callsign, ts_utc FROM checkins "
        "WHERE event_id = ? AND (callsign = ? OR callsign LIKE ?) "
        "ORDER BY ts_utc", (event_id, base, base + "-%"),
    ).fetchall()
    return [(r["callsign"], r["ts_utc"]) for r in rows]


def total_nets_by_base(conn, base):
    """Distinct events joined by any SSID of the base callsign."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT event_id) FROM checkins "
        "WHERE callsign = ? OR callsign LIKE ?", (base, base + "-%"),
    ).fetchone()
    return row[0] if row else 0


def create_daily_net(conn, name, date_str, start_iso, end_iso, net_call):
    conn.execute(
        "INSERT INTO events (name, event_date, start_utc, end_utc, net_call, "
        "status) VALUES (?, ?, ?, ?, ?, 'open')",
        (name, date_str, start_iso, end_iso, net_call),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def delete_event(conn, event_id):
    """Delete an event and its check-ins. Return the number of check-ins
    removed, or None if the event did not exist."""
    row = conn.execute("SELECT event_id FROM events WHERE event_id = ?",
                       (event_id,)).fetchone()
    if row is None:
        return None
    n = count_checkins(conn, event_id)
    conn.execute("DELETE FROM checkins WHERE event_id = ?", (event_id,))
    conn.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
    conn.commit()
    return n


# --- group chat room ------------------------------------------------------- #

def room_join(conn, call, now_iso):
    conn.execute("INSERT OR IGNORE INTO room_members "
                 "(callsign, joined_utc, last_seen_utc) VALUES (?, ?, ?)",
                 (call, now_iso, now_iso))
    conn.execute("UPDATE room_members SET last_seen_utc = ? WHERE callsign = ?",
                 (now_iso, call))
    conn.commit()


def room_leave(conn, call):
    conn.execute("DELETE FROM room_members WHERE callsign = ?", (call,))
    conn.commit()


def room_touch(conn, call, now_iso):
    conn.execute("UPDATE room_members SET last_seen_utc = ? WHERE callsign = ?",
                 (now_iso, call))
    conn.commit()


def room_is_member(conn, call):
    return conn.execute("SELECT 1 FROM room_members WHERE callsign = ?",
                        (call,)).fetchone() is not None


def room_members_list(conn):
    return [r["callsign"] for r in conn.execute(
        "SELECT callsign FROM room_members ORDER BY joined_utc").fetchall()]


def room_count(conn):
    return conn.execute("SELECT COUNT(*) FROM room_members").fetchone()[0]


def room_prune(conn, deadline_iso):
    """Remove members whose last activity is older than deadline_iso."""
    conn.execute("DELETE FROM room_members WHERE last_seen_utc < ?",
                 (deadline_iso,))
    conn.commit()


# --- certificate flow ------------------------------------------------------ #

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def looks_like_email(text):
    return bool(EMAIL_RE.match(text.strip()))


def lookup_operator_name(users_db_path, base):
    """Look up the full name for a base callsign in the (read-only) users DB."""
    if not users_db_path or not os.path.exists(users_db_path):
        return None
    try:
        uri = "file:{}?mode=ro".format(users_db_path)
        c = sqlite3.connect(uri, uri=True)
        row = c.execute("SELECT name FROM users WHERE callsign = ?",
                        (base,)).fetchone()
        c.close()
        return row[0] if row and row[0] and row[0].strip() else None
    except sqlite3.Error:
        return None


def set_cert_flow(conn, call, event_id, state, email, name_cand, now_iso):
    conn.execute(
        "INSERT INTO cert_flow (callsign, event_id, state, email, name_cand, "
        "updated_utc) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(callsign) DO UPDATE SET event_id=excluded.event_id, "
        "state=excluded.state, email=excluded.email, "
        "name_cand=excluded.name_cand, updated_utc=excluded.updated_utc",
        (call, event_id, state, email, name_cand, now_iso))
    conn.commit()


def get_cert_flow(conn, call):
    return conn.execute("SELECT * FROM cert_flow WHERE callsign = ?",
                        (call,)).fetchone()


def clear_cert_flow(conn, call):
    conn.execute("DELETE FROM cert_flow WHERE callsign = ?", (call,))
    conn.commit()


def prune_cert_flows(conn, deadline_iso):
    conn.execute("DELETE FROM cert_flow WHERE updated_utc < ?", (deadline_iso,))
    conn.commit()


def get_cert_contact(conn, base):
    return conn.execute("SELECT * FROM cert_contacts WHERE callsign = ?",
                        (base,)).fetchone()


def save_cert_contact(conn, base, email, name, now_iso):
    conn.execute(
        "INSERT INTO cert_contacts (callsign, email, name, updated_utc) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(callsign) DO UPDATE SET "
        "email=excluded.email, name=excluded.name, "
        "updated_utc=excluded.updated_utc",
        (base, email, name, now_iso))
    conn.commit()


# --------------------------------------------------------------------------- #
# APRS packet parsing / building
# --------------------------------------------------------------------------- #

def parse_packet_line(line):
    """Split a raw APRS-IS line into (source_call, info_field) or None."""
    line = line.rstrip("\r\n")
    if not line or line.startswith("#"):
        return None                      # server comment / keepalive
    if ">" not in line or ":" not in line:
        return None
    header, _, info = line.partition(":")
    source = header.split(">", 1)[0].strip().upper()
    if not source:
        return None
    return source, info


def parse_message(info):
    """
    Parse a message info field of the form ':ADDRESSEE :text{msgno'.

    Returns (addressee, text, msgno) where msgno may be None, or None if the
    info field is not a well-formed APRS message.
    """
    if not info.startswith(":") or len(info) < 11 or info[10] != ":":
        return None
    addressee = info[1:10].strip().upper()
    rest = info[11:].rstrip("\r\n")

    msgno = None
    text = rest
    if "{" in rest:
        text, _, tail = rest.rpartition("{")
        # tail may be "042", "042}" or a reply-ack form like "AB}CD"
        msgno = tail.split("}")[0].strip() or None
    return addressee, text, msgno


def pad_callsign(call):
    """APRS message addressee field is exactly 9 characters, space padded."""
    return call[:9].ljust(9)


def build_ack(net_call, to_call, msgno):
    return "{src}>APRS,TCPIP*::{dst}:ack{no}".format(
        src=net_call, dst=pad_callsign(to_call), no=msgno
    )


def build_message(net_call, to_call, text, msgno=None):
    text = text[:APRS_MAX_TEXT]
    body = "{src}>APRS,TCPIP*::{dst}:{txt}".format(
        src=net_call, dst=pad_callsign(to_call), txt=text
    )
    if msgno is not None:
        body += "{" + str(msgno)
    return body


# --------------------------------------------------------------------------- #
# The bot
# --------------------------------------------------------------------------- #

class PktNetBot:
    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn
        self.sock = None
        self.rxbuf = ""
        self.running = True
        self._out_seq = 0
        # pending[(to_call, msgno)] = {"line": str, "attempts": int, "next": float}
        self.pending = {}
        # Earliest wall-clock time the next NEW reply may first transmit, used to
        # space the reply after its ACK and to space consecutive replies.
        self._tx_gate = 0.0
        self.last_rx = 0.0
        self.last_keepalive = 0.0
        self._room_last = {}   # in-memory per-sender relay cooldown

    # -- lifecycle --------------------------------------------------------- #

    def stop(self, *_):
        self.running = False

    def run_forever(self):
        backoff = 5
        while self.running:
            try:
                self._connect()
                backoff = 5
                self._loop()
            except (socket.error, OSError) as exc:
                LOG.warning("Connection problem: %s", exc)
            finally:
                self._close_socket()
            if self.running:
                LOG.info("Reconnecting in %ss ...", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)

    def _connect(self):
        cfg = self.cfg
        LOG.info("Connecting to %s:%s", cfg["server"], cfg["port"])
        self.sock = socket.create_connection((cfg["server"], cfg["port"]),
                                              timeout=15)
        self.sock.settimeout(1.0)
        gfilter = ("g/{}/{}".format(cfg["net_call"], cfg["room_call"])
                   if cfg["room_call"] else "g/" + cfg["net_call"])
        login = ("user {call} pass {pc} vers {name} {ver} filter {flt}\r\n"
                 .format(call=cfg["login_call"], pc=cfg["passcode"],
                         name=SOFTWARE_NAME, ver=SOFTWARE_VERS, flt=gfilter))
        self.sock.sendall(login.encode("ascii", "replace"))
        now = time.time()
        self.last_keepalive = now
        self.rxbuf = ""

        verdict = self._read_login_response(now + 5.0)
        self.last_rx = time.time()
        watching = cfg["net_call"] + (" + " + cfg["room_call"]
                                      if cfg["room_call"] else "")
        if verdict == "unverified":
            LOG.warning(
                "APRS-IS login UNVERIFIED for %s - check the passcode in your "
                "config; the server will DROP all ACKs and replies until this "
                "is fixed", cfg["login_call"])
        elif verdict == "verified":
            LOG.info("Logged in as %s (verified), watching %s",
                     cfg["login_call"], watching)
        else:
            LOG.info("Logged in as %s, watching %s (login response not seen)",
                     cfg["login_call"], watching)

    def _read_login_response(self, deadline):
        """Read the server banner / '# logresp' line after login.

        Returns 'verified', 'unverified', or None if no logresp line is seen
        before `deadline`. Anything read is left in the RX buffer so the main
        loop still processes any packets that arrived during the window.
        """
        while time.time() < deadline:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                raise socket.error("server closed the connection during login")
            self.rxbuf += data.decode("utf-8", "replace")
            for line in self.rxbuf.split("\n")[:-1]:
                if line.startswith("# logresp"):
                    low = line.lower()
                    if "unverified" in low:      # check before "verified"
                        return "unverified"
                    if "verified" in low:
                        return "verified"
        return None

    def _close_socket(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # -- main loop --------------------------------------------------------- #

    def _loop(self):
        while self.running:
            ready, _, _ = select.select([self.sock], [], [], 1.0)
            now = time.time()

            if ready:
                data = self.sock.recv(4096)
                if not data:
                    raise socket.error("server closed the connection")
                self.last_rx = now
                self.rxbuf += data.decode("utf-8", "replace")
                while "\n" in self.rxbuf:
                    line, self.rxbuf = self.rxbuf.split("\n", 1)
                    self._handle_line(line)

            # Reconnect if the server has gone silent for too long.
            if now - self.last_rx > self.cfg["rx_timeout"]:
                raise socket.error("no data received within rx_timeout")

            # Keepalive comment to hold the link / NAT open.
            if now - self.last_keepalive >= self.cfg["keepalive_interval"]:
                self._send_raw("# {} keepalive".format(SOFTWARE_NAME))
                self.last_keepalive = now

            self._service_pending(now)

    # -- inbound ----------------------------------------------------------- #

    def _handle_line(self, line):
        if line.startswith("#"):
            return
        parsed = parse_packet_line(line)
        if not parsed:
            return
        source, info = parsed
        msg = parse_message(info)
        if not msg:
            return
        addressee, text, msgno = msg

        net_call = self.cfg["net_call"]
        room_call = self.cfg["room_call"]

        # Ignore anything we ourselves injected (anti-loop).
        if source == net_call or (room_call and source == room_call):
            return

        is_net = addressee == net_call
        is_room = bool(room_call) and addressee == room_call
        if not (is_net or is_room):
            return

        # Is it an ACK/REJ for one of our outgoing replies?
        m = ACK_RE.match(text.strip())
        if m:
            self._clear_pending(source, m.group(2))
            return

        LOG.info("Message from %s to %s: %r (msgno=%s)",
                 source, addressee, text, msgno)

        # Courtesy ACK, sent from whichever callsign was addressed, so the
        # sender's radio stops retransmitting.
        if msgno:
            self._send_raw(build_ack(addressee, source, msgno))

        if is_room:
            self._handle_room(source, text)
            return

        # In the middle of a certificate flow? Route the reply there.
        if self.cfg["cert_enable"] and self._cert_flow_active(source):
            self._handle_cert_flow(source, text)
            return

        # Remote-control command?
        action, arg = parse_command(text)
        if action:
            is_admin = base_call(source) in self.cfg["admin_calls"]
            if action in PUBLIC_ACTIONS or (action in ADMIN_ACTIONS and is_admin):
                LOG.info("Command %s from %s", action, source)
                self._handle_command(source, action, is_admin, arg)
                return
            # recognised admin command from a non-admin: treat as a check-in

        # Only the check-in keyword starts a check-in; anything else is ignored
        # (an optional hint is sent so operators know how to join).
        first = text.strip().strip("[]").strip().split(None, 1)
        keyword = first[0].upper() if first else ""
        if keyword == self.cfg["checkin_keyword"]:
            self._process_checkin(source, text)
        elif self.cfg["checkin_hint"]:
            LOG.info("Non-check message from %s: %r", source, text)
            self._enqueue_reply(source, self.cfg["checkin_hint"])
        else:
            LOG.info("Ignoring non-check message from %s: %r", source, text)

    # -- group chat room --------------------------------------------------- #

    def _handle_room(self, source, text):
        room = self.cfg["room_call"]
        conn = self.conn
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # Drop members idle beyond the timeout.
        deadline = (now - timedelta(
            minutes=self.cfg["room_timeout_min"])).isoformat()
        room_prune(conn, deadline)

        raction = ROOM_COMMAND_ALIASES.get(text.strip().strip("[]").strip().upper())

        if raction == "join":
            if not room_is_member(conn, source) and \
                    room_count(conn) >= self.cfg["room_max"]:
                self._enqueue_reply(source, "{} is full, try later.".format(room),
                                    from_call=room)
                return
            new = not room_is_member(conn, source)
            room_join(conn, source, now_iso)
            LOG.info("%s joined room %s", source, room)
            self._enqueue_reply(
                source, "Joined {} ({} here). Send text to talk; LEAVE to "
                "exit.".format(room, room_count(conn)), from_call=room)
            if new:
                self._relay_system(source, "{} joined".format(source))
            return

        if raction == "leave":
            if room_is_member(conn, source):
                room_leave(conn, source)
                LOG.info("%s left room %s", source, room)
                self._enqueue_reply(source, "Left {}. 73!".format(room),
                                    from_call=room)
                self._relay_system(source, "{} left".format(source))
            else:
                self._enqueue_reply(source, "You are not in {}.".format(room),
                                    from_call=room)
            return

        if raction == "who":
            members = room_members_list(conn)
            if not members:
                self._enqueue_reply(source, "{} is empty.".format(room),
                                    from_call=room)
            else:
                self._enqueue_pack(source, members,
                                   prefix="In {}: ".format(room), from_call=room)
            return

        if raction == "help":
            self._enqueue_reply(
                source, "{}: JOIN, LEAVE, WHO, HELP. Send text to talk.".format(
                    room), from_call=room)
            return

        # Not a command: a chat message to relay.
        if not room_is_member(conn, source):
            self._enqueue_reply(
                source, "Not in {}. Send JOIN to enter.".format(room),
                from_call=room)
            return

        # Simple flood guard: drop (already ACKed) if the member is too fast.
        last = self._room_last.get(source)
        self._room_last[source] = now
        if last and (now - last).total_seconds() < self.cfg["room_min_interval"]:
            LOG.info("Rate-limited relay from %s", source)
            return

        room_touch(conn, source, now_iso)
        self._relay(source, text)

    def _relay(self, source, text):
        """Relay a member's message to every other member (no ACK, no retry)."""
        room = self.cfg["room_call"]
        body = "{}: {}".format(source, text)
        n = 0
        for member in room_members_list(self.conn):
            if member == source:
                continue
            self._send_noack(room, member, body)
            n += 1
        LOG.info("Relayed from %s to %d member(s)", source, n)

    def _relay_system(self, source, text):
        """Send a short system notice (join/left) to the other members."""
        room = self.cfg["room_call"]
        for member in room_members_list(self.conn):
            if member == source:
                continue
            self._send_noack(room, member, "* " + text)

    def _send_noack(self, from_call, to_call, text):
        self._send_raw(build_message(from_call, to_call, text))

    # -- remote control ---------------------------------------------------- #

    def _handle_command(self, source, action, is_admin, arg=""):
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        conn = self.conn
        event = get_active_event(conn, now_iso)

        # --- public commands --------------------------------------------- #
        if action == "help":
            cmds = list(HELP_PUBLIC)
            if not self.cfg["cert_enable"]:
                cmds = [c for c in cmds if c != "RESEND"]
            cmds += (HELP_ADMIN if is_admin else [])
            self._enqueue_pack(source, cmds, sep=", ")
            return

        if action == "resend":
            if not self.cfg["cert_enable"]:
                self._enqueue_reply(source, "Certificates are not available.")
                return
            base = base_call(source)
            contact = get_cert_contact(conn, base)
            if not (contact and contact["email"]):
                self._enqueue_reply(
                    source, "No certificate on file. Do a check-in first.")
                return
            row = conn.execute(
                "SELECT event_id FROM checkins WHERE callsign = ? OR "
                "callsign LIKE ? ORDER BY ts_utc DESC LIMIT 1",
                (base, base + "-%")).fetchone()
            if row is None:
                self._enqueue_reply(source, "No check-in found to resend.")
                return
            name, email = contact["name"], contact["email"]
            path = self._generate_cert(row["event_id"], source, base, name)
            if not path:
                self._enqueue_reply(source, "Sorry, could not build the "
                                            "certificate right now.")
                return
            if self.cfg["email_enable"] and self.cfg["email_from"]:
                self._send_cert_email_async(email, name, path)
                self._enqueue_reply(source, "Resent to {}! 73".format(email))
            else:
                self._enqueue_reply(source, "Certificate ready as {}! 73".format(
                    name))
            return

        if action == "status":
            if event is None:
                self._enqueue_reply(source, "No active net right now.")
            else:
                n = count_checkins(conn, event["event_id"])
                self._enqueue_reply(source, "{}: {} check-in(s)".format(
                    event["name"], n))
            return

        if action == "last":
            if event is None:
                self._enqueue_reply(source, "No active net right now.")
                return
            calls = last_checkin_calls(conn, event["event_id"], 5)
            if not calls:
                self._enqueue_reply(source, "{}: no check-ins yet".format(
                    event["name"]))
                return
            self._enqueue_pack(source, calls,
                               prefix="{} last: ".format(event["name"]))
            return

        if action == "time":
            if event is None:
                self._enqueue_reply(source, "No active net (no end time set).")
                return
            remaining = _iso_to_dt(event["end_utc"]) - now
            self._enqueue_reply(source, "Time left: {}".format(
                _fmt_duration(remaining)))
            return

        if action == "me":
            base = base_call(source)
            total = total_nets_by_base(conn, base)
            head = "You ({}) {} net(s):".format(base, total)
            details = []
            if event is not None:
                for cs, ts in checkins_by_base(conn, event["event_id"], base):
                    details.append("{} {}z".format(
                        cs, _iso_to_dt(ts).strftime("%H%M")))
            if details:
                self._enqueue_pack(source, details, prefix=head + " ")
            else:
                self._enqueue_reply(source, head + " not checked in now")
            return

        # --- admin commands ---------------------------------------------- #
        if action == "users":
            if event is None:
                self._enqueue_reply(source, "No active net right now.")
                return
            calls = list_checkin_calls(conn, event["event_id"])
            if not calls:
                self._enqueue_reply(source, "No check-ins yet.")
                return
            self._enqueue_pack(source, calls)
            return

        if action == "start":
            if event is not None:
                self._enqueue_reply(source, "Net already running: {}".format(
                    event["name"]))
                return
            date_str = now.strftime("%Y-%m-%d")
            end = now.replace(hour=23, minute=59, second=59, microsecond=0)
            name = arg[:40].strip() if arg else "APRS PKTNET " + date_str
            create_daily_net(conn, name, date_str, now_iso, end.isoformat(),
                             self.cfg["net_call"])
            LOG.info("Net started by %s: %s", source, name)
            self._enqueue_reply(source, "Net started: {} (until 2359z)".format(
                name))
            return

        if action == "stop":
            if event is None:
                self._enqueue_reply(source, "No active net to stop.")
                return
            end_event(conn, event["event_id"], now_iso)
            LOG.info("Net #%s stopped by %s", event["event_id"], source)
            self._enqueue_reply(source, "Net stopped: {}".format(event["name"]))
            return

        if action == "pause":
            if event is None:
                self._enqueue_reply(source, "No active net to pause.")
                return
            set_event_status(conn, event["event_id"], "paused")
            LOG.info("Net #%s paused by %s", event["event_id"], source)
            self._enqueue_reply(source, "Net paused: {}".format(event["name"]))
            return

        if action == "restart":
            if event is None or event["status"] != "paused":
                self._enqueue_reply(source, "No paused net to resume.")
                return
            set_event_status(conn, event["event_id"], "open")
            LOG.info("Net #%s resumed by %s", event["event_id"], source)
            self._enqueue_reply(source, "Net resumed: {}".format(event["name"]))
            return

    @staticmethod
    def _pack(tokens, sep=" ", prefix="", limit=APRS_MAX_TEXT):
        """Pack tokens into messages that fit the APRS text limit; the prefix
        is added once, at the start of the first message."""
        lines, cur, first = [], "", True
        for tok in tokens:
            candidate = (prefix + tok) if (first and not cur) else (
                tok if not cur else cur + sep + tok)
            if len(candidate) > limit and cur:
                lines.append(cur)
                first = False
                cur = tok
            else:
                cur = candidate
        if cur:
            lines.append(cur)
        return lines or [prefix.rstrip()]

    def _process_checkin(self, source, text):
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        hhmm = now.strftime("%H%M")

        event = get_active_event(self.conn, now_iso)

        if event is not None and event["status"] == "paused":
            LOG.info("Net paused - check-in from %s deferred", source)
            self._enqueue_reply(source, self.cfg["paused_text"])
            return

        if event is None and self.cfg["require_active_event"]:
            LOG.info("No active event - check-in from %s ignored", source)
            self._enqueue_reply(source, self.cfg["closed_text"].format(time=hhmm))
            return

        if event is None:
            # Open mode: log under an ad-hoc event named for today's date.
            event = self._ensure_adhoc_event(now)

        is_new = record_checkin(self.conn, event["event_id"], source,
                                now_iso, text)
        template = self.cfg["confirm_text"] if is_new else self.cfg["dup_text"]
        reply = template.format(time=hhmm, call=source, event=event["name"])
        if is_new:
            LOG.info("Logged %s into event #%s (%s)",
                     source, event["event_id"], event["name"])
        else:
            LOG.info("%s already logged into event #%s",
                     source, event["event_id"])
        self._enqueue_reply(source, reply)

        # Offer a certificate on a first-time check-in.
        if is_new and self.cfg["cert_enable"]:
            self._start_cert_flow(source, event["event_id"])

    # -- certificate flow -------------------------------------------------- #

    def _cert_flow_active(self, source):
        deadline = (datetime.now(timezone.utc) - timedelta(
            minutes=self.cfg["cert_flow_timeout_min"])).isoformat()
        prune_cert_flows(self.conn, deadline)
        return get_cert_flow(self.conn, source) is not None

    def _start_cert_flow(self, source, event_id):
        conn = self.conn
        now_iso = datetime.now(timezone.utc).isoformat()
        contact = get_cert_contact(conn, base_call(source))
        if contact and contact["email"]:
            set_cert_flow(conn, source, event_id, "reuse", contact["email"],
                          contact["name"], now_iso)
            nm = contact["name"] or "?"
            em = contact["email"]
            lines = ["Use previous info? YES / NO"]
            combined = "Prev: {} / {}".format(nm, em)
            if len(combined) <= APRS_MAX_TEXT - PART_RESERVE:
                lines.append(combined)
            else:
                lines.append("Prev name: " + nm)
                lines.append("Prev email: " + em)
            self._enqueue_numbered(source, lines)
        else:
            set_cert_flow(conn, source, event_id, "await_email", None, None,
                          now_iso)
            self._enqueue_reply(
                source, "Want a certificate? Reply your email (only to send "
                "it) or NO")

    def _handle_cert_flow(self, source, text):
        conn = self.conn
        row = get_cert_flow(conn, source)
        if row is None:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        t = text.strip()
        low = t.lower()
        state = row["state"]

        # In the reuse step, NO means "don't reuse" -> collect data fresh, the
        # same as a first-time check-in (the operator can then decline the
        # certificate at the email question). It does NOT cancel here.
        if state == "reuse":
            if low in ("yes", "sim", "y", "ok"):
                self._finish_cert(source, row["event_id"], row["email"],
                                  row["name_cand"])
            elif low in ("no", "nao", "n"):
                set_cert_flow(conn, source, row["event_id"], "await_email",
                              None, None, now_iso)
                self._enqueue_reply(
                    source, "Want a certificate? Reply your email (only to "
                    "send it) or NO")
            elif looks_like_email(t):
                self._after_cert_email(source, row["event_id"], t)
            else:
                self._enqueue_reply(source, "Reply YES or NO")
            return

        # In the remaining steps, NO cancels the certificate.
        if low in ("no", "nao", "n", "cancel"):
            clear_cert_flow(conn, source)
            self._enqueue_reply(source, "OK, no certificate. 73!")
            return

        if state == "await_email":
            if looks_like_email(t):
                self._after_cert_email(source, row["event_id"], t)
            else:
                self._enqueue_reply(source, "Please reply a valid email or NO")
            return

        if state == "confirm_name":
            name = row["name_cand"] if low in ("yes", "sim", "y", "ok") \
                else t[:40]
            self._finish_cert(source, row["event_id"], row["email"], name)
            return

        if state == "await_name":
            self._finish_cert(source, row["event_id"], row["email"], t[:40])
            return

    def _after_cert_email(self, source, event_id, email):
        conn = self.conn
        now_iso = datetime.now(timezone.utc).isoformat()
        found = lookup_operator_name(self.cfg["cert_users_db"],
                                     base_call(source))
        name = name_case(found) if found else None
        if name:
            set_cert_flow(conn, source, event_id, "confirm_name", email, name,
                          now_iso)
            self._enqueue_reply(
                source, "Name: {}. Reply YES to use it, or send the name"
                .format(name))
        else:
            set_cert_flow(conn, source, event_id, "await_name", email, None,
                          now_iso)
            self._enqueue_reply(source, "Send the name for the certificate")

    def _finish_cert(self, source, event_id, email, name):
        conn = self.conn
        now_iso = datetime.now(timezone.utc).isoformat()
        base = base_call(source)
        name = name_case((name or "").strip()) if (name or "").strip() else base
        save_cert_contact(conn, base, email, name, now_iso)
        clear_cert_flow(conn, source)
        path = self._generate_cert(event_id, source, base, name)
        if not path:
            self._enqueue_reply(source, "Sorry, could not build the "
                                        "certificate right now.")
            return
        LOG.info("Certificate for %s (%s) -> %s [email: %s]",
                 base, name, path, email)
        if self.cfg["email_enable"] and email and self.cfg["email_from"]:
            self._send_cert_email_async(email, name, path)
            self._enqueue_reply(source, "Sent to {}! 73".format(email))
        else:
            self._enqueue_reply(source, "Certificate ready as {}! 73".format(
                name))

    def _send_cert_email_async(self, to_addr, name, pdf_path):
        """Send the certificate email in a background thread so the SMTP round
        trip never blocks the APRS loop."""
        threading.Thread(target=self._send_cert_email,
                         args=(to_addr, name, pdf_path), daemon=True).start()

    def _send_cert_email(self, to_addr, name, pdf_path):
        cfg = self.cfg
        try:
            msg = EmailMessage()
            msg["Subject"] = cfg["email_subject"]
            msg["From"] = formataddr((cfg["email_from_name"], cfg["email_from"]))
            msg["To"] = to_addr
            if cfg["email_reply_to"]:
                msg["Reply-To"] = cfg["email_reply_to"]
            msg.set_content(cfg["email_body"].format(name=name))
            with open(pdf_path, "rb") as fh:
                data = fh.read()
            msg.add_attachment(data, maintype="application", subtype="pdf",
                               filename=os.path.basename(pdf_path))
            with smtplib.SMTP(cfg["email_host"], cfg["email_port"],
                              timeout=30) as smtp:
                smtp.starttls()
                if cfg["email_user"]:
                    smtp.login(cfg["email_user"], cfg["email_password"])
                smtp.send_message(msg)
            LOG.info("Certificate emailed to %s", to_addr)
        except Exception as exc:
            LOG.warning("Certificate email to %s failed: %s", to_addr, exc)

    def _generate_cert(self, event_id, source, callsign, name):
        """Render one certificate PDF. Returns the path or None on failure.

        `source` is the full call used to find the check-in time; `callsign`
        is the upper-case base call shown on the certificate.
        """
        try:
            from pktnet_cert import (draw_certificate, fmt_date_br,
                                     fmt_time_utc, safe_filename)
        except Exception as exc:  # Pillow or module missing
            LOG.warning("Certificate module unavailable: %s", exc)
            return None
        conn = self.conn
        event = conn.execute("SELECT * FROM events WHERE event_id = ?",
                             (event_id,)).fetchone()
        if event is None:
            return None
        row = conn.execute(
            "SELECT ts_utc FROM checkins WHERE event_id = ? AND callsign = ?",
            (event_id, source)).fetchone()
        ts = row["ts_utc"] if row else datetime.now(timezone.utc).isoformat()
        ctx = {
            "template": self.cfg["cert_template"],
            "net_call": event["net_call"],
            "event_name": event["name"],
            "date_br": fmt_date_br(event["event_date"]),
            "callsign": callsign,
            "op_name": name,
            "checkin_time": fmt_time_utc(ts),
        }
        try:
            os.makedirs(self.cfg["cert_dir"], exist_ok=True)
            fname = "{}_ev{}_{}.pdf".format(
                safe_filename(event["net_call"]), event_id,
                safe_filename(callsign))
            out = os.path.join(self.cfg["cert_dir"], fname)
            draw_certificate(out, ctx)
            return out
        except Exception as exc:
            LOG.warning("Certificate render failed: %s", exc)
            return None

    def _ensure_adhoc_event(self, now):
        date_str = now.strftime("%Y-%m-%d")
        name = "PKTNET " + date_str
        row = self.conn.execute(
            "SELECT * FROM events WHERE event_date = ? AND name = ? "
            "AND status != 'closed' ORDER BY start_utc DESC LIMIT 1",
            (date_str, name),
        ).fetchone()
        if row:
            return row
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        eid = create_daily_net(self.conn, name, date_str, start.isoformat(),
                               end.isoformat(), self.cfg["net_call"])
        return self.conn.execute("SELECT * FROM events WHERE event_id = ?",
                                 (eid,)).fetchone()

    # -- outbound ---------------------------------------------------------- #

    def _next_msgno(self):
        self._out_seq = (self._out_seq + 1) % 100000
        return str(self._out_seq)

    def _enqueue_reply(self, to_call, text, from_call=None):
        from_call = from_call or self.cfg["net_call"]
        msgno = self._next_msgno()
        line = build_message(from_call, to_call, text, msgno)
        # Hold the first transmission until reply_delay after the ACK we just
        # sent, so the operator's device receives and processes the ACK before
        # the reply arrives (they collide otherwise on a half-duplex RF path).
        # Consecutive replies are spaced by the same amount via _tx_gate.
        now = time.time()
        first = max(now, self._tx_gate) + self.cfg["reply_delay"]
        self._tx_gate = first
        self.pending[(to_call, msgno)] = {
            "line": line, "attempts": 0, "next": first,
        }

    def _enqueue_numbered(self, to_call, lines, from_call=None):
        """Enqueue several messages as one reply. When there is more than one,
        each is prefixed "[i/n]: " so the operator can see the order and spot a
        missing part. Callers must keep each line within APRS_MAX_TEXT minus
        PART_RESERVE so the prefix fits."""
        n = len(lines)
        for i, line in enumerate(lines, 1):
            pfx = "[{}/{}]: ".format(i, n) if n > 1 else ""
            self._enqueue_reply(to_call, pfx + line, from_call)

    def _enqueue_pack(self, to_call, tokens, sep=" ", prefix="", from_call=None):
        """Pack tokens into as few messages as possible and enqueue them,
        numbering them "[i/n]: " when the result spans more than one message."""
        lines = self._pack(tokens, sep, prefix)
        if len(lines) > 1:
            # Re-pack leaving room for the part prefix so nothing is truncated.
            lines = self._pack(tokens, sep, prefix,
                               limit=APRS_MAX_TEXT - PART_RESERVE)
        self._enqueue_numbered(to_call, lines, from_call)

    def _service_pending(self, now):
        done = []
        for key, item in self.pending.items():
            if now < item["next"]:
                continue
            if item["attempts"] >= self.cfg["max_retries"]:
                LOG.warning("Giving up on reply to %s (no ack)", key[0])
                done.append(key)
                continue
            self._send_raw(item["line"])
            item["attempts"] += 1
            item["next"] = now + self.cfg["retry_interval"]
        for key in done:
            self.pending.pop(key, None)

    def _clear_pending(self, source, msgno):
        if self.pending.pop((source, msgno), None) is not None:
            LOG.info("Reply to %s acked (msgno=%s)", source, msgno)

    def _send_raw(self, line):
        if not self.sock:
            return
        try:
            self.sock.sendall((line + "\r\n").encode("ascii", "replace"))
            if not line.startswith("#"):
                LOG.debug("TX: %s", line)
        except OSError as exc:
            LOG.warning("Send failed: %s", exc)


# --------------------------------------------------------------------------- #
# CLI subcommands
# --------------------------------------------------------------------------- #

def _parse_iso(value):
    """Accept ISO 8601 with a trailing 'Z' and return a UTC ISO string."""
    raw = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def cmd_run(args, cfg):
    conn = init_db(cfg["db_path"])
    bot = PktNetBot(cfg, conn)
    signal.signal(signal.SIGTERM, bot.stop)
    signal.signal(signal.SIGINT, bot.stop)
    LOG.info("PKTNET bot starting (net=%s, login=%s)",
             cfg["net_call"], cfg["login_call"])
    bot.run_forever()
    conn.close()
    LOG.info("PKTNET bot stopped")


def cmd_addevent(args, cfg):
    conn = init_db(cfg["db_path"])
    start = _parse_iso(args.start)
    end = _parse_iso(args.end)
    if end <= start:
        LOG.error("END must be after START")
        sys.exit(2)
    conn.execute(
        "INSERT INTO events (name, event_date, start_utc, end_utc, net_call) "
        "VALUES (?, ?, ?, ?, ?)",
        (args.name, start.strftime("%Y-%m-%d"), start.isoformat(),
         end.isoformat(), cfg["net_call"]),
    )
    conn.commit()
    eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    print("Event #{} created: {} ({} -> {})".format(
        eid, args.name, start.isoformat(), end.isoformat()))
    conn.close()


def _resolve_event_id(conn, event_id):
    """Return event_id, or the active/most-recent event id if none given."""
    if event_id:
        return event_id
    now_iso = datetime.now(timezone.utc).isoformat()
    row = get_active_event(conn, now_iso)
    if row is None:
        row = conn.execute(
            "SELECT event_id FROM events ORDER BY start_utc DESC LIMIT 1"
        ).fetchone()
    return row["event_id"] if row else None


def cmd_endevent(args, cfg):
    conn = init_db(cfg["db_path"])
    eid = _resolve_event_id(conn, args.event_id)
    if eid is None:
        print("No event to end.")
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    row = end_event(conn, eid, now_iso)
    if row is None:
        print("Event #{} not found.".format(eid))
    else:
        print("Event #{} ended now: {}".format(eid, row["name"]))
    conn.close()


def cmd_delevent(args, cfg):
    conn = init_db(cfg["db_path"])
    row = conn.execute("SELECT * FROM events WHERE event_id = ?",
                       (args.event_id,)).fetchone()
    if row is None:
        print("Event #{} not found.".format(args.event_id))
        return
    n = count_checkins(conn, args.event_id)
    if not args.yes:
        ans = input("Delete event #{} '{}' and its {} check-in(s)? [y/N] "
                    .format(args.event_id, row["name"], n))
        if ans.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return
    delete_event(conn, args.event_id)
    print("Deleted event #{} ({} check-in(s) removed).".format(
        args.event_id, n))
    conn.close()


def cmd_events(args, cfg):
    conn = init_db(cfg["db_path"])
    rows = conn.execute(
        "SELECT e.event_id, e.name, e.start_utc, e.end_utc, "
        "       COUNT(c.id) AS n "
        "FROM events e LEFT JOIN checkins c ON c.event_id = e.event_id "
        "GROUP BY e.event_id ORDER BY e.start_utc DESC"
    ).fetchall()
    if not rows:
        print("No events registered.")
        return
    for r in rows:
        print("#{:<4} {:<28} {} -> {}  ({} check-ins)".format(
            r["event_id"], r["name"], r["start_utc"], r["end_utc"], r["n"]))
    conn.close()


def cmd_checkins(args, cfg):
    conn = init_db(cfg["db_path"])
    if args.event_id:
        eid = args.event_id
    else:
        row = conn.execute(
            "SELECT event_id FROM events ORDER BY start_utc DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("No events registered.")
            return
        eid = row["event_id"]
    rows = conn.execute(
        "SELECT callsign, ts_utc, message FROM checkins "
        "WHERE event_id = ? ORDER BY ts_utc",
        (eid,),
    ).fetchall()
    print("Event #{}: {} check-in(s)".format(eid, len(rows)))
    for r in rows:
        print("  {:<10} {}  {}".format(
            r["callsign"], r["ts_utc"], r["message"] or ""))
    conn.close()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    # Common options attached to both the main parser and every subparser so
    # they work in either position (e.g. "run -c X" or "-c X run").
    # SUPPRESS defaults keep the subparser copy from clobbering a value that
    # was given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS,
                        help="path to the configuration file (default: %s)"
                             % DEFAULT_CONFIG_PATH)
    common.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS, help="enable debug logging")

    parser = argparse.ArgumentParser(
        description="PKTNET APRS net check-in bot", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", parents=[common], help="run the daemon")

    p_add = sub.add_parser("addevent", parents=[common],
                           help="register a net event window (UTC)")
    p_add.add_argument("name", help="event name, e.g. 'APRS PKTNET #1'")
    p_add.add_argument("start", help="start time, ISO 8601 UTC (e.g. 2026-06-25T00:00:00Z)")
    p_add.add_argument("end", help="end time, ISO 8601 UTC (e.g. 2026-06-25T23:59:59Z)")

    sub.add_parser("events", parents=[common], help="list registered events")

    p_end = sub.add_parser("endevent", parents=[common],
                           help="end a net now (defaults to the active event)")
    p_end.add_argument("event_id", nargs="?", type=int,
                       help="event id (defaults to the active/most recent event)")

    p_del = sub.add_parser("delevent", parents=[common],
                           help="delete a net and its check-ins")
    p_del.add_argument("event_id", type=int, help="event id to delete")
    p_del.add_argument("-y", "--yes", action="store_true",
                       help="do not ask for confirmation")

    p_ck = sub.add_parser("checkins", parents=[common],
                          help="list check-ins for an event")
    p_ck.add_argument("event_id", nargs="?", type=int,
                      help="event id (defaults to the most recent event)")

    args = parser.parse_args()

    config_path = getattr(args, "config", DEFAULT_CONFIG_PATH)
    verbose = getattr(args, "verbose", False)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load_config(config_path)

    handlers = {
        "run": cmd_run,
        "addevent": cmd_addevent,
        "endevent": cmd_endevent,
        "delevent": cmd_delevent,
        "events": cmd_events,
        "checkins": cmd_checkins,
    }
    handlers[args.command](args, cfg)


if __name__ == "__main__":
    main()

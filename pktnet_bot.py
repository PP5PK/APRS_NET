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
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG_PATH = "/etc/pktnet/pktnet.conf"
APRS_MAX_TEXT = 67           # APRS message text hard limit (characters)
SOFTWARE_NAME = "PKTNET"
SOFTWARE_VERS = "1.0"

LOG = logging.getLogger("pktnet")

# Matches an APRS message ACK/REJ payload, e.g. "ack042" or "rej07".
ACK_RE = re.compile(r"^(ack|rej)([0-9A-Za-z]{1,5})$")


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
                            fallback="Ja registrado {time}z. 73 de PP5PK"),
        "closed_text": cfg.get("net", "closed_text",
                               fallback="PKTNET fora do horario. 73 de PP5PK"),

        "max_retries": cfg.getint("messaging", "max_retries", fallback=3),
        "retry_interval": cfg.getint("messaging", "retry_interval", fallback=30),
        "keepalive_interval": cfg.getint("messaging", "keepalive_interval",
                                         fallback=20),
        "rx_timeout": cfg.getint("messaging", "rx_timeout", fallback=90),

        "db_path": cfg.get("db", "path", fallback="/var/lib/pktnet/pktnet.db"),
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
            net_call   TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS checkins (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id  INTEGER NOT NULL REFERENCES events(event_id),
            callsign  TEXT    NOT NULL,
            ts_utc    TEXT    NOT NULL,           -- ISO 8601 UTC
            message   TEXT,
            UNIQUE(event_id, callsign)
        );
        """
    )
    conn.commit()
    return conn


def get_active_event(conn, now_iso):
    """Return the event row whose window contains now_iso, or None."""
    cur = conn.execute(
        "SELECT * FROM events "
        "WHERE start_utc <= ? AND end_utc >= ? "
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
        self.last_rx = 0.0
        self.last_keepalive = 0.0

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
        login = ("user {call} pass {pc} vers {name} {ver} filter g/{net}\r\n"
                 .format(call=cfg["login_call"], pc=cfg["passcode"],
                         name=SOFTWARE_NAME, ver=SOFTWARE_VERS,
                         net=cfg["net_call"]))
        self.sock.sendall(login.encode("ascii", "replace"))
        now = time.time()
        self.last_rx = now
        self.last_keepalive = now
        self.rxbuf = ""
        LOG.info("Logged in as %s, watching g/%s",
                 cfg["login_call"], cfg["net_call"])

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

        # Only messages addressed to our net callsign concern us.
        if addressee != self.cfg["net_call"]:
            return

        # Is it an ACK/REJ for one of our outgoing replies?
        m = ACK_RE.match(text.strip())
        if m:
            self._clear_pending(source, m.group(2))
            return

        LOG.info("Message from %s: %r (msgno=%s)", source, text, msgno)

        # Courtesy ACK so the sender's radio stops retransmitting.
        if msgno:
            self._send_raw(build_ack(self.cfg["net_call"], source, msgno))

        self._process_checkin(source, text)

    def _process_checkin(self, source, text):
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        hhmm = now.strftime("%H%M")

        event = get_active_event(self.conn, now_iso)
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

    def _ensure_adhoc_event(self, now):
        date_str = now.strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT * FROM events WHERE event_date = ? AND name = ? LIMIT 1",
            (date_str, "PKTNET " + date_str),
        ).fetchone()
        if row:
            return row
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        self.conn.execute(
            "INSERT INTO events (name, event_date, start_utc, end_utc, net_call) "
            "VALUES (?, ?, ?, ?, ?)",
            ("PKTNET " + date_str, date_str, start.isoformat(),
             end.isoformat(), self.cfg["net_call"]),
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM events WHERE event_date = ? AND name = ? LIMIT 1",
            (date_str, "PKTNET " + date_str),
        ).fetchone()

    # -- outbound ---------------------------------------------------------- #

    def _next_msgno(self):
        self._out_seq = (self._out_seq + 1) % 100000
        return str(self._out_seq)

    def _enqueue_reply(self, to_call, text):
        msgno = self._next_msgno()
        line = build_message(self.cfg["net_call"], to_call, text, msgno)
        self.pending[(to_call, msgno)] = {
            "line": line, "attempts": 0, "next": 0.0,
        }

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
    p_add.add_argument("name", help="event name, e.g. 'PKTNET Net #1'")
    p_add.add_argument("start", help="start time, ISO 8601 UTC (e.g. 2026-06-25T00:00:00Z)")
    p_add.add_argument("end", help="end time, ISO 8601 UTC (e.g. 2026-06-25T23:59:59Z)")

    sub.add_parser("events", parents=[common], help="list registered events")

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
        "events": cmd_events,
        "checkins": cmd_checkins,
    }
    handlers[args.command](args, cfg)


if __name__ == "__main__":
    main()

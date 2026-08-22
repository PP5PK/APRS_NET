#!/usr/bin/env python3
"""
cgnat_detect.py - self-contained CGNAT detector. No server, no router access
and no port forwarding required.

It gathers every signal it can purely from this host and outbound traffic,
then synthesises ONE verdict with a confidence level and the evidence trail:

  STUN (primary, RFC 5389)  two outbound UDP queries to different servers from
                            the same local port. Symmetric mapping (different
                            external IP/port per server) is the signature of
                            carrier NAT; a mapped IP in 100.64.0.0/10 is
                            definitive. Works behind CGNAT, needs no privileges.
  UPnP IGD (if available)   the router's own WAN IP. In 100.64/10 -> CGNAT;
                            RFC1918 -> an upstream NAT; equal to the public IP
                            -> real public edge.
  traceroute (if available) 100.64/10 hops, and how many private hops precede
                            the first public hop (layered / carrier NAT).
  HTTP + IPv6               the public IPv4 the Internet sees, and whether a
                            global IPv6 exists (a path that bypasses IPv4 CGNAT).

Why "starts with 100" is wrong: only 100.64.0.0/10 is CGNAT, and the address a
web service reports is the carrier's public egress, never the 100.64 address.
The mapping BEHAVIOUR (via STUN) is what actually reveals carrier NAT.

73 de PP5PK
"""

import argparse
import http.client
import ipaddress
import json
import platform
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
STUN_SERVERS = [
    ("stun.l.google.com", 19302),
    ("stun.cloudflare.com", 3478),
    ("stun1.l.google.com", 19302),
    ("stun.nextcloud.com", 443),
]
PUBLIC_IPV4_PROVIDERS = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
]
PUBLIC_IPV6_PROVIDERS = ["https://api6.ipify.org", "https://v6.ident.me"]
DEFAULT_TRACE_TARGET = "8.8.8.8"
STUN_MAGIC = 0x2112A442
CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
HTTP_UA = {"User-Agent": "curl/8.0"}

USE_COLOR = sys.stdout.isatty()


class Pal:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    BLUE = "\033[38;5;39m"
    AMBER = "\033[38;5;214m"
    GREY = "\033[38;5;245m"


def c(text, color):
    return f"{color}{text}{Pal.RESET}" if USE_COLOR else text


# --------------------------------------------------------------------------- #
# IP classification
# --------------------------------------------------------------------------- #
def is_cgnat(ip):
    try:
        return ipaddress.ip_address(ip) in CGNAT_NET
    except (ValueError, TypeError):
        return False


def is_private(ip):
    try:
        a = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return False
    return a.is_private or a.is_loopback or a.is_link_local or a in CGNAT_NET


def valid_v4(ip):
    try:
        return ipaddress.ip_address(ip).version == 4
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Local + public-by-HTTP + IPv6
# --------------------------------------------------------------------------- #
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def get_public_v4(timeout=5):
    for url in PUBLIC_IPV4_PROVIDERS:
        try:
            req = urllib.request.Request(url, headers=HTTP_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ip = r.read().decode().strip()
            if valid_v4(ip):
                return ip, url
        except Exception:
            continue
    return None, None


def get_public_v6(timeout=4):
    for url in PUBLIC_IPV6_PROVIDERS:
        try:
            req = urllib.request.Request(url, headers=HTTP_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ip = r.read().decode().strip()
            a = ipaddress.ip_address(ip)
            if a.version == 6 and a.is_global:
                return ip
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# STUN (primary signal)
# --------------------------------------------------------------------------- #
def _stun_request():
    txid = secrets.token_bytes(12)
    return struct.pack(">HHI", 0x0001, 0, STUN_MAGIC) + txid, txid


def _stun_parse(data, txid):
    if len(data) < 20:
        return None
    _t, mlen, cookie = struct.unpack(">HHI", data[:8])
    if cookie != STUN_MAGIC:
        return None
    pos, end = 20, min(20 + mlen, len(data))
    while pos + 4 <= end:
        atype, alen = struct.unpack(">HH", data[pos:pos + 4])
        val = data[pos + 4:pos + 4 + alen]
        if atype in (0x0020, 0x0001) and len(val) >= 8:
            fam = val[1]
            port = struct.unpack(">H", val[2:4])[0]
            xored = atype == 0x0020
            if xored:
                port ^= (STUN_MAGIC >> 16)
            if fam == 0x01:
                raw = val[4:8]
                if xored:
                    raw = bytes(a ^ b for a, b in zip(raw, struct.pack(">I", STUN_MAGIC)))
                return socket.inet_ntoa(raw), port
            if fam == 0x02 and len(val) >= 20:
                raw = val[4:20]
                if xored:
                    key = struct.pack(">I", STUN_MAGIC) + txid
                    raw = bytes(a ^ b for a, b in zip(raw, key))
                return socket.inet_ntop(socket.AF_INET6, raw), port
        pos += 4 + alen + ((4 - (alen % 4)) % 4)
    return None


def _stun_query(sock, ip, port, timeout, retries=2):
    for _ in range(retries):
        req, txid = _stun_request()
        try:
            sock.settimeout(timeout)
            sock.sendto(req, (ip, port))
            data, _a = sock.recvfrom(2048)
        except (socket.timeout, OSError):
            continue
        res = _stun_parse(data, txid)
        if res:
            return res
    return None


def probe_stun(servers, timeout=3):
    resolved, seen = [], set()
    for host, port in servers:
        try:
            ip = socket.gethostbyname(host)
        except OSError:
            continue
        if ip not in seen:
            seen.add(ip)
            resolved.append((host, ip, port))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    mappings = []
    for host, ip, port in resolved:
        m = _stun_query(sock, ip, port, timeout)
        if m:
            mappings.append({"server": host, "server_ip": ip,
                             "mapped_ip": m[0], "mapped_port": m[1]})
        if len(mappings) >= 2:
            break
    sock.close()

    if not mappings:
        behavior = "none"
    elif len(mappings) == 1:
        behavior = "single"
    else:
        ips = {m["mapped_ip"] for m in mappings}
        ports = {m["mapped_port"] for m in mappings}
        behavior = "symmetric" if (len(ips) > 1 or len(ports) > 1) else "cone"
    return {"mappings": mappings, "behavior": behavior}


# --------------------------------------------------------------------------- #
# UPnP IGD WAN IP
# --------------------------------------------------------------------------- #
def _ssdp_discover(timeout=3):
    locations = set()
    targets = [
        "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
        "urn:schemas-upnp-org:service:WANIPConnection:1",
        "urn:schemas-upnp-org:service:WANPPPConnection:1",
    ]
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout)
    except OSError:
        return locations
    for st in targets:
        msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
               'MAN: "ssdp:discover"\r\nMX: 2\r\n' f"ST: {st}\r\n\r\n")
        try:
            sock.sendto(msg.encode(), ("239.255.255.250", 1900))
        except OSError:
            pass
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, _ = sock.recvfrom(65535)
        except (socket.timeout, OSError):
            break
        m = re.search(rb"LOCATION:\s*(\S+)", data, re.IGNORECASE)
        if m:
            locations.add(m.group(1).decode(errors="ignore").strip())
    sock.close()
    return locations


def _http_get(url, timeout=4):
    p = urlparse(url)
    conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=timeout)
    path = (p.path or "/") + (("?" + p.query) if p.query else "")
    conn.request("GET", path, headers={"User-Agent": "upnp/1.0"})
    body = conn.getresponse().read().decode("utf-8", "ignore")
    conn.close()
    return body, f"{p.scheme}://{p.hostname}:{p.port or 80}"


def _soap_external_ip(stype, ctrl, timeout=4):
    body = ('<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:GetExternalIPAddress xmlns:u="{stype}"/></s:Body></s:Envelope>')
    p = urlparse(ctrl)
    conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=timeout)
    path = (p.path or "/") + (("?" + p.query) if p.query else "")
    conn.request("POST", path, body=body, headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{stype}#GetExternalIPAddress"',
        "Content-Length": str(len(body))})
    text = conn.getresponse().read().decode("utf-8", "ignore")
    conn.close()
    m = re.search(r"<NewExternalIPAddress>(.*?)</NewExternalIPAddress>", text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def probe_upnp(timeout=3):
    for loc in _ssdp_discover(timeout=timeout):
        try:
            desc, base = _http_get(loc)
        except Exception:
            continue
        m = re.search(r"<URLBase>(.*?)</URLBase>", desc, re.IGNORECASE)
        if m and m.group(1).strip():
            base = m.group(1).strip()
        for svc in re.findall(r"<service>(.*?)</service>", desc, re.DOTALL | re.IGNORECASE):
            st = re.search(r"<serviceType>(.*?)</serviceType>", svc, re.IGNORECASE)
            cu = re.search(r"<controlURL>(.*?)</controlURL>", svc, re.IGNORECASE)
            if not st or not cu:
                continue
            stype, ctrl = st.group(1).strip(), cu.group(1).strip()
            if "WANIPConnection" in stype or "WANPPPConnection" in stype:
                if not ctrl.startswith("http"):
                    ctrl = base.rstrip("/") + ("" if ctrl.startswith("/") else "/") + ctrl
                try:
                    ip = _soap_external_ip(stype, ctrl)
                except Exception:
                    continue
                if ip and valid_v4(ip):
                    return ip
    return None


# --------------------------------------------------------------------------- #
# Traceroute
# --------------------------------------------------------------------------- #
def probe_traceroute(target, max_hops=8, timeout=25):
    if shutil.which("traceroute"):
        cmd = ["traceroute", "-n", "-q", "1", "-w", "2", "-m", str(max_hops), target]
    elif shutil.which("tracert"):
        cmd = ["tracert", "-d", "-h", str(max_hops), "-w", "2000", target]
    else:
        return {"ran": False, "cgnat_hops": [], "private_chain": 0, "hops": []}
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return {"ran": False, "cgnat_hops": [], "private_chain": 0, "hops": []}

    hops = []
    for line in out.splitlines():
        if not re.match(r"\s*\d+\s", line):  # hop lines only (skip header)
            continue
        ips = re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", line)
        hops.append(ips[0] if ips else None)

    cgnat_hops = [h for h in hops if h and is_cgnat(h)]
    chain = 0
    for h in hops:
        if h is None:
            continue
        if is_private(h):
            chain += 1
        else:
            break
    return {"ran": True, "cgnat_hops": cgnat_hops, "private_chain": chain, "hops": hops}


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #
REACH = {
    "CGNAT": "Inbound IPv4 port forwarding cannot reach you. Host over IPv6, or "
             "use a tunnel (WireGuard/Tailscale) or reverse tunnel/relay.",
    "CGNAT_LIKELY": "Inbound IPv4 is probably blocked by carrier NAT. Plan "
                    "hosting as under CGNAT (IPv6 / tunnel / relay).",
    "UPSTREAM_NAT": "A NAT sits above your router. Inbound needs forwarding on "
                    "THAT device; if it is the carrier's, treat as CGNAT.",
    "PUBLIC": "Usable public IPv4. Inbound port forwarding on your router works.",
    "NAT_REACHABLE": "Stable mapping; inbound is feasible with port forwarding "
                     "or hole punching.",
    "NAT_UNDETERMINED": "Behind NAT. To confirm, forward a port and test inbound, "
                        "or run where outbound UDP (STUN) is allowed.",
    "UNDETERMINED": "Could not determine. Retry on a network that allows "
                    "outbound UDP so STUN can run.",
}


def _wrap(code, label, confidence, reasons, s):
    ipv6 = None
    if s["public_v6"]:
        ipv6 = (f"Global IPv6 present ({s['public_v6']}). Even under IPv4 CGNAT "
                "you can host over IPv6 if the firewall allows inbound.")
    return {"code": code, "label": label, "confidence": confidence,
            "reasons": reasons, "reachability": REACH.get(code, ""), "ipv6_note": ipv6}


def synthesize(s):
    reasons = []

    # 1. Definitive: any edge/observed address inside 100.64/10.
    hits = []
    if s["upnp_wan"] and is_cgnat(s["upnp_wan"]):
        hits.append(f"router WAN IP {s['upnp_wan']} is inside 100.64/10 (UPnP)")
    for m in s["stun"]["mappings"]:
        if is_cgnat(m["mapped_ip"]):
            hits.append(f"STUN-mapped IP {m['mapped_ip']} is inside 100.64/10")
            break
    if s["public_v4"] and is_cgnat(s["public_v4"]):
        hits.append(f"public IP {s['public_v4']} is inside 100.64/10")
    if s["trace"]["ran"] and s["trace"]["cgnat_hops"]:
        hits.append("traceroute hops in 100.64/10: " + ", ".join(s["trace"]["cgnat_hops"]))
    if hits:
        return _wrap("CGNAT", "CGNAT", "confirmed", reasons + hits, s)

    # 2. Router WAN IP from UPnP is the strongest non-100.64 signal.
    if s["upnp_wan"]:
        if is_private(s["upnp_wan"]):
            reasons.append(f"router WAN IP {s['upnp_wan']} is RFC1918 private; an upstream NAT exists")
            return _wrap("UPSTREAM_NAT", "BEHIND UPSTREAM NAT", "confirmed", reasons, s)
        if s["public_v4"] and s["upnp_wan"] == s["public_v4"]:
            reasons.append(f"router WAN IP equals public IP ({s['upnp_wan']}): real public edge")
            return _wrap("PUBLIC", "PUBLIC IP", "confirmed", reasons, s)
        reasons.append(f"router WAN IP {s['upnp_wan']} is a public address")
        return _wrap("PUBLIC", "PUBLIC IP", "likely", reasons, s)

    # 3. STUN mapping behaviour.
    beh = s["stun"]["behavior"]
    if beh == "symmetric":
        pairs = ", ".join(f"{m['mapped_ip']}:{m['mapped_port']}" for m in s["stun"]["mappings"])
        reasons.append(f"STUN saw different mappings per server ({pairs}): symmetric NAT, the carrier-NAT signature")
        return _wrap("CGNAT_LIKELY", "CGNAT LIKELY (symmetric NAT)", "likely", reasons, s)
    if beh == "cone":
        m0 = s["stun"]["mappings"][0]
        if s["local_ip"] and s["local_ip"] == m0["mapped_ip"]:
            reasons.append(f"STUN mapped IP equals local IP ({s['local_ip']}): public, no NAT")
            return _wrap("PUBLIC", "PUBLIC IP", "confirmed", reasons, s)
        reasons.append(f"STUN saw one stable mapping across servers ({m0['mapped_ip']}:{m0['mapped_port']}): cone NAT")
        return _wrap("NAT_REACHABLE", "BEHIND NAT (cone / reachable)", "likely", reasons, s)

    # 4. Traceroute layered-NAT chain.
    if s["trace"]["ran"] and s["trace"]["private_chain"] >= 2:
        reasons.append(f"traceroute shows {s['trace']['private_chain']} private hops before the first public hop: layered/carrier NAT")
        return _wrap("CGNAT_LIKELY", "CGNAT LIKELY (layered NAT)", "likely", reasons, s)

    # 5. Single STUN server reached.
    if beh == "single":
        m0 = s["stun"]["mappings"][0]
        if s["local_ip"] and s["local_ip"] == m0["mapped_ip"]:
            reasons.append(f"STUN mapped IP equals local IP ({s['local_ip']}): public, no NAT")
            return _wrap("PUBLIC", "PUBLIC IP", "confirmed", reasons, s)
        reasons.append(f"only one STUN server answered (mapped {m0['mapped_ip']}); cannot tell symmetric from cone")

    # 6. Fallback: local vs public.
    if s["local_ip"] and s["public_v4"] and s["local_ip"] == s["public_v4"]:
        reasons.append("local IP equals public IP: public address bound directly, no NAT")
        return _wrap("PUBLIC", "PUBLIC IP", "confirmed", reasons, s)
    if s["local_ip"] and is_private(s["local_ip"]):
        reasons.append("local IP is private and differs from the public IP: behind at least one NAT, but no probe could classify it")
        return _wrap("NAT_UNDETERMINED", "BEHIND NAT (undetermined)", "low", reasons, s)
    reasons.append("not enough signals were collected (outbound UDP blocked, no UPnP, no traceroute)")
    return _wrap("UNDETERMINED", "UNDETERMINED", "low", reasons, s)


# --------------------------------------------------------------------------- #
# Run + output
# --------------------------------------------------------------------------- #
def run(args):
    servers = []
    for entry in args.stun:
        host, _, port = entry.partition(":")
        servers.append((host, int(port) if port else 3478))
    servers += STUN_SERVERS

    local_ip = get_local_ip()
    public_v4, provider = get_public_v4()
    public_v6 = get_public_v6()
    stun = ({"mappings": [], "behavior": "skipped"} if args.no_stun
            else probe_stun(servers, timeout=args.timeout))
    upnp_wan = None if args.no_upnp else probe_upnp(timeout=args.timeout)
    trace = ({"ran": False, "cgnat_hops": [], "private_chain": 0, "hops": []}
             if args.no_trace else probe_traceroute(args.target))

    s = {"local_ip": local_ip, "public_v4": public_v4, "provider": provider,
         "public_v6": public_v6, "stun": stun, "upnp_wan": upnp_wan, "trace": trace}
    verdict = synthesize(s)
    s["verdict"] = verdict
    return s


def report(s):
    v = s["verdict"]
    title = " CGNAT Detection "
    bar = "=" * len(title)
    print(c(bar, Pal.BLUE)); print(c(title, Pal.BOLD + Pal.BLUE)); print(c(bar, Pal.BLUE))

    def row(k, val, color=Pal.BLUE):
        print(f"  {c(k.ljust(20), Pal.GREY)} {c(str(val), color)}")

    row("Local IP", s["local_ip"] or "n/a")
    row("Public IPv4", s["public_v4"] or "n/a")
    row("Public IPv6", s["public_v6"] or "none")
    st = s["stun"]
    if st["behavior"] == "skipped":
        row("STUN", "skipped")
    elif st["mappings"]:
        for m in st["mappings"]:
            row(f"STUN {m['server']}", f"{m['mapped_ip']}:{m['mapped_port']}", Pal.GREY)
        row("STUN behaviour", st["behavior"])
    else:
        row("STUN", "no UDP response")
    row("Router WAN (UPnP)", s["upnp_wan"] or "not available")
    if s["trace"]["ran"]:
        row("Traceroute CGNAT", ", ".join(s["trace"]["cgnat_hops"]) or "none")
        row("Private hop chain", s["trace"]["private_chain"])
    else:
        row("Traceroute", "skipped/unavailable")

    color = Pal.AMBER if v["code"] in ("CGNAT", "CGNAT_LIKELY", "UPSTREAM_NAT",
                                       "NAT_UNDETERMINED", "UNDETERMINED") else Pal.BLUE
    print()
    print(f"  {c('VERDICT:', Pal.BOLD)} "
          f"{c('[' + v['label'] + ']', Pal.BOLD + color)} "
          f"{c('(' + v['confidence'] + ')', Pal.GREY)}")
    for r in v["reasons"]:
        print(c(f"    - {r}", Pal.GREY))
    if v["reachability"]:
        print()
        print(c("  " + v["reachability"], Pal.GREY))
    if v["ipv6_note"]:
        print(c("  " + v["ipv6_note"], Pal.GREY))


def main():
    global USE_COLOR
    ap = argparse.ArgumentParser(description="Self-contained CGNAT detector (no server needed).")
    ap.add_argument("--stun", action="append", default=[],
                    help="extra STUN server host:port (repeatable, tried first)")
    ap.add_argument("--target", default=DEFAULT_TRACE_TARGET, help="traceroute target")
    ap.add_argument("--timeout", type=float, default=3.0)
    ap.add_argument("--no-stun", action="store_true")
    ap.add_argument("--no-upnp", action="store_true")
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.no_color or args.json:
        USE_COLOR = False

    s = run(args)
    if args.json:
        print(json.dumps(s, indent=2))
    else:
        report(s)

    sys.exit({"PUBLIC": 0, "NAT_REACHABLE": 0, "CGNAT": 2, "CGNAT_LIKELY": 2,
              "UPSTREAM_NAT": 2, "NAT_UNDETERMINED": 3, "UNDETERMINED": 3}
             .get(s["verdict"]["code"], 3))


if __name__ == "__main__":
    main()

# 📟 E-Paper HAT+ Applications for Raspberry Pi

A growing collection of Python applications for the **WaveShare 2.13" E-Paper HAT+** on Raspberry Pi (both [HAT](https://www.waveshare.com/2.13inch-e-Paper-HAT.htm) and [HAT+](https://www.waveshare.com/2.13inch-e-Paper-HAT-Plus.htm)). Each app is self-contained, uses partial refresh for smooth updates, and is designed to run as a systemd service.

> Maintained by **PP5PK** — Mafra, Santa Catarina, Brazil 🇧🇷

---

## Hardware - [2.13inch E-Paper HAT](https://www.waveshare.com/wiki/2.13inch_e-Paper_HAT) & [HAT+](https://www.waveshare.com/wiki/2.13inch_e-Paper_HAT+)

| Item | Details |
|---|---|
| Display | WaveShare 2.13" E-Paper HAT+ |
| Resolution | 250 × 122 px |
| Colors | Black & White |
| Interface | SPI |
| Partial refresh | ~0.3 s |
| Full refresh | ~2 s |
| Driver | `epd2in13_V4` |

> ⚠️ This repository targets the **HAT+** variant specifically. The older HAT (tricolor, `epd2in13b_V4`) uses a different driver and does **not** support partial refresh.

---

## Applications

### 🕐 Station Monitor — `station_monitor.py`

A real-time system dashboard showing clock, date, and live hardware metrics.

**Display layout:**

![Monitor](https://cloud.dvbr.net/images/e-paper_monitor.jpg)

**Features:**
- Clock with seconds, updated every 1 s via partial refresh
- CPU usage, RAM usage, CPU temperature, disk usage
- System hostname, IP address and operator callsign
- Background thread for collecting metrics (non-blocking)
- `--black` flag to invert colors (white text on black background)
- Automatic color inversion every 30 min to prevent permanent ghosting
- Full refresh every 10 min for display health
- Graceful shutdown screen on exit

**Usage:**
```bash
sudo python3 station_monitor.py            # white background (default)
sudo python3 station_monitor.py --black    # black background
sudo python3 station_monitor.py --simulate # save preview to /tmp/epd_preview.png
sudo python3 station_monitor.py --once     # single refresh and exit
```

---

### 📡 XLX Reflector Dashboard — `xlx_monitor.py`

A live dashboard for [XLX](https://github.com/PP5PK/XLX_Installer) D-Star/YSF/DMR reflectors. Reads the xlxd status XML in real time and displays last heard stations and connected clients — including activity relayed from linked reflectors.

**Display layout:**

![XLXBRA](https://cloud.dvbr.net/images/e-paper_XLXBRA.jpg)

**Features:**
- Last heard stations with real operator callsign, gateway (+ `via <REFLECTOR>` when relayed from a linked reflector), module and time (right-aligned)
- Connected-clients counter (`[NN]`) next to the reflector name in the header
- Connected clients/gateways listed with protocol (e.g. `PP5PK-A DCS / PU6AXE DPlus`)
- Reads `/var/log/xlxd.xml` only — no xlxd log-file parsing, no correlation between data sources
- Reflector name detected automatically, preferring the `xlxd.service` unit (`ExecStart=`) with the XML itself as fallback
- Dynamic layout: header and footer are anchored to the top/bottom edges, and the last-heard section expands to use whatever space the connected-clients list doesn't need
- Clock updated every 1 s via partial refresh
- Background XML reader thread (non-blocking)
- `--black` flag for inverted color scheme
- `--off` flag to blank the display (white, or black with `--black`) and put the hardware to sleep
- Automatic color inversion every 30 min (anti-ghosting)
- Full refresh every 10 min for display health
- Graceful shutdown screen (reflector name + antenna graphic) on exit — triggered by `Ctrl+C` **and** by `systemctl stop` (SIGTERM)

**Usage:**
```bash
sudo python3 xlx_monitor.py            # default
sudo python3 xlx_monitor.py --black    # inverted colors
sudo python3 xlx_monitor.py --simulate # PNG preview without hardware
sudo python3 xlx_monitor.py --once     # single refresh and exit
sudo python3 xlx_monitor.py --off      # blank the display and exit
```

**XML file:** `/var/log/xlxd.xml` (configurable via the `XLX_XML` constant at the top of the file). Make sure `xlxd` is configured to write this file — no `/var/log/xlx.log` text parsing is used anymore.

**Reflector name:** detected automatically, in order of preference:
1. `ExecStart=` line of the `xlxd.service` systemd unit (checked at `/etc/systemd/system/`, `/lib/systemd/system/` and `/usr/lib/systemd/system/`)
2. The `<REFLECTOR  linked peers>` section header inside the XML itself
3. `REFLECTOR_FALLBACK` constant, if neither of the above is available yet

---

## Installation

### 1. Clone this repository

```bash
git clone https://github.com/pp5pk/Waveshare_e-paper_apps.git /usr/local/bin/Waveshare_e-paper_apps
cd /usr/local/bin/Waveshare_e-paper_apps
```

### 2. Install the WaveShare driver library (* Optional)

```bash
git clone https://github.com/waveshare/e-Paper.git
cp -r e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd ./waveshare_epd
```

### 3. Install Python dependencies

```bash
sudo apt install python3-lgpio python3-psutil python3-pil python3-gpiozero gpiod
```

> `gpiod` provides `gpiodetect`, used on Raspberry Pi 5 to automatically locate the RP1 GPIO chip (its number varies by kernel/firmware and isn't fixed at 0 or 4).

### 4. Enable SPI on your Raspberry Pi

```bash
sudo raspi-config
# Interface Options → SPI → Enable
```

### 5. Run as a systemd service

Copy the desired service file, edit and enable it:

```bash
sudo cp e-paper_monitor.service /etc/systemd/system/

# Edit the service file so that the application name matches the target application as needed.
sudo nano e-paper_monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now e-paper_monitor.service

# Follow the logs
sudo journalctl -u e-paper_monitor -f
```

---

## Display Health & Longevity

E-paper displays can develop permanent ghosting if the same image is shown for extended periods. These apps implement a multi-layer protection strategy:

| Mechanism | Interval | Purpose |
|---|---|---|
| Partial refresh | 1 s | Clock update, minimal stress |
| Full refresh | 10 min | Clears partial refresh residue |
| Color inversion | 30 min | Alternates black/white background to equalize pixel wear |
| `epd.Clear()` | On every full refresh | Ensures clean slate before redraw |

---

## Project Structure

```
epaper-apps/
├── station_monitor.py        # System monitor (clock + hardware metrics)
├── xlx_monitor.py             # XLX reflector dashboard (XML-only, no log parsing)
├── e-paper_monitor.service   # systemd service file (station_monitor.py)
├── waveshare_epd/            # WaveShare driver library (copied from official repo)
│   ├── epd2in13_V4.py
│   ├── epdconfig.py
│   └── ...
└── README.md
```

---

## Compatibility

| Board | Tested |
|---|---|
| Raspberry Pi 0 (any)| ✅ |
| Raspberry Pi 3B+ | ✅ |
| Raspberry Pi 4 | ✅ |
| Raspberry Pi 5 | ✅ |

---

## Roadmap

Ideas for future applications in this repository:

- [ ] **APRS Tracker** — display latest APRS positions heard on a local iGate
- [ ] **Weather Station** — temperature, humidity and pressure from a local sensor
- [ ] **DMR Last Heard** — similar to the XLX dashboard but for BrandMeister/TGIF
- [ ] **Solar/Band conditions** — pull WWV solar flux and band conditions from the web
- [ ] **QSO Logger** — display the last logged contact from an ADIF file

Pull requests and suggestions are welcome.

---

## License

MIT License — feel free to use, modify and share.

---

*73 de PP5PK*

![73](https://cloud.dvbr.net/images/e-paper_static.jpg)


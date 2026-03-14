# MyMine Dashboard

Self-hosted IoT monitoring dashboard for **Trackpac** LoRaWAN sensors.  
Single Python file, zero external dependencies, runs on any machine with Python 3.8+.

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Client management** — add, edit and delete clients, each linked to a Trackpac sensor (EUI)
- **Live dashboard** — real-time temperature, humidity and battery charts per sensor
- **Alarm system** — configurable min/max thresholds, alerts every 2 hours when exceeded
- **Email alerts** — HTML alarm emails via Gmail SMTP (App Password)
- **SMS alerts** — SMS via Twilio with automatic E.164 phone normalization
- **Daily PDF report** — sent every morning at 09:00 with the previous day's 24 hourly averages
- **Client credentials** — auto-generated username + password per client, sendable by email
- **No database** — data stored in `clients.json` (plain JSON, easily backed up)
- **No external libs** — pure Python stdlib only; PDF generated without reportlab or fpdf

---

## Quick Start

### 1. Prerequisites

- Python 3.8 or newer
- A [Trackpac](https://trackpac.io) account with API key
- (Optional) Gmail account with [App Password](https://myaccount.google.com/apppasswords) for email alerts
- (Optional) [Twilio](https://twilio.com) account for SMS alerts

### 2. Configure

Open `trackpac_server.py` and edit the configuration block at the top:

```python
# ─── REQUIRED ────────────────────────────────────────────────────────────────
API_KEY = "YOUR_TRACKPAC_API_KEY"   # Dashboard → API → copy key
PORT    = 8765

# ─── EMAIL ALERTS (Gmail recommended) ────────────────────────────────────────
SMTP_USER = "alerts@yourdomain.com"
SMTP_PASS = "abcdefghijklmnop"      # 16-char App Password, no spaces
                                    # Generate at: myaccount.google.com/apppasswords

# ─── SMS ALERTS (Twilio) ──────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TWILIO_AUTH_TOKEN  = "your_auth_token"
TWILIO_FROM_NUMBER = "+15551234567"   # Your Twilio number
```

### 3. Run

```bash
python3 trackpac_server.py
```

Then open **http://localhost:8765** in your browser.

---

## Project Structure

```
mymine/
├── trackpac_server.py   # entire application (server + HTML/JS frontend embedded)
├── clients.json         # auto-created on first save (gitignored)
├── alerts.json          # auto-created when alarms fire (gitignored)
├── README.md
├── .gitignore
└── LICENSE
```

Everything is embedded in a single Python file for maximum portability — no build step, no npm, no venv required.

---

## Configuration Reference

| Variable | Description | Default |
|---|---|---|
| `API_KEY` | Trackpac API key | *required* |
| `BASE` | Trackpac API base URL | `https://v2-api.trackpac.io` |
| `PORT` | HTTP server port | `8765` |
| `SMTP_HOST` | SMTP server | `smtp.gmail.com` |
| `SMTP_PORT` | SMTP port (STARTTLS) | `587` |
| `SMTP_USER` | SMTP username / From address | `""` (disables email) |
| `SMTP_PASS` | SMTP password / App Password | `""` |
| `TWILIO_ACCOUNT_SID` | Twilio Account SID | `""` (disables SMS) |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token | `""` |
| `TWILIO_FROM_NUMBER` | Twilio sender number (E.164) | `""` |
| `ALERT_INTERVAL` | Min seconds between repeated alarm notifications | `600` |

---

## Sensor File Format

You can upload a `.txt` file to populate the sensor dropdown. Supported formats:

```
# Tab-separated (exported from Trackpac)
24E124785F201049	Sensore Frigo
24E124785D499946	Sensore Cantina

# Comma or semicolon separated also accepted
24E124785F201049,Sensore Frigo
```

---

## Alarm Logic

1. Every `ALERT_INTERVAL` seconds the server fetches the latest frame for each sensor
2. If any value (temperature or humidity) is outside the client's configured thresholds:
   - An **email** is sent (if `notif_email` is checked and client has an email address)
   - An **SMS** is sent (if `notif_sms` is checked and client has a phone number)
3. Alerts are suppressed for 2 hours after each send to avoid spam
4. When values return within range, the alert is cleared automatically

---

## Daily Report

Every day at **09:00** a PDF report is automatically emailed to each client who has:
- An email address configured
- The "Email notifications" toggle enabled

The report contains:
- Summary statistics (min, max, average) for temperature and humidity
- A table with 24 hourly averages for the previous day
- Alarm highlights for hours where thresholds were exceeded

You can also download a report on demand from the dashboard (↓ Report PDF button).

---

## SMS Troubleshooting

**"There were no HTTP Requests logged"** in Twilio console:
- With a **trial account**, you can only send SMS to verified numbers. Go to [twilio.com/console/phone-numbers/verified](https://www.twilio.com/console/phone-numbers/verified) and add the recipient's number.

**SMS delivered in console but not received:**
- The server automatically normalises Italian numbers (`3331234567` → `+393331234567`)
- Special characters like `°C` are replaced with ASCII equivalents before sending
- Check the server terminal output for `[SMS] OK to=+39... sid=... status=delivered`

**Error code 21608:** unverified number (trial account limitation)  
**Error code 21211:** invalid phone number format

---

## Gmail App Password Setup

1. Enable 2-Step Verification on your Google account
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. Create a new App Password (type: "Mail", device: "Other")
4. Copy the 16-character password (no spaces) into `SMTP_PASS`

---

## Running as a Service (Linux)

Create `/etc/systemd/system/mymine.service`:

```ini
[Unit]
Description=MyMine Dashboard
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/mymine
ExecStart=/usr/bin/python3 /opt/mymine/trackpac_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable mymine
sudo systemctl start mymine
```

---

## Running on macOS / Windows

**macOS:**
```bash
python3 trackpac_server.py
# Opens at http://localhost:8765
```

**Windows:**
```batch
python trackpac_server.py
```
Place the script in a folder where you have write permissions (e.g. Desktop or Documents). Running from a system folder like `C:\Program Files` will cause a permission error on `clients.json`.

---

## Security Notes

- This server has **no authentication** — it is designed for local network / VPN use only
- Do **not** expose port 8765 directly to the internet without a reverse proxy and authentication
- `clients.json` contains client contact details — add it to `.gitignore` (already done)
- Store credentials in environment variables for production deployments:

```python
import os
SMTP_PASS = os.environ.get("MYMINE_SMTP_PASS", "")
TWILIO_AUTH_TOKEN = os.environ.get("MYMINE_TWILIO_TOKEN", "")
```

---

## License

MIT — see [LICENSE](LICENSE)

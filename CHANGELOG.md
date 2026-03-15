# Changelog

All notable changes to MyMine Dashboard are documented here.

---

## [1.0.0] — 2026-03-14

### Added
- Initial public release
- Client management (add, edit, delete) with sensor EUI assignment
- Live dashboard with temperature, humidity and battery charts (Chart.js)
- Configurable alarm thresholds per client (T min/max, humidity min/max)
- Email alerts via Gmail SMTP with HTML formatting
- SMS alerts via Twilio REST API
- Automatic E.164 phone normalization for Italian numbers
- ASCII sanitization of SMS body (replaces `°C`, accented chars)
- Daily PDF report at 09:00 — 24 hourly averages for previous day
- On-demand PDF report download from dashboard
- Auto-generated username + password per client
- "Send credentials" button emails login info to client
- Edit existing clients without losing stored credentials
- Server diagnostics banner with write-test on every save
- `/api/test_notify` endpoint for testing email and SMS independently
- `/api/status` health-check endpoint
- Sensor list upload from `.txt` file (tab/comma/semicolon separated)
- Zero external Python dependencies — stdlib only
- Single-file deployment

### Architecture
- Embedded HTML/JS frontend (no build step, no npm)
- PDF generation in pure Python (no reportlab, fpdf, or wkhtmltopdf)
- JSON flat-file storage (`clients.json`, `alerts.json`)

#!/usr/bin/env python3
"""
MyMine Dashboard Server v3
Self-hosted IoT monitoring dashboard for Trackpac sensors.
No external Python dependencies required.
"""
# ─── CONFIGURATION — edit before first run ──────────────────────────────────
import http.server, urllib.request, urllib.error, json, sys, os, io, zipfile
import secrets as _sec, hashlib as _hash
import smtplib, threading, time as _time, urllib.parse as _uparse
from urllib.parse import urlparse, parse_qs
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone

import os as _os

API_KEY = _os.environ.get("TRACKPAC_API_KEY", "YOUR_TRACKPAC_API_KEY")
BASE    = _os.environ.get("TRACKPAC_BASE",    "https://v2-api.trackpac.io")
PORT    = int(_os.environ.get("PORT", "8765"))
BUILD_TS    = '2026-03-16 17:05:46'
_DATA_DIR   = _os.environ.get("DATA_DIR", _os.path.dirname(_os.path.abspath(__file__)))
DATA        = _os.path.join(_DATA_DIR, "clients.json")
ALERTS_FILE = _os.path.join(_DATA_DIR, "alerts.json")
_SCRIPT_DIR  = _os.path.dirname(_os.path.abspath(__file__))
SENSORI_FILE = _os.path.join(_SCRIPT_DIR, "sensori.txt")

# SMTP config (Gmail: myaccount.google.com > Sicurezza > Password per le app)
SMTP_HOST = _os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(_os.environ.get("SMTP_PORT", "587"))
SMTP_USER = _os.environ.get("SMTP_USER", "")
SMTP_PASS = _os.environ.get("SMTP_PASS", "")
SMTP_FROM = _os.environ.get("SMTP_FROM", "MyMine Alerts <" + _os.environ.get("SMTP_USER","") + ">")

# Telegram rimosso

# SMS via Twilio (https://twilio.com — trial gratuito con credito incluso)
# 1. Registrati su twilio.com -> Dashboard -> copia Account SID e Auth Token
# 2. Ottieni un numero "From" gratuito nella sezione Phone Numbers
# 3. Il numero del cliente va salvato in formato internazionale: +393331234567
TWILIO_ACCOUNT_SID = _os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = _os.environ.get("TWILIO_AUTH_TOKEN",  "")
TWILIO_FROM_NUMBER = _os.environ.get("TWILIO_FROM_NUMBER", "")

ALERT_INTERVAL = 600

# ─── AUTH ─────────────────────────────────────────────────────────────────────
ADMIN_USER = _os.environ.get("ADMIN_USER", "filippo@mymine.io")
ADMIN_PASS = _os.environ.get("ADMIN_PASS", "Mymine2026!")
SESSIONS   = {}   # token -> {user, role, client_idx, exp}

def _make_session(user, role, client_idx=None):
    import time
    token = _sec.token_hex(24)
    SESSIONS[token] = {"user": user, "role": role,
                       "client_idx": client_idx, "exp": time.time() + 86400}
    return token

def _get_session_from_cookie(cookie_header):
    import time
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if part.startswith("mm_sess="):
            token = part[8:]
            s = SESSIONS.get(token)
            if s and time.time() < s["exp"]:
                return s
            if s:
                del SESSIONS[token]
    return None

def _hash_pass(p):
    return _hash.sha256(p.encode()).hexdigest()

def _find_client_by_creds(username, password):
    clients = load_clients()
    for i, c in enumerate(clients):
        u = c.get("username", c.get("email", "")).lower().strip()
        p = c.get("password", "")
        if u == username.lower().strip() and p == password:
            return i, c
    return None, None

def load_clients():
    if not os.path.exists(DATA): return []
    with open(DATA) as f:
        content = f.read().strip()
    if not content: return []
    try: clients = json.loads(content)
    except: return []
    changed = False
    for c in clients:
        if "eui" in c and "sensori" not in c:
            c["sensori"] = [{"eui": c["eui"], "nome_frigo": c.get("nome_frigo","")}]
            changed = True
    if changed: save_clients(clients)
    return clients

def save_clients(lst):
    with open(DATA,"w") as f: json.dump(lst,f,indent=2,ensure_ascii=False)

def load_sensori():
    """Carica lista sensori da sensori.txt (EUI<TAB>descrizione)."""
    sensori = []
    cr = chr(13)
    for path in [SENSORI_FILE, _os.path.join(_DATA_DIR, "sensori.txt")]:
        if _os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip().replace(cr, "")
                        if not line or line.startswith("#"): continue
                        parts = line.split("\t")
                        eui = parts[0].strip().upper()
                        desc = parts[1].strip() if len(parts) > 1 else eui
                        if len(eui) >= 8:
                            sensori.append({"eui": eui, "desc": desc})
            except Exception: pass
            break
    return sensori

def get_client_sensor(client, idx=0):
    sensori = client.get("sensori", [])
    if sensori and idx < len(sensori):
        return sensori[idx]
    return {"eui": client.get("eui",""), "nome_frigo": client.get("nome_frigo","")}

def load_alerts():
    if not os.path.exists(ALERTS_FILE): return {}
    with open(ALERTS_FILE) as f: return json.load(f)

def save_alerts(d):
    with open(ALERTS_FILE,"w") as f: json.dump(d,f,indent=2,ensure_ascii=False)

def call_api(path):
    req=urllib.request.Request(BASE+path,headers={"X-API-Key":API_KEY,"Accept":"application/json"})
    try:
        with urllib.request.urlopen(req,timeout=20) as r: return r.read(),r.status
    except urllib.error.HTTPError as e: return e.read(),e.code

# XLSX builder
def xe(s): return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")
def col_letter(n):
    s="";n+=1
    while n: n,r=divmod(n-1,26);s=chr(65+r)+s
    return s
def cell_addr(row,col): return col_letter(col)+str(row+1)

def build_xlsx(sheet_rows,col_widths):
    strings,str_idx=[],{}
    def si(s):
        s=str(s)
        if s not in str_idx: str_idx[s]=len(strings);strings.append(s)
        return str_idx[s]
    F=(
        '<font><sz val="10"/><name val="Arial"/></font>'
        '<font><b/><sz val="15"/><name val="Arial"/><color rgb="FF1DB584"/></font>'
        '<font><b/><sz val="11"/><name val="Arial"/><color rgb="FF1F4E3D"/></font>'
        '<font><b/><sz val="9"/><name val="Arial"/><color rgb="FFFFFFFF"/></font>'
        '<font><sz val="9"/><name val="Arial"/><color rgb="FF2D3F3A"/></font>'
        '<font><b/><sz val="9"/><name val="Arial"/><color rgb="FF1DB584"/></font>'
        '<font><b/><sz val="8"/><name val="Arial"/><color rgb="FF1DB584"/></font>'
        '<font><b/><sz val="10"/><name val="Arial"/><color rgb="FF1F4E3D"/></font>'
        '<font><i/><sz val="8"/><name val="Arial"/><color rgb="FF7A9990"/></font>'
        '<font><b/><sz val="9"/><name val="Arial"/><color rgb="FF2D3F3A"/></font>'
        '<font><b/><sz val="8"/><name val="Arial"/><color rgb="FFFFFFFF"/></font>'
    )
    FL=(
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E3D"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFE8F5EF"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFFFF"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF5FAF7"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1DB584"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFD6EFE4"/></patternFill></fill>'
    )
    t='<left style="thin"><color rgb="FFCDE5D9"/></left><right style="thin"><color rgb="FFCDE5D9"/></right><top style="thin"><color rgb="FFCDE5D9"/></top><bottom style="thin"><color rgb="FFCDE5D9"/></bottom>'
    B=f'<border><left/><right/><top/><bottom/></border><border>{t}</border>'
    NF='<numFmt numFmtId="164" formatCode="0.0"/><numFmt numFmtId="165" formatCode="0"/>'
    C='<alignment horizontal="center" vertical="center"/>'
    CW='<alignment horizontal="center" vertical="center" wrapText="1"/>'
    L='<alignment horizontal="left" vertical="center" indent="1"/>'
    R='<alignment horizontal="right" vertical="center" indent="1"/>'
    XFS=[
        (0,0,0,0,""),(1,2,0,0,C),(2,2,0,0,R),(3,2,0,0,CW),
        (4,4,1,0,C),(4,5,1,0,C),(4,4,1,164,C),(4,5,1,164,C),(4,4,1,165,C),(4,5,1,165,C),
        (6,3,1,0,C),(7,4,1,0,L),(9,7,1,0,L),(5,4,1,0,C),(10,2,0,0,C),(8,0,0,0,C),
        (0,6,0,0,""),(4,4,1,0,C),(4,5,1,0,C),
    ]
    xf="".join(f'<xf numFmtId="{nf}" fontId="{fi}" fillId="{fli}" borderId="{bi}" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1" applyNumberFormat="1">{al}</xf>' for fi,fli,bi,nf,al in XFS)
    styles=(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<numFmts count="2">{NF}</numFmts><fonts count="11">{F}</fonts>'
        f'<fills count="8">{FL}</fills><borders count="2">{B}</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        f'<cellXfs count="{len(XFS)}">{xf}</cellXfs></styleSheet>'
    )
    cols="<cols>"+"".join(f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>' for i,w in sorted(col_widths.items()))+"</cols>"
    rxml=""
    for ri,row in enumerate(sheet_rows):
        if row is None: continue
        cells=row.get("cells",[]) if isinstance(row,dict) else row
        ht=f' ht="{row["height"]}" customHeight="1"' if isinstance(row,dict) and "height" in row else ""
        cx=""
        for ci,cell in enumerate(cells):
            if len(cell)<2: continue
            val,style=cell[0],cell[1];typ=cell[2] if len(cell)>2 else "s"
            addr=cell_addr(ri,ci)
            if typ=="n" and val is not None: cx+=f'<c r="{addr}" s="{style}" t="n"><v>{val}</v></c>'
            elif typ=="e" or val is None or val=="": cx+=f'<c r="{addr}" s="{style}"/>'
            else: cx+=f'<c r="{addr}" s="{style}" t="s"><v>{si(val)}</v></c>'
        rxml+=f'<row r="{ri+1}"{ht}>{cx}</row>'
    sheet=(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheetViews><sheetView workbookViewId="0" showGridLines="0"><selection activeCell="A1"/></sheetView></sheetViews>'
        +cols+f'<sheetData>{rxml}</sheetData>'
        '<pageSetup orientation="portrait" fitToPage="1" fitToWidth="1" fitToHeight="0"/></worksheet>'
    )
    ss=(f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(strings)}" uniqueCount="{len(strings)}">'
        +"".join(f'<si><t xml:space="preserve">{xe(s)}</t></si>' for s in strings)+'</sst>')
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        zf.writestr("_rels/.rels",'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        zf.writestr("xl/_rels/workbook.xml.rels",'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        zf.writestr("xl/workbook.xml",'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Report" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/styles.xml",styles)
        zf.writestr("xl/sharedStrings.xml",ss)
        zf.writestr("xl/worksheets/sheet1.xml",sheet)
    buf.seek(0);return buf.read()

def generate_pdf_report(client):
    """Genera PDF report con le misurazioni orarie del giorno precedente."""
    try:
        body, code = call_api("/device/")
        if code != 200: return None, f"API error {code}"
        devs = json.loads(body)
        _si  = client.get("_sensor_idx", 0)
        _s   = get_client_sensor(client, _si)
        _eui = _s.get("eui", "")
        dev  = next((d for d in devs if (d.get("dev_eui","")).upper() == _eui.upper()), None)
        if not dev: return None, "Device non trovato"
        dev_id = dev["id"]
        yday = datetime.now(timezone.utc) - timedelta(days=1)
        body, code = call_api(f"/frame/{dev_id}/{yday.strftime('%Y-%m-%dT00:00:00')}/{yday.strftime('%Y-%m-%dT23:59:59')}")
        if code != 200:
            body, code = call_api(f"/frame/days/{dev_id}/2")
        frames_raw = json.loads(body)
        if isinstance(frames_raw, dict):
            frames_raw = frames_raw.get("frames") or frames_raw.get("data") or frames_raw.get("items") or []
        # Parse and sort frames
        rows = []
        for f in frames_raw:
            try:
                ts_str = f.get("time_created") or f.get("time") or f.get("created_at","")
                ts = datetime.fromisoformat(ts_str.replace("Z","+00:00")).astimezone()
                p = _get_payload(f)
                T = _get_val(p, "temperature","temp")
                H = _get_val(p, "humidity","hum")
                B = _get_val(p, "battery_pct","battery","bat")
                rows.append({"ts": ts, "T": T, "H": H, "B": B})
            except: pass
        rows.sort(key=lambda r: r["ts"])
        # Bucket into 24 hourly slots for yesterday
        yday_local = (datetime.now() - timedelta(days=1)).date()
        hourly = {}
        for r in rows:
            h = r["ts"].hour
            if r["ts"].date() == yday_local:
                if h not in hourly:
                    hourly[h] = []
                hourly[h].append(r)
        # Average per hour
        hour_rows = []
        for h in range(24):
        	if h in hourly:
        		vals = hourly[h]
        		T_avg = sum(r["T"] for r in vals if r["T"] is not None) / max(1, sum(1 for r in vals if r["T"] is not None)) if any(r["T"] is not None for r in vals) else None
        		H_avg = sum(r["H"] for r in vals if r["H"] is not None) / max(1, sum(1 for r in vals if r["H"] is not None)) if any(r["H"] is not None for r in vals) else None
        		hour_rows.append((h, T_avg, H_avg))
        	else:
        		hour_rows.append((h, None, None))
        # All rows for summary
        all_T = [r["T"] for r in rows if r["T"] is not None]
        all_H = [r["H"] for r in rows if r["H"] is not None]
        nome = (client.get("cognome","") + " " + client.get("nome","")).strip()
        date_str = yday_local.strftime("%d/%m/%Y")
        pdf = _build_pdf(nome, client, date_str, hour_rows, all_T, all_H, rows)
        return pdf, None
    except Exception as e:
        import traceback
        print(f"  [PDF] errore: {e}\n{traceback.format_exc()}")
        return None, str(e)

def _build_pdf(nome, client, date_str, hour_rows, all_T, all_H, raw_rows):
    """Genera PDF report misurazioni — layout A4, tabella 24 ore."""
    def esc(s):
        # Encode for PDF string literals: escape \, (, )
        s = str(s).replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
        return s.encode('latin-1', errors='replace').decode('latin-1')
    def fmt(v, d=1):
        return f"{v:.{d}f}" if v is not None else "--"

    # ── Page geometry ─────────────────────────────────────
    W, H   = 595, 842           # A4 points
    ML, MR = 45, 45             # left / right margin
    ROW_H  = 19                 # table row height (pt)

    # ── Column positions (x start of each column) ─────────
    # Ora | Temperatura | Umidita  (3 colonne)
    COL_X = [ML + 6, ML + 110, ML + 320]   # left edge of text
    COL_W = [       98,        200,         207]      # width

    # ── Color helpers ─────────────────────────────────────
    C_DARKBG = "0.122 0.302 0.251"   # #1F4E3D
    C_GREEN  = "0.114 0.710 0.518"   # #1DB584
    C_TEXT   = "0.102 0.239 0.188"   # #1A3D30
    C_SUB    = "0.306 0.451 0.400"   # #4E7367
    C_RED    = "0.851 0.310 0.310"   # #D94F4F
    C_BLUE   = "0.157 0.471 0.690"   # #2878B0
    C_LIGHT  = "0.945 0.961 0.949"   # very light green-grey
    C_LINE   = "0.808 0.918 0.855"   # #CEEADB
    C_ALARM  = "1.000 0.940 0.940"   # light red for alarm rows

    # ── Helpers ───────────────────────────────────────────
    ops = []
    def g(s):  ops.append(s)
    def rect(x, y, w, h, color, fill=True):
        g(f"{color} rg")
        g(f"{x:.1f} {y:.1f} {w:.1f} {h:.1f} re {'f' if fill else 'S'}")
    def txt(x, y, font, size, color, text):
        g(f"{color} rg")
        g(f"BT /{font} {size} Tf {x:.1f} {y:.1f} Td ({esc(text)}) Tj ET")

    # ══════════════════════════════════════════════════════
    # HEADER  (logo su sfondo bianco, nessuna banda)
    # ══════════════════════════════════════════════════════
    # Thin green top accent line
    rect(ML, H-20, W-ML-MR, 4, C_GREEN)
    # Logo: scale to 42pt height, maintain aspect 97x65
    _lw, _lh = 97, 65
    _ph = 42; _pw = int(_lw * _ph / _lh)   # ~63pt wide
    _px = ML; _py = H - 72
    g(f"q {_pw} 0 0 {_ph} {_px} {_py} cm /Im1 Do Q")
    # Title text to the right of logo
    txt(ML + _pw + 12, H-38, "F2", 13, C_DARKBG, "REPORT MISURAZIONI HACCP")
    txt(ML + _pw + 12, H-52, "F1", 9,  C_SUB,    f"Data: {date_str}")
    txt(ML + _pw + 12, H-63, "F1", 8,  C_SUB,    f"Cliente: {nome}")
    # Separator line below header
    rect(ML, H-78, W-ML-MR, 0.8, C_LINE)

    # ══════════════════════════════════════════════════════
    # CLIENT INFO BOX  (compact, below header)
    # ══════════════════════════════════════════════════════
    y = H - 92
    rect(ML, y-34, W-ML-MR, 38, C_LIGHT)
    txt(ML+10, y-10, "F2", 10, C_TEXT, f"EUI: {client.get('eui','')}")
    txt(ML+10, y-23, "F1", 8,  C_SUB,  client.get('indirizzo',''))
    txt(W-MR-220, y-10, "F1", 8, C_SUB, f"Email: {client.get('email','')}")
    txt(W-MR-220, y-23, "F1", 8, C_SUB, f"Tel: {client.get('telefono','')}")

    # ══════════════════════════════════════════════════════
    # SUMMARY STATS (3 boxes)
    # ══════════════════════════════════════════════════════
    y -= 58
    t_min_v = min(all_T) if all_T else None
    t_max_v = max(all_T) if all_T else None
    t_avg_v = (sum(all_T)/len(all_T)) if all_T else None
    h_min_v = min(all_H) if all_H else None
    h_max_v = max(all_H) if all_H else None
    h_avg_v = (sum(all_H)/len(all_H)) if all_H else None
    n_ore   = sum(1 for _, T, _ in hour_rows if T is not None)

    # Battery: last available reading from raw_rows
    all_B = [r["B"] for r in raw_rows if r.get("B") is not None]
    last_B = all_B[-1] if all_B else None
    is_volt = last_B is not None and last_B < 10
    b_label = f"{last_B:.2f} V" if (last_B is not None and is_volt) else (f"{last_B:.0f} %" if last_B is not None else "--")
    b_min   = f"Min: {min(all_B):.2f}{'V' if is_volt else '%'}" if all_B else "Min: --"
    b_max   = f"Max: {max(all_B):.2f}{'V' if is_volt else '%'}" if all_B else "Max: --"

    box_w = (W - ML - MR - 12) // 3   # ~163 pt each
    for i, (title, bar_col, lines_txt) in enumerate([
        ("Temperatura", C_RED,
         [f"Min: {fmt(t_min_v)} grC", f"Max: {fmt(t_max_v)} grC", f"Media: {fmt(t_avg_v)} grC"]),
        ("Umidita relativa", C_BLUE,
         [f"Min: {fmt(h_min_v,0)} %", f"Max: {fmt(h_max_v,0)} %", f"Media: {fmt(h_avg_v,0)} %"]),
        ("Batteria sensore", C_GREEN,
         [f"Ultimo: {b_label}", b_min, b_max]),
    ]):
        bx = ML + i * (box_w + 6)
        rect(bx, y-58, box_w, 58, C_LINE)
        rect(bx, y-3,  box_w, 3,  bar_col)
        txt(bx+8, y-14, "F2", 8, C_TEXT, title)
        for j, ln in enumerate(lines_txt):
            txt(bx+8, y-27-j*11, "F1", 8, C_SUB, ln)

    # ══════════════════════════════════════════════════════
    # TABLE HEADER
    # ══════════════════════════════════════════════════════
    y -= 74
    rect(ML, y-ROW_H, W-ML-MR, ROW_H, C_DARKBG)
    for ci, (label, cx) in enumerate(zip(
            ["Ora", "Temperatura (grC)", "Umidita (%)"], COL_X)):
        txt(cx, y-13, "F2", 8, "1 1 1", label)

    # Column separator lines in header
    g(f"0.3 0.55 0.45 rg")
    for cx in COL_X[1:]:
        g(f"{cx-4:.1f} {y-ROW_H:.1f} 0.5 {ROW_H:.1f} re f")

    # ══════════════════════════════════════════════════════
    # TABLE ROWS  (24 righe orarie)
    # ══════════════════════════════════════════════════════
    t_min_th = client.get("t_min")
    t_max_th = client.get("t_max")
    h_min_th = client.get("h_min")
    h_max_th = client.get("h_max")

    for row_i, (hour, T_val, H_val) in enumerate(hour_rows):
        y -= ROW_H
        if y < 55:
            break   # Safety: won't happen for 24 rows on A4

        # Row background
        alarm_t = (T_val is not None and (
            (t_min_th is not None and T_val < t_min_th) or
            (t_max_th is not None and T_val > t_max_th)))
        alarm_h = (H_val is not None and (
            (h_min_th is not None and H_val < h_min_th) or
            (h_max_th is not None and H_val > h_max_th)))
        if alarm_t or alarm_h:
            rect(ML, y-ROW_H+1, W-ML-MR, ROW_H-1, C_ALARM)
        elif row_i % 2 == 0:
            rect(ML, y-ROW_H+1, W-ML-MR, ROW_H-1, C_LIGHT)

        # Row data
        hour_str = f"{hour:02d}:00 - {hour:02d}:59"
        T_str    = (fmt(T_val) + " grC") if T_val is not None else "--"
        H_str    = (fmt(H_val, 0) + " %") if H_val is not None else "--"

        cell_txt = [hour_str, T_str, H_str]
        cell_col = [C_TEXT, C_RED if alarm_t else C_TEXT,
                    C_BLUE if alarm_h else C_TEXT]

        for ci, (v, cx, cc) in enumerate(zip(cell_txt, COL_X, cell_col)):
            txt(cx, y-13, "F1", 9, cc, v)

        # Thin row separator
        g(f"{C_LINE} rg")
        g(f"{ML:.1f} {y-ROW_H+1:.1f} {W-ML-MR:.1f} 0.4 re f")

    # ══════════════════════════════════════════════════════
    # FOOTER
    # ══════════════════════════════════════════════════════
    rect(ML, 20, W-ML-MR, 0.5, C_LINE)
    txt(ML,       24, "F1", 7, C_SUB, "MyMine Srl  -  P.IVA IT12038850967  -  info@mymine.io")
    txt(W-MR-50,  24, "F1", 7, C_SUB, "Pag. 1 / 1")

    # ══════════════════════════════════════════════════════
    # ASSEMBLE PDF BINARY
    # ══════════════════════════════════════════════════════
    stream_str   = "\n".join(ops)
    stream_bytes = stream_str.encode("latin-1", errors="replace")

    # PDF objects
    objs = []
    def obj(n, header, payload=None):
        objs.append((n, header, payload))

    obj(1, "<< /Type /Catalog /Pages 2 0 R >>")
    obj(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    obj(3, (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] "
            f"/Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R >> "
            f"/XObject << /Im1 7 0 R >> >> >>"))
    obj(4, f"<< /Length {len(stream_bytes)} >>", stream_bytes)
    obj(5, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    obj(6, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    # Embed pre-decoded raw RGB logo (no runtime PNG parsing needed)
    import base64 as _b64i, zlib as _zlibpdf
    _raw_rgb = _b64i.b64decode(LOGO_RAW_RGB_B64)
    _img_stream = _zlibpdf.compress(_raw_rgb, 6)
    obj(7, (f"<< /Type /XObject /Subtype /Image /Width {LOGO_W} /Height {LOGO_H} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
            f"/Filter /FlateDecode /Length {len(_img_stream)} >>"), _img_stream)

    buf     = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"   # header + binary comment
    offsets = {}

    for num, header, payload in objs:
        offsets[num] = len(buf)
        buf += f"{num} 0 obj\n{header}\n".encode()
        if payload is not None:
            buf += b"stream\n" + payload + b"\nendstream\n"
        buf += b"endobj\n"

    xref_pos = len(buf)
    n_objs   = len(objs) + 1
    buf += f"xref\n0 {n_objs}\n0000000000 65535 f \n".encode()
    for i in range(1, n_objs):
        buf += f"{offsets[i]:010d} 00000 n \n".encode()
    buf += (f"trailer\n<< /Size {n_objs} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    return bytes(buf)


HACCP_LOGO_B64 = "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"
LOGO_RAW_RGB_B64 = "////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////JkEWVHdBV31AWX9CVXY/TWY8////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////FS4Gep1jg65mhbRkhrVlh7FpgqNqRVs0////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////ZYZRiLJob6RIdKxHbaVCeatSgKhidpRi////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////OTwx////////Ym9d////////JzIi////ICsdWWFW////////GR4X////////////////////////////////////////////////////PkQ2GiER////ZG1aIi4Y////WmZOFiIO////ZGtj////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////iq5we61WeLNJd7hEdLNAe7RNfKtaepxg////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////HzoRgKljdqxMeLlDabAub7Q3crE/frFVVXw5////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////SEw7////////ZHVV////EiIFXGtO////HiwSSlg/////////JzAd////GB4Q////////////////////////////////GiYS////////MUEkIzQU////TGI8GC4H////YntRKD4a////GyMY////////HSIb////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////f6NmfKlWfLNOeLRDebVEc6pEgq1dJ0YL////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////KCsi////////f4hzEB8C////hpt0HzAOHS4MjJ15Gy8KIjYTd4pq////////WGRQ////ICsaQEs7////////IScd////ISwbIy8b////QEw0QU40////a39aRV40////hZ50OVIo////iaV1PVYu////Qk09////////Nz8y////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////OEwpiqFzgp9ph6dmhKZch6ZiNUsc////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////Jj8V////////IzoO////////J0IZ////////////////////////////////////////////////////////////////HzkMCykA////GTME////////IT4O////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////IzIVPk8rYXNLQlgpHzEH////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////QEE7////FCIVf5l2L1EfTGw6gp5tTWI5OE0iiKRxR2gvXYBGfJxqMUQmM0ItbH1r////KEAmVWtU////MTYyXmBd////OEwxRV09////aHVbT1k+IjIOfppnaotSO1kjfpppWW5HMkkff59tYYROIDoNi598GikS////bXtiFCAK////Gx8e////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////P0E2////FiUSi6Z9J0oSPWAmgqFoOE4fUGY1hKJmPWIfQWkjgKRnJTwSMUIif5J0////IT0XUmxJ////MTopWWFU////M00qPVkx////YXJSWmlKESYAe5xlUng7LE8Vj615W3JGJ0IVf6VsRWsyKkYWiqB6IzUb////VmRKEx8H////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////BCAA////////////////////////////////////////////////////////////////////////////////////////////BiAACiIA////////////////////////////////////////////////////////////////////////////Ey8A////BysAUG8z////////VXU0////AiwAVXo3////////CR8A////////BSAA////////FSMJ////DCwAAiQA////IDcNGC0G////RWksHEUD////PmIlFTEA////QmspDzgA////EigB////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////Dy0VHD0iJkkrKEkqKkwrLE0uLU4vKUorIkUnGz4gGjge////////////////////Bx8FHDYZMkwvMkwvN1E0N1E2L0ctHzUeDCIN////////////////////////////GSMaFicXFy8ZDioRFjQaCSUOFCsX////////////////////////////////////Dh4REygVGDAaFS4YEysVFCoVHS8f////////////////////////////////////////////BiMAL1MVQGknWIRFbJpadaRgdaVbcqRPcqFRZ41QYoJTHTwT////////////////GTsJTnI1ZotGeqNXfqdbe6Bab5NTU3Y+OVkq////////////////////////////////////////////NEwoMU4eNFgbPGMgPWQjOlwqKEMk////////////////////////////AiMAH0QRO2MvU3s/Y4xKcZhXeqBXdp5GdJxEdZtSbY9SSGkwGDQD////////////////////////////////////////////////////////////ByAAL1AZV4A6a5hHd6RPgKxVgKdWeZpTYXs+LkIP////////////////////////////////////LTQt////////XF9O////JzYZe5hiOV4bWX09fqBkTmY0WnI+hqZlQ2sibppNdp5YPFciMUYbPVQoGzcHYIFKhaNvHTIJeIhjiZxvHjkEdp1cfqdjHkAEhKFrepViMlEYd6BacqBVPWgge6NdX39AOFsZf6tcYY1CLk0Ulq+FK0AhOEsvjZ5+IS8V////OT87////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////Jj4oL00zMVQ2Mlo4Llg0KlYyMFo0MFo0MVk0MFgzLlgyKlcwKFYvKFQwLlExDSsP////CSoLK04tOV86MVoyKlMrLFUtLFUtLlQvLlMxMVQ0LE0uGTodCiMN////////////////////LT0wOlM9MFE0MFg2N2E9Jk4sMFE2Ch8O////////////////////////////////ITokK0ktM1Q1MFMzMFE0MU00NUo3////////////////////////////////////NEojZINHg6xgeqpWeq9XerFdda9acKtRbatIcbFDeLNJhrhhb51SeaZhV4RDBjIAF0EBaJNOg69ifqxUhLNVgrJOdahDc6VEe65RfK5XdaNYX4dLCioA////////////////////////////////DyMHf5xwg6lsgaxkdaRTd6NUe6FkUW1F////////////////////FCcHZIRDa5ZGdqdVfbFcfLFXeK1ReatSeqtPfq5Ig7RLd6hMbp5Igq5fiLFrTm84////////////////////////////////////////////////////RV8vfqVih7lkcatHcrBDd7dFebRGd65GgLFViLJgeZ1VYH9DCiUA////////////////////////////////////////JCcU////////aINMJEkGK1EUeZpjJ0ESOVMjeJpdNFwWLloPa5RQFzMA////LEYZAR8AMFMZWXpBDiUAOEseXHU+////R3EnOWUaCy8AZoVKQl8pBicAXYhBSXorFUMAdZ9XRGclETYAcqFQNGAREjQAYXtOESYFHTEVcIFh////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////MU8zNFk4LFg0KVkxJFcsIlUqJ1ctJ1YsKFUsKFctKFguKFswJ18wKGAzLFw2KVcyK1k0Ll42KVwxIlcpIlgnJl4tJ18uJl4tJFkrI1YrKVwzMGA6Klk1J0sv////////////////////FisaNVY7K1QyIVQpJFktI1MrLFUzGjMe////////////////////////////////Kk0tLlYxL1wzKVMtL1c0LU4xKEEr////////////////////////////////////aIhHfqhSfa9Mdq9GdrBJdLJPcrJScbFQcbJMcbVEc7ZDa6Y6fLVOerJNda1Ke7NSdatLb6VDf7NOc6g8d609e7E/e7Q/fLdBebhDdrdDdLFJg7dkbZdXDCkA////////////////////////////Iz0WhqlxfKdfdadQc6tKgLNWg6tiIz8M////////////////////W3Y/fqZOeq5CeLJBdbQ/c7I/c7JAeLFGe7RLdq5BebFEd7BHda1IeLBNe7BWd6Jabo9a////////////////////////////////////////KkEVdZRYjLlob6dEZKQ0eMBDaLMybLc2b7Y8a7A7bq1CdrBMe7BUfaxcU3c5DSYA////////////////////////////////////////////////Mk0Y////AygAJEUY////////UHE8////ED0AV39ABiMADCUAR2I3////J00UP2Iq////LkQWOFMc////JlIHEj4A////OlgiMEwb////Tng2H04G////SHEvFDYA////VoI1Ej4A////J0ES////////PE0r////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////L1AzLlg0JlYuI1gsJV0uKl8zJ1ctKFYvKVcwKlgxJlkuIlorH1oqH1oqIlkvIFcvIFctIFkuIVotIVwsIFwpIFwpG1ckI18sJWAwIVotH1gtIlswI1wzKVU0KT4v////////////////////LFA0KFYxI10tH1soJV8vKVkxJEUq////////////////////////////////MVs1K1wtI1cnJVYpMV43LE8vIDoh////////////////////////////////CycAh65fgbRPcao5d7Q/c7JAcq9Hc69NdbNQdbRMcrJCbq87drZEdLNAcK88cbA9eLdEe7dFeLJCdq5BgrdLe65Fd6xAebFCdLM+a680a7M1c7hBcK5JfrFiTnE3////////////////////////////OlopgKtmd6lSda9Ld7VIdaxHe6FUESkA////////////////////cI9Mga9MebM7c7Q0crU0c7Q8dLNBdrFHdq5Jd69Kdq9IdLJHdLRGdbVFdbJKeK1VfqdjOlUs////////////////////////////////N10keKFbf69bdaxHeLdFd7tEbbM3cbg+a7Q+bbNBcrdKc7dKcLJGb7FFd7BPj7ltN1Mg////////////////////////HCUi////////WVpKISUWKjcdfphoT3I4GDwMKEgfJDwaXndQhaVzU3s/ZJFQeaNkSWY4b4phhaJ2TG48gahveqBnLUcahp1vhqFsTW8yfqlifKdgH0IIh6R0gJlvPlste6RqdaNjR3EyfKJpa4lVRmUsdZ5YZo9JOlwggJxsMEYiNUoriZp2OUoq////PkhA////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////LlEzLVczJlkuJl4vIVksJ1wwKFgyLlo3Mls5KVUyLFw2I1guJV8vGlUlJl80IlswJF0yHFUqJF0yIlsuJV4xHlcqJV0wJl4xIlotG1QpJV4zG1QpJl80K1o2OVI9////////////////////JUktJVUvIV0qHl8pIWArI1gqLVIz////////////////////////////EjcVLF0wHlckJF4rJFgoLVozMFMz////////////////////////////////////L1AZfqtad65Id7I+eLM9c608erFLeK5MeaxNd65JerJDebNCeLZJdbVHcbE/d7hCb7A4dLM+b6c6gLRQfapVfqlaf6teeqtZc6xNcbNHdr1FaLI5cLRFd7BRfKVfECkA////////////////////////Y4lMgrNhb6dEdrRFcbI8dq1FaotG////////////////////DSEAgaFggbBQeLM/cLA1drc/batAerNUeq5bdaVZdqlafrRedLBMcbNFdbpFcLNAc61He6pZdJVc////////////////////////////JlITfbRgbqdJd69KfrdMcKo8g7xTcKhFebFWdq1ZbqdWeLRebKxMcrVLbbI9dLREf7BVbpFR////////////////////////////////////////////////T2g+CCoAACEALk4l////KkQdcJJfGEIDHUoHYYtMBSIANE8kcI1fEjQBNV0fTXU3////SWAyYHpKCigAW4NFRm8z////WnZOUGhI////RGo7PWUxAiYAYoFVQ1w1////Y4lQLFIVAyIAQFwsEScD////M0Qg////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////LlA1K1czJlkuJl4vIVksKFsyKFQxGkEkFzshFzsfI0wsJ1UwMmc9J2AzIlsuIlkvKV40JlsxMGA6JFQuIU8qGkgjFkIfIk4qK1k0MWE7LWA3HVIoIVguKFYxNlI5IDIi////////////////DzEYKlgzIV0qH2AoImMrJV8tLFYyDygS////////////////////////HEYgJVsqIFwmIl4oI1gqLls0JEUo////////////////////////////////////RWsweqtZebJLdrJAcas7eq9HhbNbVH8wSHElSW8iXIYwfq1Rc6xLcbBHcbE/dLU/cLE5f7tJhLhTeqdUZYxLTG85S207XoVMgLNoebRYarBAcbtCbbQ+dbJKfalcKEQT////////////////////////b5lReq9Tc7FEcrM9crY9d65IT2sw////////////////////LEIbhKdjdadIdK9BdrVAeblJgbhdVYQ8THU5SnE8SHE5XY9Ke7NYcbNHb7Y8b7Y8dbVHe7BUfqNgGzIG////////////////////HzwQdKtXbLFHfr1Scq1BdKtDfrBRfKdXTnU0Gj8LCC4AGEIQR3c5g7ppeLZRbbM5dLU9ea1LfKBaCR4A////////////////////////////////////////////NEwoACEAFDgIT3BDGTMMESwBW35GAiwAI1EJXIZEByUALkoaWXdF////IkwKVH07////OlIgU209////Rm4wOWEl////U21GRFk6////JEgYBy0AAB4AYX1UHjQQDygAUnM+I0YM////Qlwt////////RFUz////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////L1A1LVY0JlkuJl4vI1gsKlo0ETgb////////////////BSwPH00pIlUsKVwxKFguLVs0KlQwKU4tCSoN////////////////Dy0RMFMzL1c0KVMtKlkvL1kzMFEyNk44////////////////////L1g2I1ssHl0mImMpJmIuK1gxHjwi////////////////////////KFctIFkmIWApIl4qJFcsMVo4EjAY////////////////////////////////////ZI5OdqxWd7RNdrFHf7NReqdWWnxA////////////////MlMce69ddLJNcbFDcbI+crE8ebJHaZpBG0IA////////////////THQ/e7Bia6xGcLlEbbQ6dbVHe6pZQ2Et////////////////////HT4HeqlYc61HeblHbbE6d7hAe69NMEYV////////////////////UGs+gKhff7JVerVLeLZJb6hHbp1VACMA////////////CC4AU4Y3cbBIdbpDcbk8crVCdKxLg6tiLUcX////////////////////W39CeLZRbbQ8bKw6grlThbFad5dUGC8D////////////////////dZ9fc6lLaqoyfrxBeaxHjLBmEicA////////////////////////////////TktC////KDInf5d3SWo9TnM/kbR+WHJDYn1KeZxcU301aplJdqJXR2Yqc5BYgJ9kTXExcp1VeKJYUXEyf5thjqpwWHg5cJpSeqNdOVsffZlmjaR4JD4PKUwSLVQVSmsyg59sbYNVRFwsgaBkdZhYOVYgh6F0O1AvITUZg5R0Ljwi////Iykl////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////L1A1LVczJlkuJl4vJFcsK1k1GDoh////////////////////HUElMV05KFksJ1YqL1kxMFMyL0wwBx8J////////////////////KkQpM1IyNFozLlkuK1YrLFItOlc7////////////////////KUovKVkvH1snH14lJGAqKlkvLE8x////////////////////BCUILl8yIVsoIF8qI10rKFgyMFQ4////////////////////////////////////BiUAdqVhd69Yc7JKeLJLgbRYg6tlQFsw////////////////CR8Ad6Vacq5KdbVHdrVDe7dGfrVQZI9A////////////////////Hj8Uf65mcK5LarA8brg9dLdGd6ZVUW87////////////////////PWEjfrNbca5Gd7dFbrE9eLhGeqtSFicA////////////////////bY5VfapVeLBNca9EdLJHdKtOZo1U////////////////////KVQNc6tIc7dAarA0cLNAebFOgapeMUwZ////////////////Mkshgqxiba09fMRGd7ZEc6ZHe59XGjIC////////////////////////cJRXgrNYhcBMb6oyeatIh6tjDiAA////////////////////////////////////////////N04yAiMABiwAUnU7CyYAFzIAcJNPDDYAE0MAUX0u////HzwCWXg8AB4AFUEASXMnAB8AKkYJQ2AgBSkASHMjLlsK////TWstPFUc////IkcBDjYA////V3U3IzoE////SWkmFDcA////KEEX////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////L1A1K1czJVosJl4tJFcsLFg1Gjch////////////////////EC4WLlYzKFkqJ1koLlkuNFY1OlI6EiMT////////////////////JDchM1AxNVsyKlgpJlYmLFcsL1Ix////////////////////Fi8aMFo0J10rH1wjIl0nKFksMVk3CiMO////////////////FzoaK1wvJV8sHlomJFwvK1c2KEgx////////////////////////////////////JUMfe6lod7Fab65GdrNLcKFGgaZhHjQN////////////////////aZhIcK5DeblHdLNBcq1Be65Ra5NN////////////////////CCUAfahgdrBKeL1GcLc9dLZId6ZWUm49////////////////////V347fLNWc7JHb7JBc7NDd7RMbplJ////////////////////////fqhcfLNOebdKcLBAc7FCe65SPF4r////////////////////HUAAgrRVd7ZDbLA3dbVHeK5QgKhfK0UV////////////////YoRIhrdeba46crY7dq9GfKlWNVcb////////////////////////FS8CeaFbeq1QbKg3eLRDgK9TbpFP////////////////////////////////////////////HykgaYBkBicACS8AZ4lNGzMBFzAAao1JEDsAJ1YFbJhJGjoAN1QaZ4ZLAycAKFQJZ5FHDi4AGzcAJUQBBiwAXYo1QnMaACYAYYI7Um8tEjEAcpxKT3smEDcAcZJLPFQYDCYAYoY+Kk4IDSoAcIZfKDsf////W2tO////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////LlEzK1gxJVsqJl4tJFcsLlc1Hzsl////////////////////ECcTL1QyKFwrJV4pKVgqLVMuNlA3EB4R////////////////////FiwVLlAtLVgrJ1kmKl8pLF4tKFArCSEJ////////////////////MFMzL2AxI14mIVwkKFwsMVs1FzUd////////////////J0oqJVYpKGEuHlgmJlsxLFQ5GDMk////////////////////////////////////SWg/dqhhd7RVba9BdbNEgrNXg6dh////////////////////////cp9Mc7FCdrZGca9CcatHd6hWWHtB////////////////////DScAf6dec6xFbrI7bbQ8dbRLfaleRGAw////////////////////bJZOdq9QdrVMbK5CdbRLd7BPUXky////////////////////ID8Vfa5VdbRCcbRBbbI+drRFgbNaITwP////////////////////L0sOiLVWeLJBdLU/ebZOcqdNf6ZjHTkJ////////////HzoRfqZdeq5KeLlDertHdKxLfKtd////////////////////////KUQPbZFLgq9afLRRd7ZLd7RMhrNgUG80////////////////////////////////////KScY////ND4zjKOHLE0iMVQegqNsQ1osW3NBfJ9fTHYsa5pMf6piQWAlb4tYfZtnQmUraZRPfaZiPVwjM1AYMlEVM1sVcKFPdqhTL1gMdJhSiKdkKksGf6pYa5hDP2gahaljb4hOJT4Fe55aXoJCHDYGgphyQFI4////gpB2Eh4G////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////LlExKlkvI1sqJl8sJFgqL1c1HDUg////////////////////////LVIwH1slH2EkI1wpKFUuNlc8EygZ////////////////////DSkQMFgzJlopHlsgJWInJ10pK1YrCyUM////////////////////Ij8jLlswJ10pIVwkKl4tKlcuJ0ku////////////DCINMFMyI1QnJ18uIVkqJ1kzKk01////////////////////////////////////////aotUdalUdbRJcbU8crI6eaxHYoc6////////////////////////e6hTdLNBcLBCdbNOf7degK9pRWY3////////////////////KEAQiK5je7JNcbI+brM+dbJLfahgKUIY////////////////GzcHeaRcda5Pc7VLc7JKdbFPd61VLFAS////////////////////Rmc4d61PbrI7cLdBcLZEdLRGdKRQ////////////////////////WG4ugKhIb6g3dLNAerRQeq5bd51gCCMA////////////TGk5hLBbdKw/dLRCcrRIdq5TQHIp////////ES4AKkcRTm4rao8/dqNEg7hQca9CbLBDYqk/dLZVaphNID4M////////////////////////////////////////////////CyQG////////NFEh////DiUAcpBaACIAETwAR28w////Iz4TX3xQ////EzwCT3Y9////DCcAJUQY////QnMxLmAb////Sm0zN1Uf////RXApHUsC////XH1GGTMG////P2EuASMA////DiIA////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////LlEzKlkvI1wpJl8sJFgqL1c0HDUg////////////////////////NFc2IVsoIGQpI18rJFcsNVo7ESoX////////////////////AB4EMF02J10rIF0iI2MnH1okLVwyCCQL////////////////////EykSLVErKlooI1ojKmAuJFQqM1g3Bh8J////////GDAYMlYyJFUoJlwrJ1wwKVczJ0gz////////////////////////////////////////fKFcdqxMc7RAdbo9b683dadETHAo////////////////////JzsWe6xTd7ZEbrBEdLJNd7BScZ9XLkok////////////////////QFwie6dSc6xDd7hEcbRAd7FNfKVjECkC////////////////Lk8Yfa5dc7FOcLJIerlQda5PeqpeEjIB////////////////////ZIpRebFOcrY/bbZAb7VFbqxHYpBF////////////////////////bYZCfKhGfLVEe7pHc61He6xabpFX////////////////bpBUgK9ReLJBdLdDcLRHdLFSc6xbaJxUdKZheqhgeahXeKtMe7BGdK87fLlEb61AeLlTc7NVdatdQGMr////////////////////////////////////////////////////VmpPBB4AKUUcgp9zJz0ZNU4mgJ5oMVgZSnQyhKxuLk4fSmY+f551HEASP2YxdZpmLUoecYtkh6V/LlMnbJdfVIFGFDsGeZ5raIdbJ0kXeaJmbphZKk8ZfZ1uWXNOFzIPfZ5zRWQ4DCIAhpd3LTsk////NUEr////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////LlA3K1gxJVspJl8qJFgoLFkyGTYg////////////////////////M1U0J1stJmIuIlwqKF0vK1czEC4S////////////////////BSMJLVczKVotJlwoJF0mI10rK1szCiMN////////////////////////O1MzK1IlLF4rKV8tJlstK1UxID4i////////K0gqL1UwLVwwJlcqIlIqM1w8DisX////////////////////////////////////KkUSf6hZebFGdLQ6cbE3eLJEfqtWIUMH////////////////////RWAtfa5ScbA+cbNHdLZKdbFAf61V////////////////////////aJBHf7NOcq49c7Q+bLA5ebJRaI5V////////////////////Unk2cahLdrlSbLFEcLBCerJRc51d////////////////////////e6Jhea9Nc7NBcLVBc7ZMda9aOWcp////////////////////////fKFReK1DdLU/cLQ9ca9Ce61UU3E7////////////DSIAh6hjd6lIdrJAb7U7brdBZ6w/er5XbK5Ma61LdLdQb7NGb7NCa7A8e7tNdrNMe7BUfKtbdZ5YQmMu////////////////////////////////////////////////////////XGRX////IjgUf5hwHzgRV3JFgqRoQWkjUnw0e6RiJ04ZY4dbeZ1xOFwsTXM6fJ9lT207YH1PdpJpQF8zgqhvaY9WGjwKfp9yZoVcID8VeJpnUHM9NVclfJtxYH5aCykFgaJ3Olcr////gI50KjYg////HiYX////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////L084K1gxJVspJWAqI1knLFkyGTYg////////////////////////NVQ0KlkvKGAvIlwqJl4vKlgxDi8Q////////////////////BiILLlY0KlkvKVsqJ1woI10rK1s1CiMN////////////////////////IzUbM1YuKloqJmAtJV8tKlgxKk8u////////K00sLlcvK1ouKVgsKlcwL1Q1////////////////////////////////////////Q2AqfqlXebFEeLU+drJAea9NeqNdByQA////////////////////YoFGfa5SdLJFbK1FdbdNc68+bJhD////////////////////////cp5ReLFIdrJBdrVCc7Q+eK5QU3VD////////////////////a5VLebNPd7pQcbdHcbFBe65RV3pE////////////////////GTUFfaVcerBOc7JAb7JBb7BKdqxgH0oV////////////////////HTcKf6tWdrBCc7dAb7Q9cK4/e6tVNU8f////////////FywBf6NZfrJNdbZCcbc9ecBIdbtJb7NGb7BIba5Ic7ROb7BIebpScK5Jb6hJd6lUhKxlZYRJMEgY////////////////////////////////////////////////////////////////////////////////////RGMq////ACAAQGkn////BCkAIkYa////HUAITm82////FDIAO1go////LlATFjkA////M1MiKEcb////LE0WIEEI////N1coCSgA////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////L083K1gxI1wpJWAoI1knLFkyGjYf////////////////////////M1U0KlkvKGAvIlwsJl4xK1czEC4U////////////////////BiIMLlU2KlkvKVsqJlwoJFwrLFozCyMN////////////////////////////M1UwJ1gpI18sImAvJlsvMFw4BicKEDESLVMuL1kxKVgsK1owMFo2H0Em////////////////////////////////////////ZYNRe6dad7BJdbNEeLJLeqxXa4tZ////////////////////////fp9oe61Wd7NPaqpKeLVYdaxPTHMy////////////////////HDQafKpic6xLdrNLdbNGebRGeKlQM1Ak////////////////EigBfahZebNNcLRFdLhHdK9Dga5ZNlMl////////////////////PV4nfalae7RNc7JAcrJCdK1Od6ZiAycA////////////////////Q10tgq1bdq1FdrdDcrVCd69Kf6pbDicA////////////HjULeaJWfrhRc7dAcLNAbKw+da9IbaNDhrNegq1egKhhgaZhfqNehKlkcJNRTmwwJT0N////////////////////////////////////////////////////////////////////RUZA////////cYFk////OlAskKp6Lk0STnExfaFkIkMUHD8VMlIpCioAaotWiadxEi4AZoJPgqFmKEsHep9SfqNWCSwAgKNjeZhfCikAg6Ngd5hTKUkKiKdsZoRSAB4AeZhdR2It////iJF+Ljcm////GCAV////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////L1A1KlkvIl0nImEoIVonLFkyHDUf////////////////////////MlU1KVotKGAvIlwsJl0zK1c0EC4W////////////////////BiEOLlU2KVkvJ1sqJlwoJF0qLFsxCyML////////////////////////////JEYlKlowHlwrIGAuIVotLFw0GkMhIkonLVcxLlsyJ1YqLVoxMFUzCykP////////////////////////////////////////fZ5ld6ZVc7FGcLBAd7FNfalcT2o/////////////////////GjYOgalqeaxQeLJLbq5NdrJYe6xbKksW////////////////////Plo0gK5jc61JcrFGcrBDebBIeaRUFS0J////////////////NE4hgK1adbBGa647drZEdq1FgqtcGDAM////////////////////XYRFeqxTe7ZKcrM/drFFea5Ua5NX////////////////////////Yn9HgKtZdKtGdrZEdbVFea9Rc5pX////////////////GDMKfKdfdrNLcLVAdrVKdK5KgLNWYo45U3QtTGgtQlgqOUslJzkTFCQA////////////////////////////////////////////////////////////////////////////////ISIc////////NUEr////Hi4Ue5BnECsALksTaIZQBiUADiwGIkAa////RGIuUm85////QFwpXHs/Ci4AT3QmRGkbACEARWgoQ2InAyIASWkmL04KDy0Aa4hQQFwr////aIVLL0cV////Mjsq////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////L1AzKVkvH14nIWIoIFonLFkyHTUf////////////////////////MVY1J1stJ2EvIVwsJl0zK1c2ES0X////////////////////CCAQLVY4KVkxJlwqJF0oJF0qLVoxDCIL////////////////////////////CykNK1kyHVssH2ExIl0vIlkvJVgvKloyKFguKlowKFctMVs1JUgo////////////////////////////////////////HDMFhK1hdatJcrY/bLA3eLNJgaxcLkMa////////////////////OVwie6tXda5Dc7I9drZIdLBQeadcDCkA////////////////////XH5Ce65SdrVDb7M6cbI8eqxJdZlT////////////////////VnJBgrBYeLNFcLE7drVCeKtGd5tT////////////////////////cJ5VdK1MdLdDcLQ9dq9GfaxbUnA+////////////////////////eJtZf6xZdKxJd7VGdLJHd6lUVXg+////////////////DCgAfaxmba9FcbdDb61KeK5We6tVPGIX////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////GDID////DSYGPlgz////GTUCOlUg////GjQFME4a////K04KGz4A////K0kVGjYG////I0AIFzIA////IToQBx8A////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////L1AxJ1stHl8nHmQoH1snLFkyHjQf////////////////////////L1c1JlstJmIuIVwsJl0zK1c2ESwZ////////////////////BiEQLVY4J1ovJF0qJF0mJF0oLVovDCMJ////////////////////////////////IUooIFsvJGM0JmEzIVotJl8yJ18yJFktJlkuKlcwM1g2FjQY////////////////////////////////////////OlcdgK1YdbBGc7s+bLQ3e7VOfKZaEiMA////////////////////U343eLBPdLA+crI4eLhIda5QaJJS////////////////////////cphPda1IdrpDcLY5dLU/fKtPXnxA////////////////////bo1Se6xQe7dFc7U6eLVAfKxJYIA9////////////////////GzsJdqpYb7FHcLc9cbU8d7BJfqhcMEkf////////////////////GjYAfahYeqxTdrBMd7VGd7FKeahaM1Mi////////////////////cJ1cbq9JdLpId7hUdrFTfbNVYo43Dy4A////////////////////////////////////////////////////////////////////////////////////////////////////////////////W1xU////JCogkp6G////X3RLh550HzQTY3tbepJwESwBcYxZgZxnECoAfpdtgZ5yGjoJdJVci6xzAR4AdZFohZ98////gptxc4pgDyUBip2BQlNA////gZF3ER8G////QEg9////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////LlExJlsvG2ApHWQqHVsoK1kyHjQf////////////////////////LFg1JlwrJmIsIV0qJl0zK1c2EC0Z////////////////////BSIQK1c2JlstI14oI14kJF0oLVovCyMJ////////////////////////////////DC4TI1cxJmE1J18yJl4vI14uIV8wIlsuJVgvKlYyLlEx////////////////////////////////////////////XoFBd6xUcrRIcLpBbbZAdrJSaZBP////////////////////Ah8AaJtOd7VQcrJAc7Q+dLNId65aSnJA////////////////////GDAAfahYdLBMcLRDcrlBc7NDf6lXOFMe////////////////EigBe6BddKpId7ZBc7U5eLVAfaxMQF4i////////////////////OmAldK9TcLZEa7g4cbc9e7FPep9ZEycE////////////////////NloSeaxNdK1MdbJLdbRCd69KfKdgFTEJ////////////////////TnU+d69UcrZHbLJCaK48crM7hL1KYZEtJEsAAB8A////////////CygAHzsAQ14pTGI8////////////////////////////////////////////////////////////////////////////////////////Iisa////GCkFS186////DCMGOlIu////Ij4LNFEb////J0IZLkkm////J0QYGTYK////LEUoJjwn////MEQpFykP////NEI1////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////LlExJlsvG2AqG2QtHVsqK1k0HjQf////////////////////////LFg3JlwrJmIsIV0qJl0zKlc2EC0X////////////////////AyMOKlg0JFwtI14mI14kJF0oLVoxCyMJ////////////////////////////////////JlIxKFsyJ1ctKl4wH10sHWAtJF8xJlkwK1MxK0kv////////////////////////////////////////////d59ZdKtQcbRKbLdAbLREc6tSVHo/////////////////////EDABeK9edLdQc7NDdrZEbq1Eeq9fM1gv////////////////////K0gOgbJZd7VQaa5BcrlDca5GgqhdHDMF////////////////KEQUg6tid61LebhFdLQ6ebNCfalQJUIK////////////////////UHg5c7BRcbpFarY4cbY/erBScpVV////////////////////////T3smebRKc7JKdrVNca9Ac6lHeKBh////////////////////////ME8jfa5fb6tHbbJHd8BLbLI1a6wug75Kc6dDdKNVb5xZbZpZeKNcgq9ad6NOiK5jaYdR////////////////////////////////////////////////////////////////////////////////////////////////////MEQh////LEMmR187////PlslRmMt////Ey8GIz4d////OlYtPFcu////QFc9PlNC////Kz0nGCcQ////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////MVY1J1wyIWMzGV8tI14yKFU0HzQh////////////////////////K1Y4J1stJl8sIV0qKGAzLFo2DS0V////////////////////AiINKFYyJFwtH1okJWAoJlwqK1UtCyII////////////////////////////////////GDkcLVczJk8nLFsvHlwpH2QuH1osKFgyLVIzDicR////////////////////////////////////////GDQEgq5jb6hKaq1Dc7xJbrNIea9bLVAY////////////////////L1IcfbdibbBHbq5Ad7VIdbJLf7RkBysF////////////////////WHg3f7JVca9KbbJFcLZEda9Ld5xZ////////////////////RWMvfqhcc6pMdbNGdbRBe7NIeKNR////////////////////////cJpad7RVabFBcLpBcbRDf7FaTm0y////////////////////////cKFFdLRCbrBGcLFJc7FGeK1RW35E////////////////////////////bpJUfK1bc69VbrJNb7RAcrY9c7Q+crFGcq5Uc7FebatYdrJYebNPdatJfqtYao5Q////////////////////////////////////////////////////////////////////////////////////////OkI3////OEcoiZx8////a4NjgJlyNVEhiaZufJtiGTYGMk8jOlYuGDUJiKRzfpppByAAhpt6g5d+////h5d6Y3JT////YGhZGiAc////Iykf////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////L1I0JlUxH1wyGVgtJFs0Llc5Gi8e////////////////////////K1M4KVguJloqHVUmI1guJ1MwDC0S////////////////////AiINKlY1KVwxI1knJ1sqKVotL1c0EScQ////////////////////////////////////////JkkoNlgzLVgrH14pHmEsI1wvKVczJ0cv////////////////////////////////////////////MEwZgKxderFUdrVNcbVIcrBLc6RTEzQA////////////////////VHc9fLRda6lEdrBJd7FKcqtKZpZKACAA////////////////////ao5IeaxNdrJOdrdPc7JHdKlNYH9E////////////////////ZoROf6lfe61Yc6pMdq9IfK9QZIpB////////////////////GTILfadodrBZcbRNcbVGc7BJga1gNlIh////////////////////FCoAbqFEdbVFdLJNe7lYdq9OeqlYNFQi////////////////////////////DykAfKBieq1icalScK1FerlHerpIb7FDa61LbrFUbbNRbbFKcbFBeLFGiLRbdplX////////////////////////////////////////////////////////////////////////////////////////////////////////////////Jj8Y////JkMNQV8p////////CSQA////ECsAITwH////HTEMHTAS////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////MU83MFk7LV88JVk1LFk6MlQ8IzUn////////////////////Ax4PNVc/Mlc1MVszLFozL104K1QyDy0T////////////////////BiMPLFM2LFg0KFUqJ1QpKlQwLk8yCx4K////////////////////////////////////////EjUUM1MuK1QoIVsoIF4rJlsxMFc4FS4Z////////////////////////////////////////////X3xGe6ZXdqhPdq5Nc6xLgLVZb5lP////////////////////////d5ldfq9ddq1PeaxPfbBTfrFVTnov////////////////////GCcIiq5md6hMcahKc6xLd6pLhbBgOVMk////////////////////h6RufqNef6ple6lghLRggq1eQGIm////////////////////PFQwgqdzdKNbea9bd65Tfa9ad5tbHDUL////////////////////Nksgga5bebNPbaVKea9beKlYfqdjESwB////////////////////////////////L0Yaf6Flha1kgq5Vc6VCcaY+erNKebVRba5KdLdOb7FDdrJAfrJGhaxTdpBPFCIA////////////////////////////////////////////////////////////////////////////////////////////////EyMI////JDgdRV05////R2MwUW08////UmxHZn9VByEATmcwVm03////L0AcPEwv////NEIf////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////LEMxL0w4LVM6J000Kks2LUg3Gysh////////////////////////KkMwMUsyL04vK1AxLFM0KUswEisV////////////////////CiATK0s0L1E2M1U0NVc2MlM4NE04Eh8O////////////////////////////////////////DzQSLFAqK1QoIVonIVspLFozLlA1////////////////////////////////////////////////c41eeJxce6Ncf6lfeqRaeaBdVXI8////////////////////FCUFfJhnd55dfadfeaFbdp5YfKRdPmIl////////////////////KjgfhqVqe6VZeadce6dceqBXg6BmHzIS////////////////GigPiaFvgp9le51qe6BsfKNkc5VZLkgb////////////////////UmZKhKB4gaNwgKhpeKBaep5edJBf////////////////////////XG1JgqVjeKRVcqBXgKpoeaFjcpJh////////////////////////////////////////////QVwleplWhaddfqVUgKtZgbBfea1YeK9Ud61Pg7NQgKhPaohAVGY0////////////////////////////////////////////////////////////////////////////////////////////////////MD4n////R1Y/d4pq////U20+iaN2DCQCY3pdgZdz////eY9gfI9h////Tl0+e4lw////OkUnHCcJ////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////HUsmLVoxKFcpJ10rIFQkN2E9GjYf////////////////////////////////////////////////////////////////////////////////////////////////////Dx4H////////Bh4A////CSMA////////////////////////////////////////////////////////////////////////////Dx4A////////////////ByAA////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////KjoVRFYuTGM3TGg4S2k3RmcuNlgcKEYKESkA////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////Dz4cJl01JFktJloqLWEwIVAiNVg3////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////OkMw////OEcmX25R////UFxQXGha////QU82V2VM////////OkA8////HSMZ////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////Cx4LJj0pHjUhESMTDSANES4YG0QmJV02ImE0F1UmJF4sKl4tKFMoFjUW////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////JSkb////LjgfQ0w3////IigkN0E5////NUEtMj4q////GB8Y////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////ECkTNlc8K04wM1Q3L1I0Mls7JlgzHFktH2ExIF4tJFwtKVIqN1Y3////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////Gz4eJVMuKF0xHlYnIVsrI14uIl4rIV0qI10qJVsqK1UtOE45FR8W////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////MjQv////ISMiLjAt////////HB8Y////////IyUk////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////JUopKVkxH1cqJWEuImEsHlwpHlonJFopK1wtLVovMlQzHioe////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////M1E5OF9ALFo1JlkwJlkuJlkwKloyLVozLFQvIUYkGTUc////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////IDUkJkQsJkgtL1Q1LlU2L1Y3L1Q1KEovHDogBSEK////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////BR4J////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////"  # Pre-decoded raw RGB for PDF (white background)
LOGO_W, LOGO_H = 250, 100
LOGO_B64 = "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"
LOGO_IMG = '<img src="data:image/png;base64,' + HACCP_LOGO_B64 + '" alt="MyMine" style="height:32px;width:auto;display:block">'

# ─── CSS + shared pieces ───────────────────────────────────────────
COMMON_CSS = """
:root{
  --bg:#F0F6F3;--bg2:#FFFFFF;--bg3:#E9F4EF;--bg4:#DAF0E6;
  --line:#CEEADB;--line2:#AEDCC8;
  --green:#1DB584;--green2:#0F9A6E;
  --text:#1A3D30;--sub:#4E7367;--dim:#8DBDAF;
  --red:#D94F4F;--blue:#2878B0;--amber:#D4891A;--purple:#6B4FA0;
  --shadow:0 1px 8px rgba(26,61,48,.07);
  --shadow-md:0 4px 20px rgba(26,61,48,.10);
  --mono:'JetBrains Mono',monospace;--sans:'Outfit',sans-serif;
}

.company-footer{background:var(--bg2);border-top:1px solid var(--line);margin-top:56px;padding:20px 28px}
.cf-inner{max-width:1300px;margin:0 auto;display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.cf-text{font-family:var(--mono);font-size:10px;color:var(--dim);line-height:1.8}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
.company-footer{background:var(--bg2);border-top:1px solid var(--line);margin-top:0;padding:20px 28px}
.cf-inner{max-width:1300px;margin:0 auto;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.cf-text{font-family:var(--mono);font-size:10px;color:var(--dim);line-height:1.8}

body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh}
"""

# ─── LOGIN PAGE ─────────────────────────────────────────────────────────────────
HTML_LOGIN = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>MyMine HACCP &middot; Accesso</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script defer src="https://analytics.mymine.cloud/script.js" data-website-id="b3681a33-bfca-4678-b997-9620faec9961"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:Outfit,sans-serif;background:linear-gradient(145deg,#0F2D22 0%,#1A3D30 40%,#0E5C3A 100%);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#fff;border-radius:20px;padding:40px 36px 32px;width:100%;max-width:380px;box-shadow:0 24px 60px rgba(0,0,0,.35)}
.logo-wrap{text-align:center;margin-bottom:24px}
.logo-wrap img{height:64px;width:auto}
.title{font-size:18px;font-weight:700;color:#1A3D30;text-align:center;margin-bottom:4px}
.sub{font-family:JetBrains Mono,monospace;font-size:10px;color:#8DBDAF;text-align:center;letter-spacing:.1em;text-transform:uppercase;margin-bottom:28px}
.field{margin-bottom:16px}
.field label{display:block;font-family:JetBrains Mono,monospace;font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:#4E7367;margin-bottom:5px}
.field input{width:100%;background:#F0F6F3;border:1.5px solid #CEEADB;color:#1A3D30;border-radius:10px;padding:11px 14px;font-family:Outfit,sans-serif;font-size:14px;font-weight:500;outline:none;transition:all .2s}
.field input:focus{border-color:#1DB584;background:#fff;box-shadow:0 0 0 3px rgba(29,181,132,.12)}
.btn-login{width:100%;background:linear-gradient(135deg,#1DB584,#0F9A6E);color:#fff;border:none;border-radius:10px;padding:13px;font-family:Outfit,sans-serif;font-size:14px;font-weight:700;cursor:pointer;transition:all .2s;margin-top:4px;box-shadow:0 4px 14px rgba(29,181,132,.3)}
.btn-login:hover{filter:brightness(1.07);transform:translateY(-1px)}
.btn-login:disabled{opacity:.6;cursor:wait;transform:none}
.err{background:#FEF2F2;border:1px solid rgba(217,79,79,.3);border-radius:8px;padding:9px 12px;font-size:12px;color:#D94F4F;margin-top:12px;display:none}
.forgot{text-align:center;margin-top:16px}
.forgot a{font-size:12px;color:#8DBDAF;text-decoration:none;cursor:pointer;font-family:JetBrains Mono,monospace;transition:color .2s}
.forgot a:hover{color:#1DB584}
.reset-box{display:none;margin-top:20px;padding-top:20px;border-top:1px solid #CEEADB}
.sub2{font-size:12px;color:#4E7367;text-align:center;margin-bottom:14px;line-height:1.5}
.btn-reset{width:100%;background:#F0F6F3;border:1.5px solid #CEEADB;color:#0F9A6E;border-radius:10px;padding:11px;font-family:Outfit,sans-serif;font-size:13px;font-weight:600;cursor:pointer;transition:all .2s}
.btn-reset:hover{border-color:#1DB584;background:#fff}
.ok-msg{background:#F0FBF6;border:1px solid #CEEADB;border-radius:8px;padding:9px 12px;font-size:12px;color:#0F9A6E;margin-top:10px;display:none;text-align:center}
footer{margin-top:24px;text-align:center;font-family:JetBrains Mono,monospace;font-size:9px;color:rgba(255,255,255,.35);letter-spacing:.06em}
</style></head><body>
<div>
<div class="card">
  <div class="logo-wrap"><img src="data:image/jpeg;base64,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" alt="MyMine HACCP"></div>
  <div class="title">Area Riservata</div>
  <div class="sub">MyMine HACCP &mdash; Monitoraggio IoT</div>
  <div id="loginForm">
    <div class="field"><label>Email / Username</label><input id="usr" type="email" autocomplete="username" placeholder="nome@azienda.it"></div>
    <div class="field"><label>Password</label><input id="pwd" type="password" autocomplete="current-password" placeholder="Password"></div>
    <button class="btn-login" id="btnL" onclick="doLogin()">Accedi</button>
    <div class="err" id="errMsg"></div>
  </div>
  <div class="forgot"><a onclick="toggleReset()">Password dimenticata?</a></div>
  <div class="reset-box" id="resetBox">
    <div class="sub2">Inserisci la tua email. Ti invieremo una nuova password.</div>
    <div class="field"><label>Email</label><input id="resetEmail" type="email" placeholder="nome@azienda.it"></div>
    <button class="btn-reset" onclick="doReset()">Invia nuova password</button>
    <div class="ok-msg" id="resetOk">Controlla la tua email.</div>
  </div>
</div>
<footer>MyMine Srl &middot; P.IVA IT12038850967 &middot; Milano</footer>
</div>
<script>
document.getElementById('pwd').addEventListener('keydown',function(e){if(e.key==='Enter')doLogin();});
document.getElementById('usr').addEventListener('keydown',function(e){if(e.key==='Enter')document.getElementById('pwd').focus();});
async function doLogin(){
  var u=document.getElementById('usr').value.trim();
  var p=document.getElementById('pwd').value;
  var btn=document.getElementById('btnL');
  var err=document.getElementById('errMsg');
  if(!u||!p){err.textContent='Inserisci email e password.';err.style.display='block';return;}
  btn.disabled=true;btn.textContent='Accesso...';err.style.display='none';
  try{
    var r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
    var j=await r.json();
    if(j.ok){window.location.href=j.redirect||'/';}
    else{err.textContent=j.error||'Credenziali non valide.';err.style.display='block';}
  }catch(e){err.textContent='Errore di rete.';err.style.display='block';}
  btn.disabled=false;btn.textContent='Accedi';
}
function toggleReset(){var b=document.getElementById('resetBox');b.style.display=b.style.display==='block'?'none':'block';}
async function doReset(){
  var email=document.getElementById('resetEmail').value.trim();
  if(!email)return;
  var ok=document.getElementById('resetOk');
  try{await fetch('/api/forgot_password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email})});ok.style.display='block';}
  catch(e){ok.textContent='Errore.';ok.style.display='block';}
}
</script>
</body></html>"""

# ─── CLIENTS PAGE ─────────────────────────────────────────────────
HTML_CLIENTS = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>MyMine · Clienti</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
__COMMONCSS__
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:radial-gradient(ellipse 800px 500px at 95% -5%,rgba(29,181,132,.07) 0%,transparent 55%),
             radial-gradient(ellipse 600px 400px at 5% 105%,rgba(29,181,132,.05) 0%,transparent 55%)}
.wrap{position:relative;z-index:1;max-width:1100px;margin:0 auto;padding:32px 28px 80px}
nav{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:14px;
    margin-bottom:40px;padding-bottom:20px;border-bottom:1px solid var(--line)}
.navcap{font-family:var(--mono);font-size:12px;color:var(--dim);letter-spacing:.12em;text-transform:uppercase}
.grid{display:grid;grid-template-columns:1fr 1.1fr;gap:22px;align-items:start}
@media(max-width:740px){.grid{grid-template-columns:1fr}}
.panel{background:var(--bg2);border:1px solid var(--line);border-radius:16px;padding:26px;box-shadow:var(--shadow)}
.panel-bar{height:3px;background:linear-gradient(90deg,var(--green),var(--green2));border-radius:3px;margin-bottom:18px}
.panel-title{font-size:17px;font-weight:700;margin-bottom:4px}
.panel-sub{font-family:var(--mono);font-size:11px;color:var(--dim);letter-spacing:.12em;text-transform:uppercase;margin-bottom:20px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.field{margin-bottom:14px}
.field label{display:block;font-family:var(--mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--sub);margin-bottom:5px}
.field input,.field select{width:100%;background:var(--bg3);border:1px solid var(--line);color:var(--text);border-radius:9px;padding:10px 12px;font-family:var(--sans);font-size:13px;font-weight:500;outline:none;transition:all .2s;appearance:none}
.field input::placeholder{color:var(--dim)}
.field input:focus,.field select:focus{border-color:var(--green);background:#fff;box-shadow:0 0 0 3px rgba(29,181,132,.11)}
.field select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='11' height='6'%3E%3Cpath d='M0 0l5.5 6 5.5-6z' fill='%234E7367'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 11px center}
.divider{height:1px;background:var(--line);margin:12px 0}
.btn-submit{width:100%;background:linear-gradient(135deg,var(--green),var(--green2));color:#fff;border:none;border-radius:9px;padding:12px;font-family:var(--sans);font-size:13px;font-weight:700;cursor:pointer;transition:all .2s;margin-top:6px;box-shadow:0 3px 12px rgba(29,181,132,.28)}
.btn-submit:hover{filter:brightness(1.06);box-shadow:0 5px 18px rgba(29,181,132,.38);transform:translateY(-1px)}
.clist{display:flex;flex-direction:column;gap:10px}
.ccard{background:var(--bg3);border:1px solid var(--line);border-radius:12px;padding:14px 16px;transition:all .2s;cursor:pointer;position:relative;overflow:hidden}
.ccard::after{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--green);border-radius:0;opacity:0;transition:opacity .18s}
.ccard:hover{border-color:var(--green);background:#fff;box-shadow:var(--shadow-md);transform:translateX(3px)}
.ccard:hover::after{opacity:1}
.ccard-name{font-size:15px;font-weight:700;margin-bottom:6px}
.ccard-details{font-family:var(--mono);font-size:12px;color:var(--sub);line-height:2}
.ccard-eui{color:var(--green);font-weight:500}
.ccard-actions{display:flex;gap:7px;margin-top:10px;flex-wrap:wrap}
.btn-creds{background:none;border:1px solid rgba(29,181,132,.2);color:var(--sub);border-radius:6px;padding:4px 10px;font-size:10px;cursor:pointer;font-family:var(--mono);transition:all .2s}
.btn-creds:hover{border-color:var(--green);color:var(--green2)}
.btn-edit{background:none;border:1px solid rgba(29,181,132,.25);color:var(--green2);border-radius:6px;padding:4px 10px;font-size:10px;cursor:pointer;font-family:var(--mono);transition:all .2s}
.btn-edit:hover{background:rgba(29,181,132,.08);border-color:var(--green)}
.btn-del{background:none;border:1px solid rgba(217,79,79,.25);color:var(--red);border-radius:6px;padding:4px 10px;font-size:10px;cursor:pointer;font-family:var(--mono);transition:all .2s}
.btn-del:hover{background:rgba(217,79,79,.08);border-color:var(--red)}
.empty{font-family:var(--mono);font-size:11px;color:var(--dim);text-align:center;padding:36px 0}
.flash{background:#E5F6EE;border:1px solid var(--green);border-radius:9px;padding:9px 14px;font-family:var(--mono);font-size:11px;color:var(--green2);margin-bottom:14px;display:none;font-weight:500}
.row4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:9px}
.sec{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--green2);font-weight:600;margin:14px 0 10px;display:flex;align-items:center;gap:8px}
.sec::after{content:'';flex:1;height:1px;background:var(--line)}
.notif-box{background:var(--bg3);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-top:4px}
.notif-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.notif-row:last-child{margin-bottom:0}
.toggle{position:relative;width:36px;height:20px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#CEEADB;border-radius:20px;cursor:pointer;transition:.2s}
.slider::before{content:'';position:absolute;width:14px;height:14px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s;box-shadow:0 1px 4px rgba(0,0,0,.15)}
input:checked+.slider{background:var(--green)}
input:checked+.slider::before{transform:translateX(16px)}
.tlabel{font-family:var(--mono);font-size:10px;color:var(--sub)}
.ccard.alarm{border-color:#D94F4F!important;background:#FEF5F5!important}
.ccard.alarm::after{background:#D94F4F!important;opacity:1!important}
.alarm-badge{display:inline-flex;align-items:center;gap:4px;background:#D94F4F;color:#fff;border-radius:6px;padding:2px 8px;font-family:var(--mono);font-size:9px;font-weight:600;margin-left:8px;animation:pulse .8s ease infinite;vertical-align:middle}
.ccard-ranges{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.crange{font-family:var(--mono);font-size:9px;background:var(--bg4,#DAF0E6);border:1px solid var(--line);border-radius:6px;padding:2px 7px;color:var(--sub)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.btn-check{width:100%;background:var(--bg3);border:1px solid var(--line);color:var(--green2);border-radius:9px;padding:10px;font-family:var(--sans);font-size:13px;font-weight:600;cursor:pointer;transition:all .2s;margin-bottom:14px}
.btn-check:hover{border-color:var(--green);background:#fff;box-shadow:0 2px 8px rgba(29,181,132,.12)}
.btn-check:disabled{opacity:.5;cursor:wait}
.btn-upload-sensor{display:inline-flex;align-items:center;background:var(--bg3);border:1px solid var(--line2);color:var(--green2);border-radius:7px;padding:5px 10px;font-family:var(--mono);font-size:9px;font-weight:600;cursor:pointer;transition:all .2s;letter-spacing:.04em}
.btn-upload-sensor:hover{border-color:var(--green);background:#fff}
.sensore-row{display:flex;gap:8px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:9px;padding:8px 10px}
.sensore-row .inp-eui,.sensore-row .inp-frigo{flex:1;background:#F0F6F3;border:1px solid var(--line);border-radius:7px;padding:7px 10px;font-family:var(--mono);font-size:11px;color:var(--text);outline:none}
.sensore-row .inp-frigo{font-family:var(--sans);font-size:12px}
.sensore-row .inp-eui:focus,.sensore-row .inp-frigo:focus{border-color:var(--green);background:#fff}
.sensore-row .btn-rm{background:none;border:none;color:var(--red);cursor:pointer;font-size:16px;padding:0 4px;opacity:.7;flex-shrink:0}
</style>
</head>
<body>
<div class="wrap">
<nav>
  <a href="/" style="text-decoration:none">LOGO_PLACEHOLDER</a>
  <div style="display:flex;align-items:center;gap:12px">
    <div class="navcap">Gestione Clienti &amp; Sensori IoT</div>
    <a href="/logout" style="font-family:var(--mono);font-size:10px;color:var(--sub);text-decoration:none;border:1px solid var(--line);border-radius:8px;padding:4px 12px;transition:all .2s;white-space:nowrap" onmouseover="this.style.color='#D94F4F';this.style.borderColor='rgba(217,79,79,.4)'" onmouseout="this.style.color='';this.style.borderColor=''">&#10148; Esci</a>
  </div>
</nav>
<div class="flash" id="flash"></div>
<div id="statusBar" style="border-radius:10px;padding:11px 16px;margin-bottom:14px;font-family:var(--mono);font-size:11px;font-weight:600;display:flex;align-items:center;gap:12px;background:#FFF3CD;border:2px solid #F59E0B;color:#92400E">
  <span style="font-size:16px">⏳</span>
  <span id="statusMsg">Verifica connessione server...</span>
  <button onclick="runDiag()" style="margin-left:auto;background:#F59E0B;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-size:10px;font-family:var(--mono);cursor:pointer;font-weight:700">🔄 TESTA ORA</button>
</div>
<div class="grid">
  <div class="panel">
    <div class="panel-bar"></div>
    <div class="panel-title">Nuovo Cliente</div>
    <div class="panel-sub">Dati di registrazione</div>
    <div class="row2">
      <div class="field"><label>Nome</label><input id="fNome" placeholder="Mario"></div>
      <div class="field"><label>Cognome</label><input id="fCognome" placeholder="Rossi"></div>
    </div>
    <div class="field"><label>Partita IVA</label><input id="fPiva" placeholder="IT01234567890"></div>
    <div class="field"><label>Email</label><input id="fEmail" type="email" placeholder="mario.rossi@azienda.it"></div>
    <div class="field"><label>Telefono</label><input id="fTel" type="tel" placeholder="+39 333 1234567"></div>
    <div class="divider"></div>
    <div class="field"><label>Indirizzo installazione</label><input id="fAddr" placeholder="Via Roma, 1 – Milano"></div>
    <div class="sec">&#10052; Frigoriferi / Sensori</div>
    <div id="sensoriList" style="display:flex;flex-direction:column;gap:8px;margin-bottom:10px"></div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px">
      <button type="button" onclick="addSensoreRow()" style="background:var(--bg3);border:1px solid var(--line2);color:var(--green2);border-radius:8px;padding:7px 14px;font-family:var(--mono);font-size:10px;cursor:pointer;font-weight:600">+ Aggiungi frigorifero</button>
      <label style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;background:var(--bg3);border:1px solid var(--line2);color:var(--sub);border-radius:8px;padding:7px 12px;font-family:var(--mono);font-size:10px;font-weight:600">
        &#128194; Aggiorna lista sensori
        <input type="file" id="sF" accept=".txt" style="display:none">
      </label>
      <span id="sensorFileLabel" style="font-family:var(--mono);font-size:10px;color:var(--dim)"></span>
    </div>
    <div class="sec" style="font-size:10px;color:var(--dim);margin-top:0">Le soglie T°/Umid. si configurano per ogni frigorifero qui sopra</div>
    <div class="sec">🔔 Notifiche allarme</div>
    <div class="notif-box">
      <div class="notif-row">
        <label class="toggle"><input type="checkbox" id="fNotifEmail" checked><span class="slider"></span></label>
        <span class="tlabel">Email al cliente</span>
      </div>
      <div class="notif-row">
        <label class="toggle"><input type="checkbox" id="fNotifSms"><span class="slider"></span></label>
        <span class="tlabel">SMS (Twilio)</span>
      </div>
    </div>
    <button class="btn-submit" onclick="addClient()">➕ Aggiungi cliente</button>
  </div>
  <div class="panel">
    <div class="panel-bar"></div>
    <div class="panel-title">Clienti registrati</div>
    <div class="panel-sub">Clicca per aprire la dashboard</div>
    <div style="display:flex;gap:7px;margin-bottom:10px;flex-wrap:wrap">
      <a class="btn" href="/api/export" download="mymine_clienti_backup.json" style="font-size:11px;padding:6px 11px">⬇ Esporta clienti</a>
      <label class="btn" style="cursor:pointer;font-size:11px;padding:6px 11px">⬆ Importa clienti<input type="file" accept=".json" id="importFile" style="display:none" onchange="importClienti(this)"></label>
    </div>
    <button class="btn-check" onclick="checkNow()" id="btnCheck">🔍 Controlla allarmi ora</button>
    <button class="btn-check" onclick="testNotify()" id="btnTest" style="background:#EEF2FF;border-color:#818CF8;color:#3730A3">📨 Testa email / SMS</button>
    <div class="clist" id="clist"><div class="empty">Nessun cliente registrato.</div></div>
  </div>
</div>
</div>
<script>
// ─── DIAGNOSTICA SERVER ──────────────────────────────────────────
async function runDiag(){
  const bar=document.getElementById('statusBar');
  const msg=document.getElementById('statusMsg');
  bar.style.background='#FFF3CD'; bar.style.borderColor='#F59E0B'; bar.style.color='#92400E';
  msg.textContent='Test connessione...';
  try{
    const r=await fetch('/api/status');
    if(r.status===401){
      // Session expired — redirect to login
      window.location.href='/login'; return;
    }
    const status=await r.json();
    if(!status.ok){
      bar.style.background='#FEF2F2'; bar.style.borderColor='#D94F4F'; bar.style.color='#B91C1C';
      msg.innerHTML='Errore server: '+JSON.stringify(status);
      return;
    }
    if(!status.writable){
      bar.style.background='#FEF2F2'; bar.style.borderColor='#D94F4F'; bar.style.color='#B91C1C';
      msg.innerHTML='Errore permessi scrittura — '+status.data_file;
      return;
    }
    bar.style.background='#F0FBF6'; bar.style.borderColor='#1DB584'; bar.style.color='#0F5132';
    msg.innerHTML='Server OK — Build: '+status.build+' — Clienti: '+status.clients;
  }catch(e){
    bar.style.background='#FEF2F2'; bar.style.borderColor='#D94F4F'; bar.style.color='#B91C1C';
    msg.innerHTML='Server non raggiungibile: '+e.message;
  }
}
runDiag();

// ─── SENSORI HELPERS ─────────────────────────────────────────────
let _sensoriDb=[];

function addSensoreRow(nome_frigo,eui_val,tmin,tmax,hmin,hmax){
  nome_frigo=nome_frigo||''; eui_val=eui_val||'';
  const list=document.getElementById('sensoriList');
  const dlId='dl'+Date.now()+Math.random().toString(36).slice(2);
  const opts=_sensoriDb.map(s=>'<option value="'+s.eui+'">'+s.eui+' — '+s.desc+'</option>').join('');
  const row=document.createElement('div');
  row.className='sensore-row';
  row.innerHTML=
    '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
    +'<input list="'+dlId+'" placeholder="EUI sensore" value="'+eui_val+'" class="inp-eui" style="min-width:200px">'
    +'<datalist id="'+dlId+'">'+opts+'</datalist>'
    +'<input placeholder="Nome frigorifero" value="'+nome_frigo+'" class="inp-frigo" style="min-width:140px">'
    +'<button class="btn-rm" type="button" onclick="this.parentNode.parentNode.remove()" title="Rimuovi">✕</button>'
    +'</div>'
    +'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px">'
    +'<input type="number" step="0.5" placeholder="T° min" value="'+(tmin!=null?tmin:'')
    +'" class="inp-tmin" style="width:80px" title="Temperatura minima (°C)">'
    +'<input type="number" step="0.5" placeholder="T° max" value="'+(tmax!=null?tmax:'')
    +'" class="inp-tmax" style="width:80px" title="Temperatura massima (°C)">'
    +'<input type="number" step="1" placeholder="H% min" value="'+(hmin!=null?hmin:'')
    +'" class="inp-hmin" style="width:80px" title="Umidità minima (%)">'
    +'<input type="number" step="1" placeholder="H% max" value="'+(hmax!=null?hmax:'')
    +'" class="inp-hmax" style="width:80px" title="Umidità massima (%)">'
    +'<span style="font-family:var(--mono);font-size:9px;color:var(--dim);align-self:center">T°C / Umid.%</span>'
    +'</div>';
  list.appendChild(row);
}

function getSensori(){
  return Array.from(document.querySelectorAll('#sensoriList .sensore-row')).map(r=>{
    const v=id=>r.querySelector('.'+id)?.value.trim();
    const n=s=>s===''||s==null?null:parseFloat(s);
    return{
      eui:v('inp-eui').toUpperCase(),
      nome_frigo:v('inp-frigo'),
      t_min:n(v('inp-tmin')), t_max:n(v('inp-tmax')),
      h_min:n(v('inp-hmin')), h_max:n(v('inp-hmax'))
    };
  }).filter(s=>s.eui.length>=8);
}

// ─── ADD CLIENT ─────────────────────────────────────────────────
async function addClient(){
  const nome=document.getElementById('fNome').value.trim();
  const cognome=document.getElementById('fCognome').value.trim();
  const sensori=getSensori();
  if(!nome||!cognome){alert('Inserisci nome e cognome');return;}
  if(sensori.length===0){alert('Aggiungi almeno un frigorifero con il suo codice EUI sensore');return;}
  const g=id=>document.getElementById(id).value.trim();
  const payload={
    nome,cognome,piva:g('fPiva'),email:g('fEmail'),telefono:g('fTel'),
    indirizzo:g('fAddr'),
    eui:sensori[0].eui,
    sensori:sensori,
    notif_email:document.getElementById('fNotifEmail').checked,
    notif_sms:document.getElementById('fNotifSms').checked,
  };
  let resp,result;
  try{
    resp=await fetch('/api/clients',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)
    });
    result=await resp.json();
  }catch(e){
    alert('Errore di rete: '+e.message+' — Verifica che il server sia in esecuzione.');
    return;
  }
  if(!result.ok){
    alert('Errore salvataggio (HTTP '+resp.status+'): '+result.error);
    runDiag();
    return;
  }
  // Reset form
  ['fNome','fCognome','fPiva','fEmail','fTel','fAddr']
    .forEach(id=>{const el=document.getElementById(id);if(el)el.value='';});
  document.getElementById('sensoriList').innerHTML='';
  addSensoreRow();
  document.getElementById('fNotifEmail').checked=true;
  document.getElementById('fNotifSms').checked=false;
  const nl=String.fromCharCode(10);
  fl('Cliente salvato! Credenziali: '+result.username+' / '+result.password);
  if(result.password){
    alert('Cliente salvato!'+nl+'USERNAME: '+result.username+nl+'PASSWORD: '+result.password+nl+nl+'Invia queste credenziali al cliente via email.');
  }
  runDiag();
  loadClients();
}

// ─── LOAD CLIENTS LIST ──────────────────────────────────────────
async function loadClients(){
  try{
    const cls=await(await fetch('/api/clients')).json();
    let als=[];
    try{als=await(await fetch('/api/alerts')).json();}catch(e){}
    const alarmSet=new Set(als.map(a=>a.eui));
    const box=document.getElementById('clist');
    if(!cls.length){box.innerHTML='<div class="empty">Nessun cliente registrato.</div>';return;}
    box.innerHTML=cls.map((c,i)=>{
      const euids=(c.sensori||[{eui:c.eui||''}]).map(s=>s.eui||'');
      const hasAlarm=euids.some(e=>alarmSet.has(e));
      const badge=hasAlarm?'<span class="alarm-badge">⚠ ALLARME</span>':'';
      const ranges=[];
      if(c.t_min!=null)ranges.push('T° min '+c.t_min+'°C');
      if(c.t_max!=null)ranges.push('T° max '+c.t_max+'°C');
      if(c.h_min!=null)ranges.push('Umid. min '+c.h_min+'%');
      if(c.h_max!=null)ranges.push('Umid. max '+c.h_max+'%');
      return`<div class="ccard${hasAlarm?' alarm':''}" onclick="go(${i})">
          <div class="ccard-name">${c.cognome} ${c.nome}${badge}</div>
        <div class="ccard-details">
          ${c.email?`✉ ${c.email}<br>`:''}${c.telefono?`📞 ${c.telefono}<br>`:''}
          P.IVA: ${c.piva||'—'} &nbsp;·&nbsp; 📍 ${c.indirizzo||'—'}<br>
          <span class="ccard-eui">${(c.sensori||[{eui:c.eui||"",nome_frigo:""}]).map(s=>"❄️ "+(s.nome_frigo||s.eui||"—")).join(" &nbsp;·&nbsp; ")}</span>
          ${c.notif_email?'&nbsp;·&nbsp; ✉ Email':''}${c.notif_sms?'&nbsp;·&nbsp; 📱 SMS':''}
        </div>
        <div class="ccard-actions" onclick="event.stopPropagation()">
          <button class="btn-edit" onclick="editClient(${i})">&#9998; Modifica</button>
          <button class="btn-del" onclick="del(${i})">&#10005; Elimina</button>
          <button class="btn-creds" onclick="sendCreds(${i})">&#9993; Credenziali</button>
        </div>
        ${ranges.length?'<div class="ccard-ranges">'+ranges.map(r=>'<span class="crange">'+r+'</span>').join('')+'</div>':''}
      </div>`;
    }).join('');
  }catch(e){console.error('loadClients error:',e);}
}
async function del(i){if(!confirm('Eliminare?'))return;await fetch('/api/clients/'+i,{method:'DELETE'});loadClients();runDiag();}
async function testNotify(){
  const b=document.getElementById('btnTest');
  b.disabled=true; b.textContent='⏳ Test in corso...';
  // Prendi email e telefono dal primo cliente
  let email='', phone='';
  try{
    const cls=await(await fetch('/api/clients')).json();
    if(cls.length){email=cls[0].email||''; phone=cls[0].telefono||'';}
  }catch(e){}
  // Chiedi se non trovati
  if(!email) email=prompt('Email destinatario per il test:','');
  if(!email){b.disabled=false;b.textContent='📨 Testa email / SMS';return;}
  if(!phone) phone=prompt('Numero telefono per test SMS (es. +393331234567, lascia vuoto per saltare):','');
  const params=new URLSearchParams();
  params.append('email',email);
  if(phone) params.append('phone',phone);
  try{
    const r=await fetch('/api/test_notify?'+params.toString());
    const j=await r.json();
    let msg='--- RISULTATO TEST NOTIFICHE ---' + String.fromCharCode(10) + String.fromCharCode(10);
    const nl=String.fromCharCode(10);
    if(j.email){
      msg+='EMAIL: '+(j.email.ok?'OK - inviata a '+j.email.to:'ERRORE: '+j.email.error)+nl+nl;
    }
    if(j.sms){
      msg+='SMS: '+(j.sms.ok?'OK - inviato SID: '+j.sms.sid:'ERRORE: '+j.sms.error)+nl+nl;
    }
    msg+='Configurazione:'+nl;
    msg+='  SMTP: '+j.details.smtp_user+' / '+j.details.smtp_host+nl;
    msg+='  Twilio SID: '+j.details.twilio_sid+nl;
    msg+='  Twilio FROM: '+j.details.twilio_from;
    alert(msg);
  }catch(e){alert('Errore chiamata test: '+e.message);}
  finally{b.disabled=false;b.textContent='📨 Testa email / SMS';}
}

async function sendCreds(i){
  const r=await fetch('/api/send_credentials?idx='+i);
  const j=await r.json();
  if(j.ok) fl('Credenziali inviate a '+j.to);
  else alert('Errore: '+j.error);
}

async function editClient(i){
  const cls=await(await fetch('/api/clients')).json();
  const c=cls[i];
  if(!c)return;
  // Populate form with existing data
  const g=id=>document.getElementById(id);
  g('fNome').value=c.nome||'';
  g('fCognome').value=c.cognome||'';
  g('fPiva').value=c.piva||'';
  g('fEmail').value=c.email||'';
  g('fTel').value=c.telefono||'';
  g('fAddr').value=c.indirizzo||'';
  // thresholds now per-sensor in addSensoreRow
  g('fNotifEmail').checked=!!c.notif_email;
  g('fNotifSms').checked=!!c.notif_sms;
  // Set sensori
  document.getElementById('sensoriList').innerHTML='';
  const sens=c.sensori||[];
  if(sens.length>0){sens.forEach(s=>addSensoreRow(s.nome_frigo||'',s.eui||'',s.t_min,s.t_max,s.h_min,s.h_max));}
  else if(c.eui){addSensoreRow(c.nome_frigo||'',c.eui,c.t_min,c.t_max,c.h_min,c.h_max);}
  else{addSensoreRow();}
  // Change button to Update
  const btn=document.querySelector('.btn-submit');
  btn.textContent='💾 Aggiorna cliente';
  btn.onclick=async function(){await updateClient(i);};
  // Scroll to form
  document.querySelector('.panel').scrollIntoView({behavior:'smooth'});
  fl('Dati cliente caricati — modifica e premi Aggiorna');
}

async function updateClient(idx){
  const g=id=>{const el=document.getElementById(id);return el?el.value.trim():'';}
  const _sensori=getSensori();
  const body={
    cognome:g('fCognome'), nome:g('fNome'), piva:g('fPiva'),
    email:g('fEmail'), telefono:g('fTel'), indirizzo:g('fAddr'),
    sensori:_sensori, eui:_sensori.length>0?_sensori[0].eui:'',
    notif_email:document.getElementById('fNotifEmail').checked,
    notif_sms:document.getElementById('fNotifSms').checked,
  };
  const resp=await fetch('/api/clients/'+idx,{method:'PUT',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const result=await resp.json();
  if(result.ok){
    fl('Cliente aggiornato!');
    // Reset button
    const btn=document.querySelector('.btn-submit');
    btn.textContent='➕ Aggiungi cliente';
    btn.onclick=addClient;
    loadClients();runDiag();
  } else {
    alert('Errore aggiornamento: '+result.error);
  }
}

async function importClienti(input){
  const file=input.files[0]; if(!file)return;
  const text=await file.text();
  let data;
  try{data=JSON.parse(text);}catch(e){alert('File JSON non valido: '+e.message);return;}
  if(!confirm('Importare '+((data.clients||data).length||0)+' clienti? Quelli esistenti (stessa email) verranno aggiornati.'))return;
  const r=await fetch('/api/import',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const j=await r.json();
  if(j.ok){fl('✓ Importati '+j.added+' nuovi, aggiornati '+j.updated+'. Totale: '+j.total+' clienti.');loadClients();}
  else{alert('Errore import: '+j.error);}
  input.value='';
}
async function checkNow(){
  const b=document.getElementById('btnCheck');
  b.disabled=true; b.textContent='⏳ Controllo in corso...';
  try{
    // Prima mostra diagnostica
    const diag=await(await fetch('/api/diag_alarms')).json();
    let lines=['=== DIAGNOSTICA ALLARMI ===',''];
    for(const c of diag){
      lines.push('Cliente: '+c.nome+' ('+c.eui+')');
      lines.push('  Soglie: T['+c.t_min+' ~ '+c.t_max+']  H['+c.h_min+' ~ '+c.h_max+']');
      lines.push('  Valori: T='+c.T+'  H='+c.H);
      lines.push('  Notif email: '+c.notif_email+' -> '+c.email);
      lines.push('  Notif SMS:   '+c.notif_sms+' -> '+c.telefono);
      lines.push('  Problemi rilevati: '+(c.issues.length?c.issues.join(', '):'nessuno'));
      lines.push('  Ultimo allarme: '+(c.last_sent||'mai'));
      lines.push('  Motivazione skip: '+(c.skip_reason||'—'));
      lines.push('');
    }
    if(!diag.length) lines.push('Nessun cliente registrato.');
    // Poi forza il controllo (bypass cooldown)
    await fetch('/api/check_now?force=1');
    await new Promise(r=>setTimeout(r,4000));
    await loadClients();
    // Mostra risultato
    alert(lines.join(String.fromCharCode(10)));
    fl('Controllo completato!');
  }catch(e){fl('Errore: '+e.message);}
  finally{b.disabled=false;b.textContent='🔍 Controlla allarmi ora';}
}
function go(i){location.href='/dashboard?client='+i;}
function fl(m){const e=document.getElementById('flash');e.textContent=m;e.style.display='block';setTimeout(()=>e.style.display='none',4000);}
// ─── FILE UPLOAD SENSORI ────────────────────────────────────────
document.getElementById('sF').addEventListener('change',function(e){
  const file=e.target.files[0]; if(!file)return;
  const reader=new FileReader();
  reader.onload=function(ev){
    _sensoriDb=[];
    ev.target.result.split(String.fromCharCode(10)).forEach(function(line){
      line=line.split(String.fromCharCode(13)).join('').trim();
      if(!line||line[0]==='#')return;
      const parts=line.split(String.fromCharCode(9));
      const eui=(parts[0]||'').replace(/[^0-9A-Fa-f]/g,'').toUpperCase();
      if(eui.length<8)return;
      const desc=(parts[1]||'').trim()||('Sensore '+eui.slice(-6));
      _sensoriDb.push({eui:eui,desc:desc});
    });
    document.getElementById('sensorFileLabel').textContent=_sensoriDb.length+' sensori caricati';
    fl(_sensoriDb.length+' sensori caricati dal file');
  };
  reader.readAsText(file);
});

// ─── INIT ────────────────────────────────────────────────────────
(async function(){
  try{
    const s=await fetch('/api/sensori');
    if(s.ok){_sensoriDb=await s.json();}
    if(_sensoriDb.length)
      document.getElementById('sensorFileLabel').textContent=_sensoriDb.length+' sensori in lista';
  }catch(e){}
  addSensoreRow();
  runDiag();
  loadClients();
  setInterval(loadClients,30000);
})();
</script>
</body></html>"""

# ─── DASHBOARD PAGE ───────────────────────────────────────────────
HTML_DASH = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>MyMine · Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
__COMMONCSS__
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:radial-gradient(ellipse 900px 600px at 100% -5%,rgba(29,181,132,.06) 0%,transparent 50%),
             radial-gradient(ellipse 700px 500px at 0% 110%,rgba(29,181,132,.04) 0%,transparent 50%)}
.wrap{position:relative;z-index:1;max-width:1300px;margin:0 auto;padding:0 28px 80px}
nav{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;
    background:rgba(255,255,255,.95);backdrop-filter:blur(12px);
    padding:14px 28px;margin-left:-28px;margin-right:-28px;margin-bottom:26px;
    border-bottom:1px solid var(--line);position:sticky;top:0;z-index:100;
    box-shadow:0 1px 0 var(--line),0 4px 14px rgba(26,61,48,.06)}
.nav-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.sb{display:flex;align-items:center;gap:7px;background:var(--bg3);border:1px solid var(--line);
    border-radius:20px;padding:5px 12px;font-family:var(--mono);font-size:10px;color:var(--sub);letter-spacing:.06em}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;background:var(--dim)}
.dot.on{background:#22C77A;box-shadow:0 0 6px rgba(34,199,122,.45);animation:pulse 2s ease infinite}
.dot.off{background:var(--red)}.dot.ld{background:var(--amber);animation:pulse .7s ease infinite}
select{appearance:none;background:var(--bg2) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%234E7367'/%3E%3C/svg%3E") no-repeat right 9px center;
  border:1px solid var(--line2);color:var(--sub);border-radius:8px;padding:7px 26px 7px 11px;
  font-family:var(--sans);font-size:12px;font-weight:500;cursor:pointer;outline:none;transition:all .2s}
select:hover{border-color:var(--green);color:var(--text)}
.btn{background:var(--bg2);border:1px solid var(--line2);color:var(--green2);border-radius:8px;
     padding:7px 13px;font-family:var(--sans);font-size:12px;font-weight:600;cursor:pointer;
     transition:all .2s;display:flex;align-items:center;gap:6px;text-decoration:none}
.btn:hover{border-color:var(--green);background:var(--bg3);box-shadow:0 2px 8px rgba(29,181,132,.12)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn.spinning .spin{animation:spin .8s linear infinite;display:inline-block}
.btn-dl{background:linear-gradient(135deg,var(--green),var(--green2));color:#fff;border:none;
        box-shadow:0 3px 10px rgba(29,181,132,.28)}
.btn-dl:hover{filter:brightness(1.06);box-shadow:0 5px 16px rgba(29,181,132,.38);transform:translateY(-1px)}
.errbanner{background:#FAEAEA;border:1px solid rgba(217,79,79,.3);border-radius:10px;padding:11px 16px;
  margin-bottom:18px;font-family:var(--mono);font-size:11px;color:var(--red);line-height:1.7;display:none;white-space:pre-wrap}
.devstrip{background:var(--bg2);border:1px solid var(--line);border-radius:13px;padding:12px 20px;
  margin-bottom:20px;display:none;flex-wrap:wrap;gap:12px 26px;align-items:center;box-shadow:var(--shadow)}
.di label{font-family:var(--mono);font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);display:block;margin-bottom:2px}
.di span{font-size:13px;font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(195px,1fr));gap:13px;margin-bottom:18px}
.card{background:var(--bg2);border:1px solid var(--line);border-radius:14px;padding:19px 20px 17px;
      position:relative;overflow:hidden;transition:all .2s;box-shadow:var(--shadow)}
.card:hover{border-color:var(--line2);transform:translateY(-2px);box-shadow:var(--shadow-md)}
.card-top{height:3px;position:absolute;top:0;left:0;right:0;background:var(--c,var(--green))}
.card-glow{position:absolute;top:-40px;right:-40px;width:120px;height:120px;border-radius:50%;
           background:var(--c,var(--green));opacity:.07;filter:blur(35px);pointer-events:none}
.cicon{font-size:19px;margin-bottom:11px;display:block}
.clabel{font-family:var(--mono);font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--sub);margin-bottom:5px}
.cval{font-size:40px;font-weight:800;line-height:1;letter-spacing:-1.5px;color:var(--c,var(--green));margin-bottom:4px}
.cunit{font-size:14px;font-weight:400;color:var(--sub);letter-spacing:0}
.cts{font-family:var(--mono);font-size:10px;color:var(--dim);margin-top:4px}
.ctrend{font-family:var(--mono);font-size:10px;margin-top:3px}
.up{color:var(--red)}.dn{color:var(--blue)}.flat{color:var(--dim)}
.cgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:13px}
.cbox{background:var(--bg2);border:1px solid var(--line);border-radius:14px;padding:19px 20px;box-shadow:var(--shadow)}
.cbox-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}
.cbox-title{font-size:13px;font-weight:700;display:flex;align-items:center;gap:6px}
.cbox-pill{font-family:var(--mono);font-size:9px;background:var(--bg3);border:1px solid var(--line);
           border-radius:20px;padding:2px 8px;color:var(--sub)}
.cbox-stats{font-family:var(--mono);font-size:10px;color:var(--sub);text-align:right;line-height:1.8}
.cbox-wrap{position:relative;height:160px}
.footer{margin-top:44px;text-align:center;font-family:var(--mono);font-size:10px;color:var(--dim);letter-spacing:.12em}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="wrap">
<nav>
  <a href="/" style="text-decoration:none">LOGO_PLACEHOLDER</a>
  <div class="nav-right">
    <div class="sb"><div class="dot ld" id="sDot"></div><span id="sTxt">CARICAMENTO</span></div>
    <select id="dsel" onchange="load()">
      <option value="1">24 ore</option><option value="3">3 giorni</option>
      <option value="7" selected>7 giorni</option><option value="30">30 giorni</option>
    </select>
    <button class="btn spinning" id="rbtn" onclick="load()" disabled><span class="spin">↻</span> Aggiorna</button>
    <a class="btn btn-dl" href="#" onclick="dlR(event)">⬇ Report XLS</a>
    <a class="btn" href="/">← Clienti</a>
    <a class="btn" href="/logout" style="color:#D94F4F;border-color:rgba(217,79,79,.25)">&#10148; Esci</a>
  </div>
</nav>
<div id="frigoTabs" style="display:none;gap:8px;flex-wrap:wrap;margin-bottom:14px"></div>
<div class="errbanner" id="err"></div>
<div class="devstrip" id="dstrip">
  <div class="di"><label>Cliente</label><span id="dClient">—</span></div>
  <div class="di"><label>Email</label><span id="dEmail">—</span></div>
  <div class="di"><label>Telefono</label><span id="dTel">—</span></div>
  <div class="di"><label>Indirizzo</label><span id="dAddr">—</span></div>
  <div class="di"><label>EUI Sensore</label><span id="dEui" style="color:var(--green)">—</span></div>
  <div class="di"><label>Frigorifero</label><span id="dFrigo" style="color:var(--green)">—</span></div>
  <div class="di"><label>Aggiornato</label><span id="dRef">—</span></div>
</div>
<div class="cards">
  <div class="card" style="--c:#D94F4F"><div class="card-top"></div><div class="card-glow"></div>
    <span class="cicon">🌡️</span><div class="clabel">Temperatura</div>
    <div class="cval" id="vT">—<span class="cunit">°C</span></div>
    <div class="cts" id="vTts"></div><div class="ctrend" id="vTtr"></div></div>
  <div class="card" style="--c:#2878B0"><div class="card-top"></div><div class="card-glow"></div>
    <span class="cicon">💧</span><div class="clabel">Umidità relativa</div>
    <div class="cval" id="vH">—<span class="cunit">%</span></div>
    <div class="cts" id="vHts"></div><div class="ctrend" id="vHtr"></div></div>
  <div class="card" style="--c:#1DB584"><div class="card-top"></div><div class="card-glow"></div>
    <span class="cicon">🔋</span><div class="clabel">Batteria</div>
    <div class="cval" id="vB">—<span class="cunit">%</span></div>
    <div class="cts" id="vBts"></div></div>
  <div class="card" style="--c:#6B4FA0"><div class="card-top"></div><div class="card-glow"></div>
    <span class="cicon">📡</span><div class="clabel">Misurazioni ricevute</div>
    <div class="cval" id="vN">—</div><div class="cts" id="vNs"></div></div>
</div>
<div class="cgrid">
  <div class="cbox"><div class="cbox-head">
      <div class="cbox-title" style="color:#D94F4F">🌡️ Temperatura <span class="cbox-pill">°C</span></div>
      <div class="cbox-stats" id="stT">—</div></div>
    <div class="cbox-wrap"><canvas id="cT"></canvas></div></div>
  <div class="cbox"><div class="cbox-head">
      <div class="cbox-title" style="color:#2878B0">💧 Umidità <span class="cbox-pill">%</span></div>
      <div class="cbox-stats" id="stH">—</div></div>
    <div class="cbox-wrap"><canvas id="cH"></canvas></div></div>
  <div class="cbox" id="boxB" style="display:none"><div class="cbox-head">
      <div class="cbox-title" style="color:#1DB584">🔋 Batteria</div>
      <div class="cbox-stats" id="stB">—</div></div>
    <div class="cbox-wrap"><canvas id="cB"></canvas></div></div>
</div>
<div class="footer">MYMINE DASHBOARD · localhost:8765 · AUTO-REFRESH 60s</div>
</div>
<script>
const CH={};let frames=[],devId=null,ci=null,cd=null;
function gP(f){let p=f.decoded_payload||f.object||f.payload;if(p&&typeof p==='object')return p;const r=f.data;if(typeof r==='string'){try{return JSON.parse(r)}catch(e){}}return r&&typeof r==='object'?r:{};}
const gT=f=>{const p=gP(f);const v=p.temperature??p.temp;return v!==undefined?+v:undefined};
const gH=f=>{const p=gP(f);const v=p.humidity??p.hum;return v!==undefined?+v:undefined};
const gB=f=>{const p=gP(f);const v=p.battery_pct??p.battery??p.bat;return v!==undefined?+v:undefined};
const gTs=f=>{const v=f.time_created??f.time??f.reported_at??f.created_at;if(!v)return null;const d=new Date(v);return isNaN(d)?null:d};
function mkC(id,color,unit){
  if(CH[id])CH[id].destroy();
  CH[id]=new Chart(document.getElementById(id),{type:'line',
    data:{labels:[],datasets:[{data:[],borderColor:color,backgroundColor:color+'18',
      borderWidth:2,pointRadius:0,pointHoverRadius:5,pointHoverBackgroundColor:color,
      pointHoverBorderColor:'#fff',pointHoverBorderWidth:2,fill:true,tension:0.38,spanGaps:true}]},
    options:{responsive:true,maintainAspectRatio:false,animation:{duration:400},interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false},tooltip:{backgroundColor:'#fff',borderColor:'#CEEADB',borderWidth:1,
        titleColor:'#4E7367',bodyColor:color,padding:10,
        titleFont:{family:'JetBrains Mono',size:10},bodyFont:{family:'JetBrains Mono',size:14,weight:'700'},
        callbacks:{label:i=>' '+Number(i.raw).toFixed(1)+' '+unit}}},
      scales:{x:{ticks:{color:'#8DBDAF',font:{family:'JetBrains Mono',size:9},maxTicksLimit:7,maxRotation:0},
                 grid:{color:'rgba(206,234,219,.5)'},border:{color:'#CEEADB'}},
              y:{ticks:{color:'#8DBDAF',font:{family:'JetBrains Mono',size:9},maxTicksLimit:5},
                 grid:{color:'rgba(206,234,219,.5)'},border:{color:'#CEEADB'}}}}});
}
function sC(id,labels,data){if(!CH[id])return;CH[id].data.labels=labels;CH[id].data.datasets[0].data=data;CH[id].update();}
async function api(path){const r=await fetch('/proxy?path='+encodeURIComponent(path));const t=await r.text();if(!r.ok)throw new Error('HTTP '+r.status+': '+t.slice(0,200));return JSON.parse(t);}
async function load(){
  setL(true);hideE();
  const days=document.getElementById('dsel').value;
  // Read sensore
      document.getElementById('dstrip').style.display='flex';
      document.getElementById('dClient').textContent=(cd?.cognome+' '+cd?.nome)||'—';
      document.getElementById('dEmail').textContent=cd?.email||'—';
      document.getElementById('dTel').textContent=cd?.telefono||'—';
      document.getElementById('dAddr').textContent=cd?.indirizzo||'—';
      document.getElementById('dEui').textContent=eui;
      
    }
    const raw=await api('/frame/days/'+devId+'/'+days);
    frames=(Array.isArray(raw)?raw:(raw.frames||raw.data||raw.items||[])).sort((a,b)=>{const ta=gTs(a),tb=gTs(b);return(!ta||!tb)?0:ta-tb});
    document.getElementById('vN').textContent=frames.length;
    document.getElementById('vNs').textContent='negli ultimi '+days+' gg';
    document.getElementById('dRef').textContent=new Date().toLocaleTimeString('it-IT');
    if(frames.length>0){rCards();rCharts(+days);}
    const lt=frames.length?gTs(frames[frames.length-1]):null;
    const on=lt&&(Date.now()-lt)<7200000;
    document.getElementById('sDot').className='dot '+(on?'on':'off');
    document.getElementById('sTxt').textContent=on?'ONLINE':'OFFLINE';
  }catch(e){showE(e.message);document.getElementById('sDot').className='dot off';document.getElementById('sTxt').textContent='ERRORE';}
  finally{setL(false);}
}
function rCards(){
  const last=frames[frames.length-1],ts=gTs(last),str=ts?ts.toLocaleString('it-IT'):'';
  const T=gT(last),H=gH(last),B=gB(last);
  if(T!==undefined){document.getElementById('vT').innerHTML=T.toFixed(1)+'<span class="cunit">°C</span>';document.getElementById('vTts').textContent=str;setTr('vTtr',T,gT(frames[Math.max(0,frames.length-6)]),.2,'°');}
  if(H!==undefined){document.getElementById('vH').innerHTML=H.toFixed(0)+'<span class="cunit">%</span>';document.getElementById('vHts').textContent=str;setTr('vHtr',H,gH(frames[Math.max(0,frames.length-6)]),1,'%');}
  if(B!==undefined){const isV=B<10;document.getElementById('vB').innerHTML=(isV?B.toFixed(2):B.toFixed(0))+'<span class="cunit">'+(isV?'V':'%')+'</span>';document.getElementById('vBts').textContent=str;}
}
function setTr(id,curr,prev,thr,unit){if(prev===undefined)return;const el=document.getElementById(id),d=curr-prev;if(Math.abs(d)<thr){el.textContent='→ stabile';el.className='ctrend flat';}else if(d>0){el.textContent='↑ +'+d.toFixed(1)+unit;el.className='ctrend up';}else{el.textContent='↓ '+d.toFixed(1)+unit;el.className='ctrend dn';}}
function rCharts(days){
  const step=Math.max(1,Math.floor(frames.length/100));
  const s=frames.filter((_,i)=>i%step===0||i===frames.length-1);
  const lbl=s.map(f=>{const ts=gTs(f);if(!ts)return '';return days<=1?ts.toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'}):ts.toLocaleDateString('it-IT',{day:'2-digit',month:'2-digit'})+' '+ts.toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'});});
  const hasT=frames.some(f=>gT(f)!==undefined),hasH=frames.some(f=>gH(f)!==undefined),hasB=frames.some(f=>gB(f)!==undefined);
  if(hasT){const d=s.map(f=>gT(f)??null),v=d.filter(x=>x!==null);mkC('cT','#D94F4F','°C');sC('cT',lbl,d);document.getElementById('stT').innerHTML='min <b>'+Math.min(...v).toFixed(1)+'°C</b>&nbsp;&nbsp;max <b>'+Math.max(...v).toFixed(1)+'°C</b>';}
  if(hasH){const d=s.map(f=>gH(f)??null),v=d.filter(x=>x!==null);mkC('cH','#2878B0','%');sC('cH',lbl,d);document.getElementById('stH').innerHTML='min <b>'+Math.min(...v).toFixed(0)+'%</b>&nbsp;&nbsp;max <b>'+Math.max(...v).toFixed(0)+'%</b>';}
  if(hasB){const d=s.map(f=>gB(f)??null),isV=(d.find(x=>x!==null)||0)<10;document.getElementById('boxB').style.display='block';mkC('cB','#1DB584',isV?'V':'%');sC('cB',lbl,d);const v=d.filter(x=>x!==null);document.getElementById('stB').innerHTML='min <b>'+Math.min(...v).toFixed(isV?2:0)+(isV?'V':'%')+'</b>&nbsp;&nbsp;max <b>'+Math.max(...v).toFixed(isV?2:0)+(isV?'V':'%')+'</b>';}
}
function dlR(e){e.preventDefault();window.location.href='/report?client='+ci;}
function setL(v){const b=document.getElementById('rbtn');b.disabled=v;b.classList.toggle('spinning',v);if(v){document.getElementById('sDot').className='dot ld';document.getElementById('sTxt').textContent='CARICAMENTO';}}
function showE(m){const e=document.getElementById('err');e.style.display='block';e.textContent='⚠ '+m;}
function hideE(){document.getElementById('err').style.display='none';}
(async()=>{
  const p=new URLSearchParams(location.search);ci=p.get('client');
  if(ci!==null){const cls=await(await fetch('/api/clients')).json();cd=cls[+ci]||null;}
  load();setInterval(load,60000);
})();
</script>
</body></html>"""



# ─── SMTP / WhatsApp / alarms ────────────────────────────────────

def send_email(to_addr, subject, body_html):
    if not SMTP_USER or not SMTP_PASS:
        print(f"  [EMAIL] SMTP non configurato — messaggio non inviato a {to_addr}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_FROM
        msg["To"]      = to_addr
        msg.attach(MIMEText(body_html, "html", "utf-8"))
        print(f"  [EMAIL] Connessione a {SMTP_HOST}:{SMTP_PORT} come {SMTP_USER}...")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, to_addr, msg.as_string())
        print(f"  [EMAIL] ✓ inviata a {to_addr}: {subject}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        print(f"  [EMAIL] ✗ ERRORE AUTENTICAZIONE: {e}")
        print(f"  [EMAIL]   Controlla SMTP_USER e SMTP_PASS — serve una App Password Gmail")
        return False
    except Exception as e:
        print(f"  [EMAIL] ✗ errore: {e}")
        return False

def send_whatsapp(phone, message):
    """CallMeBot — il cliente deve prima inviare una volta il messaggio
       'I allow callmebot to send me messages' al numero +34 644 44 19 56 su WhatsApp.
       Poi si ottiene un api_key che va salvato nel campo telefono come:
       +393331234567|APIKEY"""
    try:
        if '|' not in phone:
            print(f"  [WA] formato telefono non valido (serve +tel|apikey): {phone}")
            return False
        number, apikey = phone.split('|', 1)
        number = number.strip(); apikey = apikey.strip()
        encoded = _uparse.quote(message)
        url = f"https://api.callmebot.com/whatsapp.php?phone={number}&text={encoded}&apikey={apikey}"
        req = urllib.request.Request(url, headers={"User-Agent": "MyMine/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = r.read().decode()
        print(f"  [WA] inviato a {number}: {resp[:80]}")
        return True
    except Exception as e:
        print(f"  [WA] errore: {e}")
        return False

def send_telegram(chat_id, message):
    if not TG_BOT_TOKEN:
        print("  [TG] Token non configurato"); return False
    try:
        url = "https://api.telegram.org/bot" + TG_BOT_TOKEN + "/sendMessage"
        payload = json.dumps({"chat_id":chat_id,"text":message,"parse_mode":"HTML"}).encode()
        req = urllib.request.Request(url,data=payload,headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=15) as r:
            resp = json.loads(r.read())
        if resp.get("ok"): print("  [TG] -> chat_id",chat_id); return True
        print("  [TG] errore:",resp); return False
    except Exception as e: print("  [TG] errore:",e); return False

def _normalize_phone(phone):
    import re as _re
    phone = _re.sub(r'[\s\-\(\)]', '', phone.strip())
    if phone.startswith('00'):
        phone = '+' + phone[2:]
    elif _re.match(r'^3\d{9}$', phone):
        phone = '+39' + phone
    return phone

def _ascii_sms(text):
    for o, n in [('\u00b0',' gradi'),('\u00e8','e'),('\u00e9','e'),('\u00e0','a'),
                 ('\u00f9','u'),('\u00ec','i'),('\u00f2','o'),('\u2013','--'),('\u2014','--')]:
        text = text.replace(o, n)
    return text

def send_sms(to_number, message):
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_FROM_NUMBER:
        print(f"  [SMS] Twilio non configurato")
        return False
    try:
        import base64
        phone = _normalize_phone(to_number)
        body  = _ascii_sms(message)
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
        data = _uparse.urlencode({"From": TWILIO_FROM_NUMBER, "To": phone, "Body": body}).encode("utf-8")
        creds = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
        req = urllib.request.Request(url, data=data, headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())
        sid = resp.get("sid","?"); status = resp.get("status","?")
        print(f"  [SMS] OK to={phone} sid={sid} status={status}")
        return True
    except urllib.error.HTTPError as e:
        bd = e.read().decode()
        try:
            tw = json.loads(bd); code = tw.get("code","?"); msg = tw.get("message",bd[:200])
            if code == 21608:
                print(f"  [SMS] TRIAL: numero non verificato -> twilio.com/console/phone-numbers/verified")
            elif code == 21211:
                print(f"  [SMS] NUMERO NON VALIDO: {phone} -> formato +39XXXXXXXXXX")
            else:
                print(f"  [SMS] Twilio error {code}: {msg}")
        except: print(f"  [SMS] HTTP {e.code}: {bd[:200]}")
        return False
    except Exception as e:
        print(f"  [SMS] errore: {e}")
        return False
def _get_payload(frame):
    """Extract sensor payload - mirrors JS gP() function exactly."""
    for key in ("decoded_payload", "object", "payload"):
        p = frame.get(key)
        if p and isinstance(p, dict):
            return p
    raw = frame.get("data", "")
    if isinstance(raw, str) and raw:
        try: return json.loads(raw)
        except: pass
    if isinstance(raw, dict):
        return raw
    return {}

def _get_val(payload, *keys):
    """Get first non-None value for given keys (handles 0 correctly)."""
    for k in keys:
        v = payload.get(k)
        if v is not None:
            return float(v)
    return None

def check_all_alarms():
    clients=load_clients(); alerts=load_alerts()
    now_str=datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    try:
        body,code=call_api("/device/")
        if code!=200: print("  [ALARM] API devices code:",code); return
        devs=json.loads(body)
    except Exception as e: print("  [ALARM] fetch devices:",e); return
    print(f"  [ALARM] Controllo {len(clients)} clienti...")
    for client in clients:
        eui=client.get("eui","").upper()
        # Per-sensor thresholds (fall back to client-level)
        _si_a = 0  # default first sensor for alarm check
        _sens_a = (client.get("sensori") or [{}])[_si_a]
        t_min = _sens_a.get("t_min") if _sens_a.get("t_min") is not None else client.get("t_min")
        t_max = _sens_a.get("t_max") if _sens_a.get("t_max") is not None else client.get("t_max")
        h_min = _sens_a.get("h_min") if _sens_a.get("h_min") is not None else client.get("h_min")
        h_max = _sens_a.get("h_max") if _sens_a.get("h_max") is not None else client.get("h_max")
        if all(v is None for v in [t_min,t_max,h_min,h_max]):
            print(f"  [ALARM] {eui}: nessuna soglia definita, skip")
            continue
        dev=next((d for d in devs if (d.get("dev_eui","")).upper()==eui),None)
        if not dev:
            print(f"  [ALARM] {eui}: device non trovato nell'API, skip")
            continue
        try:
            body,code=call_api("/frame/days/"+str(dev["id"])+"/1")
            if code!=200: print(f"  [ALARM] {eui}: frame API code {code}"); continue
            frames=json.loads(body)
            if isinstance(frames,dict): frames=frames.get("frames") or frames.get("data") or frames.get("items") or []
            if not frames: print(f"  [ALARM] {eui}: nessun frame"); continue
            def gts(f):
                v=f.get("time_created") or f.get("time") or ""
                try: return datetime.fromisoformat(v.replace("Z","+00:00"))
                except: return datetime.min.replace(tzinfo=timezone.utc)
            latest=max(frames,key=gts)
            p=_get_payload(latest)
            T=_get_val(p, "temperature", "temp")
            H=_get_val(p, "humidity", "hum")
            print(f"  [ALARM] {eui}: T={T} H={H} soglie=[{t_min},{t_max},{h_min},{h_max}] payload_keys={list(p.keys())}")
        except Exception as e: print(f"  [ALARM] {eui} frames err: {e}"); continue
        issues=[]
        if T is not None:
            if t_min is not None and T<t_min: issues.append("Temperatura "+str(round(T,1))+"°C sotto minimo ("+str(t_min)+"°C)")
            if t_max is not None and T>t_max: issues.append("Temperatura "+str(round(T,1))+"°C sopra massimo ("+str(t_max)+"°C)")
        if H is not None:
            if h_min is not None and H<h_min: issues.append("Umidità "+str(round(H,0))+"% sotto minimo ("+str(h_min)+"%)")
            if h_max is not None and H>h_max: issues.append("Umidità "+str(round(H,0))+"% sopra massimo ("+str(h_max)+"%)")
        if not issues:
            print(f"  [ALARM] {eui}: valori nella norma")
            if eui in alerts:
                del alerts[eui]
                save_alerts(alerts)
                print(f"  [ALARM] {eui}: ✓ allarme rimosso (valori rientrati nelle soglie)")
            continue
        last_alert=alerts.get(eui,{}).get("last_sent","")
        try:
            if (datetime.now()-datetime.fromisoformat(last_alert)).total_seconds()<7200:
                print(f"  [ALARM] {eui}: allarme già inviato meno di 2h fa, skip")
                continue
        except: pass
        nome=(client.get("cognome","")+" "+client.get("nome","")).strip()
        issues_html="".join("<li>"+x+"</li>" for x in issues)
        issues_text="\n".join("- "+x for x in issues)
        subj="Allarme MyMine - "+nome
        html_body=("<div style='font-family:Arial,sans-serif;max-width:580px;margin:0 auto'>"
            "<div style='background:#1F4E3D;padding:18px 24px;border-radius:8px 8px 0 0'>"
            "<span style='color:#1DB584;font-size:20px;font-weight:800'>my</span>"
            "<span style='color:#fff;font-size:20px;font-weight:800'>mine</span></div>"
            "<div style='background:#FEF2F2;border:1px solid #D94F4F;border-top:none;padding:22px 24px;border-radius:0 0 8px 8px'>"
            "<h2 style='color:#D94F4F;margin:0 0 12px'>Valori fuori soglia</h2>"
            "<p><b>Cliente:</b> "+nome+"</p>"
            "<p><b>Sensore:</b> "+eui+"</p>"
            "<p><b>Indirizzo:</b> "+client.get("indirizzo","")+"</p>"
            "<ul style='color:#B02020'>"+issues_html+"</ul>"
            "<p style='color:#888;font-size:11px'>"+now_str+"</p>"
            "</div></div>")

        print(f"  [ALARM] ⚠ {nome} ({eui}): {issues}")
        if client.get("notif_email") and client.get("email"):
            send_email(client["email"],subj,html_body)
        else:
            print(f"  [ALARM]   email non inviata: notif_email={client.get('notif_email')}, email={client.get('email','')}")
        if client.get("notif_sms") and client.get("telefono") and TWILIO_ACCOUNT_SID:
            sms_body = f"ALLARME MyMine\nCliente: {nome}\nSensore: {eui}\n{issues_text}\n{now_str}"
            send_sms(client["telefono"], sms_body)
        alerts[eui]={"last_sent":now_str,"issues":issues,"nome":nome}
        save_alerts(alerts)
    print(f"  [ALARM] Controllo completato.")

def alarm_thread():
    _time.sleep(20)
    while True:
        try: check_all_alarms()
        except Exception as e: print("  [ALARM] thread err:",e)
        _time.sleep(ALERT_INTERVAL)

def generate_password(length=10):
    """Genera password alfanumerica sicura."""
    import random, string
    chars = string.ascii_letters + string.digits
    return ''.join(random.SystemRandom().choice(chars) for _ in range(length))

def daily_report_thread():
    """Invia report PDF giornaliero alle 9:00 ora italiana per ogni cliente con email."""
    import time as _t
    try:
        from zoneinfo import ZoneInfo
        _ROME = ZoneInfo("Europe/Rome")
    except Exception:
        _ROME = None

    def _now():
        if _ROME:
            return datetime.now(_ROME).replace(tzinfo=None)
        return datetime.utcnow() + timedelta(hours=1)  # fallback CET

    while True:
        now = _now()
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        print(f"  [REPORT] Prossimo invio: {target.strftime('%Y-%m-%d 09:00')} (ora italiana)")
        _t.sleep(wait)
        try:
            send_daily_reports()
        except Exception as e:
            print(f"  [REPORT] errore: {e}")

def send_daily_reports():
    clients = load_clients()
    yday = (datetime.now() - timedelta(days=1)).strftime("%d/%m/%Y")
    print(f"  [REPORT] Invio report del {yday} a {len(clients)} clienti...")
    for c in clients:
        if not c.get("email") or not c.get("notif_email"):
            continue
        try:
            pdf_bytes, err = generate_pdf_report(c)
            if err:
                print(f"  [REPORT] {c.get('cognome','')} - errore PDF: {err}")
                continue
            nome = (c.get("cognome","") + " " + c.get("nome","")).strip()
            subject = f"MyMine Report {yday} - {nome}"
            body_html = (
                "<div style='font-family:Arial,sans-serif;max-width:580px;margin:0 auto'>"
                "<div style='background:#1F4E3D;padding:18px 24px;border-radius:8px 8px 0 0'>"
                "<span style='color:#1DB584;font-size:20px;font-weight:800'>my</span>"
                "<span style='color:#fff;font-size:20px;font-weight:800'>mine</span></div>"
                "<div style='background:#F0FBF6;border:1px solid #CEEADB;border-top:none;padding:22px 24px;border-radius:0 0 8px 8px'>"
                f"<h2 style='color:#1A3D30;margin:0 0 12px'>Report giornaliero — {yday}</h2>"
                f"<p><b>Cliente:</b> {nome}</p>"
                f"<p><b>Sensore:</b> {c.get('eui','')}</p>"
                f"<p><b>Indirizzo:</b> {c.get('indirizzo','')}</p>"
                "<p style='color:#4E7367;margin-top:12px'>In allegato il report con le misurazioni delle ultime 24 ore.</p>"
                "</div></div>")
            send_email_with_attachment(c["email"], subject, body_html,
                pdf_bytes, f"mymine_report_{yday.replace('/','_')}_{c.get('eui','')[-6:]}.pdf")
            print(f"  [REPORT] Inviato a {c['email']} ({nome})")
        except Exception as e:
            print(f"  [REPORT] errore per {c.get('cognome','')}: {e}")

def send_email_with_attachment(to_addr, subject, body_html, attach_bytes, attach_name):
    from email.mime.base import MIMEBase
    from email import encoders
    if not SMTP_USER or not SMTP_PASS:
        print(f"  [EMAIL] SMTP non configurato")
        return False
    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_addr
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_html, "html", "utf-8"))
        msg.attach(alt)
        part = MIMEBase("application", "pdf")
        part.set_payload(attach_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=attach_name)
        msg.attach(part)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, to_addr, msg.as_string())
        print(f"  [EMAIL+PDF] inviata a {to_addr}: {subject}")
        return True
    except Exception as e:
        print(f"  [EMAIL+PDF] errore: {e}")
        return False


HTML_CLIENTS_FINAL = HTML_CLIENTS.replace('__COMMONCSS__', COMMON_CSS).replace('LOGO_PLACEHOLDER', LOGO_IMG).replace('</head>', '<script defer src="https://analytics.mymine.cloud/script.js" data-website-id="b3681a33-bfca-4678-b997-9620faec9961"></script></head>', 1)
HTML_LOGIN_FINAL   = HTML_LOGIN

HTML_DASH_FINAL    = '<!DOCTYPE html><html lang="it"><head>\n<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">\n<title>MyMine &middot; Dashboard</title>\n<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>\n<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">\n<style>\n:root{\n  --bg:#F0F6F3;--bg2:#fff;--bg3:#E9F4EF;--bg4:#DAF0E6;\n  --line:#CEEADB;--line2:#AEDCC8;\n  --green:#1DB584;--green2:#0F9A6E;\n  --text:#1A3D30;--sub:#4E7367;--dim:#8DBDAF;\n  --red:#D94F4F;--blue:#2878B0;--amber:#D4891A;--purple:#6B4FA0;\n  --shadow:0 1px 8px rgba(26,61,48,.07);--shadow-md:0 4px 20px rgba(26,61,48,.10);\n  --mono:\'JetBrains Mono\',monospace;--sans:\'Outfit\',sans-serif;\n}\n*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}\nhtml{scroll-behavior:smooth}\nbody{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh}\n.co-footer{background:var(--bg2);border-top:1px solid var(--line);padding:18px 28px;margin-top:36px}\n.co-inner{max-width:1300px;margin:0 auto;display:flex;align-items:center;gap:18px;flex-wrap:wrap}\n.co-text{font-family:var(--mono);font-size:10px;color:var(--dim);line-height:1.9}\n.co-text a{color:var(--dim);text-decoration:none}.co-text a:hover{color:var(--green)}\n@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}\n@keyframes spin{to{transform:rotate(360deg)}}\n@keyframes alarmPulse{0%,100%{border-color:#D94F4F}50%{border-color:#FCA5A5}}\n\nbody::before{content:\'\';position:fixed;inset:0;pointer-events:none;\n  background:radial-gradient(ellipse 900px 600px at 100% -5%,rgba(29,181,132,.06) 0%,transparent 50%),\n             radial-gradient(ellipse 700px 500px at 0% 110%,rgba(29,181,132,.04) 0%,transparent 50%)}\n.wrap{position:relative;z-index:1;max-width:1300px;margin:0 auto;padding:0 28px 0}\nnav{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;\n    background:rgba(255,255,255,.95);backdrop-filter:blur(12px);\n    padding:13px 28px;margin-left:-28px;margin-right:-28px;margin-bottom:22px;\n    border-bottom:1px solid var(--line);position:sticky;top:0;z-index:100;\n    box-shadow:0 1px 0 var(--line),0 4px 14px rgba(26,61,48,.06)}\n.nav-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap}\n.sb{display:flex;align-items:center;gap:7px;background:var(--bg3);border:1px solid var(--line);\n    border-radius:20px;padding:5px 12px;font-family:var(--mono);font-size:10px;color:var(--sub);letter-spacing:.06em}\n.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;background:var(--dim)}\n.dot.on{background:#22C77A;box-shadow:0 0 6px rgba(34,199,122,.45);animation:pulse 2s ease infinite}\n.dot.off{background:var(--red)}.dot.ld{background:var(--amber);animation:pulse .7s ease infinite}\nselect{appearance:none;background:var(--bg2) url("data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'10\' height=\'6\'%3E%3Cpath d=\'M0 0l5 6 5-6z\' fill=\'%234E7367\'/%3E%3C/svg%3E") no-repeat right 9px center;\n  border:1px solid var(--line2);color:var(--sub);border-radius:8px;padding:7px 26px 7px 11px;\n  font-family:var(--sans);font-size:12px;font-weight:500;cursor:pointer;outline:none;transition:all .2s}\nselect:hover{border-color:var(--green);color:var(--text)}\n.btn{background:var(--bg2);border:1px solid var(--line2);color:var(--green2);border-radius:8px;\n     padding:7px 13px;font-family:var(--sans);font-size:12px;font-weight:600;cursor:pointer;\n     transition:all .2s;display:flex;align-items:center;gap:6px;text-decoration:none}\n.btn:hover{border-color:var(--green);background:var(--bg3)}\n.btn:disabled{opacity:.4;cursor:not-allowed}\n.btn.spinning .spin{animation:spin .8s linear infinite;display:inline-block}\n.btn-dl{background:linear-gradient(135deg,var(--green),var(--green2));color:#fff;border:none;box-shadow:0 3px 10px rgba(29,181,132,.28)}\n.btn-dl:hover{filter:brightness(1.06);transform:translateY(-1px)}\n.errbanner{background:#FAEAEA;border:1px solid rgba(217,79,79,.3);border-radius:10px;padding:11px 16px;\n  margin-bottom:16px;font-family:var(--mono);font-size:11px;color:var(--red);display:none;white-space:pre-wrap}\n.alarm-banner{background:#FEF2F2;border:2px solid #D94F4F;border-radius:12px;padding:14px 20px;\n  margin-bottom:16px;display:none;align-items:center;gap:14px;animation:alarmPulse 2s ease infinite}\n.alarm-icon{font-size:26px;flex-shrink:0}\n.alarm-title{font-size:14px;font-weight:700;color:#D94F4F;margin-bottom:4px}\n.alarm-list{font-family:var(--mono);font-size:11px;color:#B02020;line-height:1.8}\n.devstrip{background:var(--bg2);border:1px solid var(--line);border-radius:13px;padding:11px 18px;\n  margin-bottom:16px;display:none;flex-wrap:wrap;gap:10px 24px;align-items:center;box-shadow:var(--shadow)}\n.di label{font-family:var(--mono);font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);display:block;margin-bottom:2px}\n.di span{font-size:13px;font-weight:600}\n.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(195px,1fr));gap:12px;margin-bottom:14px}\n.card{background:var(--bg2);border:1px solid var(--line);border-radius:14px;padding:18px 19px 16px;\n      position:relative;overflow:hidden;transition:all .2s;box-shadow:var(--shadow)}\n.card:hover{border-color:var(--line2);transform:translateY(-2px);box-shadow:var(--shadow-md)}\n.card.alarm{border-color:#D94F4F!important;background:#FEF8F8!important;animation:alarmPulse 2s ease infinite}\n.card-top{height:3px;position:absolute;top:0;left:0;right:0;background:var(--c,var(--green))}\n.card-glow{position:absolute;top:-40px;right:-40px;width:120px;height:120px;border-radius:50%;\n           background:var(--c,var(--green));opacity:.07;filter:blur(35px);pointer-events:none}\n.cicon{font-size:19px;margin-bottom:10px;display:block}\n.clabel{font-family:var(--mono);font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--sub);margin-bottom:4px}\n.cval{font-size:38px;font-weight:800;line-height:1;letter-spacing:-1.5px;color:var(--c,var(--green));margin-bottom:4px}\n.cunit{font-size:14px;font-weight:400;color:var(--sub)}\n.cts{font-family:var(--mono);font-size:10px;color:var(--dim);margin-top:3px}\n.ctrend{font-family:var(--mono);font-size:10px;margin-top:2px}\n.crange{font-family:var(--mono);font-size:9px;color:var(--dim);margin-top:3px}\n.up{color:var(--red)}.dn{color:var(--blue)}.flat{color:var(--dim)}\n.cgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px}\n.cbox{background:var(--bg2);border:1px solid var(--line);border-radius:14px;padding:18px 19px;box-shadow:var(--shadow)}\n.cbox-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:11px}\n.cbox-title{font-size:13px;font-weight:700;display:flex;align-items:center;gap:6px}\n.cbox-pill{font-family:var(--mono);font-size:9px;background:var(--bg3);border:1px solid var(--line);border-radius:20px;padding:2px 8px;color:var(--sub)}\n.cbox-stats{font-family:var(--mono);font-size:10px;color:var(--sub);text-align:right;line-height:1.8}\n.cbox-wrap{position:relative;height:155px}\n</style></head><body><div class="wrap">\n<nav>\n  <a href="/" style="text-decoration:none"><img src="data:image/png;base64,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" alt="MyMine" style="height:32px;width:auto;display:block"></a>\n  <div class="nav-right">\n    <div class="sb"><div class="dot ld" id="sDot"></div><span id="sTxt">CARICAMENTO</span></div>\n    <select id="dsel" onchange="load()">\n      <option value="1">24 ore</option><option value="3">3 giorni</option>\n      <option value="7" selected>7 giorni</option><option value="30">30 giorni</option>\n    </select>\n    <button class="btn spinning" id="rbtn" onclick="load()" disabled><span class="spin">&#8635;</span> Aggiorna</button>\n    <a class="btn btn-dl" href="#" onclick="dlR(event)">&#8595; Report PDF</a>\n    <a class="btn" href="/">&#8592; Clienti</a>\n    <a class=\"btn\" href=\"/logout\" style=\"color:#D94F4F;border-color:rgba(217,79,79,.25)\">&#10148; Esci</a>\n  </div>\n</nav>\n<div id="frigoTabs" style="display:none;gap:8px;flex-wrap:wrap;margin-bottom:14px"></div>\n<div class="errbanner" id="err"></div>\n<div class="alarm-banner" id="alBanner"><div class="alarm-icon">&#9888;&#65039;</div>\n  <div><div class="alarm-title">Valori fuori soglia</div><div class="alarm-list" id="alList"></div></div></div>\n<div class="devstrip" id="dstrip">\n  <div class="di"><label>Cliente</label><span id="dClient">&#8212;</span></div>\n  <div class="di"><label>Email</label><span id="dEmail">&#8212;</span></div>\n  <div class="di"><label>Indirizzo</label><span id="dAddr">&#8212;</span></div>\n  <div class="di"><label>EUI Sensore</label><span id="dEui" style="color:var(--green)">&#8212;</span></div>\n  <div class="di"><label>Frigorifero</label><span id="dFrigo" style="color:var(--green)">&#8212;</span></div>\n  <div class="di"><label>Aggiornato</label><span id="dRef">&#8212;</span></div>\n</div>\n<div class="cards">\n  <div class="card" id="cardT" style="--c:#D94F4F"><div class="card-top"></div><div class="card-glow"></div>\n    <span class="cicon">&#127777;</span><div class="clabel">Temperatura</div>\n    <div class="cval" id="vT">&#8212;<span class="cunit">&deg;C</span></div>\n    <div class="cts" id="vTts"></div><div class="ctrend" id="vTtr"></div><div class="crange" id="vTrange"></div></div>\n  <div class="card" id="cardH" style="--c:#2878B0"><div class="card-top"></div><div class="card-glow"></div>\n    <span class="cicon">&#128167;</span><div class="clabel">Umidità relativa</div>\n    <div class="cval" id="vH">&#8212;<span class="cunit">%</span></div>\n    <div class="cts" id="vHts"></div><div class="ctrend" id="vHtr"></div><div class="crange" id="vHrange"></div></div>\n  <div class="card" style="--c:#1DB584"><div class="card-top"></div><div class="card-glow"></div>\n    <span class="cicon">&#128267;</span><div class="clabel">Batteria</div>\n    <div class="cval" id="vB">&#8212;<span class="cunit">%</span></div><div class="cts" id="vBts"></div></div>\n  <div class="card" style="--c:#6B4FA0"><div class="card-top"></div><div class="card-glow"></div>\n    <span class="cicon">&#128225;</span><div class="clabel">Misurazioni</div>\n    <div class="cval" id="vN">&#8212;</div><div class="cts" id="vNs"></div></div>\n</div>\n<div class="cgrid">\n  <div class="cbox"><div class="cbox-head">\n    <div class="cbox-title" style="color:#D94F4F">&#127777; Temperatura <span class="cbox-pill">&deg;C</span></div>\n    <div class="cbox-stats" id="stT">&#8212;</div></div><div class="cbox-wrap"><canvas id="cT"></canvas></div></div>\n  <div class="cbox"><div class="cbox-head">\n    <div class="cbox-title" style="color:#2878B0">&#128167; Umidità <span class="cbox-pill">%</span></div>\n    <div class="cbox-stats" id="stH">&#8212;</div></div><div class="cbox-wrap"><canvas id="cH"></canvas></div></div>\n  <div class="cbox" id="boxB" style="display:none"><div class="cbox-head">\n    <div class="cbox-title" style="color:#1DB584">&#128267; Batteria</div>\n    <div class="cbox-stats" id="stB">&#8212;</div></div><div class="cbox-wrap"><canvas id="cB"></canvas></div></div>\n</div>\n</div>\n<div class="co-footer"><div class="co-inner"><img src="data:image/png;base64,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" alt="MyMine" style="height:24px;width:auto;opacity:.65"><div class="co-text">&copy; 2026 Mymine Srl &ndash; Startup Innovativa &nbsp;&middot;&nbsp; P.IVA: IT12038850967<br>Via Monte Bianco 2/a &ndash; 20149 Milano &nbsp;&middot;&nbsp;<a href="mailto:info@mymine.io">info@mymine.io</a></div></div></div>\n<script>\nconst CH={};let frames=[],devId=null,ci=null,cd=null;\nfunction gP(f){let p=f.decoded_payload||f.object||f.payload;if(p&&typeof p===\'object\')return p;const r=f.data;if(typeof r===\'string\'){try{return JSON.parse(r)}catch(e){}}return r&&typeof r===\'object\'?r:{};}\nconst gT=f=>{const p=gP(f);const v=p.temperature??p.temp;return v!==undefined?+v:undefined};\nconst gH=f=>{const p=gP(f);const v=p.humidity??p.hum;return v!==undefined?+v:undefined};\nconst gB=f=>{const p=gP(f);const v=p.battery_pct??p.battery??p.bat;return v!==undefined?+v:undefined};\nconst gTs=f=>{const v=f.time_created??f.time??f.reported_at??f.created_at;if(!v)return null;const d=new Date(v);return isNaN(d)?null:d};\nfunction mkC(id,color,unit){\n  if(CH[id])CH[id].destroy();\n  CH[id]=new Chart(document.getElementById(id),{type:\'line\',\n    data:{labels:[],datasets:[{data:[],borderColor:color,backgroundColor:color+\'18\',borderWidth:2,\n      pointRadius:0,pointHoverRadius:5,pointHoverBackgroundColor:color,\n      pointHoverBorderColor:\'#fff\',pointHoverBorderWidth:2,fill:true,tension:0.38,spanGaps:true}]},\n    options:{responsive:true,maintainAspectRatio:false,animation:{duration:400},\n      interaction:{mode:\'index\',intersect:false},\n      plugins:{legend:{display:false},tooltip:{backgroundColor:\'#fff\',borderColor:\'#CEEADB\',borderWidth:1,\n        titleColor:\'#4E7367\',bodyColor:color,padding:10,\n        titleFont:{family:\'JetBrains Mono\',size:10},bodyFont:{family:\'JetBrains Mono\',size:14,weight:\'700\'},\n        callbacks:{label:i=>\' \'+Number(i.raw).toFixed(1)+\' \'+unit}}},\n      scales:{x:{ticks:{color:\'#8DBDAF\',font:{family:\'JetBrains Mono\',size:9},maxTicksLimit:7,maxRotation:0},\n                 grid:{color:\'rgba(206,234,219,.5)\'},border:{color:\'#CEEADB\'}},\n              y:{ticks:{color:\'#8DBDAF\',font:{family:\'JetBrains Mono\',size:9},maxTicksLimit:5},\n                 grid:{color:\'rgba(206,234,219,.5)\'},border:{color:\'#CEEADB\'}}}}});\n}\nfunction sC(id,labels,data){if(!CH[id])return;CH[id].data.labels=labels;CH[id].data.datasets[0].data=data;CH[id].update();}\nasync function api(path){const r=await fetch(\'/proxy?path=\'+encodeURIComponent(path));const t=await r.text();if(!r.ok)throw new Error(\'HTTP \'+r.status+\': \'+t.slice(0,200));return JSON.parse(t);}\nasync function load(){\n  setL(true);hideE();\n  const days=document.getElementById(\'dsel\').value;\n  const _urlp=new URLSearchParams(location.search);\n  const _si=parseInt(_urlp.get(\'sensore\')||0);\n  const _sens=(cd?.sensori&&cd.sensori.length>_si)?cd.sensori[_si]:{eui:cd?.eui||\'\'};\n  const _eui=(_sens.eui||cd?.eui||\'\').toUpperCase();\n  const _nomeFrigo=_sens.nome_frigo||(_eui?_eui.slice(-6):\'Sensore\');\n  const _tmin=_sens.t_min!=null?_sens.t_min:(cd?.t_min??null);\n  const _tmax=_sens.t_max!=null?_sens.t_max:(cd?.t_max??null);\n  const _hmin=_sens.h_min!=null?_sens.h_min:(cd?.h_min??null);\n  const _hmax=_sens.h_max!=null?_sens.h_max:(cd?.h_max??null);\n  try{\n    if(!devId){\n      const devs=await api(\'/device/\');\n      const dev=Array.isArray(devs)?devs.find(d=>(d.dev_eui||d.eui||\'\').toUpperCase()===_eui):null;\n      if(!dev)throw new Error(\'Device non trovato (EUI: \'+_eui+\')\');\n      devId=dev.id;\n      const tabsEl=document.getElementById(\'frigoTabs\');\n      if(tabsEl&&cd?.sensori&&cd.sensori.length>1){\n        tabsEl.style.display=\'flex\';\n        tabsEl.innerHTML=cd.sensori.map((s,i)=>\'<a href="?client=\'+ci+\'&sensore=\'+i+\'"\'+((i===_si)?\' style="background:var(--green);color:#fff;border-color:var(--green)"\':\'\')+\' class="btn" style="font-size:11px">❄️ \'+( s.nome_frigo||s.eui.slice(-6))+\'</a>\').join(\'\');\n      }\n      document.getElementById(\'dstrip\').style.display=\'flex\';\n      document.getElementById(\'dClient\').textContent=(cd?.cognome+\' \'+cd?.nome)||\'—\';\n      document.getElementById(\'dEmail\').textContent=cd?.email||\'—\';\n      document.getElementById(\'dAddr\').textContent=cd?.indirizzo||\'—\';\n      document.getElementById(\'dEui\').textContent=_eui;\n      const dFEl=document.getElementById(\'dFrigo\');if(dFEl)dFEl.textContent=_nomeFrigo;\n      const tr=[],hr=[];\n      if(_tmin!=null)tr.push(\'min \'+_tmin+\'°C\');if(_tmax!=null)tr.push(\'max \'+_tmax+\'°C\');\n      if(_hmin!=null)hr.push(\'min \'+_hmin+\'%\');if(_hmax!=null)hr.push(\'max \'+_hmax+\'%\');\n      document.getElementById(\'vTrange\').textContent=tr.length?\'Soglia: \'+tr.join(\' · \'):\'\';\n      document.getElementById(\'vHrange\').textContent=hr.length?\'Soglia: \'+hr.join(\' · \'):\'\';\n    }\n    const raw=await api(\'/frame/days/\'+devId+\'/\'+days);\n    frames=(Array.isArray(raw)?raw:(raw.frames||raw.data||raw.items||[])).sort((a,b)=>{const ta=gTs(a),tb=gTs(b);return(!ta||!tb)?0:ta-tb});\n    document.getElementById(\'vN\').textContent=frames.length;\n    document.getElementById(\'vNs\').textContent=\'negli ultimi \'+days+\' gg\';\n    document.getElementById(\'dRef\').textContent=new Date().toLocaleTimeString(\'it-IT\');\n    if(frames.length>0){rCards();rCharts(+days);}\n    checkAlarms();\n    const lt=frames.length?gTs(frames[frames.length-1]):null;\n    const on=lt&&(Date.now()-lt)<7200000;\n    document.getElementById(\'sDot\').className=\'dot \'+(on?\'on\':\'off\');\n    document.getElementById(\'sTxt\').textContent=on?\'ONLINE\':\'OFFLINE\';\n  }catch(e){showE(e.message);document.getElementById(\'sDot\').className=\'dot off\';document.getElementById(\'sTxt\').textContent=\'ERRORE\';}\n  finally{setL(false);}\n}\nfunction checkAlarms(){\n  if(!frames.length||!cd)return;\n  const _si2=parseInt(new URLSearchParams(location.search).get(\'sensore\')||0);\n  const _s2=(cd?.sensori&&cd.sensori.length>_si2)?cd.sensori[_si2]:{};\n  const tmin=_s2.t_min!=null?_s2.t_min:(cd?.t_min??null);\n  const tmax=_s2.t_max!=null?_s2.t_max:(cd?.t_max??null);\n  const hmin=_s2.h_min!=null?_s2.h_min:(cd?.h_min??null);\n  const hmax=_s2.h_max!=null?_s2.h_max:(cd?.h_max??null);\n  const last=frames[frames.length-1],T=gT(last),H=gH(last),issues=[];\n  if(T!==undefined){\n    if(tmin!=null&&T<tmin)issues.push(\'Temperatura \'+T.toFixed(1)+\'°C sotto il minimo (\'+tmin+\'°C)\');\n    if(tmax!=null&&T>tmax)issues.push(\'Temperatura \'+T.toFixed(1)+\'°C sopra il massimo (\'+tmax+\'°C)\');\n  }\n  if(H!==undefined){\n    if(hmin!=null&&H<hmin)issues.push(\'Umidità \'+H.toFixed(0)+\'% sotto il minimo (\'+hmin+\'%)\');\n    if(hmax!=null&&H>hmax)issues.push(\'Umidità \'+H.toFixed(0)+\'% sopra il massimo (\'+hmax+\'%)\');\n  }\n  const b=document.getElementById(\'alBanner\');\n  document.getElementById(\'cardT\').classList.toggle(\'alarm\',issues.some(i=>i.startsWith(\'Temp\')));\n  document.getElementById(\'cardH\').classList.toggle(\'alarm\',issues.some(i=>i.startsWith(\'Umid\')));\n  if(issues.length){b.style.display=\'flex\';document.getElementById(\'alList\').innerHTML=issues.join(\'<br>\');}\n  else b.style.display=\'none\';\n}\nfunction rCards(){\n  const last=frames[frames.length-1],ts=gTs(last),str=ts?ts.toLocaleString(\'it-IT\'):\'\';\n  const T=gT(last),H=gH(last),B=gB(last);\n  const temps=frames.map(f=>gT(f)).filter(v=>v!==undefined);\n  const hums=frames.map(f=>gH(f)).filter(v=>v!==undefined);\n  if(T!==undefined){document.getElementById(\'vT\').innerHTML=T.toFixed(1)+\'<span class="cunit">°C</span>\';document.getElementById(\'vTts\').textContent=str;setTr(\'vTtr\',T,gT(frames[Math.max(0,frames.length-6)]),.2,\'°\');}\n  if(H!==undefined){document.getElementById(\'vH\').innerHTML=H.toFixed(0)+\'<span class="cunit">%</span>\';document.getElementById(\'vHts\').textContent=str;setTr(\'vHtr\',H,gH(frames[Math.max(0,frames.length-6)]),1,\'%\');}\n  if(B!==undefined){const isV=B<10;document.getElementById(\'vB\').innerHTML=(isV?B.toFixed(2):B.toFixed(0))+\'<span class="cunit">\'+(isV?\'V\':\'%\')+\'</span>\';document.getElementById(\'vBts\').textContent=str;}\n  if(temps.length)document.getElementById(\'stT\').innerHTML=\'min <b>\'+Math.min(...temps).toFixed(1)+\'°C</b>&nbsp;&nbsp;max <b>\'+Math.max(...temps).toFixed(1)+\'°C</b>\';\n  if(hums.length)document.getElementById(\'stH\').innerHTML=\'min <b>\'+Math.min(...hums).toFixed(0)+\'%</b>&nbsp;&nbsp;max <b>\'+Math.max(...hums).toFixed(0)+\'%</b>\';\n}\nfunction setTr(id,curr,prev,thr,unit){if(prev===undefined)return;const el=document.getElementById(id),d=curr-prev;if(Math.abs(d)<thr){el.textContent=\'→ stabile\';el.className=\'ctrend flat\';}else if(d>0){el.textContent=\'↑ +\'+d.toFixed(1)+unit;el.className=\'ctrend up\';}else{el.textContent=\'↓ \'+d.toFixed(1)+unit;el.className=\'ctrend dn\';}}\nfunction rCharts(days){\n  const step=Math.max(1,Math.floor(frames.length/100));\n  const s=frames.filter((_,i)=>i%step===0||i===frames.length-1);\n  const lbl=s.map(f=>{const ts=gTs(f);if(!ts)return \'\';return days<=1?ts.toLocaleTimeString(\'it-IT\',{hour:\'2-digit\',minute:\'2-digit\'}):ts.toLocaleDateString(\'it-IT\',{day:\'2-digit\',month:\'2-digit\'})+\' \'+ts.toLocaleTimeString(\'it-IT\',{hour:\'2-digit\',minute:\'2-digit\'});});\n  if(frames.some(f=>gT(f)!==undefined)){const d=s.map(f=>gT(f)??null);mkC(\'cT\',\'#D94F4F\',\'°C\');sC(\'cT\',lbl,d);}\n  if(frames.some(f=>gH(f)!==undefined)){const d=s.map(f=>gH(f)??null);mkC(\'cH\',\'#2878B0\',\'%\');sC(\'cH\',lbl,d);}\n  if(frames.some(f=>gB(f)!==undefined)){const d=s.map(f=>gB(f)??null),isV=(d.find(x=>x!==null)||0)<10;document.getElementById(\'boxB\').style.display=\'block\';mkC(\'cB\',\'#1DB584\',isV?\'V\':\'%\');sC(\'cB\',lbl,d);const v=d.filter(x=>x!==null);document.getElementById(\'stB\').innerHTML=\'min <b>\'+Math.min(...v).toFixed(isV?2:0)+(isV?\'V\':\'%\')+\'</b>&nbsp;&nbsp;max <b>\'+Math.max(...v).toFixed(isV?2:0)+(isV?\'V\':\'%\')+\'</b>\';}\n}\nfunction dlR(e){e.preventDefault();window.location.href=\'/report?client=\'+ci;}\nfunction setL(v){const b=document.getElementById(\'rbtn\');b.disabled=v;b.classList.toggle(\'spinning\',v);if(v){document.getElementById(\'sDot\').className=\'dot ld\';document.getElementById(\'sTxt\').textContent=\'CARICAMENTO\';}}\nfunction showE(m){const e=document.getElementById(\'err\');e.style.display=\'block\';e.textContent=\'⚠ \'+m;}\nfunction hideE(){document.getElementById(\'err\').style.display=\'none\';}\n(async()=>{\n  const p=new URLSearchParams(location.search);ci=p.get(\'client\');\n  if(ci!==null){const cls=await(await fetch(\'/api/clients\')).json();cd=cls[+ci]||null;}\n  try{const me=await(await fetch(\'/api/me\')).json();\n  if(me.role===\'client\'){\n    const btn=document.querySelector(\'.btn[href="/"]\');\n    if(btn)btn.style.display=\'none\';\n  }}catch(e){}\n  load();setInterval(load,60000);\n})();\n</script></body></html>'

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):
        print("  ",args[1] if len(args)>1 else "?","  ",self.path)

    def send_html(self,html):
        b=html.encode("utf-8")
        self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)

    def send_json(self,data,status=200):
        b=json.dumps(data,ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)

    def _get_sess(self):
        return _get_session_from_cookie(self.headers.get("Cookie",""))

    def _redirect(self, url, code=302):
        self.send_response(code)
        self.send_header("Location", url)
        self.end_headers()

    def do_GET(self):
        parsed=urlparse(self.path); qs=parse_qs(parsed.query); path=parsed.path

        # ── Public routes ──
        if path == "/login":
            self.send_html(HTML_LOGIN_FINAL); return
        if path == "/logout":
            cookie = self.headers.get("Cookie","")
            for part in cookie.split(";"):
                part = part.strip()
                if part.startswith("mm_sess="):
                    SESSIONS.pop(part[8:], None)
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "mm_sess=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.end_headers(); return

        # ── Public endpoints (no auth required) ──
        if path == "/api/status":
            writable=False
            try:
                with open(DATA,"a"): pass
                writable=True
            except: pass
            self.send_json({"ok":True,"data_file":DATA,"writable":writable,
                "clients":len(load_clients()),"build":BUILD_TS})
            return
        if path == "/api/sensori":
            self.send_json(load_sensori())
            return

        # ── Auth required for all other routes ──
        sess = self._get_sess()
        if sess is None:
            if path.startswith("/api/"):
                self.send_json({"ok": False, "error": "Non autenticato"}, 401); return
            self._redirect("/login"); return

        # ── Role-based routing ──
        if path in ("/", "/index.html"):
            if sess["role"] == "client":
                self._redirect(f"/dashboard?client={sess['client_idx']}"); return
            self.send_html(HTML_CLIENTS_FINAL); return
        elif path == "/dashboard":
            if sess["role"] == "client":
                ci_qs = qs.get("client",[None])[0]
                if ci_qs is None or int(ci_qs) != sess["client_idx"]:
                    self._redirect(f"/dashboard?client={sess['client_idx']}"); return
            self.send_html(HTML_DASH_FINAL); return
        elif path=="/proxy":
            body,status=call_api(qs.get("path",["/device/"])[0])
            self.send_response(status); self.send_header("Content-Type","application/json")
            self.send_header("Access-Control-Allow-Origin","*"); self.end_headers(); self.wfile.write(body)
        elif path=="/api/me":
            sess = self._get_sess()
            if sess:
                self.send_json({"role": sess["role"], "user": sess["user"],
                    "client_idx": sess.get("client_idx")})
            else:
                self.send_json({"role": "anon"}, 401)
        elif path=="/api/export":
            clients = load_clients()
            data = json.dumps({"clients": clients,
                "exported_at": datetime.now().isoformat(),
                "version": BUILD_TS}, indent=2, ensure_ascii=False)
            b = data.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition",
                'attachment; filename="mymine_clienti_backup.json"')
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        elif path=="/api/clients": self.send_json(load_clients())
        elif path=="/api/alerts":
            try:
                al=load_alerts()
                self.send_json([{"eui":k,"issues":v.get("issues",[]),"nome":v.get("nome",""),"last_sent":v.get("last_sent","")} for k,v in al.items()])
            except:
                self.send_json([])
        elif path=="/api/check_now":
            force = qs.get("force",["0"])[0]=="1"
            if force:
                # Azzera il cooldown così le notifiche vengono reinviate
                alerts = load_alerts()
                for k in alerts: alerts[k]["last_sent"] = "2000-01-01T00:00:00"
                save_alerts(alerts)
                print("  [ALARM] Controllo forzato — cooldown azzerato")
            threading.Thread(target=check_all_alarms,daemon=True).start()
            self.send_json({"ok":True,"force":force})
        elif path=="/api/diag_alarms":
            # Diagnostica senza inviare notifiche
            clients=load_clients(); alerts=load_alerts(); result=[]
            try:
                body,code=call_api("/device/")
                devs=json.loads(body) if code==200 else []
            except: devs=[]
            for c in clients:
                eui=c.get("eui","").upper()
                t_min=c.get("t_min"); t_max=c.get("t_max")
                h_min=c.get("h_min"); h_max=c.get("h_max")
                T=None; H=None; skip_reason=""
                dev=next((d for d in devs if (d.get("dev_eui","")).upper()==eui),None)
                if not dev: skip_reason="Device non trovato nell'API Trackpac"
                else:
                    try:
                        fb,fc=call_api("/frame/days/"+str(dev["id"])+"/1")
                        frames=json.loads(fb) if fc==200 else []
                        if isinstance(frames,dict): frames=frames.get("frames") or frames.get("data") or []
                        if frames:
                            def gts2(f):
                                v=f.get("time_created") or f.get("time") or ""
                                try: return datetime.fromisoformat(v.replace("Z","+00:00"))
                                except: return datetime.min.replace(tzinfo=timezone.utc)
                            latest=max(frames,key=gts2)
                            p=_get_payload(latest)
                            T=_get_val(p,"temperature","temp")
                            H=_get_val(p,"humidity","hum")
                        else: skip_reason="Nessun frame ricevuto nelle ultime 24h"
                    except Exception as e: skip_reason=f"Errore frame: {e}"
                issues=[]
                if T is not None:
                    if t_min is not None and T<t_min: issues.append(f"T={T:.1f} sotto min {t_min}")
                    if t_max is not None and T>t_max: issues.append(f"T={T:.1f} sopra max {t_max}")
                if H is not None:
                    if h_min is not None and H<h_min: issues.append(f"H={H:.0f} sotto min {h_min}")
                    if h_max is not None and H>h_max: issues.append(f"H={H:.0f} sopra max {h_max}")
                last_sent=alerts.get(eui,{}).get("last_sent","")
                in_cooldown=False
                try:
                    if last_sent and (datetime.now()-datetime.fromisoformat(last_sent)).total_seconds()<7200:
                        in_cooldown=True
                        if not skip_reason: skip_reason=f"Cooldown 2h attivo (ultimo: {last_sent})"
                except: pass
                if not skip_reason and issues:
                    parts=[]
                    if not c.get("notif_email") or not c.get("email"): parts.append("email disabilitata o mancante")
                    if not c.get("notif_sms") or not c.get("telefono"): parts.append("SMS disabilitato o numero mancante")
                    if parts and len(parts)==2: skip_reason="Tutte le notifiche disabilitate: "+", ".join(parts)
                result.append({
                    "nome":(c.get("cognome","")+" "+c.get("nome","")).strip(),
                    "eui":eui, "T":round(T,1) if T is not None else None,
                    "H":round(H,0) if H is not None else None,
                    "t_min":t_min,"t_max":t_max,"h_min":h_min,"h_max":h_max,
                    "notif_email":c.get("notif_email",False),"email":c.get("email",""),
                    "notif_sms":c.get("notif_sms",False),"telefono":c.get("telefono",""),
                    "issues":issues,"last_sent":last_sent,"skip_reason":skip_reason
                })
            self.send_json(result)
        elif path=="/api/test_notify":
            # Test email e SMS con diagnostica completa
            result = {"email": None, "sms": None, "details": {}}
            to_email = qs.get("email", [None])[0]
            to_phone = qs.get("phone", [None])[0]
            # --- EMAIL ---
            if to_email:
                if not SMTP_USER or not SMTP_PASS:
                    result["email"] = {"ok": False, "error": "SMTP non configurato (SMTP_USER o SMTP_PASS vuoti)"}
                else:
                    try:
                        msg = MIMEMultipart("alternative")
                        msg["Subject"] = "Test Allarme MyMine"
                        msg["From"] = SMTP_FROM
                        msg["To"] = to_email
                        msg.attach(MIMEText("<h2 style='color:#1DB584'>✓ Email di test MyMine</h2><p>Se ricevi questo messaggio, le notifiche email funzionano correttamente.</p>", "html", "utf-8"))
                        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
                            s.sendmail(SMTP_USER, to_email, msg.as_string())
                        result["email"] = {"ok": True, "to": to_email}
                        print(f"  [TEST] ✓ Email inviata a {to_email}")
                    except smtplib.SMTPAuthenticationError as e:
                        result["email"] = {"ok": False, "error": f"Autenticazione fallita: {e} — Verifica che SMTP_PASS sia una App Password Gmail (16 caratteri senza spazi)"}
                    except Exception as e:
                        result["email"] = {"ok": False, "error": str(e)}
            # --- SMS ---
            if to_phone:
                if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_FROM_NUMBER:
                    result["sms"] = {"ok": False, "error": "Twilio non configurato"}
                else:
                    import base64
                    try:
                        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
                        data = _uparse.urlencode({"From": TWILIO_FROM_NUMBER, "To": to_phone,
                            "Body": "Test allarme MyMine - se ricevi questo messaggio gli SMS funzionano."}).encode()
                        creds = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
                        req = urllib.request.Request(url, data=data, headers={
                            "Authorization": f"Basic {creds}",
                            "Content-Type": "application/x-www-form-urlencoded"})
                        with urllib.request.urlopen(req, timeout=15) as r:
                            resp = json.loads(r.read())
                        result["sms"] = {"ok": True, "sid": resp.get("sid"), "to": to_phone}
                        print(f"  [TEST] ✓ SMS inviato a {to_phone}")
                    except urllib.error.HTTPError as e:
                        body = e.read().decode()
                        try:
                            tw = json.loads(body)
                            code = tw.get("code", "?")
                            msg = tw.get("message", body)
                            # Twilio error codes: 21608=unverified number (trial), 21211=invalid number
                            hint = ""
                            if code == 21608:
                                hint = " — ACCOUNT TRIAL: il numero destinatario non è verificato. Vai su twilio.com/console/phone-numbers/verified e aggiungi il numero."
                            elif code == 21211:
                                hint = " — Numero di telefono non valido. Usa il formato internazionale: +393331234567"
                            result["sms"] = {"ok": False, "error": f"Twilio errore {code}: {msg}{hint}"}
                        except:
                            result["sms"] = {"ok": False, "error": body[:300]}
                    except Exception as e:
                        result["sms"] = {"ok": False, "error": str(e)}
            # Config summary
            result["details"] = {
                "smtp_user": SMTP_USER or "(vuoto)",
                "smtp_host": SMTP_HOST,
                "twilio_sid": TWILIO_ACCOUNT_SID[:8]+"..." if TWILIO_ACCOUNT_SID else "(vuoto)",
                "twilio_from": TWILIO_FROM_NUMBER or "(vuoto)",
            }
            self.send_json(result)
        elif path=="/api/send_credentials":
            try:
                ci_str=qs.get("idx",[""])[0]
                clients_list=load_clients()
                if not ci_str.isdigit() or int(ci_str)>=len(clients_list):
                    self.send_json({"ok":False,"error":"cliente non trovato"},400); return
                c=clients_list[int(ci_str)]
                if not c.get("email"):
                    self.send_json({"ok":False,"error":"cliente senza email"},400); return
                nome=(c.get("cognome","")+" "+c.get("nome","")).strip()
                uname=c.get("username",c.get("email",""))
                pwd=c.get("password","—")
                subject=f"MyMine — Le tue credenziali di accesso"
                body_creds=(
                    "<div style='font-family:Arial,sans-serif;max-width:580px;margin:0 auto'>"
                    "<div style='background:#1F4E3D;padding:18px 24px;border-radius:8px 8px 0 0'>"
                    "<span style='color:#1DB584;font-size:20px;font-weight:800'>my</span>"
                    "<span style='color:#fff;font-size:20px;font-weight:800'>mine</span></div>"
                    "<div style='background:#F0FBF6;border:1px solid #CEEADB;border-top:none;padding:22px 24px;border-radius:0 0 8px 8px'>"
                    f"<h2 style='color:#1A3D30;margin:0 0 12px'>Benvenuto su MyMine, {nome}</h2>"
                    "<p>Ecco le tue credenziali di accesso al portale:</p>"
                    "<div style='background:#fff;border:1px solid #CEEADB;border-radius:8px;padding:14px 18px;margin:16px 0;font-family:monospace'>"
                    f"<b>Username:</b> {uname}<br>"
                    f"<b>Password:</b> {pwd}</div>"
                    "<p>Potrai accedere alla tua area personale per visualizzare i dati ambientali "
                    "e gestire le notifiche.</p>"
                    "<div style='background:#fff;border:1px solid #CEEADB;border-radius:8px;padding:12px 16px;margin:14px 0'>"
                    "<p style='margin:0 0 6px;color:#1A3D30;font-weight:600'>Accedi alla tua dashboard:</p>"
                    "<a href='https://mymine.cloud' style='color:#1DB584;font-size:16px;font-weight:700'>"
                    "&#8594; mymine.cloud</a></div>"
                    "<p style='color:#888;font-size:11px;margin-top:16px'>MyMine Srl — info@mymine.io</p>"
                    "</div></div>")
                ok=send_email(c["email"],subject,body_creds)
                self.send_json({"ok":ok,"to":c["email"]})
            except Exception as e:
                self.send_json({"ok":False,"error":str(e)},500)
        elif path=="/api/test_email":
            to=qs.get("to",[SMTP_USER])[0]
            ok=send_email(to,"Test MyMine","<h2 style='color:#1DB584'>✓ Email di test MyMine</h2><p>Se vedi questo messaggio, la configurazione SMTP funziona correttamente.</p>")
            self.send_json({"ok":ok,"to":to,"smtp_user":SMTP_USER,"smtp_host":SMTP_HOST})
        elif path=="/api/debug_alarm":
            threading.Thread(target=check_all_alarms,daemon=True).start()
            _time.sleep(5)
            al=load_alerts()
            clients=load_clients()
            result=[]
            for c in clients:
                eui=c.get("eui","")
                result.append({"nome":c.get("cognome","")+" "+c.get("nome",""),"eui":eui,
                    "soglie":{"t_min":c.get("t_min"),"t_max":c.get("t_max"),"h_min":c.get("h_min"),"h_max":c.get("h_max")},
                    "notif_email":c.get("notif_email"),"email":c.get("email",""),
                    "allarme":al.get(eui.upper(),{})})
            self.send_json(result)
        elif path=="/api/tg_updates":
            if not TG_BOT_TOKEN: self.send_json({"error":"TG_BOT_TOKEN non configurato"}); return
            try:
                url="https://api.telegram.org/bot"+TG_BOT_TOKEN+"/getUpdates"
                req=urllib.request.Request(url)
                with urllib.request.urlopen(req,timeout=10) as r: data=json.loads(r.read())
                ups=[{"chat_id":u["message"]["chat"]["id"],"nome":u["message"]["from"].get("first_name",""),"testo":u["message"].get("text","")} for u in data.get("result",[]) if "message" in u]
                self.send_json({"updates":ups,"hint":"Copia il chat_id nel profilo cliente"})
            except Exception as e: self.send_json({"error":str(e)})
        elif path=="/report":
            ci=qs.get("client",[None])[0]; clients=load_clients()
            client=clients[int(ci)] if ci and ci.isdigit() and int(ci)<len(clients) else None
            if not client: self.send_json({"error":"not found"},404); return
            pdf,err=generate_pdf_report(client)
            if err: self.send_json({"error":err},500); return
            dt=(datetime.now()-timedelta(days=1)).strftime("%Y%m%d")
            fname="mymine_report_"+client["eui"]+"_"+dt+".pdf"
            self.send_response(200)
            self.send_header("Content-Type","application/pdf")
            self.send_header("Content-Disposition","attachment; filename=\""+fname+"\"" )
            self.send_header("Content-Length",str(len(pdf))); self.end_headers(); self.wfile.write(pdf)
        elif path=="/version":
            self.send_json({"version":"3.1","build":BUILD_TS,"alarms":True,"email":True,"telegram":True,"sms":True})
        else: self.send_response(404); self.end_headers()

    def do_PUT(self):
        parts=urlparse(self.path).path.strip("/").split("/")
        if len(parts)==3 and parts[0]=="api" and parts[1]=="clients":
            try:
                idx=int(parts[2])
                length=int(self.headers.get("Content-Length",0))
                raw=self.rfile.read(length)
                updates=json.loads(raw)
                clients=load_clients()
                if 0<=idx<len(clients):
                    # Preserve credentials and server-set fields
                    for keep in ("username","password","_created"):
                        if keep in clients[idx]: updates[keep]=clients[idx][keep]
                    clients[idx]=updates
                    save_clients(clients)
                    print(f"  [OK] Aggiornato idx={idx}: {updates.get('cognome','')} {updates.get('nome','')}")
                    self.send_json({"ok":True})
                else:
                    self.send_json({"ok":False,"error":"indice non valido"},400)
            except Exception as e:
                self.send_json({"ok":False,"error":str(e)},500)
        else: self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path=="/api/import":
            sess=self._get_sess()
            if not sess or sess["role"]!="admin":
                self.send_json({"ok":False,"error":"non autorizzato"},401); return
            try:
                length=int(self.headers.get("Content-Length",0))
                raw=json.loads(self.rfile.read(length))
                imported=raw.get("clients",raw if isinstance(raw,list) else [])
                if not isinstance(imported,list): raise ValueError("formato non valido")
                existing=load_clients()
                by_email={c.get("email","").lower():i for i,c in enumerate(existing)}
                added=0; updated=0
                for c in imported:
                    em=c.get("email","").lower()
                    if em and em in by_email:
                        idx=by_email[em]
                        for k in ("username","password","_created"):
                            if k in existing[idx]: c[k]=existing[idx][k]
                        existing[idx]=c; updated+=1
                    else:
                        existing.append(c); added+=1
                save_clients(existing)
                self.send_json({"ok":True,"added":added,"updated":updated,"total":len(existing)})
            except Exception as e:
                self.send_json({"ok":False,"error":str(e)},500)
            return
        if self.path=="/api/login":
            try:
                length=int(self.headers.get("Content-Length",0))
                body=json.loads(self.rfile.read(length))
                username=body.get("username","").strip()
                password=body.get("password","")
                # Check admin
                if username.lower()==ADMIN_USER.lower() and password==ADMIN_PASS:
                    token=_make_session(ADMIN_USER,"admin")
                    self.send_response(200)
                    self.send_header("Content-Type","application/json")
                    self.send_header("Set-Cookie",f"mm_sess={token}; Path=/; Max-Age=86400; HttpOnly; SameSite=Lax")
                    b=json.dumps({"ok":True,"redirect":"/"}).encode()
                    self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
                    print(f"  [AUTH] Admin login: {username}")
                    return
                # Check clients
                idx, client = _find_client_by_creds(username, password)
                if client is not None:
                    token=_make_session(username,"client",idx)
                    self.send_response(200)
                    self.send_header("Content-Type","application/json")
                    self.send_header("Set-Cookie",f"mm_sess={token}; Path=/; Max-Age=86400; HttpOnly; SameSite=Lax")
                    b=json.dumps({"ok":True,"redirect":f"/dashboard?client={idx}"}).encode()
                    self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
                    print(f"  [AUTH] Client login: {username} idx={idx}")
                    return
                self.send_json({"ok":False,"error":"Email o password non corretti"},401)
            except Exception as e:
                self.send_json({"ok":False,"error":str(e)},500)
            return
        if self.path=="/api/forgot_password":
            try:
                length=int(self.headers.get("Content-Length",0))
                body=json.loads(self.rfile.read(length))
                email=body.get("email","").strip().lower()
                clients=load_clients()
                found=False
                for i,c in enumerate(clients):
                    if c.get("email","").lower()==email or c.get("username","").lower()==email:
                        new_pwd=generate_password(10)
                        clients[i]["password"]=new_pwd
                        save_clients(clients)
                        nome=(c.get("cognome","")+" "+c.get("nome","")).strip()
                        ok=send_email(email,"MyMine — Nuova password",
                            "<div style='font-family:Arial,sans-serif;max-width:500px;margin:0 auto'>"
                            "<div style='background:#1F4E3D;padding:16px 22px;border-radius:8px 8px 0 0'>"
                            "<span style='color:#1DB584;font-weight:800;font-size:18px'>my</span>"
                            "<span style='color:#fff;font-weight:800;font-size:18px'>mine</span></div>"
                            "<div style='background:#F0FBF6;border:1px solid #CEEADB;border-top:none;"
                            "padding:20px 22px;border-radius:0 0 8px 8px'>"
                            f"<p>Ciao {nome},</p>"
                            "<p style='margin-top:10px'>Ecco le tue nuove credenziali:</p>"
                            f"<div style='background:#fff;border:1px solid #CEEADB;border-radius:8px;"
                            f"padding:12px 16px;margin:14px 0;font-family:monospace'>"
                            f"<b>Username:</b> {email}<br><b>Password:</b> {new_pwd}</div>"
                            "<p style='color:#888;font-size:11px'>MyMine Srl</p></div></div>")
                        found=True
                        print(f"  [AUTH] Password reset sent to {email}")
                        break
                self.send_json({"ok":True})
            except Exception as e:
                self.send_json({"ok":True})  # Always return OK (don't leak existence)
            return
        if self.path=="/api/clients":
            try:
                length=int(self.headers.get("Content-Length",0))
                raw=self.rfile.read(length)
                body=json.loads(raw)
                # Generate credentials if not already set
                if not body.get("username"):
                    body["username"] = body.get("email","").lower().strip() or (
                        body.get("cognome","").lower()[:4]+body.get("nome","").lower()[:3])
                if not body.get("password"):
                    body["password"] = generate_password(10)
                clients=load_clients()
                clients.append(body)
                save_clients(clients)
                nome = body.get('cognome','') + ' ' + body.get('nome','')
                print(f"  [OK] Salvato: {nome.strip()} — tot:{len(clients)}")
                self.send_json({"ok":True,"total":len(clients),
                    "username":body["username"],"password":body["password"]})
            except PermissionError as e:
                msg=f"Permesso negato — impossibile scrivere {DATA}"
                print(f"  [ERRORE] {msg}")
                self.send_json({"ok":False,"error":msg},500)
            except Exception as e:
                print(f"  [ERRORE] POST /api/clients: {type(e).__name__}: {e}")
                self.send_json({"ok":False,"error":f"{type(e).__name__}: {e}"},500)
        else: self.send_response(404); self.end_headers()

    def do_DELETE(self):
        parts=urlparse(self.path).path.strip("/").split("/")
        if len(parts)==3 and parts[0]=="api" and parts[1]=="clients":
            clients=load_clients(); idx=int(parts[2])
            if 0<=idx<len(clients): clients.pop(idx); save_clients(clients)
            self.send_json({"ok":True})
        else: self.send_response(404); self.end_headers()

if __name__=="__main__":
    threading.Thread(target=alarm_thread,daemon=True).start()
    threading.Thread(target=daily_report_thread,daemon=True).start()
    srv=http.server.HTTPServer(("0.0.0.0",PORT),Handler)
    print("\n  MyMine Dashboard v3  ->  http://localhost:"+str(PORT))
    print("  Build: "+BUILD_TS)
    print("  Clienti DB : "+DATA)
    try:
        with open(DATA,"a"): pass
        print("  [OK] Scrittura clients.json abilitata")
    except Exception as e:
        print(f"  [ERRORE] Impossibile scrivere {DATA}: {e}")
        print("  >>> Sposta lo script in una cartella scrivibile (Desktop, Home)")
    print("  Controllo allarmi ogni "+str(ALERT_INTERVAL//60)+" minuti")
    if not SMTP_USER: print("  [!] Configura SMTP_USER e SMTP_PASS per le email")
    elif SMTP_PASS and ('!' in SMTP_PASS or '?' in SMTP_PASS or len(SMTP_PASS)<12):
        print("  [!] ATTENZIONE: SMTP_PASS non sembra una App Password Gmail valida")
        print("      Le App Password Gmail sono 16 lettere senza spazi (es: abcdefghijklmnop)")
        print("      Generala da: myaccount.google.com > Sicurezza > Password per le app")
    if not TWILIO_ACCOUNT_SID: print("  [!] Configura TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM_NUMBER per SMS")
    print("  CTRL+C per fermare\n")
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\n  Fermato."); sys.exit(0)
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
BUILD_TS    = '2026-03-17 13:09:19'
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

# SMS via SMSAPI (https://smsapi.com — provider europeo, semplice e affidabile)
# 1. Registrati su smsapi.com e ricarica il credito
# 2. Vai su Customer Portal > API > OAuth Tokens > genera token con scope "sms"
# 3. Imposta SMSAPI_SENDER (max 11 caratteri alfanumerici, es. "MyMine")
# 4. Il numero del cliente va salvato in formato internazionale: +393331234567
SMSAPI_TOKEN  = _os.environ.get("SMSAPI_TOKEN", "")
SMSAPI_SENDER = _os.environ.get("SMSAPI_SENDER", "MyMine")
# Mantieni anche Twilio per retrocompatibilità (non più usato)
TWILIO_ACCOUNT_SID = _os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = _os.environ.get("TWILIO_AUTH_TOKEN",  "")
TWILIO_FROM_NUMBER = _os.environ.get("TWILIO_FROM_NUMBER", "")

ALERT_INTERVAL = 600

# ─── PostgreSQL (backup automatico clienti) ──────────────────────────────────
DATABASE_URL = _os.environ.get("DATABASE_URL", "")

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

# ─── PostgreSQL helpers ──────────────────────────────────────────────────────
def _pg_conn():
    """Restituisce una connessione psycopg2 o None se non disponibile."""
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, connect_timeout=5)
    except Exception as e:
        print(f"  [DB] Connessione fallita: {e}")
        return None

def _pg_init():
    """Crea la tabella clients se non esiste."""
    conn = _pg_conn()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    id SERIAL PRIMARY KEY,
                    data JSONB NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW()
                )""")
            conn.commit()
        print("  [DB] Tabella clients OK")
    except Exception as e:
        print(f"  [DB] Init tabella: {e}")
    finally:
        conn.close()

def _pg_save(lst):
    """Sovrascrive i clienti nel DB con la lista corrente."""
    conn = _pg_conn()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM clients")
            for c in lst:
                cur.execute(
                    "INSERT INTO clients (data) VALUES (%s)",
                    (json.dumps(c, ensure_ascii=False),)
                )
        conn.commit()
    except Exception as e:
        print(f"  [DB] Salvataggio: {e}")
    finally:
        conn.close()

def _pg_load():
    """Carica clienti dal DB. Ritorna [] in caso di errore."""
    conn = _pg_conn()
    if not conn: return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM clients ORDER BY id")
            rows = cur.fetchall()
        return [r[0] for r in rows] if rows else []
    except Exception as e:
        print(f"  [DB] Caricamento: {e}")
        return []
    finally:
        conn.close()

def _migrate(clients):
    """Migra vecchio formato eui → sensori[]."""
    changed = False
    for c in clients:
        if "eui" in c and "sensori" not in c:
            c["sensori"] = [{"eui": c["eui"], "nome_frigo": c.get("nome_frigo","")}]
            changed = True
    return clients, changed

def load_clients():
    # 1. Prova file locale
    if os.path.exists(DATA):
        with open(DATA) as f:
            content = f.read().strip()
        if content:
            try:
                clients = json.loads(content)
                clients, changed = _migrate(clients)
                if changed: save_clients(clients)
                return clients
            except:
                pass
    # 2. File locale vuoto/assente → ripristina da PostgreSQL
    if DATABASE_URL:
        print("  [DB] File locale assente — ripristino da PostgreSQL...")
        clients = _pg_load()
        if clients:
            clients, _ = _migrate(clients)
            # Salva localmente per le prossime chiamate
            try:
                with open(DATA,"w") as f:
                    json.dump(clients, f, indent=2, ensure_ascii=False)
                print(f"  [DB] Ripristinati {len(clients)} clienti da PostgreSQL")
            except Exception as e:
                print(f"  [DB] Impossibile salvare file locale: {e}")
            return clients
    return []

def save_clients(lst):
    # Salva su file locale
    with open(DATA,"w") as f:
        json.dump(lst, f, indent=2, ensure_ascii=False)
    # Sincronizza su PostgreSQL in background
    if DATABASE_URL:
        import threading
        threading.Thread(target=_pg_save, args=(lst,), daemon=True).start()

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

def _fetch_frames(dev_id, date_obj):
    """Fetch frames for a given date. Returns sorted list of parsed rows."""
    ds = date_obj.strftime("%Y-%m-%d")
    body, code = call_api(f"/frame/{dev_id}/{ds}T00:00:00/{ds}T23:59:59")
    if code != 200:
        body, code = call_api(f"/frame/days/{dev_id}/2")
    frames_raw = json.loads(body)
    if isinstance(frames_raw, dict):
        frames_raw = frames_raw.get("frames") or frames_raw.get("data") or frames_raw.get("items") or []
    rows = []
    for f in frames_raw:
        try:
            ts_str = f.get("time_created") or f.get("time") or f.get("created_at","")
            ts = datetime.fromisoformat(ts_str.replace("Z","+00:00")).astimezone()
            p = _get_payload(f)
            T = _get_val(p,"temperature","temp")
            H = _get_val(p,"humidity","hum")
            rows.append({"ts": ts, "T": T, "H": H})
        except: pass
    rows.sort(key=lambda r: r["ts"])
    return rows

def _rows_ogni_4h(rows, target_date, sensore_nome):
    """Campiona una misurazione ogni 4 ore (00,04,08,12,16,20) dalla lista raw."""
    MESI_IT = ["","Gennaio","Febbraio","Marzo","Aprile","Maggio","Giugno",
               "Luglio","Agosto","Settembre","Ottobre","Novembre","Dicembre"]
    slots = [0, 4, 8, 12, 16, 20]
    result = []
    for slot in slots:
        # Prendi la misurazione più vicina all'ora slot
        candidates = [r for r in rows
                      if r["ts"].date() == target_date and abs(r["ts"].hour - slot) <= 2]
        if candidates:
            best = min(candidates, key=lambda r: abs(r["ts"].hour - slot))
            result.append({
                "giorno": target_date.strftime("%d/%m/%Y"),
                "ora":    best["ts"].strftime("%H:%M"),
                "sensore": sensore_nome,
                "T":      best.get("T"),
                "H":      best.get("H"),
            })
        else:
            result.append({
                "giorno": target_date.strftime("%d/%m/%Y"),
                "ora":    f"{slot:02d}:00",
                "sensore": sensore_nome,
                "T": None, "H": None,
            })
    return result

def generate_pdf_report(client, tipo="giornaliero"):
    """
    Genera PDF HACCP.
    tipo='giornaliero': misurazioni di ieri ogni 4 ore (inviato alle 09:00)
    tipo='mensile': misurazioni del mese scorso ogni 4 ore (inviato il 1° del mese)
    """
    try:
        body, code = call_api("/device/")
        if code != 200: return None, f"API error {code}"
        devs = json.loads(body)
        MESI_IT = ["","Gennaio","Febbraio","Marzo","Aprile","Maggio","Giugno",
                   "Luglio","Agosto","Settembre","Ottobre","Novembre","Dicembre"]
        pdfs = []  # un PDF per ogni sensore
        for _si_idx, _s in enumerate(client.get("sensori", [{"eui": client.get("eui","")}])):
            _eui = _s.get("eui","").upper()
            dev = next((d for d in devs if (d.get("dev_eui","")).upper() == _eui), None)
            if not dev: continue
            dev_id = dev["id"]
            nome   = (client.get("cognome","") + " " + client.get("nome","")).strip()
            sname  = _s.get("nome_frigo", _eui[-6:])

            if tipo == "giornaliero":
                yday = (datetime.now() - timedelta(days=1)).date()
                rows = _fetch_frames(dev_id, yday)
                rows_4h = _rows_ogni_4h(rows, yday, sname)
                mese_anno = MESI_IT[yday.month] + " " + str(yday.year)
            else:  # mensile
                first_today = datetime.now().date().replace(day=1)
                last_month_end = first_today - timedelta(days=1)
                last_month_start = last_month_end.replace(day=1)
                rows_4h = []
                d = last_month_start
                while d <= last_month_end:
                    day_rows = _fetch_frames(dev_id, d)
                    rows_4h.extend(_rows_ogni_4h(day_rows, d, sname))
                    d += timedelta(days=1)
                mese_anno = MESI_IT[last_month_end.month] + " " + str(last_month_end.year)

            pdf = _build_pdf(nome, client, mese_anno, rows_4h, mese_anno, tipo)
            pdfs.append((pdf, sname))

        if not pdfs: return None, "Nessun sensore trovato"
        return pdfs[0][0], None  # per compatibilità ritorna il primo
    except Exception as e:
        import traceback
        print(f"  [PDF] errore: {e}\n{traceback.format_exc()}")
        return None, str(e)

def _build_pdf(nome, client, date_str, rows_4h, mese_anno, tipo="giornaliero"):
    """
    Genera PDF Registro HACCP.
    rows_4h: lista di dict {ora: "HH:MM", sensore: str, T: float|None, H: float|None}
    mese_anno: stringa es. "Marzo 2026"
    tipo: "giornaliero" o "mensile"
    """
    def esc(s):
        s = str(s).replace("\\","\\\\").replace("(","\\(").replace(")","\\)")
        return s.encode("latin-1", errors="replace").decode("latin-1")
    def fmt(v, d=1):
        return f"{v:.{d}f}" if v is not None else ""

    W, H   = 595, 842    # A4
    ML, MR = 30, 30
    MT     = 820         # top y
    C_BLK  = "0 0 0"
    C_GRY  = "0.5 0.5 0.5"
    C_LGR  = "0.85 0.85 0.85"
    C_VLG  = "0.95 0.95 0.95"
    C_RED  = "0.8 0 0"

    ops = []
    def g(s): ops.append(s)

    def rect(x, y, w, h, color, fill=True):
        g(f"{color} {'rg' if fill else 'RG'}")
        g(f"{x:.1f} {y:.1f} {w:.1f} {h:.1f} re {'f' if fill else 'S'}")

    def line(x1, y1, x2, y2, width=0.5, color="0 0 0"):
        g(f"{width} w {color} RG")
        g(f"{x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    def txt(x, y, font, size, color, text, align="left"):
        if align == "center":
            # approximate centering
            g(f"BT /{font} {size} Tf")
            g(f"{color} rg")
            g(f"{x:.1f} {y:.1f} Td ({esc(text)}) Tj ET")
        else:
            g(f"BT /{font} {size} Tf {color} rg {x:.1f} {y:.1f} Td ({esc(text)}) Tj ET")

    def hrule(y, lw=0.5):
        line(ML, y, W-MR, y, lw)

    # ─── TITOLO ──────────────────────────────────────────
    y = MT - 10
    txt(ML, y, "F2", 14, C_BLK, "REGISTRO CONTROLLO TEMPERATURE FRIGORIFERI")
    y -= 16
    txt(ML, y, "F1", 10, C_GRY, "Sistema HACCP - Conformita D.Lgs. 193/2007")
    y -= 8
    hrule(y, 1.5)

    # ─── DATI ANAGRAFICI ─────────────────────────────────
    y -= 14
    bh = 52
    rect(ML, y-bh, W-ML-MR, bh, C_VLG)
    rect(ML, y-bh, W-ML-MR, bh, C_BLK, False)
    txt(ML+5, y-13,  "F2", 9, C_BLK, "DATI OPERATORE / AZIENDA")
    rag_soc = nome
    addr    = client.get("indirizzo","")
    city    = " ".join(filter(None, [client.get("cap",""), client.get("citta",""), client.get("provincia","")]))
    piva    = client.get("piva","")
    tel     = client.get("telefono","")
    txt(ML+5, y-26,  "F1", 9, C_BLK, f"Ragione Sociale: {rag_soc}")
    txt(ML+5, y-38,  "F1", 9, C_BLK, f"Indirizzo: {addr}{('  -  ' + city) if city else ''}")
    txt(ML+5, y-50,  "F1", 9, C_BLK, f"P.IVA: {piva}   Tel: {tel}")

    # ─── MESE/ANNO ───────────────────────────────────────
    y -= bh + 10
    txt((W//2)-60, y, "F2", 18, C_BLK, mese_anno)
    y -= 6
    hrule(y, 0.8)

    # ─── BOX TEMPERATURE DI RIFERIMENTO ──────────────────
    y -= 12
    bref = 28
    rect(ML, y-bref, W-ML-MR, bref, "0.9 0.95 1.0")
    rect(ML, y-bref, W-ML-MR, bref, C_BLK, False)
    txt(ML+5, y-12, "F2", 8, C_BLK, "TEMPERATURE DI RIFERIMENTO:")
    ref_line = ("Prodotti Freschi: 0 grC / +4 grC   |   "
                "Prodotti Surgelati: -18 grC (+-3 grC)   |   "
                "Prodotti Congelati: -12 grC")
    txt(ML+5, y-24, "F1", 8, "0.1 0.1 0.5", ref_line)

    # ─── INTESTAZIONE TABELLA ────────────────────────────
    y -= bref + 8
    ROW_H  = 16
    # Colonne: Giorno | Ora | Sensore | Temperatura | Umidita | Note/Anomalie | Azioni correttive
    CW = [38, 32, 65, 55, 42, 120, 120]  # larghezze
    LABELS = ["Giorno","Ora","Sensore","Temp. (grC)","Umid. (%)","Note / Anomalie","Azioni Correttive"]
    cx = ML
    rect(ML, y-ROW_H, W-ML-MR, ROW_H, C_LGR)
    rect(ML, y-ROW_H, W-ML-MR, ROW_H, C_BLK, False)
    for i,(lbl,cw) in enumerate(zip(LABELS, CW)):
        txt(cx+2, y-12, "F2", 7, C_BLK, lbl)
        if i < len(CW)-1:
            line(cx+cw, y, cx+cw, y-ROW_H, 0.3)
        cx += cw

    # ─── RIGHE DATI ──────────────────────────────────────
    for ri, row in enumerate(rows_4h):
        y -= ROW_H
        if y < 90:
            break
        bg = C_VLG if ri % 2 == 0 else "1 1 1"
        # Evidenzia allarmi
        T_val = row.get("T")
        _si_obj = row.get("_sens", {})
        t_min = _si_obj.get("t_min") if isinstance(_si_obj, dict) else None
        t_max = _si_obj.get("t_max") if isinstance(_si_obj, dict) else None
        alarm = (T_val is not None and (
            (t_min is not None and T_val < t_min) or
            (t_max is not None and T_val > t_max)))
        if alarm: bg = "1 0.85 0.85"
        rect(ML, y-ROW_H+1, W-ML-MR, ROW_H-1, bg)
        # border
        line(ML, y-ROW_H+1, W-MR, y-ROW_H+1, 0.2, C_LGR)

        vals = [
            row.get("giorno",""),
            row.get("ora",""),
            row.get("sensore",""),
            fmt(T_val) if T_val is not None else "",
            fmt(row.get("H"),0) if row.get("H") is not None else "",
            "",  # Note (vuoto, compilato a mano)
            "",  # Azioni (vuoto, compilato a mano)
        ]
        cx = ML
        for i,(v,cw) in enumerate(zip(vals, CW)):
            col = C_RED if (alarm and i==3) else C_BLK
            txt(cx+2, y-12, "F1", 8, col, v)
            if i < len(CW)-1:
                line(cx+cw, y, cx+cw, y-ROW_H+1, 0.2, C_GRY)
            cx += cw

    # ─── FOOTER FIRME ────────────────────────────────────
    fy = 75
    hrule(fy+2, 0.8)
    # Sinistra
    txt(ML, fy-8,  "F1", 8, C_BLK, "Data compilazione: ___/___/_______")
    txt(ML, fy-22, "F1", 8, C_BLK, "Firma Responsabile HACCP: _________________________")
    # Destra
    rx = W//2 + 10
    txt(rx, fy-8,  "F1", 8, C_BLK, "Data controllo ASL: ___/___/_______")
    txt(rx, fy-22, "F1", 8, C_BLK, "Firma Ispettore ASL: _________________________")

    # ─── NOTA IMPORTANTE ─────────────────────────────────
    hrule(fy-30, 0.5)
    nota = ("NOTA IMPORTANTE: Questo registro deve essere conservato per almeno 12 mesi dalla data di compilazione. "
            "In caso di temperature fuori norma, annotare immediatamente le azioni correttive e informare il Responsabile HACCP.")
    txt(ML, fy-42, "F1", 7, C_GRY, nota)

    # ─── PIEDE PAGINA ────────────────────────────────────
    txt(ML,      fy-55, "F1", 6, C_GRY, "MyMine Srl  -  P.IVA IT12038850967  -  info@mymine.io  -  Sistema HACCP IoT")
    txt(W-MR-40, fy-55, "F1", 6, C_GRY, "Pag. 1/1")

    # ─── ASSEMBLE PDF ────────────────────────────────────
    stream_str   = "\n".join(ops)
    stream_bytes = stream_str.encode("latin-1", errors="replace")

    objs = []
    def obj(n, header, payload=None):
        objs.append((n, header, payload))

    obj(1, "<< /Type /Catalog /Pages 2 0 R >>")
    obj(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    obj(3, (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] "
            f"/Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>"))
    obj(4, f"<< /Length {len(stream_bytes)} >>", stream_bytes)
    obj(5, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    obj(6, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")

    buf     = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
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
    """Invia SMS tramite SMSAPI (Bearer token OAuth2)."""
    if not SMSAPI_TOKEN:
        print(f"  [SMS] SMSAPI_TOKEN non configurato")
        return False
    try:
        phone = _normalize_phone(to_number)
        body  = _ascii_sms(message)
        params = {
            "to":      phone,
            "message": body,
            "format":  "json"
        }
        # Usa sender solo se configurato, altrimenti SMSAPI usa il default "Test"
        if SMSAPI_SENDER:
            params["from"] = SMSAPI_SENDER
        data = _uparse.urlencode(params).encode("utf-8")
        req  = urllib.request.Request(
            "https://api.smsapi.com/sms.do", data=data,
            headers={"Authorization": f"Bearer {SMSAPI_TOKEN}",
                     "Content-Type":  "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw_resp = r.read()
        print(f"  [SMS] Risposta SMSAPI raw: {raw_resp[:200]}")
        try:
            resp = json.loads(raw_resp)
        except Exception:
            resp = raw_resp.decode("utf-8","replace").strip()
        # SMSAPI può restituire: intero (codice errore), dict con error/list, o stringa
        SMSAPI_ERRORS = {
            1:"Autorizzazione non valida",2:"Autorizzazione non valida",
            4:"Credito insufficiente",8:"Numero di telefono non valido",
            13:"Sender non trovato",14:"Sender non approvato — usa SMSAPI_SENDER=Test o approva il sender",
            101:"Token non valido o scaduto — rigenera su smsapi.com > OAuth Tokens",
            103:"Indirizzo IP non autorizzato",
        }
        if isinstance(resp, int):
            msg = SMSAPI_ERRORS.get(resp, f"Codice errore sconosciuto: {resp}")
            print(f"  [SMS] Errore SMSAPI {resp}: {msg}")
            return False
        if isinstance(resp, dict):
            err = resp.get("error")
            if err:
                if isinstance(err, dict):
                    code = err.get("code","?"); emsg = err.get("message", SMSAPI_ERRORS.get(code,"?"))
                elif isinstance(err, int):
                    code = err; emsg = SMSAPI_ERRORS.get(code, f"Codice {code}")
                else:
                    code = "?"; emsg = str(err)
                print(f"  [SMS] Errore SMSAPI {code}: {emsg}")
                return False
            if resp.get("invalid_numbers"):
                print(f"  [SMS] Numero non valido: {phone}")
                return False
            lst    = resp.get("list") or [{}]
            sid    = lst[0].get("id","?") if lst else "?"
            status = lst[0].get("status","?") if lst else "?"
            print(f"  [SMS] OK to={phone} id={sid} status={status}")
            return True
        print(f"  [SMS] Risposta inattesa: {resp}")
        return False
    except urllib.error.HTTPError as e:
        bd = e.read().decode()
        print(f"  [SMS] HTTP {e.code}: {bd[:300]}")
        if e.code == 401:
            print(f"  [SMS] Token non valido — rigenera il token su smsapi.com > OAuth Tokens")
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
        if client.get("notif_sms") and client.get("telefono") and SMSAPI_TOKEN:
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

def monthly_report_thread():
    """Invia report mensile HACCP il 1° di ogni mese alle 06:00."""
    import time as _t
    try:
        from zoneinfo import ZoneInfo
        _ROME = ZoneInfo("Europe/Rome")
    except Exception:
        _ROME = None
    def _now():
        if _ROME: return datetime.now(_ROME).replace(tzinfo=None)
        return datetime.utcnow() + timedelta(hours=1)
    while True:
        now = _now()
        # Next 1st of month at 06:00
        if now.day == 1 and now.hour < 6:
            target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        else:
            # Go to next month
            if now.month == 12:
                target = now.replace(year=now.year+1, month=1, day=1, hour=6, minute=0, second=0, microsecond=0)
            else:
                target = now.replace(month=now.month+1, day=1, hour=6, minute=0, second=0, microsecond=0)
        wait = (target - now).total_seconds()
        print(f"  [REPORT MENSILE] Prossimo invio: {target.strftime('%Y-%m-%d 06:00')} (ora italiana)")
        _t.sleep(wait)
        try:
            send_monthly_reports()
        except Exception as e:
            print(f"  [REPORT MENSILE] errore: {e}")

def backup_thread():
    """Invia backup automatico clients.json via email ogni notte alle 02:00."""
    import time as _t, json as _json
    try:
        from zoneinfo import ZoneInfo
        _ROME = ZoneInfo("Europe/Rome")
    except Exception:
        _ROME = None

    def _now():
        if _ROME:
            return datetime.now(_ROME).replace(tzinfo=None)
        return datetime.utcnow() + timedelta(hours=1)

    while True:
        now = _now()
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        _t.sleep(wait)
        if not SMTP_USER or not SMTP_PASS or not ADMIN_USER:
            continue
        try:
            clients = load_clients()
            if not clients:
                continue
            data_json = _json.dumps({"clients": clients,
                "exported_at": datetime.now().isoformat(),
                "version": BUILD_TS}, indent=2, ensure_ascii=False)
            ts = datetime.now().strftime("%Y-%m-%d")
            subject = f"MyMine — Backup automatico clienti {ts}"
            body_html = f"""<html><body>
<p>Backup automatico notturno del database clienti MyMine.</p>
<p><b>Data:</b> {ts}<br>
<b>Clienti:</b> {len(clients)}<br>
<b>Versione server:</b> {BUILD_TS}</p>
<p>Il file JSON allegato contiene tutti i dati clienti.<br>
Per ripristinare: pannello admin → ⬆ Importa clienti.</p>
<hr><small>MyMine Dashboard — backup automatico</small>
</body></html>"""
            # Send with attachment
            import email.mime.multipart as _mime_m
            import email.mime.text as _mime_t
            import email.mime.base as _mime_b
            import email.encoders as _enc
            msg = _mime_m.MIMEMultipart()
            msg["From"] = SMTP_USER
            msg["To"] = ADMIN_USER
            msg["Subject"] = subject
            msg.attach(_mime_t.MIMEText(body_html, "html", "utf-8"))
            part = _mime_b.MIMEBase("application", "json")
            part.set_payload(data_json.encode("utf-8"))
            _enc.encode_base64(part)
            part.add_header("Content-Disposition",
                f'attachment; filename="mymine_backup_{ts}.json"')
            msg.attach(part)
            import smtplib as _smtp2
            port = int(SMTP_PORT) if SMTP_PORT else 587
            with _smtp2.SMTP(SMTP_HOST, port, timeout=30) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
            print(f"  [BACKUP] ✓ Backup inviato a {ADMIN_USER} ({len(clients)} clienti)")
        except Exception as e:
            print(f"  [BACKUP] errore invio: {e}")

def _send_haccp_report(c, tipo="giornaliero"):
    """Genera e invia PDF HACCP per tutti i sensori di un cliente."""
    if not c.get("email") or not c.get("notif_email"):
        return
    MESI_IT = ["","Gennaio","Febbraio","Marzo","Aprile","Maggio","Giugno",
               "Luglio","Agosto","Settembre","Ottobre","Novembre","Dicembre"]
    today = datetime.now()
    if tipo == "giornaliero":
        yday = (today - timedelta(days=1)).date()
        period = yday.strftime("%d/%m/%Y")
        mese_anno = MESI_IT[yday.month] + " " + str(yday.year)
    else:
        first = today.date().replace(day=1)
        lme = first - timedelta(days=1)
        period = MESI_IT[lme.month] + " " + str(lme.year)
        mese_anno = period
    nome = (c.get("cognome","") + " " + c.get("nome","")).strip()
    try:
        body, code = call_api("/device/")
        if code != 200: return
        devs = json.loads(body)
        for _s in c.get("sensori", [{"eui": c.get("eui","")}]):
            _eui  = _s.get("eui","").upper()
            sname = _s.get("nome_frigo", _eui[-6:])
            dev   = next((d for d in devs if (d.get("dev_eui","")).upper()==_eui), None)
            if not dev: continue
            dev_id = dev["id"]
            if tipo == "giornaliero":
                yday = (today - timedelta(days=1)).date()
                rows = _fetch_frames(dev_id, yday)
                rows_4h = _rows_ogni_4h(rows, yday, sname)
            else:
                first = today.date().replace(day=1)
                lms = (first - timedelta(days=1)).replace(day=1)
                lme = first - timedelta(days=1)
                rows_4h = []
                d = lms
                while d <= lme:
                    rows_4h.extend(_rows_ogni_4h(_fetch_frames(dev_id, d), d, sname))
                    d += timedelta(days=1)
            pdf = _build_pdf(nome, c, mese_anno, rows_4h, mese_anno, tipo)
            subject = f"MyMine HACCP - Registro {sname} - {period}"
            tipo_label = "giornaliero" if tipo=="giornaliero" else "mensile"
            body_html = (
                "<div style='font-family:Arial,sans-serif;max-width:580px;margin:0 auto'>"
                "<div style='background:#1F4E3D;padding:18px 24px;border-radius:8px 8px 0 0'>"
                "<span style='color:#1DB584;font-size:20px;font-weight:800'>my</span>"
                "<span style='color:#fff;font-size:20px;font-weight:800'>mine</span></div>"
                "<div style='background:#F0FBF6;border:1px solid #CEEADB;border-top:none;padding:22px 24px;border-radius:0 0 8px 8px'>"
                f"<h2 style='color:#1A3D30;margin:0 0 12px'>Registro HACCP {tipo_label} — {period}</h2>"
                f"<p><b>Cliente:</b> {nome}</p>"
                f"<p><b>Frigorifero:</b> {sname}</p>"
                f"<p><b>Indirizzo:</b> {c.get('indirizzo','')}</p>"
                f"<p style='color:#4E7367;margin-top:12px'>In allegato il registro con misurazioni ogni 4 ore.</p>"
                "</div></div>")
            fn = f"HACCP_{sname.replace(' ','_')}_{period.replace('/','_').replace(' ','_')}.pdf"
            send_email_with_attachment(c["email"], subject, body_html, pdf, fn)
            print(f"  [REPORT] {tipo} inviato a {c['email']} ({nome} - {sname})")
    except Exception as e:
        print(f"  [REPORT] errore {tipo} per {c.get('cognome','')}: {e}")

def send_daily_reports():
    clients = load_clients()
    yday = (datetime.now() - timedelta(days=1)).strftime("%d/%m/%Y")
    print(f"  [REPORT] Invio report giornaliero del {yday} a {len(clients)} clienti...")
    for c in clients:
        _send_haccp_report(c, "giornaliero")

def send_monthly_reports():
    clients = load_clients()
    print(f"  [REPORT] Invio report mensile a {len(clients)} clienti...")
    for c in clients:
        _send_haccp_report(c, "mensile")

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
    <div class="field"><label>Indirizzo installazione</label><input id="fAddr" placeholder="Via Roma, 1"></div>
    <div class="row4">
      <div class="field"><label>CAP</label><input id="fCap" placeholder="20100" maxlength="5"></div>
      <div class="field"><label>Città</label><input id="fCitta" placeholder="Milano"></div>
      <div class="field"><label>Provincia</label><input id="fProv" placeholder="MI" maxlength="2" style="text-transform:uppercase"></div>
    </div>
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
        <span class="tlabel">SMS (SMSAPI)</span>
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
    cap:g('fCap'), citta:g('fCitta'), provincia:g('fProv').toUpperCase(),
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
  ['fNome','fCognome','fPiva','fEmail','fTel','fAddr','fCap','fCitta','fProv']
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
    msg+='  SMSAPI token: '+j.details.smsapi_token+nl;
    msg+='  SMSAPI sender: '+j.details.smsapi_sender;
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
  g('fCap').value=c.cap||'';
  g('fCitta').value=c.citta||'';
  g('fProv').value=c.provincia||'';
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
    cap:g('fCap'), citta:g('fCitta'), provincia:g('fProv').toUpperCase(),
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
    """Invia SMS tramite SMSAPI (Bearer token OAuth2)."""
    if not SMSAPI_TOKEN:
        print(f"  [SMS] SMSAPI_TOKEN non configurato")
        return False
    try:
        phone = _normalize_phone(to_number)
        body  = _ascii_sms(message)
        params = {
            "to":      phone,
            "message": body,
            "format":  "json"
        }
        # Usa sender solo se configurato, altrimenti SMSAPI usa il default "Test"
        if SMSAPI_SENDER:
            params["from"] = SMSAPI_SENDER
        data = _uparse.urlencode(params).encode("utf-8")
        req  = urllib.request.Request(
            "https://api.smsapi.com/sms.do", data=data,
            headers={"Authorization": f"Bearer {SMSAPI_TOKEN}",
                     "Content-Type":  "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw_resp = r.read()
        print(f"  [SMS] Risposta SMSAPI raw: {raw_resp[:200]}")
        try:
            resp = json.loads(raw_resp)
        except Exception:
            resp = raw_resp.decode("utf-8","replace").strip()
        # SMSAPI può restituire: intero (codice errore), dict con error/list, o stringa
        SMSAPI_ERRORS = {
            1:"Autorizzazione non valida",2:"Autorizzazione non valida",
            4:"Credito insufficiente",8:"Numero di telefono non valido",
            13:"Sender non trovato",14:"Sender non approvato — usa SMSAPI_SENDER=Test o approva il sender",
            101:"Token non valido o scaduto — rigenera su smsapi.com > OAuth Tokens",
            103:"Indirizzo IP non autorizzato",
        }
        if isinstance(resp, int):
            msg = SMSAPI_ERRORS.get(resp, f"Codice errore sconosciuto: {resp}")
            print(f"  [SMS] Errore SMSAPI {resp}: {msg}")
            return False
        if isinstance(resp, dict):
            err = resp.get("error")
            if err:
                if isinstance(err, dict):
                    code = err.get("code","?"); emsg = err.get("message", SMSAPI_ERRORS.get(code,"?"))
                elif isinstance(err, int):
                    code = err; emsg = SMSAPI_ERRORS.get(code, f"Codice {code}")
                else:
                    code = "?"; emsg = str(err)
                print(f"  [SMS] Errore SMSAPI {code}: {emsg}")
                return False
            if resp.get("invalid_numbers"):
                print(f"  [SMS] Numero non valido: {phone}")
                return False
            lst    = resp.get("list") or [{}]
            sid    = lst[0].get("id","?") if lst else "?"
            status = lst[0].get("status","?") if lst else "?"
            print(f"  [SMS] OK to={phone} id={sid} status={status}")
            return True
        print(f"  [SMS] Risposta inattesa: {resp}")
        return False
    except urllib.error.HTTPError as e:
        bd = e.read().decode()
        print(f"  [SMS] HTTP {e.code}: {bd[:300]}")
        if e.code == 401:
            print(f"  [SMS] Token non valido — rigenera il token su smsapi.com > OAuth Tokens")
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
        if client.get("notif_sms") and client.get("telefono") and SMSAPI_TOKEN:
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

def backup_thread():
    """Invia backup automatico clients.json via email ogni notte alle 02:00."""
    import time as _t, json as _json
    try:
        from zoneinfo import ZoneInfo
        _ROME = ZoneInfo("Europe/Rome")
    except Exception:
        _ROME = None

    def _now():
        if _ROME:
            return datetime.now(_ROME).replace(tzinfo=None)
        return datetime.utcnow() + timedelta(hours=1)

    while True:
        now = _now()
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        _t.sleep(wait)
        if not SMTP_USER or not SMTP_PASS or not ADMIN_USER:
            continue
        try:
            clients = load_clients()
            if not clients:
                continue
            data_json = _json.dumps({"clients": clients,
                "exported_at": datetime.now().isoformat(),
                "version": BUILD_TS}, indent=2, ensure_ascii=False)
            ts = datetime.now().strftime("%Y-%m-%d")
            subject = f"MyMine — Backup automatico clienti {ts}"
            body_html = f"""<html><body>
<p>Backup automatico notturno del database clienti MyMine.</p>
<p><b>Data:</b> {ts}<br>
<b>Clienti:</b> {len(clients)}<br>
<b>Versione server:</b> {BUILD_TS}</p>
<p>Il file JSON allegato contiene tutti i dati clienti.<br>
Per ripristinare: pannello admin → ⬆ Importa clienti.</p>
<hr><small>MyMine Dashboard — backup automatico</small>
</body></html>"""
            # Send with attachment
            import email.mime.multipart as _mime_m
            import email.mime.text as _mime_t
            import email.mime.base as _mime_b
            import email.encoders as _enc
            msg = _mime_m.MIMEMultipart()
            msg["From"] = SMTP_USER
            msg["To"] = ADMIN_USER
            msg["Subject"] = subject
            msg.attach(_mime_t.MIMEText(body_html, "html", "utf-8"))
            part = _mime_b.MIMEBase("application", "json")
            part.set_payload(data_json.encode("utf-8"))
            _enc.encode_base64(part)
            part.add_header("Content-Disposition",
                f'attachment; filename="mymine_backup_{ts}.json"')
            msg.attach(part)
            import smtplib as _smtp2
            port = int(SMTP_PORT) if SMTP_PORT else 587
            with _smtp2.SMTP(SMTP_HOST, port, timeout=30) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
            print(f"  [BACKUP] ✓ Backup inviato a {ADMIN_USER} ({len(clients)} clienti)")
        except Exception as e:
            print(f"  [BACKUP] errore invio: {e}")

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
                if not SMSAPI_TOKEN:
                    result["sms"] = {"ok": False, "error": "SMSAPI_TOKEN non configurato"}
                else:
                    try:
                        phone_n = _normalize_phone(to_phone)
                        body_t  = _ascii_sms("Test allarme MyMine - se ricevi questo messaggio gli SMS funzionano.")
                        url_t   = "https://api.smsapi.com/sms.do"
                        data_t  = _uparse.urlencode({
                            "to": phone_n, "from": SMSAPI_SENDER,
                            "message": body_t, "format": "json"
                        }).encode("utf-8")
                        req_t = urllib.request.Request(url_t, data=data_t, headers={
                            "Authorization": f"Bearer {SMSAPI_TOKEN}",
                            "Content-Type": "application/x-www-form-urlencoded"
                        })
                        with urllib.request.urlopen(req_t, timeout=20) as r_t:
                            raw_t = r_t.read()
                        try: resp_t = json.loads(raw_t)
                        except: resp_t = raw_t.decode("utf-8","replace").strip()
                        ERRS = {1:"Auth non valida",4:"Credito insufficiente",8:"Numero non valido",
                                13:"Sender non trovato",14:"Sender non approvato — prova SMSAPI_SENDER=Test",
                                101:"Token non valido — rigenera su smsapi.com > OAuth Tokens"}
                        if isinstance(resp_t, int):
                            result["sms"] = {"ok": False, "error": f"SMSAPI errore {resp_t}: {ERRS.get(resp_t, 'codice sconosciuto')}"}
                        elif isinstance(resp_t, dict) and resp_t.get("error"):
                            err_t = resp_t["error"]
                            if isinstance(err_t, int): ec,em = err_t, ERRS.get(err_t,"?")
                            elif isinstance(err_t, dict): ec,em = err_t.get("code","?"),err_t.get("message","?")
                            else: ec,em = "?", str(err_t)
                            result["sms"] = {"ok": False, "error": f"SMSAPI errore {ec}: {em}"}
                        elif isinstance(resp_t, dict) and resp_t.get("list"):
                            lst_t = resp_t["list"]
                            result["sms"] = {"ok": True, "to": phone_n,
                                "id": lst_t[0].get("id","?"), "status": lst_t[0].get("status","?")}
                            print(f"  [TEST] ✓ SMS inviato a {phone_n}")
                        else:
                            result["sms"] = {"ok": False, "error": f"Risposta inattesa: {str(resp_t)[:200]}"}
                    except urllib.error.HTTPError as e_t:
                        bd_t = e_t.read().decode()
                        result["sms"] = {"ok": False, "error": f"HTTP {e_t.code}: {bd_t[:200]}"}
                    except Exception as e_t:
                        result["sms"] = {"ok": False, "error": str(e_t)}
            # Config summary
            result["details"] = {
                "smtp_user":     SMTP_USER or "(vuoto)",
                "smtp_host":     SMTP_HOST,
                "smsapi_token":  (SMSAPI_TOKEN[:8]+"...") if SMSAPI_TOKEN else "(vuoto)",
                "smsapi_sender": SMSAPI_SENDER or "(vuoto)",
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
    _pg_init()  # Inizializza tabella PostgreSQL
    threading.Thread(target=alarm_thread,daemon=True).start()
    threading.Thread(target=daily_report_thread,daemon=True).start()
    threading.Thread(target=monthly_report_thread,daemon=True).start()
    threading.Thread(target=backup_thread,daemon=True).start()
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
    if not SMSAPI_TOKEN: print("  [!] Configura SMSAPI_TOKEN per gli SMS")
    print("  CTRL+C per fermare\n")
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\n  Fermato."); sys.exit(0)
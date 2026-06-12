"""
sync_work_requests.py  v1.0
============================
Sincroniza Solicitudes de Trabajo de Fracttal One a Supabase.
Lee /api/work_requests y actualiza solicitudes_trabajo con:
  - fecha_incidente = date_incident  ← T0 CORRECTO para SLA
  - wo_folio        = wo_folio       ← Link a la OT

Uso:
  python sync_work_requests.py           (full - todas las solicitudes)
  python sync_work_requests.py --modo incremental  (solo ultimas 72h)
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ── Credenciales ───────────────────────────────────────────────────────────────
FRACTTAL_BASE      = "https://app.fracttal.com"
FRACTTAL_TOKEN_URL = f"{FRACTTAL_BASE}/oauth/token"
FRACTTAL_WR_URL    = f"{FRACTTAL_BASE}/api/work_requests"
CLIENT_ID          = "KtHFO5pMskBbJ3lhPr"
CLIENT_SECRET      = "bnpkpimGY4O0N9TxLUeKPXlKYRPV517m"

SUPABASE_URL   = "https://puefgkyjghwwgdfxbrex.supabase.co"
SUPABASE_KEY   = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
)
SUPABASE_TABLE = "solicitudes_trabajo"

PAGE_SIZE   = 100
BATCH_SIZE  = 500
SLEEP_OK    = 0.25
SLEEP_429   = 8
MAX_RETRIES = 6

CHROME_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "es-CL,es;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Origin":          "https://app.fracttal.com",
    "Referer":         "https://app.fracttal.com/",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def log(msg, lvl="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    tags = {"INFO": "    ", "OK": "[OK]", "WARN": "[!] ", "ERR": "[X] ", "PROG": "-->"}
    print(f"[{ts}] {tags.get(lvl,'    ')} {msg}")

def _str(v, maxlen=None):
    s = str(v or "").strip()
    if not s or s.upper() in ("NONE", "NULL"): return None
    return s[:maxlen] if maxlen else s

# ── Autenticacion ──────────────────────────────────────────────────────────────

_token_cache = {"token": None, "expires": datetime.min}

def get_token() -> str:
    if _token_cache["token"] and datetime.now() < _token_cache["expires"]:
        return _token_cache["token"]
    r = requests.post(
        FRACTTAL_TOKEN_URL,
        data={"grant_type": "client_credentials",
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        headers={**CHROME_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    _token_cache.update({
        "token":   d["access_token"],
        "expires": datetime.now() + timedelta(seconds=d.get("expires_in", 3600) - 60),
    })
    log("Token OK", "OK")
    return _token_cache["token"]

# ── Extraccion Fracttal ────────────────────────────────────────────────────────

def fetch_page(start: int) -> list:
    headers = {**CHROME_HEADERS, "Authorization": f"Bearer {get_token()}"}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(FRACTTAL_WR_URL, headers=headers,
                             params={"start": start, "limit": PAGE_SIZE}, timeout=45)
            if r.status_code == 429:
                wait = SLEEP_429 * attempt
                log(f"Rate limit -> esperando {wait}s (intento {attempt})", "WARN")
                time.sleep(wait)
                headers["Authorization"] = f"Bearer {get_token()}"
                continue
            if r.status_code in (401, 403):
                headers["Authorization"] = f"Bearer {get_token()}"
                continue
            r.raise_for_status()
            return r.json().get("data") or []
        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES: raise
            log(f"Error red: {e} (intento {attempt})", "WARN")
            time.sleep(3 * attempt)
    return []

# ── Mapeo work_request → solicitudes_trabajo ──────────────────────────────────

def map_record(wr: dict, desde: str) -> dict | None:
    id_code = wr.get("id_code")
    if not id_code:
        return None

    # Filtro por fecha (usar date_incident si existe, si no date)
    date_ref = _str(wr.get("date_incident") or wr.get("date") or "")
    if date_ref and date_ref[:10] < desde:
        return None  # mas antiguo que el filtro

    wo_folio = _str(wr.get("wo_folio"))

    # Detectar cliente desde parent_description o descripcion
    parent = str(wr.get("parent_description") or "").upper()
    desc   = str(wr.get("items_description") or "").upper()
    combo  = parent + " " + desc
    if "COPEC" in combo:
        cliente = "COPEC"
    elif "ESMAX" in combo or "ARAMCO" in combo:
        cliente = "ESMAX (Aramco)"
    elif "SHELL" in combo or "ENEX" in combo:
        cliente = "SHELL (Enex)"
    else:
        cliente = _str(wr.get("requested_by"), 100) or _str(wr.get("accounts_name"), 100)

    return {
        "id_solicitud":   int(id_code),
        "wo_folio":       wo_folio,
        # T0 CORRECTO: fecha_incidente = date_incident de Fracttal
        "fecha_incidente": _str(wr.get("date_incident")),
        "fecha_solicitud": _str(wr.get("date")),
        "fecha_solucion":  _str(wr.get("date_solution")),
        "tipo":            _str(wr.get("types_description"), 100),
        "descripcion":     _str(wr.get("description"), 1000),
        "estado":          _str(wr.get("requests_x_status_description"), 100),
        "cliente":         cliente,
        "equipo_eds":      _str(wr.get("items_description"), 300),
        "solicitado_por":  _str(wr.get("requested_by"), 200),
        "synced_at":       datetime.now(timezone.utc).isoformat(),
    }

# ── Upsert Supabase ────────────────────────────────────────────────────────────

def upsert_batch(records: list) -> int:
    if not records: return 0
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=minimal",
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        headers=headers,
        data=json.dumps(records, ensure_ascii=False),
        timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        log(f"Error Supabase {r.status_code}: {r.text[:300]}", "ERR")
        r.raise_for_status()
    return len(records)

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--desde",  default="2026-01-01")
    parser.add_argument("--modo",   choices=["completo","incremental"], default="completo")
    args = parser.parse_args()

    if args.modo == "incremental":
        desde = (datetime.now() - timedelta(hours=72)).strftime("%Y-%m-%d")
        log(f"Modo incremental - ultimas 72h (desde {desde})")
    else:
        desde = args.desde
        log(f"Modo completo - desde {desde}")

    # Obtener total
    headers = {**CHROME_HEADERS, "Authorization": f"Bearer {get_token()}"}
    r0 = requests.get(FRACTTAL_WR_URL, headers=headers,
                      params={"start":0,"limit":1}, timeout=20)
    total_api = r0.json().get("total", 3000)
    log(f"Total work_requests en Fracttal: {total_api}")

    start  = 0
    total  = 0
    buffer = []
    stopped = False

    log("Iniciando sync work_requests -> Supabase...", "PROG")
    print("-" * 65)

    while not stopped and start <= total_api:
        batch = fetch_page(start)
        if not batch:
            log("Sin mas registros", "OK")
            break

        for wr in batch:
            row = map_record(wr, desde)
            if row is None:
                # Si el registro es mas viejo que el filtro y estamos en modo completo,
                # los datos mas antiguos de la API vienen al final -> parar si esta muy antiguo
                date_ref = str(wr.get("date_incident") or wr.get("date") or "")
                if date_ref and date_ref[:10] < "2020-01-01":
                    stopped = True
                    break
                continue
            buffer.append(row)

        if len(buffer) >= BATCH_SIZE:
            n = upsert_batch(buffer)
            total += n
            buffer.clear()
            log(f"start={start:>6} | total sincronizados: {total:>6}", "PROG")

        start += PAGE_SIZE
        time.sleep(SLEEP_OK)

    if buffer:
        n = upsert_batch(buffer)
        total += n
        log(f"Ultimo batch: {n} registros", "OK")

    print("-" * 65)
    log(f"COMPLETADO - {total:,} solicitudes sincronizadas en '{SUPABASE_TABLE}'", "OK")

    # Verificar cuantas tienen wo_folio y fecha_incidente correcta
    log("Verificando resultado...", "PROG")
    r_check = requests.get(
        SUPABASE_URL + "/rest/v1/solicitudes_trabajo"
        "?wo_folio=not.is.null"
        "&fecha_incidente=not.is.null"
        "&select=id_solicitud,wo_folio,fecha_incidente,fecha_solicitud"
        "&limit=5000",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        timeout=20
    )
    linked = r_check.json()
    log(f"Solicitudes con wo_folio + fecha_incidente: {len(linked)}", "OK")
    return total


if __name__ == "__main__":
    main()

"""
sync_fracttal_supabase.py  v2.0
================================
Sincroniza Ordenes de Trabajo de Fracttal One a Supabase.
Incluye todos los campos necesarios para reemplazar Fracttal en el dashboard.

Uso:
  python sync_fracttal_supabase.py                      (completo desde 2026-01-01)
  python sync_fracttal_supabase.py --desde 2025-11-01   (otra fecha)
  python sync_fracttal_supabase.py --modo incremental   (solo ultimas 48h)
  python sync_fracttal_supabase.py --reanudar           (continuar checkpoint)
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# Credenciales Fracttal
FRACTTAL_BASE      = "https://app.fracttal.com"
FRACTTAL_TOKEN_URL = f"{FRACTTAL_BASE}/oauth/token"
FRACTTAL_WO_URL    = f"{FRACTTAL_BASE}/api/work_orders"
CLIENT_ID          = "KtHFO5pMskBbJ3lhPr"
CLIENT_SECRET      = "bnpkpimGY4O0N9TxLUeKPXlKYRPV517m"

# Credenciales Supabase
SUPABASE_URL   = "https://puefgkyjghwwgdfxbrex.supabase.co"
SUPABASE_KEY   = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
)
SUPABASE_TABLE = "ordenes_trabajo"

BATCH_SIZE  = 250
PAGE_SIZE   = 100
SLEEP_OK    = 0.25
SLEEP_429   = 8
MAX_RETRIES = 6
PROGRESS_FILE = Path("sync_progress.json")

CHROME_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "es-CL,es;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Origin":          "https://app.fracttal.com",
    "Referer":         "https://app.fracttal.com/",
}

# ── Helpers ────────────────────────────────────────────────────────────────

def log(msg, lvl="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    tags = {"INFO": "    ", "OK": "[OK]", "WARN": "[!] ", "ERR": "[X] ", "PROG": "--> "}
    print(f"[{ts}] {tags.get(lvl,'    ')} {msg}")

def _int(v):
    try: return int(v) if v is not None else None
    except: return None

def _float(v):
    try: return float(v) if v is not None else None
    except: return None

def _str(v, maxlen=None):
    s = str(v or "").strip()
    if not s or s.upper() in ("NONE", "NULL"): return None
    return s[:maxlen] if maxlen else s

# ── Autenticacion ──────────────────────────────────────────────────────────

_token_cache = {"token": None, "expires": datetime.min}

def get_token() -> str:
    if _token_cache["token"] and datetime.now() < _token_cache["expires"]:
        return _token_cache["token"]
    log("Obteniendo token...")
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

# ── Extraccion Fracttal ────────────────────────────────────────────────────

def fetch_page(start: int) -> list:
    headers = {**CHROME_HEADERS, "Authorization": f"Bearer {get_token()}"}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(FRACTTAL_WO_URL, headers=headers,
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

# ── Mapeo Fracttal → Supabase ──────────────────────────────────────────────

# Mapeo de prioridad a P1-P4
def _priority(raw: str) -> str:
    r = str(raw or "").upper().replace(" ", "_")
    if "VERY_HIGH" in r or "CRITIC" in r: return "P1"
    if r == "HIGH":                        return "P2"
    if "MEDIUM" in r:                      return "P3"
    if "LOW" in r:                         return "P4"
    return None

# Parsear cliente y estacion desde jerarquia
def _parse_client(parent: str):
    if not parent: return None, None
    parts = [p.strip() for p in parent.split("/") if p.strip()]
    client  = parts[0].upper() if parts else None
    station = parts[1] if len(parts) > 1 else client
    # Normalizar nombre cliente
    if client:
        if "COPEC"   in client: client = "COPEC"
        elif "ESMAX" in client or "ARAMCO" in client or "PETROBRAS" in client:
            client = "ESMAX (Aramco)"
        elif "SHELL" in client or "ENEX" in client:
            client = "SHELL (Enex)"
    return client, station

# Tipo de mantenimiento simplificado
def _maint_type(raw: str) -> str:
    r = str(raw or "").upper()
    if "CORRECTIVA" in r:   return "CORRECTIVA"
    if "PREVENTIVA" in r:   return "PREVENTIVA"
    if "INSPECCI"   in r:   return "INSPECCION"
    if "ENTREGA"    in r:   return "ENTREGA_INSUMOS"
    if "GARANTIA"   in r:   return "GARANTIA"
    return "OTRA"

# Detectar si nota tiene numeral (>=4 digitos)
def _has_numeral(note: str, task_note: str) -> bool:
    text = f"{note or ''} {task_note or ''}".upper()
    return bool(re.search(r"\b\d{4,}\b", text))

# Verificar si tiene recursos registrados
def _has_resources(wo: dict) -> bool:
    return any(
        wo.get(f) not in (None, "", "None", "none", "0", 0)
        for f in ["resources_inventory", "resources_human_resources",
                  "resources_hours", "resources_services"]
    )

def map_record(wo: dict) -> dict | None:
    folio = _str(wo.get("wo_folio"))
    if not folio:
        return None

    client, station = _parse_client(wo.get("parent_description") or "")
    nota      = _str(wo.get("note"), 3000)
    nota_tarea= _str(wo.get("task_note"), 3000)

    # Estado legible
    estado_id = wo.get("id_status_work_order")
    estado_map = {1:"Por Iniciar", 2:"En Progreso", 3:"Finalizadas",
                  4:"Por Validar", 5:"Canceladas"}
    estado_raw = wo.get("work_orders_status_custom_description")
    estado = _str(estado_raw) or estado_map.get(estado_id, _str(estado_id))

    return {
        # Identificador principal
        "id_ot":              folio,

        # Estado
        "estado":             estado,
        "estado_tarea":       _str(wo.get("task_status")),
        "completada":         str(wo.get("done","false")).lower() in ("true","1","yes"),

        # Jerarquia y ubicacion
        "ubicacion":          _str(wo.get("parent_description"), 500),
        "cliente":            client,
        "estacion":           _str(station, 300),
        "codigo_eds":         _str(wo.get("groups_2_description")),

        # Equipo/Activo
        "codigo_activo":      _str(wo.get("code")),
        "nombre_activo":      _str(wo.get("items_log_description"), 300),

        # Tecnico
        "responsable":        _str(wo.get("personnel_description"), 200),

        # Tipo y prioridad
        "tipo_tarea":         _str(wo.get("tasks_log_task_type_main"), 100),
        "prioridad":          _str(wo.get("priorities_description")),
        "prioridad_calc":     _priority(wo.get("priorities_description")),

        # Fechas
        "fecha_creacion":     wo.get("creation_date"),
        "fecha_finalizacion": wo.get("final_date"),
        # initial_date = cuando el tecnico abrio Fracttal para trabajar (Fecha de Inicio)
        # Usado por KPI Precision para calcular elapsed_sec (tiempo real de trabajo)
        "fecha_inicio":       wo.get("initial_date"),
        # event_date = "Fecha del Incidente" en Fracttal (T0 real llenado por el admin en la OT)
        "fecha_incidente":    wo.get("event_date"),

        # KPI Efectividad MP - causa de falla
        "causa_raiz":         _str(wo.get("causes_description"), 300),
        "tipo_falla":         _str(wo.get("types_description"), 100),

        # KPI Precision - tiempo
        "duracion_real_seg":  _int(wo.get("tasks_duration")),
        "duracion_estim_seg": _int(wo.get("duration")),

        # KPI Precision - modalidad de atencion (4to componente)
        "modalidad_atencion": _str(wo.get("detection_method_description"), 100),

        # KPI Precision - numeral
        "nota":               nota,
        "nota_tarea":         nota_tarea,
        "tiene_numeral":      _has_numeral(nota, nota_tarea),

        # Recursos
        "tiene_recursos":     _has_resources(wo),

        # Sync metadata
        "updated_at":         datetime.now(timezone.utc).isoformat(),
    }

# ── Upsert Supabase ────────────────────────────────────────────────────────

def upsert_batch(records: list) -> int:
    if not records: return 0

    # Deduplicar por id_ot (varias tareas por OT comparten wo_folio)
    # Priorizar: el que tiene fecha_finalizacion (OT completa) y duracion real
    dedup: dict = {}
    for row in records:
        oid = row["id_ot"]
        if oid not in dedup:
            dedup[oid] = row
        else:
            prev = dedup[oid]
            # Preferir registro con mas datos completos
            score_new  = sum(1 for v in row.values()  if v not in (None, False, ""))
            score_prev = sum(1 for v in prev.values() if v not in (None, False, ""))
            if score_new > score_prev:
                dedup[oid] = row

    unique = list(dedup.values())

    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=minimal",
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
        headers=headers,
        data=json.dumps(unique, ensure_ascii=False),
        timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        log(f"Error Supabase {r.status_code}: {r.text[:250]}", "ERR")
        r.raise_for_status()
    return len(unique)

# ── Checkpoint ─────────────────────────────────────────────────────────────

def save_progress(start: int, total: int):
    PROGRESS_FILE.write_text(
        json.dumps({"last_start": start, "total": total, "ts": datetime.now().isoformat()}),
        encoding="utf-8"
    )

def load_progress():
    if PROGRESS_FILE.exists():
        d = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        log(f"Reanudando desde start={d['last_start']} ({d['total']} ya cargados)", "WARN")
        return d["last_start"], d["total"]
    return 0, 0

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--desde",    default="2026-01-01")
    parser.add_argument("--modo",     choices=["completo","incremental"], default="completo")
    parser.add_argument("--reanudar", action="store_true")
    args = parser.parse_args()

    if args.modo == "incremental":
        desde = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d")
        log(f"Modo incremental - ultimas 48h (desde {desde})")
    else:
        desde = args.desde
        log(f"Modo completo - desde {desde}")

    start, total = load_progress() if args.reanudar else (0, 0)

    buffer, pages, stopped = [], 0, False

    log("Iniciando sync Fracttal -> Supabase...", "PROG")
    print("-" * 65)

    while not stopped:
        pages += 1
        records = fetch_page(start)
        if not records:
            log("Sin mas registros", "OK")
            break

        for wo in records:
            cd = str(wo.get("creation_date") or "")
            if cd and cd[:10] < desde:
                log(f"Fecha {cd[:10]} < {desde} - deteniendo", "OK")
                stopped = True
                break
            row = map_record(wo)
            if row:
                buffer.append(row)

        if len(buffer) >= BATCH_SIZE:
            n = upsert_batch(buffer)
            total += n
            buffer.clear()
            save_progress(start, total)
            log(f"Pag {pages:>4} | start={start:>6} | total: {total:>6}", "PROG")

        if not stopped:
            start += PAGE_SIZE
            time.sleep(SLEEP_OK)

    if buffer:
        n = upsert_batch(buffer)
        total += n
        log(f"Ultimo batch: {n} registros", "OK")

    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()

    print("-" * 65)
    log(f"COMPLETADO - {total:,} registros cargados en '{SUPABASE_TABLE}'", "OK")
    return total


if __name__ == "__main__":
    main()

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

# ── Pre-carga del mapa de Planes de Tareas ─────────────────────────────────
# Fracttal expone el nombre del plan ("PLAN MTTO MSELF GENERAL", etc.) en el
# endpoint /api/tasks (campo groups_tasks_description), no en /api/work_orders.
# Pre-cargamos {id_group_task → nombre_del_plan} para resolverlo al mapear.

FRACTTAL_TASKS_URL = f"{FRACTTAL_BASE}/api/tasks"
_PLAN_MAP: dict[int, str] = {}

def build_plan_map() -> dict[int, str]:
    """Devuelve {id_group_task: nombre_plan} consultando /api/tasks paginadamente."""
    global _PLAN_MAP
    if _PLAN_MAP:
        return _PLAN_MAP
    log("Cargando mapa de planes de tareas...", "PROG")
    headers = {**CHROME_HEADERS, "Authorization": f"Bearer {get_token()}"}
    start, m = 0, {}
    while True:
        try:
            r = requests.get(FRACTTAL_TASKS_URL, headers=headers,
                             params={"start": start, "limit": PAGE_SIZE}, timeout=30)
            if r.status_code in (401, 403):
                headers["Authorization"] = f"Bearer {get_token()}"
                continue
            r.raise_for_status()
            items = r.json().get("data") or []
        except requests.exceptions.RequestException as e:
            log(f"Error cargando planes: {e}", "WARN")
            break
        if not items:
            break
        for t in items:
            gid = t.get("id_group_task")
            name = t.get("groups_tasks_description")
            if gid and name:
                m[int(gid)] = str(name).strip()
        start += PAGE_SIZE
        if len(items) < PAGE_SIZE:
            break
    _PLAN_MAP = m
    log(f"Planes cargados: {len(m)} grupos de tareas", "OK")
    return m


# ── Mapeo Fracttal → Supabase ──────────────────────────────────────────────

# Mapeo de prioridad a P1-P4 (SOLO fallback para Shell u otros clientes sin
# fuente autoritativa. NUNCA usar para COPEC/Aramco — ver override abajo.)
def _priority(raw: str) -> str:
    r = str(raw or "").upper().replace(" ", "_")
    if "VERY_HIGH" in r or "CRITIC" in r: return "P1"
    if r == "HIGH":                        return "P2"
    if "MEDIUM" in r:                      return "P3"
    if "LOW" in r:                         return "P4"
    return None


# ═══════════════════════════════════════════════════════════════════════════
# FUENTES AUTORITATIVAS DE PRIORIDAD (COPEC + Aramco)
# ═══════════════════════════════════════════════════════════════════════════
# Fracttal asigna prioridad de forma poco confiable. Cada cliente tiene su
# propia fuente de verdad:
#   COPEC:  nota_tarea contiene "Tiempo de respuesta: XX horas" (del email
#           SLA que llega desde el cliente).
#   Aramco: Cotalker (panel Metabase) tiene "SLA esperado" en horas para
#           cada N° Cotalker; el N° Cotalker aparece al inicio de nota_tarea.
# ═══════════════════════════════════════════════════════════════════════════

_METABASE_COTALKER_URL = (
    "https://bi.cotalker.com/api/public/card"
    "/56662edd-715d-4dbe-af9a-21891f4dbb97/query/json"
)
_PAT_SLA_COPEC   = re.compile(r"Tiempo\s+de\s+respuesta\s*:\s*(\d+)", re.IGNORECASE)
_PAT_COTALKER_N  = re.compile(r"^(\d{5,8})(?:\s*-|\s*$)")

# COPEC SLA (horas) → prioridad. Ambigüedad de 24h se resuelve por zona
# (Santiago=P2, Regiones=P1). Aquí no tenemos la zona al momento del sync,
# así que 24h → P1 por defecto y el consumo en dashboard sigue usando el
# umbral correcto vía sla_umbrales_horas.
_COPEC_SLA_TO_PRIO = {18: "P1", 24: "P1", 36: "P3", 48: "P2", 72: "P3", 96: "P4"}

# Aramco / Cotalker: mapeo directo horas → prioridad.
_ARAMCO_SLA_TO_PRIO = {24: "P1", 48: "P2", 72: "P3"}

_COTALKER_SLA_INDEX: dict = {}  # {n_cotalker(int): sla_horas(int)}


def build_cotalker_sla_index() -> None:
    """
    Descarga el panel Metabase de Cotalker y llena _COTALKER_SLA_INDEX.
    Debe llamarse ANTES del sync principal. Si falla, se registra warning
    y las OTs Aramco quedan sin corrección (mejor eso que datos incorrectos).
    """
    global _COTALKER_SLA_INDEX
    log("Descargando panel Cotalker/Metabase (SLA Aramco)...", "PROG")
    try:
        r = requests.get(_METABASE_COTALKER_URL,
                         headers={"Accept": "application/json"}, timeout=60)
        r.raise_for_status()
        data = r.json()
        idx = {}
        for row in data:
            n   = row.get("N° Cotalker")
            sla = row.get("SLA esperado")
            if n and sla:
                try:
                    idx[int(n)] = int(float(sla))
                except (ValueError, TypeError):
                    pass
        _COTALKER_SLA_INDEX = idx
        log(f"Cotalker SLA index: {len(idx)} OTs con SLA", "OK")
    except Exception as e:
        log(f"No se pudo cargar Cotalker Metabase: {e}", "WARN")
        log("Prioridades Aramco quedarán SIN CORRECCIÓN esta corrida", "WARN")
        _COTALKER_SLA_INDEX = {}


def _copec_prio_from_nota(nota_tarea: str) -> str | None:
    """Parsea 'Tiempo de respuesta: XX horas' de nota_tarea COPEC → P1-P4."""
    if not nota_tarea:
        return None
    m = _PAT_SLA_COPEC.search(nota_tarea)
    if not m:
        return None
    try:
        sla_h = int(m.group(1))
    except ValueError:
        return None
    return _COPEC_SLA_TO_PRIO.get(sla_h)


def _aramco_prio_from_nota(nota_tarea: str) -> str | None:
    """
    Extrae primer N° del nota_tarea (formato '155097 - 169357 - ee_s268 - ...'
    o solo '155097') y consulta _COTALKER_SLA_INDEX. Retorna P1-P4 o None.
    """
    if not nota_tarea or not _COTALKER_SLA_INDEX:
        return None
    m = _PAT_COTALKER_N.match(str(nota_tarea).strip())
    if not m:
        return None
    n_cot = int(m.group(1))
    sla_h = _COTALKER_SLA_INDEX.get(n_cot)
    if sla_h is None:
        return None
    return _ARAMCO_SLA_TO_PRIO.get(sla_h, "P4")


def compute_prioridad_calc(client: str, nota: str, nota_tarea: str,
                           fracttal_prio_desc: str) -> str | None:
    """
    Fuente de verdad de prioridad_calc según cliente:
      - COPEC:  nota_tarea (email SLA). Fallback: Fracttal.
      - Aramco: Cotalker Metabase por N°. Fallback: Fracttal.
      - Otros: Fracttal (comportamiento anterior).
    """
    if client == "COPEC":
        prio = _copec_prio_from_nota(nota_tarea) or _copec_prio_from_nota(nota)
        if prio:
            return prio
        return _priority(fracttal_prio_desc)

    if client == "ESMAX (Aramco)":
        prio = _aramco_prio_from_nota(nota_tarea)
        if prio:
            return prio
        return _priority(fracttal_prio_desc)

    return _priority(fracttal_prio_desc)

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
    # NOTA: id_status_work_order es la fuente de verdad para estados terminales.
    # Fracttal puede dejar una descripción custom obsoleta (ej. "En Revisión") cuando
    # una OT se cancela, por eso los estados terminales tienen prioridad sobre la custom.
    estado_id = wo.get("id_status_work_order")
    estado_map = {1:"Por Iniciar", 2:"En Progreso", 3:"Finalizadas",
                  4:"Por Validar", 5:"Canceladas"}
    _ESTADOS_TERMINAL = {3, 5}   # Finalizadas y Canceladas — no pisar con custom
    if estado_id in _ESTADOS_TERMINAL:
        estado = estado_map[estado_id]
    else:
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
        # prioridad     = valor crudo de Fracttal (auditoría)
        # prioridad_calc = FUENTE DE VERDAD para el dashboard:
        #                  COPEC → nota_tarea (email SLA)
        #                  Aramco → Cotalker Metabase (SLA esperado)
        #                  Otros → Fracttal
        "tipo_tarea":         _str(wo.get("tasks_log_task_type_main"), 100),
        "prioridad":          _str(wo.get("priorities_description")),
        "prioridad_calc":     compute_prioridad_calc(
                                  client, nota, nota_tarea,
                                  wo.get("priorities_description")
                              ),

        # Fechas
        "fecha_creacion":     wo.get("creation_date"),
        "fecha_finalizacion": wo.get("final_date"),
        # initial_date = cuando el tecnico abrio Fracttal para trabajar (Fecha de Inicio)
        # Usado por KPI Precision para calcular elapsed_sec (tiempo real de trabajo)
        "fecha_inicio":       wo.get("initial_date"),
        # event_date = "Fecha del Incidente" en Fracttal (T0 real llenado por el admin en la OT)
        "fecha_incidente":    wo.get("event_date"),
        # date_maintenance = "Fecha Programada" en Fracttal (el día comprometido
        # para ejecutar la OT). Es la referencia del KPI Cumplimiento MP.
        # cal_date_maintenance es la fecha calculada inicial (suele coincidir).
        "fecha_programada":   wo.get("date_maintenance") or wo.get("cal_date_maintenance"),

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

        # Mantenciones Preventivas — paro de equipo (cálculo Uptime)
        "paro_equipo":           bool(wo.get("stop_assets")) if wo.get("stop_assets") is not None else None,
        "tiempo_paro_estim_seg": _int(wo.get("stop_assets_sec")),
        "tiempo_paro_real_seg":  _int(wo.get("real_stop_assets_sec")),

        # Plan de tareas — resuelto via id_group_task → groups_tasks_description
        "plan_tareas":           _PLAN_MAP.get(_int(wo.get("id_group_task"))) if wo.get("id_group_task") else None,

        # Sync metadata
        "updated_at":         datetime.now(timezone.utc).isoformat(),
    }

# ── Upsert Supabase ────────────────────────────────────────────────────────

def upsert_batch(records: list) -> int:
    if not records: return 0

    # Deduplicar por id_ot (varias sub-tareas por OT comparten wo_folio)
    # Estrategia:
    #   1. Elegir la sub-tarea "ganadora" con más datos completos.
    #   2. ACUMULAR los codigo_activo y nombre_activo de TODAS las sub-tareas
    #      con el mismo id_ot — así no perdemos info cuando una OT tiene 4
    #      equipos distintos en sus sub-tareas.
    grupos: dict = {}
    for row in records:
        oid = row["id_ot"]
        cod = row.get("codigo_activo")
        nom = row.get("nombre_activo")
        if oid not in grupos:
            grupos[oid] = {"winner": row, "codes": [], "names": []}
        else:
            prev = grupos[oid]["winner"]
            score_new  = sum(1 for v in row.values()  if v not in (None, False, ""))
            score_prev = sum(1 for v in prev.values() if v not in (None, False, ""))
            if score_new > score_prev:
                grupos[oid]["winner"] = row
        if cod and cod not in grupos[oid]["codes"]:
            grupos[oid]["codes"].append(cod)
        if nom and nom not in grupos[oid]["names"]:
            grupos[oid]["names"].append(nom)

    # Combinar: si hay más de un equipo distinto, juntarlos
    unique = []
    for oid, g in grupos.items():
        winner = dict(g["winner"])  # copia
        if len(g["codes"]) > 1:
            winner["codigo_activo"] = ", ".join(g["codes"])[:200]
        if len(g["names"]) > 1:
            winner["nombre_activo"] = " · ".join(g["names"])[:300]
        unique.append(winner)

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
    parser.add_argument("--sin-numerales", action="store_true",
                        help="No ejecutar el sync de numerales reales al terminar")
    args = parser.parse_args()

    if args.modo == "incremental":
        desde = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d")
        log(f"Modo incremental - ultimas 48h (desde {desde})")
    else:
        desde = args.desde
        log(f"Modo completo - desde {desde}")

    start, total = load_progress() if args.reanudar else (0, 0)

    # Cargar mapa de planes ANTES del sync principal (para que map_record lo use)
    build_plan_map()

    # Cargar índice SLA Cotalker (Aramco) ANTES del sync principal.
    # Sin esto, prioridad_calc de Aramco cae al fallback (Fracttal, incorrecto).
    build_cotalker_sla_index()

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

    # ── Paso 2: numerales reales (lavadoras/aspiradoras) ─────────────────────
    # Corre DESPUÉS del sync principal para fijar el valor real del numeral por
    # encima del flag tiene_numeral que el regex dejó como baseline.
    if not args.sin_numerales:
        try:
            import sync_numerales
            print("-" * 65)
            log("Paso 2: extrayendo numerales reales de subtareas...", "PROG")
            folios = sync_numerales.query_lavadora_folios(desde)
            log(f"{len(folios)} OTs lavadora/aspiradora a revisar", "OK")
            con = sin = 0
            CHUNK = 200
            for i in range(0, len(folios), CHUNK):
                res = sync_numerales.fetch_numerales_batch(folios[i:i+CHUNK])
                for folio, vals in res.items():
                    ini, fin = vals[0], vals[1]
                    com = vals[2] if len(vals) > 2 else None
                    form_num = vals[3] if len(vals) > 3 else None
                    sync_numerales.patch_numeral(folio, ini, fin, com, form_num)
                    if ini or fin: con += 1
                    else:          sin += 1
                log(f"Numerales {min(i+CHUNK,len(folios)):>5}/{len(folios)} | "
                    f"con: {con} sin: {sin}", "PROG")
            log(f"Numerales OK - {con} con valor, {sin} sin valor", "OK")
        except Exception as e:
            log(f"Sync de numerales falló (no crítico): {e}", "WARN")

    return total


if __name__ == "__main__":
    main()

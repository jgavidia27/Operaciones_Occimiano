"""
sync_estim_neta.py
==================
Pobla duracion_estim_neta_seg y duracion_real_neta_seg en ordenes_trabajo.

Regla acordada con Occimiano:
  - Una OT preventiva de LAVADORA en Fracttal suele venir con múltiples
    subtareas (lavadora, lavatapiz, bomba, ablandador, etc.). Para el
    indicador de precisión solo interesa el tiempo de la LAVADORA.
  - Si la OT incluye una subtarea LAVADORA, se toman SOLO los tiempos
    estimado y real de esa subtarea (no el total de la OT).
  - Si la OT no incluye lavadora, los campos "netos" copian los
    originales (total de subtareas).

Uso:
  python sync_estim_neta.py                      (backfill desde 2026-01-01)
  python sync_estim_neta.py --modo incremental   (últimas 72h)
  python sync_estim_neta.py --folios OS-38066,OS-38249
"""

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

FRACTTAL_BASE      = "https://app.fracttal.com"
FRACTTAL_TOKEN_URL = f"{FRACTTAL_BASE}/oauth/token"
FRACTTAL_WO        = f"{FRACTTAL_BASE}/api/work_orders/"
CLIENT_ID          = "KtHFO5pMskBbJ3lhPr"
CLIENT_SECRET      = "bnpkpimGY4O0N9TxLUeKPXlKYRPV517m"
ID_COMPANY         = 1507

SUPABASE_URL   = "https://puefgkyjghwwgdfxbrex.supabase.co"
SUPABASE_KEY   = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
)
SUPABASE_TABLE = "ordenes_trabajo"

WORKERS = 16


def log(msg, lvl="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    tags = {"INFO": "    ", "OK": "[OK]", "WARN": "[!] ", "ERR": "[X] ", "PROG": "--> "}
    print(f"[{ts}] {tags.get(lvl,'    ')} {msg}")


_token_cache = {"token": None, "expires": datetime.min}

def get_token() -> str:
    if _token_cache["token"] and datetime.now() < _token_cache["expires"]:
        return _token_cache["token"]
    r = requests.post(
        FRACTTAL_TOKEN_URL,
        data={"grant_type": "client_credentials",
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    _token_cache.update({
        "token":   d["access_token"],
        "expires": datetime.now() + timedelta(seconds=d.get("expires_in", 3600) - 60),
    })
    return _token_cache["token"]


def _sb_headers(write=False):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
         "Accept": "application/json"}
    if write:
        h["Content-Type"] = "application/json"
        h["Prefer"] = "return=minimal"
    return h


def query_preventiva_folios(desde: str) -> list:
    """Folios de OTs preventivas desde `desde` (solo preventivas: solo ahí
    aplica la regla de descontar bomba/ablandador del estimado)."""
    folios, offset, page = [], 0, 1000
    while True:
        url = (f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
               f"?select=id_ot"
               f"&fecha_creacion=gte.{desde}"
               f"&tipo_tarea=ilike.*PREVENTIVA*"
               f"&order=fecha_creacion.desc&limit={page}&offset={offset}")
        r = requests.get(url, headers=_sb_headers(), timeout=30)
        if r.status_code != 200:
            log(f"Error Supabase {r.status_code}: {r.text[:200]}", "ERR")
            break
        batch = r.json()
        if not batch:
            break
        folios.extend(row["id_ot"] for row in batch if row.get("id_ot"))
        if len(batch) < page:
            break
        offset += page
    return folios


# Identifica el tipo de activo por nombre. Usa el PRIMER TOKEN como
# clasificador primario para evitar falsos positivos (ej. "BOMBA CAT
# LAVADORA NORTE" es BOMBA, no lavadora — antes se clasificaba mal
# porque contenía "LAVAD" en cualquier parte).
#
# Marcas conocidas que SÍ son lavadora aunque no digan "LAVADORA":
#   - MSELF, SEAL, SMART: siempre vienen precedidas por LAVADORA
#     en Fracttal (ej. "LAVADORA MSELF 2020") → cae en primer token.
#   - HIDROLAVADORA: aceptado como token propio.
def _tipo_activo(nombre: str) -> str:
    n = (nombre or "").strip().upper()
    if not n:
        return "otro"
    first = n.split()[0]
    # Clasificación por primer token (más robusta)
    if first in ("BOMBA", "BOMBAS", "KIT"):
        return "bomba"
    if first.startswith("ABLANDADOR"):     # ABLANDADOR / ABLANDADORES
        return "ablandador"
    if first == "LAVAINTERIORES":
        return "lavainterior"
    if first.startswith("LAVATAP"):        # LAVATAPETE / LAVATAPETES
        return "lavainterior"
    if first in ("LAVABICICLETAS",):       # no es lavadora de autos
        return "otro"
    if first.startswith("ASPIRA"):
        return "aspiradora"
    if first in ("FICHERO", "TERMO", "DISPENSADOR", "COMPRESOR",
                 "GRUPO", "PUENTE"):
        return "otro"
    if first.startswith("LAVADORA") or first.startswith("HIDROLAV"):
        return "lavadora"
    # Fallback: si el primer token no matchea pero HIDROLAVADORA aparece
    # (raro pero posible), aceptar como lavadora.
    if "HIDROLAVADORA" in n:
        return "lavadora"
    return "otro"


def fetch_subtasks(folio: str) -> tuple:
    """Consulta /api/work_orders/ por folio y devuelve las subtareas
    con su tipo de activo y duraciones.
    Retorna (folio, estim_neta_seg, real_neta_seg, ajustada_bool).
    Si la OT no tiene lavadora, devuelve los originales como neto.

    IMPORTANTE (07-jul-2026): una OT puede tener MÚLTIPLES subtareas
    con activo LAVADORA (ej: 'PLAN MTTO MSELF CON SANITIZADO' que es
    PREVENTIVA + 'REVISIÓN DATOS DE TERMOS' que es INSPECCIÓN). El
    tiempo neto de lavadora debe contar SOLO las subtareas que son
    la mantención principal (tasks_log_task_type_main = PREVENTIVA*),
    excluyendo INSPECCIÓN y otras tareas auxiliares.
    """
    headers = {"Authorization": f"Bearer {get_token()}"}
    try:
        r = requests.get(
            FRACTTAL_WO, headers=headers,
            params={"wo_folio": folio, "id_company": ID_COMPANY, "limit": 50},
            timeout=30,
        )
        items = r.json().get("data", []) or []
    except Exception:
        return folio, None, None, False
    if not items:
        return folio, None, None, False

    # Mapeo correcto de campos del endpoint /api/work_orders/:
    #   - duration       = total de la OT (REPLICADO en cada subtarea, NO usar)
    #   - tasks_duration = duración ESTIMADA por subtarea (en segundos)
    #   - real_duration  = duración REAL ejecutada por subtarea (en segundos)
    #   - tasks_log_task_type_main = tipo de la tarea (PREVENTIVA*, INSPECCIÓN, etc.)
    #   - description = nombre de la tarea (ej. 'PLAN MTTO MSELF')
    subtasks = []
    for it in items:
        tipo = _tipo_activo(it.get("items_log_description") or "")
        tipo_tarea = str(it.get("tasks_log_task_type_main") or "").upper()
        # Solo cuentan como "mantención de lavadora" las tareas PREVENTIVA*
        # (excluye INSPECCIÓN, CORRECTIVA embebida, etc.)
        es_preventiva = "PREVENTIVA" in tipo_tarea
        dur_estim = _to_sec(it.get("tasks_duration"))
        dur_real  = _to_sec(it.get("real_duration"))
        subtasks.append({
            "tipo": tipo, "estim": dur_estim, "real": dur_real,
            "es_preventiva": es_preventiva,
            "task_desc": (it.get("description") or "").strip()[:80],
        })

    estim_total = sum(s["estim"] for s in subtasks)
    real_total  = sum(s["real"]  for s in subtasks)

    # Buscar subtareas de LAVADORA que sean PREVENTIVA (mantención principal)
    lav_prev = [s for s in subtasks if s["tipo"] == "lavadora" and s["es_preventiva"]]

    if not lav_prev:
        # Fallback: si no hay preventiva de lavadora, ver si hay al menos
        # alguna subtarea de lavadora — mantener el comportamiento anterior.
        lav = [s for s in subtasks if s["tipo"] == "lavadora"]
        if not lav:
            # No hay lavadora en absoluto → neto = total
            return folio, estim_total, real_total, False
        estim_neta = sum(s["estim"] for s in lav)
        real_neta  = sum(s["real"]  for s in lav)
        return folio, estim_neta, real_neta, True

    # Caso normal: sumar solo las PREVENTIVAS de lavadora
    estim_neta = sum(s["estim"] for s in lav_prev)
    real_neta  = sum(s["real"]  for s in lav_prev)
    return folio, estim_neta, real_neta, True


def _to_sec(v) -> int:
    """Convierte HH:MM:SS o entero a segundos."""
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    # Si trae ":" asumir HH:MM:SS o MM:SS
    if ":" in s:
        parts = [int(p) for p in s.split(":") if p.isdigit() or (p and p[0]=="-" and p[1:].isdigit())]
        if len(parts) == 3:
            return parts[0]*3600 + parts[1]*60 + parts[2]
        if len(parts) == 2:
            return parts[0]*60 + parts[1]
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def patch_estim_neta(folio: str, estim, real) -> bool:
    payload = {
        "duracion_estim_neta_seg": estim,
        "duracion_real_neta_seg":  real,
        "updated_at":              datetime.now(timezone.utc).isoformat(),
    }
    for intento in range(3):
        try:
            r = requests.patch(
                f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id_ot=eq.{folio}",
                headers=_sb_headers(write=True), data=json.dumps(payload), timeout=20,
            )
            return r.status_code in (200, 204)
        except requests.exceptions.RequestException:
            if intento == 2:
                return False
            time.sleep(2 * (intento + 1))
    return False


def fetch_batch(folios: list, workers=WORKERS) -> dict:
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_subtasks, f): f for f in folios}
        for fut in as_completed(futs):
            folio, e, r, adj = fut.result()
            out[folio] = (e, r, adj)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default="2026-01-01")
    ap.add_argument("--modo", choices=["completo","incremental"], default="completo")
    ap.add_argument("--folios", default="")
    args = ap.parse_args()

    if args.folios:
        folios = [f.strip() for f in args.folios.split(",") if f.strip()]
        log(f"Folios puntuales: {len(folios)}")
    else:
        desde = ((datetime.now() - timedelta(hours=72)).strftime("%Y-%m-%d")
                 if args.modo == "incremental" else args.desde)
        log(f"Buscando OTs preventivas desde {desde}...")
        folios = query_preventiva_folios(desde)
        log(f"Encontradas {len(folios)} OTs preventivas", "OK")

    if not folios:
        log("Sin folios que procesar", "WARN")
        return

    log("Extrayendo subtareas de Fracttal (paralelo)...", "PROG")
    print("-"*65)
    t0 = time.time()
    ok = ajustadas = sin_datos = err = 0
    CHUNK = 200
    for i in range(0, len(folios), CHUNK):
        chunk = folios[i:i+CHUNK]
        res = fetch_batch(chunk)
        for folio, (e, r, adj) in res.items():
            if e is None and r is None:
                sin_datos += 1
                continue
            if patch_estim_neta(folio, e, r):
                ok += 1
                if adj:
                    ajustadas += 1
            else:
                err += 1
        log(f"Procesadas {min(i+CHUNK,len(folios)):>5}/{len(folios)} | "
            f"ok={ok} | ajustadas={ajustadas} | sin_datos={sin_datos} | err={err}", "PROG")

    print("-"*65)
    log(f"COMPLETADO en {time.time()-t0:.0f}s | {ok} actualizadas | "
        f"{ajustadas} ajustadas por lavadora+bomba/ablandador | "
        f"{sin_datos} sin datos | {err} errores", "OK")


if __name__ == "__main__":
    main()

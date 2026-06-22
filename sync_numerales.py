"""
sync_numerales.py
=================
Extrae el numeral REAL (inicial/final) de cada OT de lavadora/aspiradora
desde las subtareas de Fracttal y lo persiste en Supabase (ordenes_trabajo).

Reemplaza la heurística de regex sobre la nota (poco confiable) por el valor
exacto que el técnico registró en el formulario de la tarea:
    - "TOMA DE NUMERAL INICIAL"  → id_task_form_item_type = 3
    - "TOMA DE NUMERAL FINAL"    → id_task_form_item_type = 5
(El filtro exige que la descripción contenga "NUMERAL" porque los types 3/5
 también los usan otros ítems como "REGISTRO DE HOROMETRO".)

Requisito previo: correr migrate_numerales.sql en Supabase (agrega columnas).

Uso:
  python sync_numerales.py                      (backfill completo desde 2026-01-01)
  python sync_numerales.py --desde 2026-05-01   (otra fecha)
  python sync_numerales.py --modo incremental   (solo OTs de las últimas 72h)
  python sync_numerales.py --folios OS-38216,OS-38248   (folios puntuales)
"""

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ── Credenciales Fracttal (mismas que sync_fracttal_supabase.py) ─────────────
FRACTTAL_BASE      = "https://app.fracttal.com"
FRACTTAL_TOKEN_URL = f"{FRACTTAL_BASE}/oauth/token"
FRACTTAL_SUBTASKS  = f"{FRACTTAL_BASE}/api/work_orders_subtasks/"
CLIENT_ID          = "KtHFO5pMskBbJ3lhPr"
CLIENT_SECRET      = "bnpkpimGY4O0N9TxLUeKPXlKYRPV517m"
ID_COMPANY         = 1507

# ── Credenciales Supabase ────────────────────────────────────────────────────
SUPABASE_URL   = "https://puefgkyjghwwgdfxbrex.supabase.co"
SUPABASE_KEY   = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
)
SUPABASE_TABLE = "ordenes_trabajo"

WORKERS     = 16
PAGE_LIMIT  = 200      # ítems de formulario por OT (cubre lavadora+aspiradora)
BATCH_PATCH = 1        # Supabase PATCH es por folio (filtro id_ot=eq.X)


def log(msg, lvl="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    tags = {"INFO": "    ", "OK": "[OK]", "WARN": "[!] ", "ERR": "[X] ", "PROG": "--> "}
    print(f"[{ts}] {tags.get(lvl,'    ')} {msg}")


# ── Autenticación Fracttal ───────────────────────────────────────────────────
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


# ── Supabase helpers ─────────────────────────────────────────────────────────
def _sb_headers(write: bool = False) -> dict:
    h = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept":        "application/json",
    }
    if write:
        h["Content-Type"] = "application/json"
        h["Prefer"]       = "return=minimal"
    return h


def query_lavadora_folios(desde: str) -> list:
    """Folios de OTs cuyo activo es lavadora/aspiradora/lavainterior desde `desde`."""
    folios, offset, page = [], 0, 1000
    while True:
        url = (
            f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
            f"?select=id_ot,nombre_activo"
            f"&fecha_creacion=gte.{desde}"
            f"&or=(nombre_activo.ilike.*LAVAD*,nombre_activo.ilike.*ASPIRA*,nombre_activo.ilike.*LAVAINT*)"
            f"&order=fecha_creacion.desc&limit={page}&offset={offset}"
        )
        r = requests.get(url, headers=_sb_headers(), timeout=30)
        if r.status_code != 200:
            log(f"Error Supabase query {r.status_code}: {r.text[:200]}", "ERR")
            break
        batch = r.json()
        if not batch:
            break
        folios.extend(row["id_ot"] for row in batch if row.get("id_ot"))
        if len(batch) < page:
            break
        offset += page
    return folios


def patch_numeral(folio: str, inicial, final, comentario=None, form_tiene_numeral=None) -> bool:
    payload = {
        "numeral_inicial": inicial,
        "numeral_final":   final,
        "tiene_numeral":   bool(inicial or final),
        "updated_at":      datetime.now(timezone.utc).isoformat(),
    }
    # Solo escribir comentario si se extrajo alguno (no pisar con None datos previos)
    if comentario:
        payload["comentario_tecnico"] = comentario
    if form_tiene_numeral is not None:
        payload["form_tiene_numeral"] = bool(form_tiene_numeral)
    # Reintentos ante cortes de red transitorios (ConnectionReset, timeouts).
    # Un solo blip no debe abortar todo el backfill.
    for intento in range(3):
        try:
            r = requests.patch(
                f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?id_ot=eq.{folio}",
                headers=_sb_headers(write=True),
                data=json.dumps(payload),
                timeout=20,
            )
            return r.status_code in (200, 204)
        except requests.exceptions.RequestException:
            if intento == 2:
                return False
            time.sleep(2 * (intento + 1))
    return False


# ── Extracción de numerales + comentario del técnico por folio ───────────────
# Campos de texto libre relevantes (id_task_form_item_type = 1). Se ignoran
# los "PENDIENTE(S)" (suelen ser "No" / ruido). El resto = conclusión del técnico.
_COMENTARIO_MAX = 600   # tope de caracteres del comentario consolidado

def _consolidar_comentario(items: list) -> str:
    """Une los campos de texto libre del técnico en un solo string legible."""
    partes = []
    for it in items:
        if it.get("id_task_form_item_type") != 1:
            continue
        desc = (it.get("description") or "").strip()
        val  = (str(it.get("value")) if it.get("value") is not None else "").strip()
        if not val or val.lower() in ("none", "null"):
            continue
        if desc.upper().startswith("PENDIENTE"):
            continue
        # Normalizar saltos de línea internos a un espacio
        val = " ".join(val.split())
        partes.append(f"{desc}: {val}" if desc else val)
    texto = " | ".join(partes)
    return texto[:_COMENTARIO_MAX]


def fetch_numerales(folio: str) -> tuple:
    """
    Retorna (folio, inicial, final, comentario, form_tiene_numeral).
    inicial    = primer ítem type=3 con 'NUMERAL' en la descripción.
    final      = primer ítem type=5 con 'NUMERAL' en la descripción.
    comentario = texto libre del técnico (falla, trabajo, observaciones).
    form_tiene_numeral = True si el formulario incluía el campo TOMA DE NUMERAL
                         (aunque venga vacío) → permite distinguir "no lo llenó"
                         de "el formulario no lo pedía".
    """
    headers = {"Authorization": f"Bearer {get_token()}"}
    try:
        r = requests.get(
            FRACTTAL_SUBTASKS, headers=headers,
            params={"wo_folio": folio, "id_company": ID_COMPANY, "limit": PAGE_LIMIT},
            timeout=30,
        )
        items = r.json().get("data", [])
    except Exception:
        return folio, None, None, None, None

    inicial = final = None
    form_tiene_numeral = False
    for it in items:
        desc = (it.get("description") or "").upper()
        if "NUMERAL" not in desc:
            continue
        t = it.get("id_task_form_item_type")
        # El campo existe en el formulario (independiente de si trae valor)
        if t in (3, 5):
            form_tiene_numeral = True
        val = (str(it.get("value")) if it.get("value") is not None else "").strip()
        if not val or val.lower() in ("none", "null"):
            continue
        if t == 3 and inicial is None:
            inicial = val
        elif t == 5 and final is None:
            final = val

    comentario = _consolidar_comentario(items) or None
    return folio, inicial, final, comentario, form_tiene_numeral


def fetch_numerales_batch(folios: list, workers: int = WORKERS) -> dict:
    """{folio: (inicial, final, comentario, form_tiene_numeral)} en paralelo."""
    out: dict = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_numerales, f): f for f in folios}
        for fut in as_completed(futs):
            folio, ini, fin, com, form_num = fut.result()
            out[folio] = (ini, fin, com, form_num)
    return out


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--desde", default="2026-01-01")
    parser.add_argument("--modo",  choices=["completo", "incremental"], default="completo")
    parser.add_argument("--folios", default="", help="lista separada por comas (omite query)")
    args = parser.parse_args()

    if args.folios:
        folios = [f.strip() for f in args.folios.split(",") if f.strip()]
        log(f"Folios puntuales: {len(folios)}")
    else:
        desde = ((datetime.now() - timedelta(hours=72)).strftime("%Y-%m-%d")
                 if args.modo == "incremental" else args.desde)
        log(f"Buscando OTs lavadora/aspiradora desde {desde}...")
        folios = query_lavadora_folios(desde)
        log(f"Encontradas {len(folios)} OTs candidatas", "OK")

    if not folios:
        log("Sin folios que procesar", "WARN")
        return

    log("Extrayendo numerales desde subtareas Fracttal (paralelo)...", "PROG")
    print("-" * 65)

    t0 = time.time()
    con_num = sin_num = errores = 0

    # Procesar en lotes para poder ir guardando y reportando progreso
    CHUNK = 200
    for i in range(0, len(folios), CHUNK):
        chunk = folios[i : i + CHUNK]
        res = fetch_numerales_batch(chunk)
        for folio, (ini, fin, com, form_num) in res.items():
            if ini or fin:
                ok = patch_numeral(folio, ini, fin, com, form_num)
                if ok:
                    con_num += 1
                else:
                    errores += 1
            else:
                # Lavadora sin numeral registrado → marcar explícitamente vacío
                # (el comentario y el flag de campo del formulario se guardan igual)
                patch_numeral(folio, None, None, com, form_num)
                sin_num += 1
        log(f"Procesados {min(i+CHUNK, len(folios)):>5}/{len(folios)} | "
            f"con numeral: {con_num} | sin: {sin_num} | err: {errores}", "PROG")

    print("-" * 65)
    log(f"COMPLETADO en {time.time()-t0:.0f}s | "
        f"{con_num} con numeral, {sin_num} sin numeral, {errores} errores", "OK")


if __name__ == "__main__":
    main()

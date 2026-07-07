"""
sync_numerales_subtarea.py
==========================
Pobla la tabla `numerales_subtarea` con 1 fila por (OT × activo con numeral).

A diferencia de sync_numerales.py (que persiste 1 numeral por OT en
`ordenes_trabajo`), aquí desglosamos:
  - Cada subtarea de la OT en Fracttal que tiene su par TOMA DE NUMERAL
    INICIAL / FINAL en el formulario.
  - Asociación correcta usando id_work_order_task (en singular) — el campo
    que liga cada item del formulario a su subtarea específica.

Resultado: una OS-37930 con Lavadora + Aspiradora genera 2 filas, cada una
con su propio numeral y veredicto de calidad. El KPI considera la OT mala
si CUALQUIER subtarea con numeral falla.

Uso:
  python sync_numerales_subtarea.py                    (backfill 2026-01-01)
  python sync_numerales_subtarea.py --desde 2026-06-01
  python sync_numerales_subtarea.py --modo incremental (últimas 72h)
  python sync_numerales_subtarea.py --folios OS-37930,OS-38066
"""

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# Compartimos credenciales / helpers con sync_numerales.py
from sync_numerales import (
    SUPABASE_URL, SUPABASE_KEY, FRACTTAL_BASE, ID_COMPANY,
    get_token, _sb_headers, log,
)
from data import eval_numeral_kpi, _numeral_raw_int

TABLE_DESTINO = "numerales_subtarea"
TABLE_OT      = "ordenes_trabajo"

FRACTTAL_WO       = f"{FRACTTAL_BASE}/api/work_orders/"
FRACTTAL_SUBTASKS = f"{FRACTTAL_BASE}/api/work_orders_subtasks/"

WORKERS = 16


def _tipo_activo(nombre: str) -> str:
    """Devuelve 'lavadora' | 'aspiradora' | 'lavainterior' | 'otro'.

    Usa primer token para evitar falsos positivos (ej. 'FICHERO LAVADORA
    RM5' es FICHERO, no lavadora). Marcas MSELF/SEAL/SMART siempre
    vienen precedidas por 'LAVADORA' en Fracttal → cae en el primer token.
    """
    n = (nombre or "").strip().upper()
    if not n:
        return "otro"
    first = n.split()[0]
    if first == "LAVAINTERIORES" or first.startswith("LAVATAP"):
        return "lavainterior"
    if first == "LAVABICICLETAS":
        return "otro"
    if first.startswith("ASPIRA"):
        return "aspiradora"
    if first in ("FICHERO", "TERMO", "DISPENSADOR", "COMPRESOR",
                 "BOMBA", "BOMBAS", "KIT", "GRUPO", "PUENTE") \
       or first.startswith("ABLANDADOR"):
        return "otro"
    if first.startswith("LAVADORA") or first.startswith("HIDROLAV"):
        return "lavadora"
    if "HIDROLAVADORA" in n:
        return "lavadora"
    return "otro"


def query_preventiva_folios(desde: str) -> list:
    """Folios candidatos: preventivas y correctivas (todas) desde `desde`.
    No filtramos por nombre_activo porque OTs compuestas tienen activos
    principales que no son lavadora pero contienen subtareas que sí lo son."""
    folios, offset, page = [], 0, 1000
    while True:
        url = (f"{SUPABASE_URL}/rest/v1/{TABLE_OT}"
               f"?select=id_ot&fecha_creacion=gte.{desde}"
               f"&or=(tipo_tarea.ilike.*PREVENTIVA*,tipo_tarea.ilike.*CORRECTIVA*)"
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


def fetch_subtareas_numeral(folio: str) -> list:
    """Devuelve una lista de dicts con el numeral por subtarea:
       [{id_work_order_task, codigo_activo, nombre_activo, tipo_activo,
         numeral_inicial, numeral_final}, ...]
       Solo incluye subtareas que en el formulario tenían el par NUMERAL.
    """
    h = {"Authorization": f"Bearer {get_token()}"}
    # 1) Subtareas de la OT
    try:
        r = requests.get(FRACTTAL_WO, headers=h,
                         params={"wo_folio": folio, "id_company": ID_COMPANY, "limit": 50},
                         timeout=30)
        subtareas = r.json().get("data", []) or []
    except Exception:
        return []
    if not subtareas:
        return []
    # Indexar por id_work_orders_tasks (clave del join con items)
    idx = {}
    for s in subtareas:
        kid = s.get("id_work_orders_tasks")
        if kid is None:
            continue
        # Saltar subtareas que nunca se ejecutaron
        if str(s.get("task_status", "")).upper() == "NO_STARTED":
            continue
        idx[kid] = {
            "id_work_order_task": kid,
            "codigo_activo":      s.get("code"),
            "nombre_activo":      (s.get("items_log_description") or "").strip(),
            "tipo_activo":        _tipo_activo(s.get("items_log_description") or ""),
            "numeral_inicial":    None,
            "numeral_final":      None,
            "form_tiene_numeral": False,
            # Flags: plantilla del formulario incluye estos campos operativos
            # (Shell rollout 08-jun-2026). Necesarios para distinguir en el
            # ranking "técnico no llenó" vs "plantilla vieja de Fracttal".
            "form_tiene_bomba":       False,
            "form_tiene_consumo":     False,
            "form_tiene_tiempo":      False,
            "form_tiene_produccion":  False,
            "bomba_dosificadora":     None,
            "consumo_insumos":        None,
            "tiempo_fichas_seg":      None,
            "lts_hr_produccion_final": None,
            "fecha_inicio_subtarea":  s.get("initial_date"),
            "fecha_fin_subtarea":     s.get("final_date"),
        }

    # 2) Items del formulario (NUMERAL INICIAL/FINAL) — limit alto para no truncar
    try:
        r2 = requests.get(FRACTTAL_SUBTASKS, headers=h,
                          params={"wo_folio": folio, "id_company": ID_COMPANY, "limit": 500},
                          timeout=30)
        items = r2.json().get("data", []) or []
    except Exception:
        items = []

    for it in items:
        desc = (it.get("description") or "").upper()
        kid = it.get("id_work_order_task")
        if kid is None or kid not in idx:
            continue
        val = (str(it.get("value")) if it.get("value") is not None else "").strip()
        val_empty = (not val or val.lower() in ("none", "null"))
        if "NUMERAL" in desc:
            idx[kid]["form_tiene_numeral"] = True
            if val_empty:
                continue
            t = it.get("id_task_form_item_type")
            if t == 3 and idx[kid]["numeral_inicial"] is None:
                idx[kid]["numeral_inicial"] = val
            elif t == 5 and idx[kid]["numeral_final"] is None:
                idx[kid]["numeral_final"] = val
        else:
            # Marcamos que la plantilla incluye estos campos aunque el
            # valor esté vacío (técnico no llenó vs plantilla vieja).
            if "BOMBA DOSIFICADORA" in desc:
                idx[kid]["form_tiene_bomba"] = True
                if not val_empty:
                    idx[kid]["bomba_dosificadora"] = val
            elif "CONSUMO DE INSUMOS" in desc:
                idx[kid]["form_tiene_consumo"] = True
                if not val_empty:
                    idx[kid]["consumo_insumos"] = val
            elif "TIEMPO FICHAS" in desc:
                idx[kid]["form_tiene_tiempo"] = True
                if not val_empty:
                    idx[kid]["tiempo_fichas_seg"] = val
            elif "LT/HRS PRODUCCI" in desc and "FINAL" in desc:
                # Ejemplo Fracttal: "LT/HRS PRODUCCIÓN FINAL" = "125"
                # (Solo el FINAL; ignoramos LT/HRS PRODUCCIÓN INICIAL,
                # LT/HRS DESCARGA INICIAL/FINAL — no son relevantes.)
                idx[kid]["form_tiene_produccion"] = True
                if not val_empty:
                    idx[kid]["lts_hr_produccion_final"] = val

    # 3) Filtrar: solo subtareas cuyo formulario tiene campos NUMERAL.
    #    Subtareas duplicadas del mismo equipo con plantilla sin numeral
    #    quedan excluidas (no penalizan al técnico).
    out = []
    for kid, row in idx.items():
        if row["form_tiene_numeral"]:
            out.append(row)
    return out


def evaluar_calidad(row: dict) -> tuple:
    """Aplica eval_numeral_kpi a esta subtarea concreta. Devuelve
    (numeral_ok, motivo, fichas_periodo)."""
    es_lavadora = row["tipo_activo"] in ("lavadora", "aspiradora", "lavainterior")
    ok, motivo = eval_numeral_kpi(es_lavadora, row["numeral_inicial"], row["numeral_final"])
    vi = _numeral_raw_int(row["numeral_inicial"])
    vf = _numeral_raw_int(row["numeral_final"])
    fichas = None
    if vi is not None and vf is not None and 0 <= vf - vi <= 1_000_000:
        fichas = vf - vi
    return ok, motivo, fichas


def upsert_subtareas(folio: str, filas: list) -> tuple:
    """Upsert por (id_ot, id_work_order_task). Devuelve (ok_count, err_count)."""
    if not filas:
        return 0, 0
    payload = []
    for r in filas:
        ok, motivo, fichas = evaluar_calidad(r)
        payload.append({
            "id_ot":              folio,
            "id_work_order_task": r["id_work_order_task"],
            "codigo_activo":      r["codigo_activo"],
            "nombre_activo":      r["nombre_activo"],
            "tipo_activo":        r["tipo_activo"],
            "numeral_inicial":    r["numeral_inicial"],
            "numeral_final":      r["numeral_final"],
            "fichas_periodo":     fichas,
            "numeral_ok":         ok,
            "motivo":             motivo,
            "bomba_dosificadora":    r.get("bomba_dosificadora"),
            "consumo_insumos":       r.get("consumo_insumos"),
            "tiempo_fichas_seg":     r.get("tiempo_fichas_seg"),
            "lts_hr_produccion_final": r.get("lts_hr_produccion_final"),
            "form_tiene_bomba":      r.get("form_tiene_bomba", False),
            "form_tiene_consumo":    r.get("form_tiene_consumo", False),
            "form_tiene_tiempo":     r.get("form_tiene_tiempo", False),
            "form_tiene_produccion": r.get("form_tiene_produccion", False),
            "fecha_inicio_subtarea": r.get("fecha_inicio_subtarea"),
            "fecha_fin_subtarea":    r.get("fecha_fin_subtarea"),
            "updated_at":         datetime.now(timezone.utc).isoformat(),
        })
    # Upsert por (id_ot, id_work_order_task) — declarada UNIQUE en la migración
    h = _sb_headers(write=True)
    h["Prefer"] = "resolution=merge-duplicates,return=minimal"
    h["Content-Type"] = "application/json"
    url = (f"{SUPABASE_URL}/rest/v1/{TABLE_DESTINO}"
           f"?on_conflict=id_ot,id_work_order_task")
    for intento in range(3):
        try:
            r = requests.post(url, headers=h, data=json.dumps(payload), timeout=30)
            if r.status_code in (200, 201, 204):
                return len(payload), 0
            # Fallback: si aún no se aplicó la migración form_tiene_*, quitar
            # esas 3 columnas del payload y reintentar sin ellas (backward
            # compat). Detectamos por el mensaje PGRST204 "column ... does
            # not exist".
            if r.status_code == 400 and ("form_tiene_" in r.text or "lts_hr_" in r.text):
                for rec in payload:
                    rec.pop("form_tiene_bomba", None)
                    rec.pop("form_tiene_consumo", None)
                    rec.pop("form_tiene_tiempo", None)
                    rec.pop("form_tiene_produccion", None)
                    rec.pop("lts_hr_produccion_final", None)
                r2 = requests.post(url, headers=h, data=json.dumps(payload), timeout=30)
                if r2.status_code in (200, 201, 204):
                    return len(payload), 0
                if intento == 2:
                    log(f"upsert {folio} (retry sin form_tiene_*) -> {r2.status_code}: {r2.text[:200]}", "ERR")
                    return 0, len(payload)
                continue
            if intento == 2:
                # Usar ASCII en el log (Windows cp1252 explota con flechas Unicode)
                log(f"upsert {folio} -> HTTP {r.status_code}: {r.text[:200]}", "ERR")
                return 0, len(payload)
        except requests.exceptions.RequestException:
            if intento == 2:
                return 0, len(payload)
            time.sleep(2 * (intento + 1))
    return 0, len(payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default="2026-01-01")
    ap.add_argument("--modo",  choices=["completo","incremental"], default="completo")
    ap.add_argument("--folios", default="")
    args = ap.parse_args()

    if args.folios:
        folios = [f.strip() for f in args.folios.split(",") if f.strip()]
        log(f"Folios puntuales: {len(folios)}")
    else:
        desde = ((datetime.now() - timedelta(hours=72)).strftime("%Y-%m-%d")
                 if args.modo == "incremental" else args.desde)
        log(f"Buscando OTs desde {desde}...")
        folios = query_preventiva_folios(desde)
        log(f"Encontradas {len(folios)} OTs candidatas", "OK")

    if not folios:
        log("Sin folios", "WARN")
        return

    log("Extrayendo subtareas por activo (paralelo)...", "PROG")
    print("-"*65)
    t0 = time.time()
    rows_ok = rows_err = ots_con_sub = ots_sin_sub = 0
    CHUNK = 200
    for i in range(0, len(folios), CHUNK):
        chunk = folios[i:i+CHUNK]
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(fetch_subtareas_numeral, f): f for f in chunk}
            for fut in as_completed(futs):
                folio = futs[fut]
                try:
                    filas = fut.result()
                except Exception:
                    filas = []
                if not filas:
                    ots_sin_sub += 1
                    continue
                ots_con_sub += 1
                ok, err = upsert_subtareas(folio, filas)
                rows_ok  += ok
                rows_err += err
        log(f"Procesadas {min(i+CHUNK,len(folios)):>5}/{len(folios)} | "
            f"OTs con subtareas: {ots_con_sub} | filas upserted: {rows_ok} | err: {rows_err}",
            "PROG")
    print("-"*65)
    log(f"COMPLETADO en {time.time()-t0:.0f}s | "
        f"{ots_con_sub} OTs con subtareas-numeral | "
        f"{rows_ok} filas upserted | "
        f"{ots_sin_sub} OTs sin subtareas-numeral | "
        f"{rows_err} errores", "OK")


if __name__ == "__main__":
    main()

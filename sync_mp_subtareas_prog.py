"""
sync_mp_subtareas_prog.py
=========================
Puebla mp_subtareas_prog: 1 fila por subtarea de cada OT preventiva,
con su cal_date_maintenance individual (equivalente a la 'Fecha
Programada' del export Excel de Fracttal — hoja BASE).

Razón: ordenes_trabajo guarda 1 sola fecha por OT (la del activo
principal), pero una OT preventiva compuesta tiene N subtareas y
cada una tiene su propia fecha programada. Para el Kanban de
Planificación necesitamos verlas todas.

Uso:
    python sync_mp_subtareas_prog.py                  (últimos 90 días)
    python sync_mp_subtareas_prog.py --desde 2026-05-01
    python sync_mp_subtareas_prog.py --folios OS-39106,OS-39232
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

# Reutilizamos credenciales/helpers del sync existente.
from sync_numerales_subtarea import (
    SUPABASE_URL, SUPABASE_KEY, FRACTTAL_BASE, ID_COMPANY,
    get_token, _sb_headers, log,
)

TABLE = "mp_subtareas_prog"
FRACTTAL_WO = f"{FRACTTAL_BASE}/api/work_orders/"
WORKERS = 12


def query_preventiva_folios(desde: str) -> list:
    """Folios de OTs preventivas en Supabase desde una fecha (usa
    fecha_programada, no fecha_creacion, para captar OTs viejas con
    subtareas programadas recientes)."""
    folios, offset, page = [], 0, 1000
    while True:
        url = (f"{SUPABASE_URL}/rest/v1/ordenes_trabajo"
               f"?select=id_ot&fecha_programada=gte.{desde}"
               f"&tipo_tarea=ilike.*PREVENTIVA*"
               f"&order=fecha_programada.desc&limit={page}&offset={offset}")
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
    return sorted(set(folios))


def fetch_subtareas_folio(folio: str) -> list:
    """Devuelve lista de dicts (una por subtarea) desde /api/work_orders/."""
    h = {"Authorization": f"Bearer {get_token()}"}
    for attempt in range(3):
        try:
            r = requests.get(FRACTTAL_WO, headers=h,
                             params={"wo_folio": folio, "id_company": ID_COMPANY,
                                     "limit": 100},
                             timeout=60)
            if r.status_code == 200:
                subs = r.json().get("data", []) or []
                break
            if attempt == 2:
                return []
            time.sleep(2 * (attempt + 1))
        except Exception:
            if attempt == 2:
                return []
            time.sleep(2 * (attempt + 1))
    else:
        return []

    rows = []
    for s in subs:
        kid = s.get("id_work_orders_tasks")
        if kid is None:
            continue
        # Cliente: extraemos de parent_description que trae la jerarquía
        # completa (ej. '// COPEC/ COPEC BUIN/ LAVADORA/'). El primer
        # segmento es siempre el cliente. groups_1_description a veces
        # trae valores no confiables ('ACTIVA', 'CONTRATO', etc.).
        _parent = str(s.get("parent_description", "") or "").strip()
        _parent_parts = [p.strip().upper() for p in _parent.split("/") if p.strip()]
        cli_raw = _parent_parts[0] if _parent_parts else ""
        cli_norm = {
            "ENEX":   "SHELL (Enex)",
            "SHELL":  "SHELL (Enex)",
            "ARAMCO": "ESMAX (Aramco)",
            "ESMAX":  "ESMAX (Aramco)",
            "COPEC":  "COPEC",
            "ABASTIBLE": "ABASTIBLE",
            "AUTEC":  "AUTEC",
        }.get(cli_raw, cli_raw or None)
        rows.append({
            "id_ot":                folio,
            "id_work_order_task":   kid,
            "codigo_activo":        s.get("code"),
            "nombre_activo":        (s.get("items_log_description") or "").strip() or None,
            "cliente":              cli_norm,
            "codigo_eds":           s.get("groups_2_description") or None,
            "estacion":             (s.get("parent_description") or "").strip() or None,
            "tipo_tarea":           s.get("tasks_log_task_type_main") or None,
            "plan_tareas":          s.get("groups_description") or None,
            "responsable":          s.get("personnel_description") or s.get("user_assigned") or None,
            "cal_date_maintenance": s.get("cal_date_maintenance"),
            "task_status":          str(s.get("task_status", "")).upper() or None,
            "updated_at":           datetime.now(timezone.utc).isoformat(),
        })
    return rows


def upsert(rows: list) -> int:
    """Upsert por (id_ot, id_work_order_task)."""
    if not rows:
        return 0
    h = _sb_headers(write=True)
    h["Prefer"] = "resolution=merge-duplicates,return=minimal"
    h["Content-Type"] = "application/json"
    url = f"{SUPABASE_URL}/rest/v1/{TABLE}?on_conflict=id_ot,id_work_order_task"
    for i in range(3):
        try:
            r = requests.post(url, headers=h, data=json.dumps(rows), timeout=60)
            if r.status_code in (200, 201, 204):
                return len(rows)
            if i == 2:
                log(f"upsert -> {r.status_code}: {r.text[:200]}", "ERR")
                return 0
        except requests.exceptions.RequestException:
            if i == 2:
                return 0
            time.sleep(2 * (i + 1))
    return 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default=None,
                    help="YYYY-MM-DD (default: hoy - 90 días)")
    ap.add_argument("--folios", default="",
                    help="Folios específicos coma-separados (bypass query Supabase)")
    args = ap.parse_args()

    desde = args.desde or (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

    if args.folios:
        folios = [f.strip() for f in args.folios.split(",") if f.strip()]
        log(f"Folios manuales: {len(folios)}")
    else:
        log(f"Buscando OTs preventivas con fecha_programada >= {desde}...")
        folios = query_preventiva_folios(desde)
        log(f"Encontradas {len(folios)} OTs preventivas", "OK")

    if not folios:
        log("Sin folios", "WARN")
        return

    log(f"Descargando subtareas en paralelo (workers={WORKERS})...", "PROG")
    print("-" * 65)
    t0 = time.time()
    total_rows = 0
    ots_ok = 0
    ots_sin = 0
    CHUNK = 100
    for i in range(0, len(folios), CHUNK):
        chunk = folios[i:i + CHUNK]
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(fetch_subtareas_folio, f): f for f in chunk}
            batch_rows = []
            for fut in as_completed(futs):
                try:
                    rows = fut.result()
                except Exception:
                    rows = []
                if rows:
                    ots_ok += 1
                    batch_rows.extend(rows)
                else:
                    ots_sin += 1
        if batch_rows:
            up = upsert(batch_rows)
            total_rows += up
        log(f"Procesadas {min(i+CHUNK,len(folios)):>5}/{len(folios)} · "
            f"OTs OK: {ots_ok} · Subtareas: {total_rows}", "PROG")

    print("-" * 65)
    log(f"COMPLETADO en {time.time()-t0:.0f}s · {ots_ok} OTs · "
        f"{total_rows} subtareas upserted · {ots_sin} OTs sin datos", "OK")


if __name__ == "__main__":
    main()

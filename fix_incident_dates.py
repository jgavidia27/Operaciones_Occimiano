"""
fix_incident_dates.py — Poblar fecha_incidente en Supabase para todas las OTs.

Jerarquía de fuentes (del perfil de Fracttal + análisis de la API):
  1. date_incident de work_requests (solicitudes) — Ya cargado para 536 OTs
  2. event_date de work_orders — Fecha del incidente registrada directamente en la OT
  3. creation_date de work_orders — Último fallback (momento de creación de la OT)

Solo se actualizan filas donde fecha_incidente IS NULL.
"""
import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip()

FRACTTAL_BASE_URL      = os.getenv("FRACTTAL_BASE_URL", "https://app.fracttal.com")
FRACTTAL_CLIENT_ID     = os.getenv("FRACTTAL_CLIENT_ID")
FRACTTAL_CLIENT_SECRET = os.getenv("FRACTTAL_CLIENT_SECRET")
SUPABASE_URL           = os.getenv("SUPABASE_URL")
SUPABASE_KEY           = os.getenv("SUPABASE_KEY")

_FRACTTAL_MAX = 100
_WORKERS      = 16


def get_fracttal_token() -> str:
    resp = requests.post(f"{FRACTTAL_BASE_URL}/oauth/token", data={
        "grant_type":    "client_credentials",
        "client_id":     FRACTTAL_CLIENT_ID,
        "client_secret": FRACTTAL_CLIENT_SECRET,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _fetch_page(endpoint, params, headers):
    try:
        resp = requests.get(f"{FRACTTAL_BASE_URL}{endpoint}",
                            headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        return resp.json().get("data") or []
    except Exception:
        return []


def fetch_all(endpoint, params, headers, max_records=50_000):
    base_params = dict(params or {})
    first = _fetch_page(endpoint, {**base_params, "start": 0, "limit": _FRACTTAL_MAX}, headers)
    if not first or len(first) < _FRACTTAL_MAX:
        return first or []
    all_results = list(first)
    start = _FRACTTAL_MAX
    while start < max_records:
        batch_starts = list(range(start, min(start + _WORKERS * _FRACTTAL_MAX, max_records + 1), _FRACTTAL_MAX))
        pages = {}
        with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
            futs = {ex.submit(_fetch_page, endpoint, {**base_params, "start": s, "limit": _FRACTTAL_MAX}, headers): s for s in batch_starts}
            for fut in as_completed(futs):
                pages[futs[fut]] = fut.result()
        done = False
        for s in sorted(pages.keys()):
            batch = pages[s]
            all_results.extend(batch)
            if len(batch) < _FRACTTAL_MAX:
                done = True
                break
        if done:
            break
        start += _WORKERS * _FRACTTAL_MAX
    return all_results[:max_records]


def main():
    print("=" * 70)
    print("FIX: Poblando fecha_incidente en Supabase")
    print("=" * 70)

    # ── 1. Autenticación ─────────────────────────────────────────────────────
    token = get_fracttal_token()
    headers_f = {"Authorization": f"Bearer {token}"}
    headers_sb = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    print("Autenticación exitosa.\n")

    # ── 2. Cargar TODAS las work_requests (fuente #1: date_incident) ─────────
    print("Cargando solicitudes de trabajo (work_requests)...")
    raw_req = fetch_all("/api/work_requests/",
                        params={"since": "2025-01-01T00:00:00-00", "type_date": "date"},
                        headers=headers_f, max_records=10_000)
    # folio → date_incident (o date como fallback)
    req_incidents = {}
    for r in raw_req:
        folio = str(r.get("wo_folio") or "").strip()
        if folio:
            # Priorizar date_incident sobre date
            val = r.get("date_incident") or r.get("date")
            if val and (folio not in req_incidents):
                req_incidents[folio] = val
    print(f"  → {len(req_incidents)} solicitudes con fecha de incidente.\n")

    # ── 3. Cargar TODAS las work_orders (fuente #2: event_date) ──────────────
    print("Cargando órdenes de trabajo (work_orders)...")
    raw_wo = fetch_all("/api/work_orders",
                       params={"since": "2026-01-01T00:00:00-00", "type_date": "creation_date"},
                       headers=headers_f, max_records=20_000)
    
    # Deduplicar por folio, guardando event_date y creation_date
    wo_dates = {}
    for wo in raw_wo:
        folio = wo.get("wo_folio")
        if not folio or folio in wo_dates:
            continue
        wo_dates[folio] = {
            "event_date":    wo.get("event_date"),
            "creation_date": wo.get("creation_date"),
        }
    print(f"  → {len(wo_dates)} OTs únicas con campos de fecha.\n")

    # ── 4. Construir mapa final: folio → mejor fecha_incidente ───────────────
    # Jerarquía: work_request.date_incident > work_order.event_date > work_order.creation_date
    incident_map = {}
    for folio, dates in wo_dates.items():
        # Fuente 1: work_requests
        if folio in req_incidents:
            incident_map[folio] = req_incidents[folio]
            continue
        # Fuente 2: event_date de la OT
        if dates["event_date"]:
            incident_map[folio] = dates["event_date"]
            continue
        # Fuente 3: creation_date como fallback
        if dates["creation_date"]:
            incident_map[folio] = dates["creation_date"]
    
    print(f"Mapa de fecha_incidente construido: {len(incident_map)} folios.\n")

    # ── 5. Obtener folios con fecha_incidente NULL en Supabase ────────────────
    print("Obteniendo OTs sin fecha_incidente desde Supabase...")
    null_folios = []
    offset = 0
    page_size = 1000
    while True:
        resp = requests.get(f"{SUPABASE_URL}/rest/v1/ordenes_trabajo",
            headers=headers_sb,
            params={
                "fecha_incidente": "is.null",
                "select": "id_ot",
                "limit": page_size,
                "offset": offset,
            }, timeout=15)
        batch = resp.json()
        null_folios.extend([r["id_ot"] for r in batch])
        if len(batch) < page_size:
            break
        offset += page_size
    print(f"  → {len(null_folios)} OTs con fecha_incidente NULL.\n")

    # ── 6. Actualizar en Supabase ─────────────────────────────────────────────
    updates_to_send = []
    for folio in null_folios:
        if folio in incident_map:
            updates_to_send.append({
                "id_ot": folio,
                "fecha_incidente": incident_map[folio],
            })
    
    print(f"Actualizaciones a enviar: {len(updates_to_send)} de {len(null_folios)} nulas.")
    
    if not updates_to_send:
        print("No hay actualizaciones pendientes.")
        return

    # UPSERT en lotes de 100
    headers_sb_upsert = {**headers_sb, "Prefer": "resolution=merge-duplicates"}
    url_sb = f"{SUPABASE_URL}/rest/v1/ordenes_trabajo"
    batch_size = 100
    total_ok = 0

    for i in range(0, len(updates_to_send), batch_size):
        batch = updates_to_send[i:i+batch_size]
        try:
            resp = requests.post(url_sb, headers=headers_sb_upsert, json=batch, timeout=30)
            if resp.status_code in (200, 201):
                total_ok += len(batch)
                if total_ok % 500 == 0 or total_ok == len(updates_to_send):
                    print(f"  Progreso: {total_ok}/{len(updates_to_send)}")
            else:
                print(f"  Error lote {i//batch_size + 1}: {resp.text[:200]}")
        except Exception as e:
            print(f"  Excepción lote {i//batch_size + 1}: {e}")

    print(f"\n{'=' * 70}")
    print(f"Finalizado. {total_ok} registros actualizados con fecha_incidente.")
    print(f"{'=' * 70}")


if __name__ == '__main__':
    main()

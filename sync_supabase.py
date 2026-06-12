import os
import requests
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── CARGAR VARIABLES DE ENTORNO ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip()

# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────
FRACTTAL_BASE_URL      = os.getenv("FRACTTAL_BASE_URL", "https://app.fracttal.com")
FRACTTAL_CLIENT_ID     = os.getenv("FRACTTAL_CLIENT_ID")
FRACTTAL_CLIENT_SECRET = os.getenv("FRACTTAL_CLIENT_SECRET")
FRACTTAL_COMPANY_ID    = os.getenv("FRACTTAL_COMPANY_ID", "1507")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

_FRACTTAL_MAX = 100
_WORKERS      = 16

# ── AUTENTICACIÓN FRACTTAL ───────────────────────────────────────────────────
def get_fracttal_token() -> str:
    url = f"{FRACTTAL_BASE_URL}/oauth/token"
    resp = requests.post(url, data={
        "grant_type":    "client_credentials",
        "client_id":     FRACTTAL_CLIENT_ID,
        "client_secret": FRACTTAL_CLIENT_SECRET,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]

# ── PAGINACIÓN PARALELA FRACTTAL ──────────────────────────────────────────────
def _fetch_page(endpoint: str, params: dict, headers: dict) -> list:
    try:
        resp = requests.get(
            f"{FRACTTAL_BASE_URL}{endpoint}",
            headers=headers,
            params=params,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json().get("data") or []
    except Exception as e:
        print(f"Error cargando página {params.get('start')}: {e}")
        return []

def fetch_all_fracttal(endpoint: str, params: dict, headers: dict, max_records: int = 20_000) -> list:
    base_params = dict(params or {})
    
    # Primera llamada serial (detectar si hay datos)
    first = _fetch_page(endpoint, {**base_params, "start": 0, "limit": _FRACTTAL_MAX}, headers)
    if not first or len(first) < _FRACTTAL_MAX:
        return first or []

    all_results = list(first)
    start = _FRACTTAL_MAX

    while start < max_records:
        batch_starts = list(range(start, min(start + _WORKERS * _FRACTTAL_MAX, max_records + 1), _FRACTTAL_MAX))
        pages = {}
        
        with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
            fut_to_start = {
                ex.submit(_fetch_page, endpoint, {**base_params, "start": s, "limit": _FRACTTAL_MAX}, headers): s
                for s in batch_starts
            }
            for fut in as_completed(fut_to_start):
                pages[fut_to_start[fut]] = fut.result()

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

# ── MAPEOS DE DATOS ───────────────────────────────────────────────────────────
def parse_trigger(trigger_str) -> str | None:
    """Convierte 'DATE$EVERY$1$MONTHS' → 'Cada 1 mes'."""
    if not trigger_str:
        return None
    parts = str(trigger_str).split("$")
    if len(parts) >= 4 and parts[1].upper() == "EVERY":
        n, unit = parts[2], parts[3].upper()
        unit_map = {
            "MONTHS":  "mes"     if n == "1" else "meses",
            "WEEKS":   "semana"  if n == "1" else "semanas",
            "DAYS":    "día"     if n == "1" else "días",
            "YEARS":   "año"     if n == "1" else "años",
            "HOURS":   "hora"    if n == "1" else "horas",
        }
        return f"Cada {n} {unit_map.get(unit, unit.lower())}"
    return trigger_str  # fallback: devolver raw si el formato es desconocido

def map_task_status(status) -> str | None:
    """Mapea task_status de Fracttal a texto legible."""
    mapping = {
        "DONE":        "Finalizada",
        "IN_PROCESS":  "En Proceso",
        "IN_PROGRESS": "En Proceso",
        "STARTED":     "En Proceso",
        "PENDING":     "No Iniciada",
        "NO_STARTED":  "No Iniciada",
        "WAITING":     "En Espera",
        "REVIEWED":    "En Revisión",
    }
    if not status:
        return None
    return mapping.get(str(status).upper(), str(status))

def secs_to_hhmm(secs) -> str | None:
    """Convierte segundos a formato HH:MM (ej: 600 → '00:10')."""
    if secs is None:
        return None
    try:
        total = int(secs)
        h = total // 3600
        m = (total % 3600) // 60
        return f"{h:02d}:{m:02d}"
    except Exception:
        return None

def map_priority(prio_desc: str) -> str:
    p = str(prio_desc or "").upper().strip()
    if "VERY_HIGH" in p or "CRITIC" in p:
        return "Muy Alta"
    if p == "HIGH":
        return "Alta"
    if "MEDIUM" in p:
        return "Media"
    if "LOW" in p or "VERY_LOW" in p:
        return "Baja"
    return prio_desc or "Media"

def map_status(status_id: int, done: bool, wo_final_date: str, custom_desc: str) -> str:
    # 1. Cancelaciones explícitas en el campo personalizado
    if custom_desc and any(kw in str(custom_desc).upper() for kw in ["CANCEL", "ERROR", "RECHAZ"]):
        return "Cancelado"
    
    # 2. Mapeo por ID de estado de Fracttal
    # (1 = No Iniciada, 2 = En Proceso, 3 = Realizada, 4 = En Revisión, 5 = Cerrada, 6 = Cancelada)
    if status_id == 1:
        return "No Iniciada"
    elif status_id == 2:
        return "En Proceso"
    elif status_id == 3:
        if done:
            return "Finalizadas" if wo_final_date else "En Revisión"
        return "En Proceso"
    elif status_id == 4:
        return "En Revisión"
    elif status_id == 5:
        return "Finalizadas"
    elif status_id == 6:
        return "Cancelado"
    
    # Fallback por completitud de tareas
    return "Finalizadas" if done else "En Proceso"

# ── EJECUCIÓN PRINCIPAL (ETL) ────────────────────────────────────────────────
def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Error: Credenciales de Supabase no configuradas en el archivo .env")
        return
        
    print("Iniciando sincronización de Órdenes de Trabajo a Supabase...")
    
    # 1. Obtener token de Fracttal
    try:
        fracttal_token = get_fracttal_token()
        fracttal_headers = {"Authorization": f"Bearer {fracttal_token}"}
        print("Autenticación con Fracttal exitosa.")
    except Exception as e:
        print(f"Error de autenticación con Fracttal: {e}")
        return

    # 2. Obtener solicitudes de trabajo desde 2026 para mapear fecha_incidente
    print("Cargando solicitudes de trabajo desde Fracttal...")
    since_date = "2026-01-01T00:00:00-00"
    raw_req = fetch_all_fracttal(
        "/api/work_requests/",
        params={"since": since_date, "type_date": "date"},
        headers=fracttal_headers,
        max_records=10_000
    )
    
    # Construir diccionario de incidentes por folio
    # folio -> fecha de incidente o fecha de creación de solicitud
    requests_dict = {}
    for r in raw_req:
        folio = str(r.get("wo_folio") or "").strip()
        if folio:
            requests_dict[folio] = r.get("date_incident") or r.get("date")
    print(f"Se cargaron {len(requests_dict)} solicitudes para mapeo de incidentes.")

    # 3. Obtener órdenes de trabajo desde 2026
    print("Cargando órdenes de trabajo desde Fracttal...")
    raw_wo = fetch_all_fracttal(
        "/api/work_orders",
        params={"since": since_date, "type_date": "creation_date"},
        headers=fracttal_headers,
        max_records=20_000
    )
    print(f"Se obtuvieron {len(raw_wo)} registros brutos de tareas/OTs desde Fracttal.")

    # 4. Procesar y DEDUPLICAR filas por folio (id_ot) para evitar errores de restricción en Supabase
    print("Deduplicando y preparando registros...")
    unique_wos = {}
    for wo in raw_wo:
        folio = wo.get("wo_folio")
        if not folio:
            continue
            
        fecha_creacion = wo.get("creation_date") or wo.get("date_maintenance")
        fecha_finalizacion = wo.get("final_date") or wo.get("wo_final_date")
        fecha_incidente = requests_dict.get(folio)
        
        status_id = wo.get("id_status_work_order")
        done = bool(wo.get("done"))
        wo_final_date = wo.get("wo_final_date")
        custom_desc = wo.get("work_orders_status_custom_description")
        estado = map_status(status_id, done, wo_final_date, custom_desc)
        
        priority = map_priority(wo.get("priorities_description"))
        
        if folio in unique_wos:
            existing = unique_wos[folio]
            # Si encontramos una fecha de finalización válida, actualizarla si es mayor
            if fecha_finalizacion:
                if not existing["fecha_finalizacion"] or fecha_finalizacion > existing["fecha_finalizacion"]:
                    existing["fecha_finalizacion"] = fecha_finalizacion
            # Si el nuevo estado es más avanzado (ej: Finalizadas gana sobre En Proceso), actualizar
            if estado == "Finalizadas" and existing["estado"] != "Finalizadas":
                existing["estado"] = "Finalizadas"
            elif estado == "En Revisión" and existing["estado"] == "En Proceso":
                existing["estado"] = "En Revisión"
        else:
            unique_wos[folio] = {
                "id_ot":              folio,
                "estado":             estado,
                "codigo_activo":      wo.get("code"),
                "nombre_activo":      wo.get("items_log_description"),
                "ubicacion":          wo.get("parent_description"),
                "prioridad":          priority,
                "tipo_tarea":         wo.get("tasks_log_task_type_main"),
                "responsable":        str(wo.get("personnel_description") or "").strip(),
                "fecha_incidente":    fecha_incidente,
                "fecha_creacion":     fecha_creacion,
                "fecha_finalizacion": fecha_finalizacion,
                # ── Campos nuevos (preventivas) ──────────────────────────────
                "activador":          parse_trigger(wo.get("trigger_description")),
                "nombre_tarea":       str(wo.get("description") or "").strip() or None,
                "estado_tarea":       map_task_status(wo.get("task_status")),
                "duracion_estimada":  secs_to_hhmm(wo.get("tasks_duration")),
                "tiempo_ejecucion":   secs_to_hhmm(wo.get("real_duration")),
                "clasificacion_2":    str(wo.get("groups_2_description") or "").strip() or None,
                "fecha_programada":   wo.get("cal_date_maintenance"),
            }

    rows_to_upsert = list(unique_wos.values())
    print(f"Total OTs únicas preparadas para Supabase: {len(rows_to_upsert)}")

    # 5. Enviar a Supabase en lotes
    supabase_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates" # Activa UPSERT por clave primaria
    }
    url_sb = f"{SUPABASE_URL}/rest/v1/ordenes_trabajo"
    
    batch_size = 100
    total_upserted = 0
    
    for i in range(0, len(rows_to_upsert), batch_size):
        batch = rows_to_upsert[i:i+batch_size]
        try:
            resp = requests.post(url_sb, headers=supabase_headers, json=batch, timeout=30)
            if resp.status_code in (200, 201):
                total_upserted += len(batch)
                print(f"  Progreso: {total_upserted}/{len(rows_to_upsert)} cargados exitosamente.")
            else:
                print(f"  Error cargando lote {i//batch_size + 1}: {resp.text}")
        except Exception as e:
            print(f"  Excepción en lote {i//batch_size + 1}: {e}")

    print(f"Sincronización finalizada. Total cargados: {total_upserted} registros.")

if __name__ == '__main__':
    main()

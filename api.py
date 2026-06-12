"""
Fracttal One API — capa de acceso de datos (solo lectura).
─────────────────────────────────────────────────────────────────────────────
IMPORTANTE: Este módulo es READ-ONLY. Nunca se escriben ni modifican recursos
en Fracttal. Toda operación es GET.

Paginación paralela
───────────────────
La API de Fracttal limita cada llamada a 100 registros.
`fetch_all()` lanza N páginas en paralelo (ThreadPoolExecutor) para obtener
el historial completo en segundos en lugar de minutos.

Ejemplo de rendimiento (5 workers):
  1 000 registros =  10 páginas → ~2 s
  5 000 registros =  50 páginas → ~6 s
 10 000 registros = 100 páginas → ~12 s
"""

import requests
import threading
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

# ── Credenciales Fracttal Occimiano ──────────────────────────────────────────
BASE_URL      = "https://app.fracttal.com"
TOKEN_URL     = f"{BASE_URL}/oauth/token"
CLIENT_ID     = "KtHFO5pMskBbJ3lhPr"
CLIENT_SECRET = "bnpkpimGY4O0N9TxLUeKPXlKYRPV517m"
ID_COMPANY    = 1507    # Occimiano — servidor AMERICAN

_FRACTTAL_MAX = 100     # Máximo de registros por llamada (límite API)
_WORKERS      = 16      # Páginas en paralelo — 16 reduce el tiempo a ~mitad vs 8

_token_cache: dict = {"token": None, "expires_at": None}
_token_lock         = threading.Lock()


# ── Autenticación ─────────────────────────────────────────────────────────────

def get_token() -> str:
    """Devuelve un bearer token válido (thread-safe, renueva si expiró)."""
    with _token_lock:
        now = datetime.utcnow()
        if _token_cache["token"] and _token_cache["expires_at"] > now:
            return _token_cache["token"]
        resp = requests.post(TOKEN_URL, data={
            "grant_type":    "client_credentials",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        _token_cache["token"]      = data["access_token"]
        _token_cache["expires_at"] = now + timedelta(seconds=data["expires_in"] - 60)
        return _token_cache["token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_token()}"}


def _since(months_back: int) -> str:
    """Fecha de inicio en formato Fracttal: YYYY-MM-DDTHH:MM:SS-00"""
    dt = datetime.utcnow() - timedelta(days=months_back * 31)
    return dt.strftime("%Y-%m-%dT00:00:00-00")


# ── Página única (worker) ─────────────────────────────────────────────────────

def _fetch_page(endpoint: str, params: dict) -> list:
    """Descarga una página; retorna lista vacía si hay error no-crítico."""
    try:
        resp = requests.get(
            f"{BASE_URL}{endpoint}",
            headers=_headers(),
            params=params,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json().get("data") or []
    except Exception:
        return []


# ── Paginador paralelo ────────────────────────────────────────────────────────

def fetch_all(
    endpoint:    str,
    params:      dict | None = None,
    max_records: int  = 10_000,
    workers:     int  = _WORKERS,
) -> list:
    """
    Pagina completamente un endpoint Fracttal usando paginación paralela.

    Estrategia:
      1. Primera llamada serial para obtener el primer batch.
      2. Si el batch es completo (100), lanza páginas en lotes paralelos
         de `workers` hasta encontrar un batch incompleto o alcanzar max_records.
      3. El campo `total` del API no se usa para planificar páginas porque
         Fracttal lo devuelve como conteo global (sin considerar filtros de fecha).
    """
    base_params = dict(params or {})

    # ── Página 0 (serial para detectar si hay datos) ─────────────────────────
    first = _fetch_page(endpoint, {**base_params, "start": 0, "limit": _FRACTTAL_MAX})
    if not first or len(first) < _FRACTTAL_MAX:
        return first  # una sola página o sin datos

    all_results: list = list(first)
    start = _FRACTTAL_MAX

    # ── Páginas restantes en paralelo ────────────────────────────────────────
    while start < max_records:
        # Calcular starts para este lote paralelo
        batch_starts = list(range(start, min(start + workers * _FRACTTAL_MAX, max_records + 1), _FRACTTAL_MAX))

        pages: dict[int, list] = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_to_start = {
                ex.submit(_fetch_page, endpoint, {**base_params, "start": s, "limit": _FRACTTAL_MAX}): s
                for s in batch_starts
            }
            for fut in as_completed(fut_to_start):
                pages[fut_to_start[fut]] = fut.result()

        # Agregar resultados en orden de start
        done = False
        for s in sorted(pages.keys()):
            batch = pages[s]
            all_results.extend(batch)
            if len(batch) < _FRACTTAL_MAX:
                done = True
                break  # ya no hay más datos

        if done:
            break

        start += workers * _FRACTTAL_MAX

    return all_results[:max_records]


# ══════════════════════════════════════════════════════════════════════════════
# ÓRDENES DE TRABAJO
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600 * 8, show_spinner=False, persist="disk")
def load_work_orders(months_back: int = 3) -> list:
    """
    Carga OTs de los últimos `months_back` meses.
    Con paginación paralela (16 workers): ~3 meses ≈ 30-35 seg.
    Se cachea 8 h en memoria + en disco (persist='disk'): sobrevive
    reinicios del servidor sin volver a descargar.
    Usar "Actualizar datos" para forzar recarga manual.

    NOTA: la API de Fracttal devuelve OTs ordenadas de MÁS RECIENTE a MÁS ANTIGUA.
    El filtro `since`/`type_date` no actúa como se esperaría; en la práctica
    se traen todas las OTs del sistema ordenadas newest-first.
    Con max_records=20.000 y ~2.100 OTs/mes cubre ~9-10 meses hacia atrás
    (ej: hoy = Jun 2026 → datos desde ~Ago 2025), suficiente para T1, T2 y T3 2026
    más contexto para detección de reincidencias.
    """
    return fetch_all(
        "/api/work_orders",
        params={"since": _since(months_back), "type_date": "creation_date"},
        max_records=20_000,
    )


@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def load_work_orders_subtasks(months_back: int = 2) -> list:
    """
    Carga las sub-tareas (checks/formularios) de todas las OTs.
    Cada fila = un ítem del formulario de una tarea.
    Campos clave: wo_folio, description, value, is_required,
                  id_task_form_item_type, group, meter_description.
    TTL 30 min + persist disk: sobrevive reinicios sin re-descarga.
    """
    return fetch_all(
        "/api/work_orders_subtasks/",
        params={"since": _since(months_back), "type_date": "creation_date"},
        max_records=50_000,
    )


@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def load_wo_resources(months_back: int = 3) -> list:
    """
    Recursos (repuestos, RRHH, servicios) usados en OTs.
    Campos clave: wo_folio, type (1=inventario, 2=RRHH, 3=servicio),
                  description, qty, unit_cost, total_cost.
    TTL 30 min + persist disk: sobrevive reinicios sin re-descarga.
    """
    return fetch_all(
        "/api/wo_resources/",
        params={"since": _since(months_back)},
        max_records=15_000,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ITEMS / UBICACIONES (Catálogo Fracttal)
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600 * 8, show_spinner=False, persist="disk")
def load_items_catalog() -> list:
    """
    Carga el catálogo de ubicaciones (LOC-XXX) desde Fracttal.
    Filtra: code empieza con LOC- + active=True + available=True
    (equivale a Habilitado=SI y Fuera de servicio=No en la UI de Fracttal).
    Campos clave: code (LOC-XXX), barcode (código cliente), description, available.
    """
    all_items = fetch_all("/api/items/", params={"active": "true"}, max_records=5_000)
    return [
        r for r in all_items
        if str(r.get("code") or "").startswith("LOC-")
        and r.get("available") is not False   # excluye fuera de servicio
    ]


# ══════════════════════════════════════════════════════════════════════════════
# MEDIDORES
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def load_meters() -> list:
    """
    Catálogo completo de medidores activos (estado actual).
    Campos clave: serial, description, is_counter, counter_value,
                  counter_offset_value, last_data, units_description, code.
    """
    return fetch_all("/api/meters/", params={"active": "true"}, max_records=5_000)


@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def load_meters_reading(months_back: int = 6) -> list:
    """
    Historial de lecturas de todos los medidores.
    Campos clave: date_reading, data (date/value/accumulated_value),
                  items_code, items_description, units_description,
                  source (API/WORK_ORDER/MANUAL), trigger_run.
    TTL 30 min + persist disk: lecturas no cambian cada 5 min.
    """
    return fetch_all(
        "/api/meters_reading/",
        params={"since": _since(months_back), "type_date": "date_reading"},
        max_records=30_000,
    )


# ══════════════════════════════════════════════════════════════════════════════
# SOLICITUDES DE TRABAJO
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def load_work_requests(months_back: int = 6) -> list:
    """
    Solicitudes de trabajo de los últimos `months_back` meses.
    Campos clave: id_code, date, description, types_description,
                  items_description, requests_x_status_description,
                  wo_folio, accounts_name, requested_by, date_solution.
    """
    return fetch_all(
        "/api/work_requests/",
        params={"since": _since(months_back), "type_date": "date"},
        max_records=5_000,
    )


# ══════════════════════════════════════════════════════════════════════════════
# TERCEROS
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def load_third_parties() -> list:
    """Catálogo de terceros (clientes, proveedores)."""
    return fetch_all("/api/third_parties", max_records=5_000)

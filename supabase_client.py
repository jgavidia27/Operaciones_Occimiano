"""
supabase_client.py
==================
Cliente Supabase para el dashboard Occimiano.
Reemplaza las llamadas directas a Fracttal API y Excel.

Todas las funciones mantienen el mismo nombre y tipo de retorno
que las originales en api.py y gdrive.py para compatibilidad.
"""

import os
import requests
import streamlit as st
import pandas as pd
from datetime import datetime, timezone

# ── Cargar .env en desarrollo local ──────────────────────────────────────────
def _load_env_file():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())

_load_env_file()

# ── Credenciales — se leen en tiempo de ejecución (no al importar) ───────────
def _get_creds() -> tuple[str, str]:
    """
    Lee SUPABASE_URL y SUPABASE_KEY en este orden:
    1. st.secrets  (Streamlit Cloud)
    2. os.environ  (local via .env ya cargado por _load_env_file)
    Se llama dentro de _query() para que st.secrets esté disponible.
    """
    url = key = ""
    try:
        url = str(st.secrets["SUPABASE_URL"])
        key = str(st.secrets["SUPABASE_KEY"])
    except Exception:
        pass
    if not url:
        url = os.getenv("SUPABASE_URL", "")
    if not key:
        key = os.getenv("SUPABASE_KEY", "")
    return url, key

# ─────────────────────────────────────────────────────────────────────────────
# Helper base
# ─────────────────────────────────────────────────────────────────────────────

def _query(tabla: str, params: str = "", limit: int = 10_000) -> list:
    """Paginación automática hasta limit registros."""
    supabase_url, supabase_key = _get_creds()
    headers = {
        "apikey":        supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept":        "application/json",
    }
    results = []
    offset  = 0
    page    = 1000
    while offset < limit:
        url = f"{supabase_url}/rest/v1/{tabla}?{params}&limit={page}&offset={offset}"
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        results.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return results[:limit]


# ═════════════════════════════════════════════════════════════════════════════
# 1. ÓRDENES DE TRABAJO  (reemplaza load_work_orders + build_work_orders_df)
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def load_work_orders_supabase() -> list:
    """
    Retorna lista de dicts compatible con el formato raw de Fracttal.
    El dashboard puede llamar build_work_orders_df() sobre este resultado.
    """
    rows = _query(
        "ordenes_trabajo",
        "select=id_ot,estado,estado_tarea,codigo_activo,nombre_activo,"
        "ubicacion,cliente,estacion,codigo_eds,responsable,tipo_tarea,"
        "prioridad,prioridad_calc,fecha_creacion,fecha_inicio,"
        "fecha_finalizacion,causa_raiz,tipo_falla,modalidad_atencion,"
        "nota,nota_tarea,tiene_numeral,duracion_real_seg,duracion_estim_seg,"
        "tiene_recursos,completada"
        "&order=fecha_creacion.desc",
        limit=20_000
    )
    # Mapear al formato que espera build_work_orders_df
    mapped = []
    for r in rows:
        mapped.append({
            "wo_folio":                   r.get("id_ot"),
            "parent_description":         r.get("ubicacion") or f"// {r.get('cliente','')}/{r.get('estacion','')}/",
            "personnel_description":      r.get("responsable"),
            "tasks_log_task_type_main":   r.get("tipo_tarea"),
            "priorities_description":     r.get("prioridad_calc") or r.get("prioridad"),
            "creation_date":              r.get("fecha_creacion"),
            "final_date":                 r.get("fecha_finalizacion"),
            "initial_date":               r.get("fecha_inicio"),   # <-- KPI Precision: elapsed_sec
            "code":                       r.get("codigo_activo"),
            "items_log_description":      r.get("nombre_activo"),
            "groups_2_description":       r.get("codigo_eds"),
            "id_status_work_order":       None,
            "task_status":                r.get("estado_tarea"),
            "done":                       r.get("completada", False),
            "tasks_duration":             r.get("duracion_real_seg"),
            "duration":                   r.get("duracion_estim_seg"),
            "causes_description":         r.get("causa_raiz"),
            "types_description":          r.get("tipo_falla"),
            "detection_method_description": r.get("modalidad_atencion"),
            "note":                       r.get("nota"),
            "task_note":                  r.get("nota_tarea"),
            "stop_assets_sec":            0,
            "total_cost_task":            None,
            "resources_inventory":        "1" if r.get("tiene_recursos") else None,
        })
    return mapped


# ═════════════════════════════════════════════════════════════════════════════
# 2. LISTADO DE EDS  (reemplaza load_listado_eds)
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False, persist="disk")
def load_listado_eds_supabase() -> pd.DataFrame:
    """
    Retorna DataFrame compatible con el formato de load_listado_eds().
    """
    rows = _query(
        "estaciones_servicio",
        "select=eds_occim,cliente,nombre,direccion,comuna,region,zona,activa,"
        "loc_fracttal,barcode_cliente,cod_occim_fracttal",
        limit=2000
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "zona":              "zona_occim",
        "loc_fracttal":      "_loc_code",
        "barcode_cliente":   "_cód_cliente_f",
        "cod_occim_fracttal":"_cod_occim_frac",
    })
    # Compatibilidad: campos que el dashboard espera
    if "nombre" in df.columns:
        df["direccion"] = df["nombre"]
    return df


# ═════════════════════════════════════════════════════════════════════════════
# 3. TÉCNICOS Y EQUIPOS  (reemplaza load_base_tecnicos + GRUPOS_TERRENO)
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False, persist="disk")
def load_tecnicos_supabase() -> pd.DataFrame:
    """Retorna DataFrame de técnicos con su equipo."""
    rows = _query(
        "tecnicos",
        "select=nombre_corto,nombre_completo,aplica_bono,equipo_id,"
        "equipos(nombre_equipo,senior,zona)",
        limit=200
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["equipo"]  = df["equipos"].apply(lambda x: (x or {}).get("nombre_equipo") if isinstance(x, dict) else None)
    df["senior"]  = df["equipos"].apply(lambda x: (x or {}).get("senior") if isinstance(x, dict) else None)
    df["zona"]    = df["equipos"].apply(lambda x: (x or {}).get("zona") if isinstance(x, dict) else None)
    return df.drop(columns=["equipos"], errors="ignore")


@st.cache_data(ttl=3600, show_spinner=False, persist="disk")
def load_equipos_supabase() -> dict:
    """
    Retorna estructura compatible con GRUPOS_TERRENO de data.py.
    {nombre_equipo: {senior, miembros, zona}}
    """
    rows = _query("equipos", "select=id,nombre_equipo,senior,zona&activo=eq.true", limit=100)
    tecs = _query("tecnicos", "select=nombre_corto,nombre_completo,equipo_id,aplica_bono", limit=200)

    tecs_por_equipo: dict = {}
    for t in tecs:
        eid = t.get("equipo_id")
        if eid:
            tecs_por_equipo.setdefault(eid, []).append(t.get("nombre_corto",""))

    grupos = {}
    for eq in rows:
        miembros = tecs_por_equipo.get(eq["id"], [])
        grupos[eq["nombre_equipo"]] = {
            "senior":   eq.get("senior", eq["nombre_equipo"]),
            "miembros": miembros,
            "zona":     eq.get("zona", ""),
        }
    return grupos


# ═════════════════════════════════════════════════════════════════════════════
# 4. LLAMADOS SLA  (reemplaza load_all_llamados / Excel COPEC+Shell+ESMAX)
# ═════════════════════════════════════════════════════════════════════════════

def load_all_llamados_supabase(desde: str = "2026-01-01") -> pd.DataFrame:
    """No usa @st.cache_data — el dashboard lo cachea via _sc() para control total."""
    """
    Retorna DataFrame compatible con load_all_llamados().
    Lee desde v_llamados_sla (vista que replica estructura del Excel).
    """
    rows = _query(
        "v_llamados_sla",
        f"select=os_fracttal,n_llamado,cliente,eds_occim,eds_nombre,comuna,region,"
        f"fecha_llamado,hora_llamado,fecha_atencion,hora_fin,tecnico,tecnico_corto,"
        f"equipo,equipo_senior,prioridad,zona,tiempo_resp_horas,tiempo_resp_esp,"
        f"cumplimiento,estado_atencion,facturacion,fecha_creacion"
        f"&fecha_llamado=gte.{desde}",
        limit=10_000
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Compatibilidad con campos que el dashboard espera
    df = df.rename(columns={
        "os_fracttal":      "os_fracttal",
        "n_llamado":        "n_llamado",
        "tiempo_resp_horas":"horas_resolucion",
        "tiempo_resp_esp":  "tiempo_resp_esp",
    })
    # Convertir fechas: pd.to_datetime falla con timestamps sin microsegundos en pandas 3.x
    # pd.Timestamp() maneja ambos formatos correctamente
    def _safe_ts(x):
        if not x or str(x).strip() in ("", "None", "null"):
            return pd.NaT
        try:
            t = pd.Timestamp(str(x))
            return t.tz_convert(None) if t.tzinfo is not None else t
        except Exception:
            return pd.NaT

    df["fecha_llamado"]  = df["fecha_llamado"].apply(_safe_ts)
    df["fecha_atencion"] = df["fecha_atencion"].apply(_safe_ts)
    df["fecha_llamado_dt"] = df["fecha_llamado"]

    # Mapear cumplimiento al formato original
    df["cumplimiento"] = df["cumplimiento"].replace({
        "CUMPLE":    "CUMPLE",
        "NO CUMPLE": "NO CUMPLE",
        "PENDIENTE": "SIN DATOS",
        "SIN UMBRAL":"SIN DATOS",
    })

    # Campo Año y Mes (compatibilidad)
    df["Año"] = df["fecha_llamado"].dt.year
    df["Mes"] = df["fecha_llamado"].dt.month

    return df


# ═════════════════════════════════════════════════════════════════════════════
# 5. UMBRALES SLA  (reemplaza SLA_HOURS hardcodeado)
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=86400, show_spinner=False)
def load_sla_umbrales_supabase() -> dict:
    """
    Retorna dict compatible con SLA_HOURS de gdrive.py.
    {cliente: {prioridad: {zona: horas}}}
    """
    rows = _query("sla_umbrales_horas", "select=cliente,prioridad,zona,horas", limit=200)
    umbrales: dict = {}
    for r in rows:
        cli  = r["cliente"]
        prio = r["prioridad"]
        zona = r["zona"]
        hrs  = r["horas"]
        umbrales.setdefault(cli, {}).setdefault(prio, {})[zona] = hrs
    return umbrales


# ═════════════════════════════════════════════════════════════════════════════
# 6. MANTENCIONES PREVENTIVAS
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def load_preventivas_supabase() -> list:
    """OTs preventivas con todos los campos del módulo."""
    return _query(
        "ordenes_trabajo",
        "select=id_ot,estado,estado_tarea,nombre_tarea,tipo_tarea,"
        "activador,duracion_estimada,tiempo_ejecucion,"
        "codigo_activo,nombre_activo,ubicacion,clasificacion_2,"
        "responsable,fecha_creacion,fecha_finalizacion,fecha_programada"
        "&tipo_tarea=ilike.*PREVENTIV*"
        "&fecha_creacion=gte.2026-01-01"
        "&order=fecha_programada.desc",
        limit=10_000,
    )

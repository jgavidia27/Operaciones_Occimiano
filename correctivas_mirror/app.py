"""
Correctivas Mirror — Espejo amigable de OTs correctivas de Supabase.
====================================================================

Vista en vivo de llamados correctivos, con:
- v_llamados_sla como fuente base (misma vista que usa el dashboard
  principal). Trae cumplimiento calculado, técnico, tiempo de respuesta,
  zona, excepciones, etc.
- llamados_correctivos aporta la columna `fuente` (robot_email /
  robot_shell / robot_esmax / ot_directa) que la vista no expone.

Es un espejo real de lo que ve el dashboard, en un formato más ameno
(feed cronológico + tabla enriquecida).
"""

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_CL_TZ = ZoneInfo("America/Santiago")

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# ══════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="Panel de Órdenes · Operaciones Occimiano",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Marker de versión visible para confirmar qué commit deployó Streamlit Cloud.
# Si el usuario ve "Oh no", pero cambia este valor al recargar, sabemos que
# el deploy sí llegó y el crash es diferente al que arreglé.
APP_VERSION = "v2026.07.10-fix7"
FECHA_CORTE = "2026-05-01"

# Prioridad → color / label
PRI_STYLE = {
    "P1": ("#dc2626", "#fee2e2", "P1 · Crítico"),
    "P2": ("#ea580c", "#ffedd5", "P2 · Alto"),
    "P3": ("#ca8a04", "#fef9c3", "P3 · Medio"),
    "P4": ("#0284c7", "#e0f2fe", "P4 · Bajo"),
    "P5": ("#64748b", "#f1f5f9", "P5 · Info"),
    None: ("#64748b", "#f1f5f9", "Sin prioridad"),
}

FUENTE_META = {
    "robot_esmax": ("🤖", "Robot Aramco", "#7c3aed", "#ede9fe"),
    "robot_shell": ("🤖", "Robot Shell",  "#c026d3", "#fae8ff"),
    "robot_email": ("🤖", "Robot Copec",  "#2563eb", "#dbeafe"),
    "ot_directa":  ("📞", "Directa Fracttal", "#475569", "#f1f5f9"),
}

# Estado derivado combinando fechas de Fracttal + cumplimiento SLA
#
# Lógica basada en FECHAS (más fiable que el campo estado_atencion):
#   - Sin fecha_inicio NI fecha_final         → 🔴 SIN ATENDER
#   - Con fecha_inicio pero SIN fecha_final   → 🟡 TÉCNICO ATENDIENDO
#   - Con fecha_final pero OT no cerrada      → 🟢/🟠 TRABAJO TERMINADO (pend. cierre)
#   - OT cerrada por completo (Finalizadas)   → ✅ CUMPLE / ❌ NO CUMPLE
#   - Eximida por operaciones                 → ⚪ EXCEPCIÓN
#   - Estados basura                          → 🚫 DESCARTADA
_BASURA_EST = {"ERROR DE INGRESO", "DUPLICADO", "Duplicidad", "PRUEBA ROBOT"}

def estado_ot(row):
    # 1) Excepción SLA gana sobre todo
    if pd.notna(row.get("excepcion_motivo")) and str(row.get("excepcion_motivo") or "").strip():
        return ("Excepción", "⚪", "#0284c7", "Eximida por operaciones")
    est = str(row.get("estado_atencion") or "").strip()
    cum = str(row.get("cumplimiento") or "").upper()

    # 2) Estados basura (filtrables aparte)
    if est in _BASURA_EST:
        return ("Descartada", "🚫", "#94a3b8", f"Estado Fracttal: {est}")

    # 3) Finalizadas: cerrada por completo en Fracttal
    if est == "Finalizadas":
        if cum == "CUMPLE":
            return ("Finalizada - Cumple SLA", "✅", "#16a34a", "Cerrada · SLA cumplido")
        if cum == "NO CUMPLE":
            return ("Finalizada - No cumple SLA", "❌", "#dc2626", "Cerrada · SLA excedido")

    # Fechas para determinar estado operativo
    tiene_inicio = pd.notna(row.get("fecha_inicio_atencion"))
    tiene_final  = pd.notna(row.get("fecha_atencion"))

    # 4) Técnico terminó su trabajo (tiene fecha_final) pero OT aún
    #    no cerrada administrativamente en Fracttal.
    if tiene_final:
        if cum == "CUMPLE":
            return ("OT atendida - Cumple SLA (Pend. Cierre)", "🟢", "#16a34a",
                    "Técnico terminó · pendiente cierre administrativo · SLA cumple")
        if cum == "NO CUMPLE":
            return ("OT atendida - No cumple SLA (Pend. Cierre)", "🟠", "#ea580c",
                    "Técnico terminó · pendiente cierre administrativo · SLA excedido")
        return ("OT atendida (Pend. Cierre)", "🟢", "#16a34a",
                "Técnico terminó · pendiente cierre administrativo")

    # 5) Técnico inició pero aún no termina (fecha_inicio sin fecha_final)
    if tiene_inicio:
        return ("Técnico atendiendo", "🟡", "#f59e0b",
                "Técnico inició la atención · trabajo en curso")

    # 6) Sin fecha_inicio ni fecha_final: nadie la ha tomado
    return ("OT Pendiente - Sin atender", "🔴", "#dc2626",
            "Nadie la ha tomado en Fracttal")


# ══════════════════════════════════════════════════════════════════════
# Supabase client
# ══════════════════════════════════════════════════════════════════════
def _sb_config():
    try:
        url = st.secrets["SUPABASE_URL"]
    except Exception:
        url = os.getenv("SUPABASE_URL", "")
    try:
        key = st.secrets["SUPABASE_KEY"]
    except Exception:
        key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        st.error("Faltan credenciales de Supabase. Configura los secrets "
                 "**SUPABASE_URL** y **SUPABASE_KEY** en Streamlit Cloud "
                 "(Settings → Secrets).")
        st.stop()
    return url, key


def _sb_get(path, params, timeout=25):
    url, key = _sb_config()
    r = requests.get(
        f"{url}/rest/v1/{path}",
        params=params,
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "code" in data:
        st.error(f"Error Supabase: {data.get('message')}")
        st.stop()
    return data


@st.cache_data(ttl=900, show_spinner="Cargando llamados correctivos...", persist="disk")
def cargar_llamados(fecha_desde: str) -> pd.DataFrame:
    """Base: v_llamados_sla (vista ya enriquecida con cumplimiento,
    horas, técnico, zona, excepciones). Cruza con llamados_correctivos
    para inyectar la columna `fuente` que la vista no expone."""

    # 1) Vista principal (misma que usa el dashboard)
    rows = []
    for page in range(30):   # tope 30k filas
        batch = _sb_get("v_llamados_sla", {
            "select": ("os_fracttal,n_llamado,cliente,eds_occim,eds_nombre,"
                       "comuna,region,zona,fecha_llamado,hora_llamado,"
                       "fecha_inicio_atencion,fecha_atencion,hora_fin,"
                       "tecnico,tecnico_corto,"
                       "equipo,equipo_senior,prioridad,tiempo_resp_horas,"
                       "tiempo_resp_esp,cumplimiento,excepcion_motivo,"
                       "estado_atencion,facturacion,tipo_tarea,"
                       "codigo_activo,nombre_activo,fecha_creacion"),
            "fecha_llamado": f"gte.{fecha_desde}",
            "order": "fecha_llamado.desc",
            "limit": 1000,
            "offset": page * 1000,
        })
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # 2) Fuentes por OT.
    # Lógica en 3 capas (más confiable primero):
    #  a) `fuente` de llamados_correctivos si viene poblada por el robot
    #     (robot_email/robot_shell/robot_esmax) → confiable.
    #  b) Si es 'ot_directa' PERO el cliente tiene robot activo y la OT
    #     es POSTERIOR a la fecha de inicio del robot → sobrescribir al
    #     robot correspondiente (asumimos que sync_llamados_directos
    #     corrió antes que el robot procesara el correo).
    #  c) Sin match en llamados_correctivos → inferir por cliente:
    #       Copec/Shell/Aramco con OT posterior a inicio robot → robot_*
    #       resto o pre-robot → ot_directa
    #
    # Fechas de inicio de robots (aprox. — validado con operaciones):
    ROBOT_START = {
        "COPEC":          pd.Timestamp("2026-06-02"),  # robot email Copec
        "SHELL (Enex)":   pd.Timestamp("2026-06-12"),  # robot Shell
        "ESMAX (Aramco)": pd.Timestamp("2026-06-12"),  # robot Aramco/Esmax
        "Aramco (Esmax)": pd.Timestamp("2026-06-12"),
        "ESMAX":          pd.Timestamp("2026-06-12"),
    }
    _cli_to_robot = {
        "COPEC":          "robot_email",
        "SHELL (Enex)":   "robot_shell",
        "ESMAX":          "robot_esmax",
        "Aramco (Esmax)": "robot_esmax",
        "ESMAX (Aramco)": "robot_esmax",
    }

    lc = _sb_get("llamados_correctivos", {
        "select": "os_fracttal,fuente,falla,n_aviso",
        "fecha_llamado": f"gte.{fecha_desde}",
        "limit": 10000,
    })
    fuente_map = {r["os_fracttal"]: r.get("fuente")
                  for r in lc if r.get("os_fracttal")}

    # 2c) Estado real Fracttal (para nueva columna 'Estado Fracttal' en
    # tabla enriquecida). Fracttal UI muestra 'En Revisión' cuando el
    # tecnico marca DONE (completada=true + estado_tarea=Finalizada +
    # fecha_finalizacion IS NULL). Necesitamos esos campos crudos de
    # ordenes_trabajo porque v_llamados_sla solo expone `estado_atencion`
    # (que corresponde al id_status_work_order y no coincide con la UI).
    ot_rows = []
    for _pg in range(30):
        _batch = _sb_get("ordenes_trabajo", {
            "select": "id_ot,estado,estado_tarea,completada,fecha_finalizacion",
            "fecha_creacion": f"gte.{fecha_desde}",
            "limit": 1000,
            "offset": _pg * 1000,
        })
        if not _batch:
            break
        ot_rows.extend(_batch)
        if len(_batch) < 1000:
            break
    _ot_map = {r["id_ot"]: r for r in ot_rows if r.get("id_ot")}
    df["_ot_estado"]       = df["os_fracttal"].map(lambda x: (_ot_map.get(x) or {}).get("estado"))
    df["_ot_estado_tarea"] = df["os_fracttal"].map(lambda x: (_ot_map.get(x) or {}).get("estado_tarea"))
    df["_ot_completada"]   = df["os_fracttal"].map(lambda x: (_ot_map.get(x) or {}).get("completada"))
    df["_ot_fin_admin"]    = df["os_fracttal"].map(lambda x: (_ot_map.get(x) or {}).get("fecha_finalizacion"))
    falla_map = {r["os_fracttal"]: r["falla"]
                 for r in lc if r.get("os_fracttal") and r.get("falla")}
    n_aviso_map = {r["os_fracttal"]: r["n_aviso"]
                   for r in lc if r.get("os_fracttal") and r.get("n_aviso")}
    df["falla"] = df["os_fracttal"].map(falla_map)
    df["n_aviso"] = df["os_fracttal"].map(n_aviso_map)
    df["fuente_bd"] = df["os_fracttal"].map(fuente_map)

    # ── Fechas vectorizado (antes: .apply(_ts) x3 = O(N) Python) ─────────────
    def _vec_ts(col):
        return (pd.to_datetime(col, errors="coerce", format="ISO8601", utc=True)
                  .dt.tz_convert("America/Santiago").dt.tz_localize(None))
    df["fecha_llamado"]  = _vec_ts(df["fecha_llamado"])
    df["fecha_atencion"] = _vec_ts(df["fecha_atencion"])
    if "fecha_inicio_atencion" in df.columns:
        df["fecha_inicio_atencion"] = _vec_ts(df["fecha_inicio_atencion"])
    else:
        df["fecha_inicio_atencion"] = pd.NaT

    # ── Resolver fuente vectorizado ──────────────────────────────────────────
    # Antes: df.apply(_resolver_fuente, axis=1) sobre 600+ filas cada rerun.
    # Ahora: máscaras booleanas vectorizadas.
    _robot_target_series = df["cliente"].map(_cli_to_robot)
    _robot_start_series  = df["cliente"].map(ROBOT_START)
    _fbd_series          = df["fuente_bd"]

    # Default: si BD tiene fuente, usar BD; si no, ot_directa
    _resolved = _fbd_series.fillna("ot_directa")
    # Override: cliente con robot activo Y fecha >= inicio robot → robot
    _mask_post_robot = (
        _robot_target_series.notna()
        & _robot_start_series.notna()
        & df["fecha_llamado"].notna()
        & (df["fecha_llamado"] >= _robot_start_series)
    )
    _resolved = _resolved.where(~_mask_post_robot, _robot_target_series)
    # Pero si la BD ya dice robot_*, respetar la BD (más confiable que inferencia)
    _mask_bd_es_robot = _fbd_series.isin(["robot_email", "robot_shell", "robot_esmax"])
    _resolved = _resolved.where(~_mask_bd_es_robot, _fbd_series)
    df["fuente"] = _resolved
    df["fuente_inferida"] = df["fuente_bd"] != df["fuente"]
    df = df.drop(columns=["fuente_bd"], errors="ignore")

    # 2b) nota_tarea → falla_desc (descripción real de la falla)
    _PAT_COPEC_FALLA = re.compile(
        r"Falla reportada[:\s]+(.+?)(?:\r?\n|$)", re.IGNORECASE)
    _PAT_SHELL_DESC = re.compile(
        r"Descripci[oó]n del Requerimiento[:\s]+\"?(.+?)\"?\s*(?:\r?\n|$)", re.IGNORECASE)
    _PAT_ARAMCO_DET = re.compile(
        r"Detalles del incidente[:\s]+(.+?)(?:\r?\n|$)", re.IGNORECASE)

    def _extract_falla(nota, cliente):
        if not nota:
            return None
        nota = str(nota)
        if "COPEC" in (cliente or ""):
            m = _PAT_COPEC_FALLA.search(nota)
            if m:
                return m.group(1).strip()
        elif "SHELL" in (cliente or "").upper():
            m = _PAT_SHELL_DESC.search(nota)
            if m:
                return m.group(1).strip()
            first = nota.split("\n")[0].strip()
            if first and len(first) < 120:
                return first
        elif "ARAMCO" in (cliente or "").upper() or "ESMAX" in (cliente or "").upper():
            m = _PAT_ARAMCO_DET.search(nota)
            if m:
                return m.group(1).strip()
            first = nota.split("\n")[0].strip()
            if first and len(first) < 120:
                return first
        first = nota.split("\n")[0].strip()
        if first and len(first) < 120:
            return first
        return None

    _ot_ids = df["os_fracttal"].dropna().unique().tolist()
    _nota_map = {}
    for _off in range(0, len(_ot_ids), 200):
        _chunk = _ot_ids[_off:_off + 200]
        _nt = _sb_get("ordenes_trabajo", {
            "select": "id_ot,nota_tarea",
            "id_ot": f"in.({','.join(_chunk)})",
            "limit": 200,
        })
        for r in _nt:
            _nota_map[r["id_ot"]] = r.get("nota_tarea")
    df["falla_desc"] = [
        _extract_falla(_nota_map.get(ot), cli)
        for ot, cli in zip(df["os_fracttal"], df["cliente"])
    ]

    # 3) Normalización
    df["cliente"] = df["cliente"].replace({"ESMAX (Aramco)": "Aramco (Esmax)"})

    # Numéricos seguros
    df["tiempo_resp_horas"] = pd.to_numeric(df["tiempo_resp_horas"], errors="coerce")
    df["tiempo_resp_esp"]   = pd.to_numeric(df["tiempo_resp_esp"],   errors="coerce")

    # Técnico "amigable"
    df["tecnico_disp"] = df["tecnico_corto"].fillna(df["tecnico"])

    # ── Estado derivado vectorizado ──────────────────────────────────────────
    # Antes: df.apply(lambda r: pd.Series(estado_ot(r)), axis=1) — el hotspot
    # MÁS lento (Python fila x fila + pd.Series por cada iteración).
    # Ahora: itertuples() con list-comp + unzip → 5-10× más rápido y sin
    # crear un Series por cada fila.
    _est_rows = [estado_ot(r._asdict()) for r in df.itertuples(index=False)]
    if _est_rows:
        _lbl, _ico, _fg, _desc = zip(*_est_rows)
        df["estado_lbl"]  = list(_lbl)
        df["estado_ico"]  = list(_ico)
        df["estado_fg"]   = list(_fg)
        df["estado_desc"] = list(_desc)
    else:
        df["estado_lbl"] = df["estado_ico"] = df["estado_fg"] = df["estado_desc"] = ""

    return df


# ══════════════════════════════════════════════════════════════════════
# Estilos
# ══════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
.block-container {padding-top:1.5rem; padding-bottom:3rem; max-width:1400px;}
h1 {font-size:1.7rem !important; margin-bottom:.3rem !important;}
.hdr-sub {color:#64748b; font-size:.92rem; margin-bottom:1rem;}

.card {
    background:#fff; border:1px solid #e2e8f0; border-radius:10px;
    padding:14px 16px; margin-bottom:10px;
    border-left:4px solid var(--pri, #64748b);
    transition: transform .1s, box-shadow .1s;
}
.card:hover {transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,.06);}
.card .top {display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap;}
.card .os {font-weight:700; font-size:1.02rem; color:#0f172a;}
.card .aviso {color:#64748b; font-weight:500; font-size:.78rem; margin-left:6px;}
.card .eds {color:#334155; font-size:.88rem; margin-top:2px;}
.card .cli {color:#64748b; font-size:.78rem; margin-top:2px;}
.card .meta {color:#94a3b8; font-size:.72rem; margin-top:6px;}
.card .exc {background:#eff6ff; border-left:3px solid #0284c7; padding:4px 8px;
            margin-top:6px; font-size:.75rem; color:#0369a1; border-radius:4px;}

.badge {
    display:inline-block; padding:2px 8px; border-radius:12px;
    font-size:.68rem; font-weight:700; margin:1px 3px 1px 0;
    letter-spacing:.02em; white-space:nowrap;
}
.badge.fuente {background: var(--f-bg); color: var(--f-fg);}
.badge.pri {background: var(--p-bg); color: var(--p-fg); border:1px solid var(--p-fg);}
.badge.est {background:#fff; color: var(--e-fg); border:1px solid var(--e-fg);}

.section-hdr {
    font-weight:700; color:#475569; font-size:.78rem;
    text-transform:uppercase; letter-spacing:.05em;
    margin:18px 0 8px 0; padding-bottom:4px;
    border-bottom:1px solid #e2e8f0;
}
[data-testid="stMetricValue"] {font-size:1.6rem;}
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════
# Header
# ══════════════════════════════════════════════════════════════════════
_LOGO_PATH = Path(__file__).parent / "assets" / "logo_occim_25.png"
_hdr_logo, _hdr_txt = st.columns([1, 8], vertical_alignment="center")
with _hdr_logo:
    if _LOGO_PATH.exists():
        st.image(str(_LOGO_PATH), width=130)
with _hdr_txt:
    st.markdown("# 🌐 Panel de Órdenes — Operaciones")
    st.markdown(
        f'<div class="hdr-sub">Fuente: <code>v_llamados_sla</code> (misma vista que el dashboard principal) · '
        f'Enriquecida con <code>fuente</code> desde <code>llamados_correctivos</code> · '
        f'Datos desde <b>{FECHA_CORTE}</b> · Cache 5 min · '
        f'<span style="color:#94a3b8;">Build {APP_VERSION}</span></div>',
        unsafe_allow_html=True,
    )

_c1, _c2 = st.columns([6, 1])
with _c2:
    if st.button("🔄 Recargar", use_container_width=True,
                 help="Limpia todos los caches y trae los datos frescos de Supabase"):
        # Limpiar TODOS los caches @st.cache_data (no solo llamados)
        st.cache_data.clear()
        st.rerun()

# Wrap toda la carga en try/except para que Streamlit Cloud muestre
# el error real en vez de "Oh no. Error running app."
try:
    df = cargar_llamados(FECHA_CORTE)
except Exception as _e_load:
    import traceback as _tb
    st.error(f"❌ Error cargando datos de Supabase: {type(_e_load).__name__}: {_e_load}")
    st.code(_tb.format_exc())
    st.stop()

if df.empty:
    st.warning("No hay datos en Supabase para el período configurado.")
    st.stop()


# ══════════════════════════════════════════════════════════════════════
# Filtros
# ══════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-hdr">Filtros</div>', unsafe_allow_html=True)

_f1, _f2, _f3, _f4, _f5 = st.columns([1.3, 1.3, 1.1, 1.2, 2])

with _f1:
    _fuentes = sorted([f for f in df["fuente"].dropna().unique() if f])
    fuente_sel = st.multiselect(
        "Fuente", _fuentes, default=_fuentes, key="fuente_v2",
        format_func=lambda f: f"{FUENTE_META.get(f, ('❓','?','',''))[0]} {FUENTE_META.get(f, ('','?','',''))[1]}"
                              if f in FUENTE_META else f,
    )
with _f2:
    _clientes = sorted(df["cliente"].dropna().unique())
    cliente_sel = st.multiselect("Cliente", _clientes, default=_clientes, key="cliente_v2")
with _f3:
    _prios = sorted(df["prioridad"].dropna().unique())
    pri_sel = st.multiselect("Prioridad", _prios, default=_prios, key="pri_v2")
with _f4:
    _est_opts = [
        "Finalizada - Cumple SLA",
        "Finalizada - No cumple SLA",
        "OT atendida - Cumple SLA (Pend. Cierre)",
        "OT atendida - No cumple SLA (Pend. Cierre)",
        "OT atendida (Pend. Cierre)",
        "Técnico atendiendo",
        "OT Pendiente - Sin atender",
        "Excepción",
        "Descartada",
    ]
    # Por defecto ocultamos Descartada (basura de Fracttal)
    _est_default = [e for e in _est_opts if e != "Descartada"]
    est_sel = st.multiselect("Estado / SLA", _est_opts,
                             default=_est_default, key="estado_v2")
with _f5:
    buscar = st.text_input(
        "Buscar",
        placeholder="OS-XXXXX · N° aviso · código EDS · nombre · falla · técnico · comuna",
        key="q",
    )

_r1, _r2, _r3 = st.columns([1.6, 1.4, 3.6])
with _r1:
    _hoy_date = datetime.now(_CL_TZ).date()
    _max_dt = df["fecha_llamado"].max()
    _min_dt = df["fecha_llamado"].min()
    _fmax_data = _max_dt.date() if pd.notna(_max_dt) else _hoy_date
    _fmax = max(_fmax_data, _hoy_date)
    _fmin_data = _min_dt.date() if pd.notna(_min_dt) else _fmax
    _fmin = min(_fmin_data, _fmax)
    # Defensivo: date_input puede crashear si state stale queda fuera de rango.
    # Damos key explícita + fallback si excepta.
    try:
        fecha_rng = st.date_input(
            "Rango de fechas", (_fmin, _fmax),
            min_value=_fmin, max_value=_fmax, key="fecha_rng_v3",
        )
    except Exception:
        # Reset state y reintentar
        st.session_state.pop("fecha_rng_v3", None)
        fecha_rng = (_fmin, _fmax)
with _r2:
    st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    _solo_pend = st.toggle("Solo pendientes (abiertas)", key="solo_pend",
                           help="Muestra únicamente OTs sin atender + técnico atendiendo")

# Aplicar filtros
_df = df.copy()
if fuente_sel:
    _df = _df[_df["fuente"].isin(fuente_sel)]
if cliente_sel:
    _df = _df[_df["cliente"].isin(cliente_sel)]
if pri_sel:
    _df = _df[_df["prioridad"].isin(pri_sel)]
if est_sel:
    _df = _df[_df["estado_lbl"].isin(est_sel)]
if buscar and buscar.strip():
    q = buscar.strip().upper()
    _df = _df[
        _df["os_fracttal"].astype(str).str.upper().str.contains(q, na=False)
        | _df["n_llamado"].astype(str).str.upper().str.contains(q, na=False)
        | _df["eds_occim"].astype(str).str.upper().str.contains(q, na=False)
        | _df["eds_nombre"].astype(str).str.upper().str.contains(q, na=False)
        | _df["tecnico_disp"].astype(str).str.upper().str.contains(q, na=False)
        | _df["comuna"].astype(str).str.upper().str.contains(q, na=False)
    ]
# Filtro de fecha defensivo: si fecha_rng no es tupla de 2, o si hay NaT,
# aplicamos filtro sobre serie datetime en lugar de .dt.date (más robusto).
try:
    if isinstance(fecha_rng, (tuple, list)) and len(fecha_rng) == 2:
        d0, d1 = fecha_rng
        _d0_ts = pd.Timestamp(d0)
        _d1_ts = pd.Timestamp(d1) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        _fl = _df["fecha_llamado"]
        _mask = _fl.notna() & (_fl >= _d0_ts) & (_fl <= _d1_ts)
        _df = _df[_mask].copy()
except Exception as _e_fec:
    st.warning(f"⚠️ Filtro de fecha ignorado por error: {type(_e_fec).__name__}: {_e_fec}")
if _solo_pend:
    _df = _df[_df["estado_lbl"].isin(["OT Pendiente - Sin atender", "Técnico atendiendo"])]


# ══════════════════════════════════════════════════════════════════════
# KPIs — se calculan siempre (Feed usa _n_tot en el caption), pero el
# bloque visual "Panorama" solo se renderiza en la Tabla enriquecida
# porque en Feed/Estadísticas/Registro no aporta contexto.
# ══════════════════════════════════════════════════════════════════════
_hoy = pd.Timestamp.now(tz="America/Santiago").tz_localize(None).date()
_semana = _hoy - timedelta(days=7)

_n_tot = len(_df)
_n_hoy = int((_df["fecha_llamado"].dt.date == _hoy).sum()) if not _df.empty else 0
_n_cumple = int((_df["estado_lbl"] == "Finalizada - Cumple SLA").sum())
_n_nocump = int((_df["estado_lbl"] == "Finalizada - No cumple SLA").sum())
_n_trab   = int(_df["estado_lbl"].isin([
    "OT atendida - Cumple SLA (Pend. Cierre)",
    "OT atendida - No cumple SLA (Pend. Cierre)",
    "OT atendida (Pend. Cierre)",
]).sum())
_n_sinat  = int((_df["estado_lbl"] == "OT Pendiente - Sin atender").sum())
_n_terr   = int((_df["estado_lbl"] == "Técnico atendiendo").sum())
_evaluadas = _n_cumple + _n_nocump
_pct_cumpl = (_n_cumple / _evaluadas * 100) if _evaluadas else 0


def _render_panorama():
    """Bloque visual del Panorama (KPIs + distribución por canal).
    Solo se muestra dentro de la Tabla enriquecida."""
    st.markdown('<div class="section-hdr">Panorama</div>', unsafe_allow_html=True)

    _k1, _k2, _k3, _k4, _k5 = st.columns(5)
    _k1.metric("Total (filtrado)", f"{_n_tot:,}",
               delta=f"{_n_hoy} hoy" if _n_hoy else "", delta_color="off")
    _k2.metric("✅ Finalizada · Cumple SLA", f"{_n_cumple:,}",
               delta=f"{_pct_cumpl:.1f}% del SLA evaluado", delta_color="off")
    _k3.metric("❌ Finalizada · No cumple", f"{_n_nocump:,}",
               delta_color="inverse")
    _k4.metric("🟢 OT atendida", f"{_n_trab:,}",
               delta="pend. cierre en Fracttal", delta_color="off",
               help="Técnico terminó y registró fecha_finalizacion, pero la "
                    "OT sigue abierta en Fracttal por cierre administrativo.")
    _k5.metric("🔴 Sin atender / 🟡 Atendiendo", f"{_n_sinat:,} / {_n_terr:,}",
               delta=f"{_n_terr} técnico en vivo" if _n_terr else "sin atender",
               delta_color="off",
               help="🔴 Sin fecha de inicio = nadie la ha tomado · "
                    "🟡 Con fecha de inicio, sin final = técnico trabajando en vivo.")

    # Distribución por fuente
    if _n_tot:
        _dist = _df["fuente"].fillna("(sin fuente)").value_counts()
        _bar = '<div style="display:flex;gap:4px;margin-top:14px;height:42px;overflow:hidden;border-radius:6px;">'
        for _f, _n in _dist.items():
            _meta = FUENTE_META.get(_f, ("❓", _f or "(sin fuente)", "#64748b", "#f1f5f9"))
            _pct = _n / _n_tot * 100
            _bar += (
                f'<div style="flex:{_n};background:{_meta[2]};color:#fff;'
                f'display:flex;flex-direction:column;align-items:center;'
                f'justify-content:center;font-weight:600;min-width:110px;padding:0 6px;'
                f'text-align:center;line-height:1.15" '
                f'title="{_meta[1]}: {_n:,} · {_pct:.1f}%">'
                f'<div style="font-size:.78rem">{_meta[0]} {_meta[1]}</div>'
                f'<div style="font-size:.72rem;opacity:.9">{_n:,} · {_pct:.1f}%</div>'
                f'</div>')
        _bar += "</div>"
        st.markdown(_bar, unsafe_allow_html=True)

        _n_robots = int(_df["fuente"].isin(
            ["robot_esmax","robot_shell","robot_email"]).sum())
        _n_directa = int((_df["fuente"] == "ot_directa").sum())
        _pct_r = _n_robots / _n_tot * 100 if _n_tot else 0
        _pct_d = _n_directa / _n_tot * 100 if _n_tot else 0

        _resumen = (
            '<div style="display:flex;gap:16px;margin-top:10px;flex-wrap:wrap;'
            'font-size:.85rem;color:#475569;">'
            f'<div>🤖 <b>Robots</b>: {_n_robots:,} ({_pct_r:.1f}%)</div>'
            f'<div>📞 <b>Directa Fracttal</b>: {_n_directa:,} ({_pct_d:.1f}%)</div>'
            f'<div>📊 <b>Total</b>: {_n_tot:,}</div>'
            '</div>'
        )
        st.markdown(_resumen, unsafe_allow_html=True)

        _n_inf = int(_df["fuente_inferida"].sum()) if "fuente_inferida" in _df.columns else 0
        st.caption(
            f"Distribución por canal · **{_n_inf:,}** OTs con fuente corregida "
            f"(BD decía 'ot_directa' pero cliente tiene robot activo). "
            f"Robots iniciaron: Copec 02-jun-2026 · Shell 12-jun-2026 · "
            f"Aramco 12-jun-2026. OTs anteriores al inicio del robot se "
            f"mantienen como aparecen en la BD."
        )


# ══════════════════════════════════════════════════════════════════════
# Vistas
# ══════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-hdr">Vista</div>', unsafe_allow_html=True)

vista = st.radio("vista", ["📰 Feed cronológico", "📋 Tabla enriquecida",
                           "📊 Estadísticas", "📝 Registro (Excel)",
                           "🔍 Validación En Revisión"],
                 horizontal=True, label_visibility="collapsed")


# ────────── Feed ──────────
if vista == "📰 Feed cronológico":

    # ── Panel "En curso ahora": 3 grupos separados de OTs vivas ─────────
    # Sin atender SLA vencido  |  Sin atender SLA vigente  |  Atendiendo.
    # Solo se calcula sobre las OTs no finalizadas; el resto del Feed
    # cronológico se mantiene igual debajo.
    _now_naive = datetime.now(_CL_TZ).replace(tzinfo=None)

    def _sla_vencido(row) -> bool:
        _fl = row.get("fecha_llamado")
        _um = row.get("tiempo_resp_esp")
        if pd.isna(_fl) or pd.isna(_um) or float(_um or 0) <= 0:
            return False
        return _now_naive > (_fl + timedelta(hours=float(_um)))

    _pendientes_todas = _df[_df["estado_lbl"] == "OT Pendiente - Sin atender"].copy()
    _atendiendo = _df[_df["estado_lbl"] == "Técnico atendiendo"].copy()

    if not _pendientes_todas.empty:
        _pendientes_todas["_vencida"] = _pendientes_todas.apply(_sla_vencido, axis=1)
        _sin_at_venc = _pendientes_todas[_pendientes_todas["_vencida"]]
        _sin_at_vig  = _pendientes_todas[~_pendientes_todas["_vencida"]]
    else:
        _sin_at_venc = _pendientes_todas
        _sin_at_vig  = _pendientes_todas

    _n_venc = len(_sin_at_venc)
    _n_vig  = len(_sin_at_vig)
    _n_atd  = len(_atendiendo)

    st.markdown('<div class="section-hdr">En curso ahora</div>', unsafe_allow_html=True)
    _pk1, _pk2, _pk3 = st.columns(3)
    _pk1.metric("🔴 Vencidas · sin atender", f"{_n_venc}",
                help="Sin fecha de inicio de atención y el SLA ya pasó.")
    _pk2.metric("🟠 Sin atender · SLA vigente", f"{_n_vig}",
                help="Sin fecha de inicio pero todavía dentro del SLA.")
    _pk3.metric("🟡 Atendiendo (técnico en curso)", f"{_n_atd}",
                help="Técnico marcó fecha de inicio pero no ha cerrado.")

    _c_lim, _ = st.columns([1, 5])
    with _c_lim:
        _lim = st.selectbox("Mostrar", [50, 100, 250, 500, "Todo"], index=1, key="feed_lim")
    # NaT al fondo, resto por fecha_llamado desc (última llegada primero)
    _dff = _df.sort_values("fecha_llamado", ascending=False, na_position="last")
    if _lim != "Todo":
        _dff = _dff.head(int(_lim))

    st.caption(f"Mostrando **{len(_dff):,}** de {_n_tot:,} llamados · "
               "orden: más recientes primero.")

    def _v(x, default="—"):
        """Sanea NaN / None / '' para display."""
        if x is None:
            return default
        if isinstance(x, float) and pd.isna(x):
            return default
        s = str(x).strip()
        if not s or s.lower() in ("nan", "none", "null", "nat"):
            return default
        return s

    def _card(r):
        _p = _v(r.get("prioridad"), "").upper() or None
        _p_fg, _p_bg, _p_lbl = PRI_STYLE.get(_p, PRI_STYLE[None])

        _f = r.get("fuente")
        if _f in FUENTE_META:
            _f_ico, _f_lbl, _f_fg, _f_bg = FUENTE_META[_f]
        else:
            _f_ico, _f_lbl, _f_fg, _f_bg = ("📋", "Registro previo", "#64748b", "#f1f5f9")

        _e_lbl, _e_ico, _e_fg = r["estado_lbl"], r["estado_ico"], r["estado_fg"]
        _e_desc = r.get("estado_desc") or ""

        _fl = r["fecha_llamado"]
        _fl_s = _fl.strftime("%d/%m %H:%M") if pd.notna(_fl) else "—"
        _fi = r.get("fecha_inicio_atencion")
        _fi_s = ("🔧 inicio " + _fi.strftime("%d/%m %H:%M")) if pd.notna(_fi) else ""
        _fc = r["fecha_atencion"]
        _fc_s = ("cerrada " + _fc.strftime("%d/%m %H:%M")) if pd.notna(_fc) else ("abierta" if not _fi_s else _fi_s)

        _hr = r.get("tiempo_resp_horas")
        _um = r.get("tiempo_resp_esp")
        _hr_s = ""
        if pd.notna(_hr):
            _u = f"{int(_um)}h" if pd.notna(_um) else "?h"
            _hr_s = f" · <b>{_hr:.1f}h</b> resp. / SLA {_u}"

        _sla_line = ""
        if pd.notna(_fl) and pd.notna(_um) and float(_um) > 0:
            _deadline = _fl + timedelta(hours=float(_um))
            _dl_s = _deadline.strftime("%d/%m %H:%M")
            _sla_total_sec = float(_um) * 3600
            if pd.isna(_fc):
                _now = datetime.now(_CL_TZ).replace(tzinfo=None)
                _diff_sec = int((_deadline - _now).total_seconds())
                if _diff_sec > 0:
                    _hh, _rr = divmod(_diff_sec, 3600)
                    _mm = _rr // 60
                    # % de holgura restante (0-100)
                    _pct = max(0, min(100, round(100 * _diff_sec / _sla_total_sec)))
                    # Color segun holgura
                    if _pct >= 50:
                        _bar_color, _txt_color, _icon = "#16a34a", "#16a34a", "🟢"  # verde
                    elif _pct >= 25:
                        _bar_color, _txt_color, _icon = "#eab308", "#a16207", "🟡"  # amarillo
                    elif _pct >= 10:
                        _bar_color, _txt_color, _icon = "#f97316", "#c2410c", "🟠"  # naranja
                    else:
                        _bar_color, _txt_color, _icon = "#dc2626", "#dc2626", "🔴"  # rojo critico
                    _bar_html = (
                        f'<div style="margin-top:4px;background:#f1f5f9;'
                        f'border-radius:6px;height:8px;overflow:hidden;'
                        f'border:1px solid #e2e8f0;">'
                        f'<div style="height:100%;width:{_pct}%;'
                        f'background:{_bar_color};transition:width .3s;"></div>'
                        f'</div>'
                        f'<div style="font-size:.7rem;color:#64748b;margin-top:2px;">'
                        f'Holgura: <b style="color:{_txt_color};">{_pct}%</b> del SLA restante</div>'
                    )
                    _sla_line = (f'<div class="meta" style="margin-top:2px;">'
                        f'⏱ <b>SLA:</b> inicio {_fl_s} · vence {_dl_s} '
                        f'· <span style="color:{_txt_color};font-weight:600;">'
                        f'{_icon} quedan {_hh}h {_mm}min</span>'
                        f'{_bar_html}</div>')
                else:
                    _hh, _rr = divmod(abs(_diff_sec), 3600)
                    _mm = _rr // 60
                    _bar_html = (
                        f'<div style="margin-top:4px;background:#fee2e2;'
                        f'border-radius:6px;height:8px;overflow:hidden;'
                        f'border:1px solid #fca5a5;">'
                        f'<div style="height:100%;width:100%;background:#dc2626;"></div>'
                        f'</div>'
                        f'<div style="font-size:.7rem;color:#dc2626;margin-top:2px;'
                        f'font-weight:600;">⚠️ SLA vencido</div>'
                    )
                    _sla_line = (f'<div class="meta" style="margin-top:2px;">'
                        f'⏱ <b>SLA:</b> inicio {_fl_s} · vence {_dl_s} '
                        f'· <span style="color:#dc2626;font-weight:600;">'
                        f'⚠️ vencida hace {_hh}h {_mm}min</span>{_bar_html}</div>')
            else:
                _sla_line = (f'<div class="meta" style="margin-top:2px;">'
                    f'⏱ <b>SLA:</b> inicio {_fl_s} · límite {_dl_s} ({int(_um)}h)</div>')

        _exc = r.get("excepcion_motivo")
        _exc_html = ""
        if pd.notna(_exc) and str(_exc).strip():
            _exc_html = f'<div class="exc">⚪ <b>Excepción:</b> {_exc}</div>'

        _os  = _v(r.get("os_fracttal"))
        _av  = _v(r.get("n_llamado"))
        _eds = _v(r.get("eds_occim"))
        _nom = _v(r.get("eds_nombre"))
        _cli = _v(r.get("cliente"))
        _cm  = _v(r.get("comuna"))
        _zn  = _v(r.get("zona"))
        _eq  = _v(r.get("equipo"))
        _tec = _v(r.get("tecnico_disp"))

        return (
            f'<div class="card" style="--pri:{_p_fg}">'
            f'<div class="top">'
            f'<div>'
            f'<span class="os">{_os}</span>'
            f'<span class="aviso">· Aviso {_av}</span>'
            f'</div>'
            f'<div>'
            f'<span class="badge fuente" style="--f-bg:{_f_bg};--f-fg:{_f_fg}">{_f_ico} {_f_lbl}</span>'
            f'<span class="badge pri" style="--p-bg:{_p_bg};--p-fg:{_p_fg}">{_p_lbl}</span>'
            f'<span class="badge est" style="--e-fg:{_e_fg}" title="{_e_desc}">{_e_ico} {_e_lbl}</span>'
            f'</div>'
            f'</div>'
            f'<div class="eds">{_eds} · {_nom}</div>'
            f'<div class="cli">{_cli} · {_cm} ({_zn}) · Equipo: {_eq} · Téc: {_tec}</div>'
            f'{_exc_html}'
            f'<div class="meta">📅 {_fl_s} · {_fc_s}{_hr_s}</div>'
            f'{_sla_line}'
            f'</div>'
        )

    # ── Detalle de las 3 categorías vivas (arriba del feed cronológico) ──
    # Cada expander lista las tarjetas para poder actuar rápido.
    def _mostrar_tarjetas(df_sub, vacio_msg):
        if df_sub.empty:
            st.caption(vacio_msg)
            return
        _dfs = df_sub.sort_values("fecha_llamado", ascending=False, na_position="last")
        st.markdown("".join(_card(r) for _, r in _dfs.iterrows()),
                    unsafe_allow_html=True)

    if _n_venc or _n_vig or _n_atd:
        with st.expander(f"🔴 Vencidas · sin atender ({_n_venc})", expanded=(_n_venc > 0)):
            _mostrar_tarjetas(_sin_at_venc, "Ninguna. 🎉")
        with st.expander(f"🟠 Sin atender · SLA vigente ({_n_vig})", expanded=False):
            _mostrar_tarjetas(_sin_at_vig, "Ninguna sin atender con SLA vigente.")
        with st.expander(f"🟡 Atendiendo (técnico en curso) ({_n_atd})", expanded=False):
            _mostrar_tarjetas(_atendiendo, "Sin técnicos atendiendo en este momento.")
        st.divider()

    st.markdown('<div class="section-hdr">Feed cronológico completo</div>',
                unsafe_allow_html=True)
    st.markdown("".join(_card(r) for _, r in _dff.iterrows()), unsafe_allow_html=True)


# ────────── Tabla ──────────
elif vista == "📋 Tabla enriquecida":
    _render_panorama()
    _dft = _df.copy()
    _dft["Fuente"] = _dft["fuente"].map(
        lambda f: (f"{FUENTE_META.get(f, ('❓','?','',''))[0]} "
                   f"{FUENTE_META.get(f, ('','?','',''))[1]}")
                  if f in FUENTE_META else "❓ (sin fuente)")
    _dft["Estado"] = _dft["estado_ico"] + " " + _dft["estado_lbl"]

    # Nueva columna: "Estado Fracttal" (ciclo de vida de la OT).
    # Replica la logica que usa Fracttal UI: cuando el tecnico marca DONE
    # (completada=True + estado_tarea='Finalizada') y NO hay cierre admin
    # (fecha_finalizacion IS NULL) => la OT aparece en 'En Revisión' en
    # la pantalla de Fracttal aunque id_status_work_order siga siendo 2.
    _CICLO_LABELS = {
        "Finalizadas":  "✅ Finalizada",
        "Finalizada":   "✅ Finalizada",
        "Cancelado":    "🚫 Cancelada",
        "Canceladas":   "🚫 Cancelada",
        "En Revisión":  "👀 En Revisión",
        "En Proceso":   "🔧 En Proceso",
        "En Progreso":  "🔧 En Proceso",
        "En Espera":    "⏸️ En Espera",
        "Por Validar":  "👀 En Revisión",
        "No Iniciada":  "📋 Pendiente",
        "Por Iniciar":  "📋 Pendiente",
        "ERROR DE INGRESO": "🚫 Error ingreso",
        "DUPLICADO":        "🚫 Duplicado",
        "Duplicidad":       "🚫 Duplicado",
        "PRUEBA ROBOT":     "🚫 Prueba",
    }
    def _estado_fracttal(row):
        est = str(row.get("_ot_estado") or "").strip()
        # Terminales tienen prioridad
        if est in ("Finalizadas", "Finalizada", "Cancelado", "Canceladas",
                   "ERROR DE INGRESO", "DUPLICADO", "Duplicidad", "PRUEBA ROBOT"):
            return _CICLO_LABELS.get(est, est)
        # OT completada por tecnico pero aun no cerrada admin => 'En Revisión'
        # (Fracttal UI usa esta misma logica). NO chequeamos fecha_finalizacion
        # porque esa se llena con final_date (cierre tecnico), no wo_final_date.
        # El indicador administrativo es que el estado NO sea "Finalizadas".
        completada = row.get("_ot_completada")
        est_tarea  = str(row.get("_ot_estado_tarea") or "").strip().upper()
        if completada is True and est_tarea in ("DONE", "FINALIZADA", "REVIEWED", "IN_REVIEW"):
            return "👀 En Revisión"
        # Sino, mapear el estado crudo
        if est:
            return _CICLO_LABELS.get(est, est)
        # Fallback a estado_atencion (mapa viejo)
        est_alt = str(row.get("estado_atencion") or "").strip()
        return _CICLO_LABELS.get(est_alt, est_alt or "—")
    _dft["Estado Fracttal"] = _dft.apply(_estado_fracttal, axis=1)

    _dft["F. Llamado"] = _dft["fecha_llamado"].dt.strftime("%d/%m/%Y %H:%M")
    _dft["F. Inicio"]  = _dft["fecha_inicio_atencion"].dt.strftime("%d/%m/%Y %H:%M").fillna("—") if "fecha_inicio_atencion" in _dft.columns else "—"
    _dft["F. Cierre"]  = _dft["fecha_atencion"].dt.strftime("%d/%m/%Y %H:%M").fillna("—")
    _dft["Horas resp."]= _dft["tiempo_resp_horas"].round(2)
    _dft["SLA (h)"]    = _dft["tiempo_resp_esp"]
    # Renombrada: 'Excepción' -> 'Observación' (misma data)
    _dft["Observación"] = _dft["excepcion_motivo"].fillna("")

    _cols = ["os_fracttal","n_llamado","cliente","eds_occim","eds_nombre",
             "comuna","zona","prioridad","Fuente","Estado","Estado Fracttal",
             "F. Llamado","F. Inicio","F. Cierre","Horas resp.","SLA (h)",
             "equipo","tecnico_disp","Observación","facturacion"]
    _ren = {
        "os_fracttal":"OS Fracttal", "n_llamado":"N° Aviso",
        "cliente":"Cliente", "eds_occim":"Cód. EDS", "eds_nombre":"EDS",
        "comuna":"Comuna", "zona":"Zona", "prioridad":"Prioridad",
        "equipo":"Equipo", "tecnico_disp":"Técnico", "facturacion":"Facturación",
    }
    # Ordenar por datetime REAL antes de formatear (NaT al fondo)
    _dft = _dft.sort_values("fecha_llamado", ascending=False, na_position="last")
    _show = _dft[_cols].rename(columns=_ren)

    st.dataframe(
        _show, hide_index=True, use_container_width=True, height=680,
        column_config={
            "OS Fracttal": st.column_config.TextColumn(width=105),
            "N° Aviso":    st.column_config.TextColumn(width=85),
            "Cliente":     st.column_config.TextColumn(width=140),
            "Cód. EDS":    st.column_config.TextColumn(width=85),
            "EDS":         st.column_config.TextColumn(width=180),
            "Comuna":      st.column_config.TextColumn(width=105),
            "Zona":        st.column_config.TextColumn(width=70),
            "Prioridad":   st.column_config.TextColumn(width=70),
            "Fuente":      st.column_config.TextColumn(width=140),
            "Estado":      st.column_config.TextColumn(width=115,
                help="Estado del SLA (cumple, no cumple, atendiendo, etc.)"),
            "Estado Fracttal": st.column_config.TextColumn(width=140,
                help="Estado de la OT en Fracttal (misma clasificación que "
                     "muestra la UI de Fracttal): Pendiente / En Proceso / "
                     "En Revisión (esperando validación) / Finalizada / Cancelada"),
            "F. Llamado":  st.column_config.TextColumn(width=125),
            "F. Inicio":   st.column_config.TextColumn(width=125),
            "F. Cierre":   st.column_config.TextColumn(width=125),
            "Horas resp.": st.column_config.NumberColumn(width=90, format="%.2f"),
            "SLA (h)":     st.column_config.NumberColumn(width=70),
            "Equipo":      st.column_config.TextColumn(width=85),
            "Técnico":     st.column_config.TextColumn(width=140),
            "Observación": st.column_config.TextColumn(width=200,
                help="Observaciones y motivos de excepción SLA registrados por Operaciones"),
            "Facturación": st.column_config.TextColumn(width=115),
        },
    )

    _csv = _show.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ Descargar CSV (filtro actual)", _csv,
        file_name=f"correctivas_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )


# ────────── Estadísticas ──────────
elif vista == "📊 Estadísticas":
    _e1, _e2, _e3 = st.columns(3)

    with _e1:
        st.markdown("#### Finalizadas")
        _nc = int((_df["estado_lbl"] == "Finalizada - Cumple SLA").sum())
        _nn = int((_df["estado_lbl"] == "Finalizada - No cumple SLA").sum())
        if _nc + _nn:
            fig = go.Figure(go.Pie(
                labels=["Cumple SLA", "No cumple SLA"],
                values=[_nc, _nn], hole=.5,
                marker_colors=["#16a34a", "#dc2626"],
                textinfo="value+percent",
            ))
            fig.update_layout(height=380, margin=dict(t=30, b=30, l=20, r=20),
                              legend=dict(orientation="h", y=-0.1))
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"Total finalizadas: **{_nc + _nn:,}** "
                       f"({_nc:,} cumple / {_nn:,} no cumple)")
        else:
            st.info("Sin OTs finalizadas en el filtro actual.")

    with _e2:
        st.markdown("#### Pendientes de cierre")
        _pc = int((_df["estado_lbl"] == "OT atendida - Cumple SLA (Pend. Cierre)").sum())
        _pn = int((_df["estado_lbl"] == "OT atendida - No cumple SLA (Pend. Cierre)").sum())
        _po = int((_df["estado_lbl"] == "OT atendida (Pend. Cierre)").sum())
        _tp = _pc + _pn + _po
        if _tp:
            _plbl, _pval, _pcol = [], [], []
            if _pc:
                _plbl.append("Cumple SLA"); _pval.append(_pc); _pcol.append("#16a34a")
            if _pn:
                _plbl.append("No cumple SLA"); _pval.append(_pn); _pcol.append("#ea580c")
            if _po:
                _plbl.append("Sin evaluar"); _pval.append(_po); _pcol.append("#94a3b8")
            fig = go.Figure(go.Pie(
                labels=_plbl, values=_pval, hole=.5,
                marker_colors=_pcol, textinfo="value+percent",
            ))
            fig.update_layout(height=380, margin=dict(t=30, b=30, l=20, r=20),
                              legend=dict(orientation="h", y=-0.1))
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"Total pend. cierre: **{_tp:,}** "
                       f"(OT atendida, falta cierre en Fracttal)")
        else:
            st.info("Sin OTs pendientes de cierre en el filtro.")

    with _e3:
        st.markdown("#### Ordenes pendientes")
        _na = int((_df["estado_lbl"] == "Técnico atendiendo").sum())
        _ns = int((_df["estado_lbl"] == "OT Pendiente - Sin atender").sum())
        if _na + _ns:
            fig = go.Figure(go.Pie(
                labels=["Técnico atendiendo", "Sin atender"],
                values=[_na, _ns], hole=.5,
                marker_colors=["#f59e0b", "#dc2626"],
                textinfo="value+percent",
            ))
            fig.update_layout(height=380, margin=dict(t=30, b=30, l=20, r=20),
                              legend=dict(orientation="h", y=-0.1))
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"Total pendientes: **{_na + _ns:,}** "
                       f"({_na:,} atendiendo / {_ns:,} sin atender)")
        else:
            st.info("Sin OTs pendientes en el filtro actual.")

    # ── Salud del backlog "En Revisión" ──────────────────────────────────
    st.divider()
    st.markdown("### 🔍 Backlog de validación — OTs En Revisión")
    st.caption(
        "Cuántas OTs están esperando validación administrativa (estado "
        "*En Revisión* en Fracttal). Meta: mantener el backlog **≤35**. "
        "· 🟢 ≤35  ·  🟡 36–75  ·  🔴 76–149  ·  🟥 ≥150"
    )

    @st.cache_data(ttl=300, show_spinner=False)
    def _cargar_revision_stats():
        rows = _sb_get("ots_en_revision",
                       {"select": "folio,review_date,dias_en_revision,color_semaforo",
                        "limit": 3000})
        return pd.DataFrame(rows)

    try:
        _rev = _cargar_revision_stats()
    except Exception:
        _rev = pd.DataFrame()

    if _rev.empty:
        st.info("No hay OTs En Revisión en este momento. 🎉")
    else:
        _total_rev = len(_rev)
        # Color de salud según umbrales (4 niveles)
        if _total_rev <= 35:
            _health_col, _health_lbl = "#16a34a", "Saludable"
        elif _total_rev <= 75:
            _health_col, _health_lbl = "#eab308", "Atención"
        elif _total_rev < 150:
            _health_col, _health_lbl = "#f87171", "Crítico"  # rojo suave / coral
        else:
            _health_col, _health_lbl = "#ff2800", "Crisis"   # rojo Ferrari intenso

        _g1, _g2 = st.columns([1, 1.4])

        # Gauge de salud
        with _g1:
            _max_gauge = max(180, int(_total_rev * 1.15))
            fig_g = go.Figure(go.Indicator(
                mode="gauge+number",
                value=_total_rev,
                number=dict(font=dict(size=46, color=_health_col)),
                title=dict(text=f"<b>{_health_lbl}</b>",
                           font=dict(size=18, color=_health_col)),
                gauge=dict(
                    axis=dict(range=[0, _max_gauge], tickwidth=1),
                    bar=dict(color=_health_col, thickness=0.3),
                    steps=[
                        dict(range=[0, 35],   color="rgba(22,163,74,0.20)"),
                        dict(range=[35, 75],  color="rgba(234,179,8,0.22)"),
                        dict(range=[75, 150], color="rgba(248,113,113,0.28)"),   # rojo suave
                        dict(range=[150, _max_gauge], color="rgba(255,40,0,0.35)"),  # Ferrari
                    ],
                    threshold=dict(line=dict(color="#334155", width=3),
                                   thickness=0.75, value=75),
                ),
            ))
            fig_g.update_layout(height=300, margin=dict(t=50, b=10, l=30, r=30))
            st.plotly_chart(fig_g, use_container_width=True)
            st.caption(f"**{_total_rev}** OTs En Revisión · umbral crítico marcado en 75.")

        # Desglose por semana de ingreso a revisión (apilado por semáforo)
        with _g2:
            _rev["_rd"] = pd.to_datetime(_rev["review_date"], errors="coerce", utc=True)
            _rev_v = _rev.dropna(subset=["_rd"]).copy()
            if _rev_v.empty:
                st.info("Sin fecha de ingreso a revisión para segmentar.")
            else:
                _rev_v["_rd_cl"] = _rev_v["_rd"].dt.tz_convert(_CL_TZ)
                # Lunes de la semana + número de semana ISO del año
                _rev_v["_wk_start"] = (_rev_v["_rd_cl"]
                                       - pd.to_timedelta(_rev_v["_rd_cl"].dt.weekday, unit="D")
                                       ).dt.normalize()
                _rev_v["_wk_iso"] = _rev_v["_rd_cl"].dt.isocalendar().week.astype(int)
                _rev_v["_color"] = _rev_v["color_semaforo"].fillna("SIN").astype(str)

                # Orden cronológico de semanas + etiqueta "Sem NN" con el mes
                # debajo (el mes del jueves de esa semana ISO, que define a qué
                # mes "pertenece" la semana).
                _MES_ABR = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
                            "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
                _wk_order = (_rev_v[["_wk_start", "_wk_iso"]]
                             .drop_duplicates().sort_values("_wk_start"))
                _wk_labels = [
                    f"Sem {_iso}<br><span style='font-size:11px;color:#94a3b8'>"
                    f"{_MES_ABR[(_start + pd.Timedelta(days=3)).month]}</span>"
                    for _start, _iso in zip(_wk_order["_wk_start"], _wk_order["_wk_iso"])
                ]

                # Conteo por (semana, color)
                _piv = (_rev_v.groupby(["_wk_iso", "_color"])
                        .size().unstack(fill_value=0))
                _piv = _piv.reindex(_wk_order["_wk_iso"].tolist())

                _sem_defs = [
                    ("VERDE",    "🟢 Cerrar hoy", "#16a34a"),
                    ("AMARILLO", "🟡 Revisar",    "#eab308"),
                    ("ROJO",     "🔴 Devolver",   "#dc2626"),
                ]
                fig_wk = go.Figure()
                for _ckey, _clbl, _ccol in _sem_defs:
                    if _ckey in _piv.columns:
                        _yv = _piv[_ckey].tolist()
                        fig_wk.add_trace(go.Bar(
                            name=_clbl, x=_wk_labels, y=_yv,
                            marker_color=_ccol,
                            text=[v if v else "" for v in _yv],
                            textposition="inside",
                        ))
                fig_wk.update_layout(
                    barmode="stack",
                    height=300, margin=dict(t=30, b=30, l=10, r=10),
                    title=dict(text="OTs en revisión por semana de ingreso",
                               font=dict(size=14)),
                    yaxis=dict(title="N° OTs", showgrid=True,
                               gridcolor="rgba(128,128,128,0.15)"),
                    xaxis=dict(title="Semana ISO en que pasó a revisión"),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                xanchor="center", x=0.5),
                )
                st.plotly_chart(fig_wk, use_container_width=True)
                st.caption(
                    "Cada barra = una semana ISO; los colores muestran cuántas de "
                    "esas OTs están listas para cerrar (verde), por revisar (amarillo) "
                    "o para devolver (rojo)."
                )


# ────────── Registro (Excel) ──────────
elif vista == "📝 Registro (Excel)":
    st.caption("Vista unificada que consolida los formatos Excel de Shell, Copec y Aramco. "
               "Todas las columnas se muestran para todos los clientes.")

    _dfr = _df.copy()
    _dfr["Asunto"] = (_dfr["falla_desc"] if "falla_desc" in _dfr.columns
                      else pd.Series(dtype="object", index=_dfr.index))
    _dfr["Asunto"] = _dfr["Asunto"].fillna(_dfr["nombre_activo"]).fillna("—")
    _dfr["N_llamado"] = (_dfr["n_aviso"].fillna(_dfr["n_llamado"])
                         if "n_aviso" in _dfr.columns else _dfr["n_llamado"])
    _dfr["Codigo_EDS"] = _dfr["eds_occim"]
    _dfr["EDS_r"] = _dfr["eds_nombre"]
    _dfr["Facturacion_r"] = _dfr["facturacion"].fillna("—")
    _dfr["Fecha_llamado"] = _dfr["fecha_llamado"].dt.strftime("%d/%m/%Y").fillna("—")
    _dfr["Hora"] = _dfr["fecha_llamado"].dt.strftime("%H:%M:%S").fillna("—")
    _dfr["Atencion"] = _dfr["tipo_tarea"].fillna("—") if "tipo_tarea" in _dfr.columns else "—"
    _dfr["Mecanico"] = _dfr["tecnico_disp"].fillna("—")
    _fa = _dfr["fecha_atencion"]
    _dfr["Fecha_atencion"] = _fa.dt.strftime("%d/%m/%Y").where(_fa.notna(), "—")
    _dfr["Hora_Atencion_FIN"] = _fa.dt.strftime("%H:%M:%S").where(_fa.notna(), "—")
    _dfr["OS_FRACTTAL"] = _dfr["os_fracttal"]
    _dfr["PRIORIDAD"] = _dfr["prioridad"]
    _dfr["TMPO_RESP_ESP"] = _dfr["tiempo_resp_esp"]
    _dfr["ZONA"] = _dfr["zona"]
    _dfr["TMPO_RESP_REAL"] = _dfr["tiempo_resp_horas"].round(2)
    _dfr["STATUS_CUMPLIMIENTO"] = _dfr["cumplimiento"].fillna("—")
    _dfr["Mes"] = _dfr["fecha_llamado"].dt.month
    _dfr["Anio"] = _dfr["fecha_llamado"].dt.year
    _dfr["Dia"] = _dfr["fecha_llamado"].dt.day_name()

    _excel_cols = [
        "Asunto", "N_llamado", "Codigo_EDS", "EDS_r",
        "Facturacion_r", "Fecha_llamado", "Hora",
        "Atencion", "Mecanico", "Fecha_atencion", "Hora_Atencion_FIN",
        "OS_FRACTTAL", "PRIORIDAD", "TMPO_RESP_ESP", "ZONA",
        "TMPO_RESP_REAL", "STATUS_CUMPLIMIENTO", "Mes", "Anio", "Dia",
    ]
    _excel_ren = {
        "N_llamado": "N° llamado", "Codigo_EDS": "Codigo EDS",
        "EDS_r": "EDS",
        "Facturacion_r": "Facturación",
        "Fecha_llamado": "Fecha llamado",
        "Fecha_atencion": "Fecha de atencion",
        "Hora_Atencion_FIN": "Hora Atencion (FIN)",
        "OS_FRACTTAL": "OS FRACTTAL",
        "TMPO_RESP_ESP": "TMPO.RESP.ESP",
        "TMPO_RESP_REAL": "TMPO.RESP.REAL",
        "STATUS_CUMPLIMIENTO": "STATUS CUMPLIMIENTO",
        "Anio": "Año", "Dia": "Día",
    }
    # Ordenar por fecha_llamado datetime REAL desc (mas recientes primero)
    # NaT al fondo asi no molestan
    _dfr = _dfr.sort_values("fecha_llamado", ascending=False, na_position="last")
    _show_r = _dfr[_excel_cols].rename(columns=_excel_ren)

    st.dataframe(
        _show_r, hide_index=True, use_container_width=True, height=680,
        column_config={
            "Asunto":       st.column_config.TextColumn(width=220),
            "N° llamado":   st.column_config.TextColumn(width=110),
            "Codigo EDS":   st.column_config.TextColumn(width=90),
            "EDS":          st.column_config.TextColumn(width=180),
            "Facturación":  st.column_config.TextColumn(width=120),
            "Fecha llamado": st.column_config.TextColumn(width=110,
                help="Fecha en que se registró el llamado / aviso del cliente"),
            "Hora":         st.column_config.TextColumn(width=80,
                help="Hora del llamado"),
            "Atencion":     st.column_config.TextColumn(width=150),
            "Mecanico":     st.column_config.TextColumn(width=150),
            "Fecha de atencion": st.column_config.TextColumn(width=120),
            "Hora Atencion (FIN)": st.column_config.TextColumn(width=100),
            "OS FRACTTAL":  st.column_config.TextColumn(width=105),
            "PRIORIDAD":    st.column_config.TextColumn(width=80),
            "TMPO.RESP.ESP": st.column_config.NumberColumn(width=80),
            "ZONA":         st.column_config.TextColumn(width=80),
            "TMPO.RESP.REAL": st.column_config.NumberColumn(width=90, format="%.2f"),
            "STATUS CUMPLIMIENTO": st.column_config.TextColumn(width=130),
            "Mes":          st.column_config.NumberColumn(width=50),
            "Año":          st.column_config.NumberColumn(width=60),
            "Día":          st.column_config.TextColumn(width=90),
        },
    )

    _csv_r = _show_r.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ Descargar CSV (formato registro)", _csv_r,
        file_name=f"registro_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )


# ══════════════════════════════════════════════════════════════════════
# Vista: Validación En Revisión
# ══════════════════════════════════════════════════════════════════════
if vista == "🔍 Validación En Revisión":

    @st.cache_data(ttl=300, show_spinner="Cargando OTs en revisión...")
    def cargar_ots_revision() -> pd.DataFrame:
        rows = _sb_get("ots_en_revision", {
            "select": "*",
            "order": "dias_en_revision.desc,folio.desc",
            "limit": 2000,
        })
        return pd.DataFrame(rows)

    _dfr = cargar_ots_revision()

    # ── Barra de actualización: última sincronización + botón manual ──────
    _upd_lbl = "—"
    if not _dfr.empty and "updated_at" in _dfr.columns:
        try:
            _last = pd.to_datetime(_dfr["updated_at"], errors="coerce", utc=True).max()
            if pd.notna(_last):
                _upd_lbl = _last.tz_convert(_CL_TZ).strftime("%d/%m/%Y %H:%M")
        except Exception:
            pass

    _uc1, _uc2 = st.columns([3, 1])
    with _uc1:
        st.caption(
            f"🕒 Última sincronización con Fracttal: **{_upd_lbl}**  ·  "
            f"El sync automático corre en segundo plano; usá el botón para "
            f"traer los datos más recientes al instante."
        )
    with _uc2:
        _do_sync = st.button("🔄 Actualizar ahora", use_container_width=True,
            help="Trae en vivo las OTs En Revisión desde Fracttal (~2 min).")

    if _do_sync:
        import sys as _sys
        _need = ("SUPABASE_URL", "SUPABASE_KEY",
                 "FRACTTAL_CLIENT_ID", "FRACTTAL_CLIENT_SECRET")
        _missing = []
        for _k in _need:
            _v = None
            try:
                _v = st.secrets[_k]
            except Exception:
                _v = os.getenv(_k)
            if _v:
                os.environ[_k] = str(_v)
            else:
                _missing.append(_k)
        if _missing:
            st.error(
                f"Faltan credenciales para sincronizar: **{', '.join(_missing)}**. "
                f"Agrégalas en Streamlit Cloud → Settings → Secrets."
            )
        else:
            _root = str(Path(__file__).parent.parent)
            if _root not in _sys.path:
                _sys.path.insert(0, _root)
            try:
                with st.spinner("Sincronizando con Fracttal… puede tardar ~2 min."):
                    import importlib
                    import sync_ots_revision as _sync
                    importlib.reload(_sync)   # re-lee las env vars recién seteadas
                    _sync.main()
                st.cache_data.clear()
                st.success("✅ Datos actualizados desde Fracttal.")
                st.rerun()
            except (Exception, SystemExit) as _e:
                st.error(f"No se pudo sincronizar: {_e}")

    if _dfr.empty:
        st.info("No hay OTs En Revisión en este momento. Todo al día. 🎉")
    else:
        # KPIs arriba
        _n_total = len(_dfr)
        _n_verde = int((_dfr["color_semaforo"] == "VERDE").sum())
        _n_amar  = int((_dfr["color_semaforo"] == "AMARILLO").sum())
        _n_rojo  = int((_dfr["color_semaforo"] == "ROJO").sum())
        _n_15    = int((_dfr["dias_en_revision"] >= 15).sum())
        _n_old   = int((_dfr["dias_en_revision"] >= 30).sum())
        _monto_v = _dfr.loc[_dfr["color_semaforo"] == "VERDE", "total_cost"].sum()

        _k1, _k2, _k3, _k4, _k5, _k6 = st.columns(6)
        _k1.metric("Total pendientes", f"{_n_total}")
        _k2.metric("🟢 Cerrar hoy", f"{_n_verde}", f"${int(_monto_v):,}".replace(",", "."))
        _k3.metric("🟡 Revisar", f"{_n_amar}")
        _k4.metric("🔴 Devolver", f"{_n_rojo}")
        _k5.metric("⏳ >15 días", f"{_n_15}",
                   help="OTs con 15 días o más esperando validación")
        _k6.metric("⚠️ >30 días", f"{_n_old}",
                   help="OTs con 30 días o más — priorizar cierre urgente")

        # Filtros
        st.markdown('<div class="section-hdr">Filtros</div>', unsafe_allow_html=True)
        _f1, _f2, _f3, _f4 = st.columns([1.2, 1.2, 1, 1.5])
        with _f1:
            _f_color = st.multiselect("Semáforo",
                ["VERDE", "AMARILLO", "ROJO"],
                default=["VERDE", "AMARILLO", "ROJO"],
                format_func=lambda x: x.capitalize())
        with _f2:
            _tecnicos_disp = sorted(t for t in _dfr["personnel"].dropna().unique() if t)
            _f_tec = st.multiselect("Técnico", _tecnicos_disp, default=[])
        with _f3:
            _tipos_disp = sorted(t for t in _dfr["tipo"].dropna().unique() if t)
            _f_tipo = st.multiselect("Tipo", _tipos_disp, default=[])
        with _f4:
            _f_buscar = st.text_input("Buscar (folio / activo / EDS)", "")

        # Segunda fila de filtros: rango de fechas "pasó a revisión"
        _dfr["_review_dt"] = pd.to_datetime(
            _dfr["review_date"], errors="coerce", utc=True).dt.tz_convert(_CL_TZ)
        _fechas_validas = _dfr["_review_dt"].dropna()
        if not _fechas_validas.empty:
            _min_date = _fechas_validas.min().date()
            _max_date = _fechas_validas.max().date()

            _ff1, _ff2 = st.columns([2, 4])
            with _ff1:
                _f_fechas = st.date_input(
                    "Rango 'Pasó a revisión' (desde — hasta)",
                    value=(_min_date, _max_date),
                    min_value=_min_date,
                    max_value=_max_date,
                    format="DD/MM/YYYY",
                    help="Filtra por la fecha en que el técnico marcó DONE",
                )
            with _ff2:
                st.caption(f"OTs entre **{_min_date.strftime('%d/%m/%Y')}** "
                           f"y **{_max_date.strftime('%d/%m/%Y')}** disponibles. "
                           f"Ajustá el rango para acotar.")
        else:
            _f_fechas = None

        _dff = _dfr.copy()
        if _f_color:
            _dff = _dff[_dff["color_semaforo"].isin(_f_color)]
        if _f_tec:
            _dff = _dff[_dff["personnel"].isin(_f_tec)]
        if _f_tipo:
            _dff = _dff[_dff["tipo"].isin(_f_tipo)]
        if _f_buscar:
            q = _f_buscar.upper()
            _mask = pd.Series(False, index=_dff.index)
            for c in ("folio", "activo", "parent_desc", "eds_occim", "personnel"):
                _mask = _mask | _dff[c].astype(str).str.upper().str.contains(
                    q, na=False, regex=False)
            _dff = _dff[_mask]
        # Filtro por rango de fechas
        if _f_fechas and isinstance(_f_fechas, tuple) and len(_f_fechas) == 2:
            _d0, _d1 = _f_fechas
            _dff = _dff[
                (_dff["_review_dt"].dt.date >= _d0) &
                (_dff["_review_dt"].dt.date <= _d1)
            ]
        _dff = _dff.drop(columns=["_review_dt"], errors="ignore")

        # ── Resolución: conclusión breve sobre si cerrar o no ─────────────
        def _s(v) -> str:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return ""
            return str(v)
        def _resolucion(r) -> str:
            color = _s(r.get("color_semaforo"))
            motivo = _s(r.get("motivo_semaforo"))
            tipo = _s(r.get("tipo")).upper()
            metodo = _s(r.get("metodo_deteccion")).upper()
            try:
                pct = int(r.get("completed_pct") or 0)
            except Exception:
                pct = 0
            try:
                costo = float(r.get("total_cost") or 0)
            except Exception:
                costo = 0.0
            es_correctiva = tipo.startswith("CORRECT")
            # Preventivas nunca son remotas (por definición del negocio)
            es_remota = "REMOTA" in metodo and not tipo.startswith("PREVENT")

            if color == "VERDE":
                if es_remota:
                    return "Atendida vía remota — 100% completa, sin necesidad de recursos físicos. Cerrar"
                partes = ["100% completa"]
                if costo > 0:
                    partes.append(f"costos cargados (${int(costo):,})".replace(",", "."))
                else:
                    partes.append("con recursos cargados")
                if es_correctiva:
                    return "Correctiva OK — " + ", ".join(partes) + ", datos de falla registrados. Cerrar"
                return ("Preventiva OK — " if tipo.startswith("PREVENT") else "OT OK — ") + ", ".join(partes) + ". Cerrar"

            if color == "AMARILLO":
                if "sin: tipo falla" in motivo.lower() or "sin: causa" in motivo.lower() or "sin: deteccion" in motivo.lower():
                    return f"Correctiva incompleta — {motivo}. Pedir al técnico completar. NO cerrar"
                if "dice 'si'" in motivo.lower() or "dice 'no'" in motivo.lower():
                    return f"Incongruencia repuestos — {motivo}. Validar con técnico antes de cerrar"
                if "cambio" in motivo.lower() or "reemplaz" in motivo.lower():
                    return f"Trabajo menciona cambio de pieza pero no hay repuesto cargado. Validar antes de cerrar"
                return f"Revisar: {motivo}. Confirmar con técnico antes de cerrar"

            if color == "ROJO":
                if "completitud" in motivo.lower():
                    return f"Incompleta ({pct}%) — falta terminar el trabajo en Fracttal. NO cerrar"
                if "sin recursos" in motivo.lower():
                    return "Sin recursos registrados — pedir al técnico cargar mano de obra / repuestos / servicios. NO cerrar"
                return f"Requiere corrección: {motivo}. NO cerrar"

            return motivo or "—"

        _dff = _dff.copy()
        _dff["_resolucion"] = _dff.apply(_resolucion, axis=1)

        # ── Método de atención simplificado ──────────────────────────────
        # Preventivas → siempre Presencial MP (nunca remotas).
        # Correctivas → Presencial MC o Remota según método de detección.
        # Otros (Solicitud Comercial, etc.) → según método.
        def _metodo_corto(row) -> str:
            tipo = _s(row.get("tipo")).upper()
            metodo = _s(row.get("metodo_deteccion")).upper()
            if tipo.startswith("PREVENT"):
                return "👷 Presencial MP"
            if "REMOTA" in metodo:
                return "🌐 Remota"
            if tipo.startswith("CORRECT"):
                return "👷 Presencial MC"
            if "PRESENCIAL" in metodo:
                return "👷 Presencial"
            if not metodo:
                return "—"
            import re as _re
            return _re.sub(r"^\d+\.-\s*", "", _s(row.get("metodo_deteccion"))).title()
        _dff["_metodo_short"] = _dff.apply(_metodo_corto, axis=1)

        # ── Separar activo y estación ─────────────────────────────────────
        # Fracttal devuelve el activo como "LAVADORA MSELF 2021 ... COPEC LOMAS
        # COLORADAS" — pegado con el nombre de la estación. El parent_desc
        # trae la ruta: "// COPEC/ COPEC LOMAS COLORADAS/". Usamos eso para
        # separar limpiamente en dos columnas: Activo (solo equipo) y Estación.
        def _extraer_estacion_full(pd_val) -> str:
            parts = [p.strip() for p in _s(pd_val).replace("//", "").split("/") if p.strip()]
            return parts[1] if len(parts) >= 2 else (parts[0] if parts else "")

        def _limpiar_activo(activo_val, pd_val) -> str:
            act = _s(activo_val).strip()
            est_full = _extraer_estacion_full(pd_val)
            if est_full and act.upper().endswith(est_full.upper()):
                act = act[:-len(est_full)].strip().rstrip("-").strip()
            return act

        def _estacion_sin_cliente(pd_val, cliente_val) -> str:
            est = _extraer_estacion_full(pd_val)
            cli = _s(cliente_val).strip().upper()
            if cli and est.upper().startswith(cli + " "):
                est = est[len(cli) + 1:].strip()
            return est

        _dff["_estacion"] = _dff.apply(
            lambda r: _estacion_sin_cliente(r.get("parent_desc"), r.get("cliente")), axis=1)
        if "activo" in _dff.columns:
            _dff["activo"] = _dff.apply(
                lambda r: _limpiar_activo(r.get("activo"), r.get("parent_desc")), axis=1)

        # Normalizar Tipo, Activo y Estación a formato título
        if "tipo" in _dff.columns:
            _dff["tipo"] = _dff["tipo"].astype(str).apply(
                lambda x: x.title() if x and x.lower() not in ("nan", "none") else "—"
            )
        if "activo" in _dff.columns:
            _dff["activo"] = _dff["activo"].astype(str).apply(
                lambda x: x.title() if x and x.lower() not in ("nan", "none") else "—"
            )
        _dff["_estacion"] = _dff["_estacion"].astype(str).apply(
            lambda x: x.title() if x and x.lower() not in ("nan", "none") else "—"
        )

        st.caption(f"Mostrando **{len(_dff)}** de {_n_total} OTs.")

        # Acciones
        _a1, _a2, _a3 = st.columns([1, 1, 3])
        with _a1:
            _folios_verdes = _dfr.loc[
                _dfr["color_semaforo"] == "VERDE", "folio"].tolist()
            if _folios_verdes:
                st.download_button(
                    f"📋 Copiar {_n_verde} folios verdes (TXT)",
                    "\n".join(_folios_verdes),
                    file_name=f"folios_verdes_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
                    mime="text/plain",
                    help="Descarga la lista de folios listos para cerrar. Copiar y pegar en Fracttal.",
                )
        with _a2:
            _csv = _dff.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "⬇️ Exportar Excel/CSV",
                _csv,
                file_name=f"ots_en_revision_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
            )

        # Tabla principal - ORDEN: Semáforo, Fecha, N° OT, luego el resto
        _COL_MAP = {
            "color_semaforo":     "Semáforo",
            "review_date":        "Fecha - pasó a revisión",
            "folio":              "N° OT",
            "tipo":               "Tipo",
            "_metodo_short":      "Método",
            "personnel":          "Técnico",
            "cliente":            "Cliente",
            "activo":             "Activo",
            "_estacion":          "Estación",
            "eds_occim":          "Cód. EDS",
            "dias_en_revision":   "Días",
            "completed_pct":      "%",
            "total_cost":         "Costo $",
            "motivo_semaforo":    "Motivo",
            "trabajo_realizado":  "Trabajo realizado (técnico)",
            "entrega_repuestos":  "¿Entregó rep.?",
            "repuestos_detalle":  "Repuestos usados",
            "descripcion_falla":  "Descripción falla",
            "_resolucion":        "Resolución",
        }
        _cols_out = [c for c in _COL_MAP if c in _dff.columns]
        _tbl = _dff[_cols_out].rename(columns=_COL_MAP).copy()

        # Emoji en semaforo (solo icono) + emoji en entrega repuestos
        _emoji = {"VERDE": "🟢", "AMARILLO": "🟡", "ROJO": "🔴"}
        _tbl["Semáforo"] = _tbl["Semáforo"].map(lambda x: _emoji.get(x, x or ""))
        if "¿Entregó rep.?" in _tbl.columns:
            _emoji_rep = {"SI": "✅ SI", "NO": "❌ NO", "N/A": "➖ N/A"}
            _tbl["¿Entregó rep.?"] = _tbl["¿Entregó rep.?"].map(
                lambda x: _emoji_rep.get(x, "—" if pd.isna(x) else str(x)))

        # Formatear review_date (UTC -> Chile)
        if "Fecha - pasó a revisión" in _tbl.columns:
            _tbl["Fecha - pasó a revisión"] = (pd.to_datetime(
                _tbl["Fecha - pasó a revisión"], errors="coerce", utc=True)
                .dt.tz_convert(_CL_TZ)
                .dt.strftime("%d/%m/%Y %H:%M"))

        # Controles de selección masiva
        _sel1, _sel2, _sel3 = st.columns([1, 1, 4])
        with _sel1:
            _marcar_verdes = st.button("✅ Marcar todas VERDES",
                help="Selecciona automáticamente todas las OTs con semáforo verde")
        with _sel2:
            _desmarcar = st.button("⬜ Desmarcar todo")

        # Preparar dataframe con columna checkbox al inicio
        _tbl.insert(0, "Cerrar", False)

        # Init/actualizar estado con base en filas visibles
        _folio_col = "N° OT"
        if _marcar_verdes:
            _tbl["Cerrar"] = _tbl["Semáforo"].eq("🟢")
        elif _desmarcar:
            _tbl["Cerrar"] = False

        _edited = st.data_editor(
            _tbl,
            use_container_width=True,
            hide_index=True,
            disabled=[c for c in _tbl.columns if c != "Cerrar"],
            key="tbl_revision_editor",
            column_config={
                "Cerrar": st.column_config.CheckboxColumn(
                    "☐",
                    width=40,
                    help="Marcá las OTs que querés cerrar automáticamente",
                    default=False,
                ),
                "Fecha - pasó a revisión": st.column_config.TextColumn(width=110,
                    help="Fecha en que el técnico marcó DONE y quedó esperando validación"),
                "N° OT":            st.column_config.TextColumn(width=90),
                "Tipo":             st.column_config.TextColumn(width=110),
                "Método":           st.column_config.TextColumn(width=120,
                    help="Método de detección/atención: Presencial, Remota, etc."),
                "Técnico":          st.column_config.TextColumn(width=180),
                "Cliente":          st.column_config.TextColumn(width=90),
                "Activo":           st.column_config.TextColumn(width=200,
                    help="Equipo (sin la estación — la estación va en su propia columna)"),
                "Estación":         st.column_config.TextColumn(width=180,
                    help="Nombre de la estación de servicio (extraído del árbol Fracttal)"),
                "Cód. EDS":         st.column_config.TextColumn(width=80,
                    help="Código Occimiano de la estación (ej. SH_211, 60072, EE_S195)"),
                "Días":             st.column_config.NumberColumn(
                    width=60, format="%d",
                    help="Días esperando validación"),
                "%":                st.column_config.NumberColumn(
                    width=60, format="%d%%"),
                "Costo $":          st.column_config.NumberColumn(
                    width=100, format="$%d"),
                "Semáforo":         st.column_config.TextColumn(width=60,
                    help="🟢 listo para cerrar · 🟡 revisar · 🔴 no cerrar"),
                "Motivo":           st.column_config.TextColumn(width=250,
                    help="Motivo del color del semáforo (incluye incongruencias)"),
                "Trabajo realizado (técnico)": st.column_config.TextColumn(
                    width=280,
                    help="Comentario del técnico en 'TRABAJO REALIZADO PARA CORRECCIÓN'"),
                "¿Entregó rep.?":   st.column_config.TextColumn(width=100,
                    help="Campo 'ENTREGA DE REPUESTOS CAMBIADOS' del técnico"),
                "Repuestos usados": st.column_config.TextColumn(width=220,
                    help="Recursos tipo inventario/repuesto registrados en Fracttal"),
                "Descripción falla": st.column_config.TextColumn(width=220,
                    help="Campo 'DESCRIPCIÓN DE LA FALLA ENCONTRADA' del técnico"),
                "Resolución": st.column_config.TextColumn(width=320,
                    help="Conclusión sugerida: por qué (o no) se debería cerrar esta OT"),
            },
        )

        # Folios seleccionados por el usuario
        _folios_seleccionados = _edited.loc[
            _edited["Cerrar"] == True, _folio_col].dropna().tolist()

        if _folios_seleccionados:
            st.success(
                f"**{len(_folios_seleccionados)} OT(s) seleccionadas para cerrar:** "
                f"{', '.join(_folios_seleccionados[:5])}"
                f"{'...' if len(_folios_seleccionados) > 5 else ''}"
            )

            # Generar contenido del .bat
            _folios_arg = " ".join(_folios_seleccionados)
            _bat_content = (
                "@echo off\r\n"
                f"REM Cierre automatico de {len(_folios_seleccionados)} OTs generado desde el panel\r\n"
                "cd /d C:\\Users\\jgavi\\Documents\\occimiano_dashboard\r\n"
                f"python cierre_ots_playwright.py {_folios_arg}\r\n"
                "echo.\r\n"
                "echo === Presiona ENTER para cerrar esta ventana ===\r\n"
                "pause > nul\r\n"
            )
            _bat_name = f"cerrar_{len(_folios_seleccionados)}_ots_{datetime.now().strftime('%Y%m%d_%H%M')}.bat"

            _b1, _b2 = st.columns([1.5, 4])
            with _b1:
                st.download_button(
                    label=f"⬇️ Descargar cerrar_{len(_folios_seleccionados)}_ots.bat",
                    data=_bat_content.encode("utf-8"),
                    file_name=_bat_name,
                    mime="application/x-bat",
                    type="primary",
                    help=f"Descarga un archivo .bat que ejecuta el cierre automático de las {len(_folios_seleccionados)} OTs seleccionadas",
                )
            with _b2:
                st.markdown(
                    f"👉 **Pasos:** 1) Click en el botón azul de la izquierda "
                    f"para descargar el archivo · 2) **Doble click** al archivo "
                    f"descargado · Chrome se abre solo, hace login, cierra las "
                    f"**{len(_folios_seleccionados)}** OTs y reporta al final."
                )

        # ══════ Historial de cierres (auditoría) ══════
        st.divider()
        st.markdown("### 📜 Historial de cierres (últimos 50)")

        @st.cache_data(ttl=60, show_spinner=False)
        def cargar_auditoria() -> pd.DataFrame:
            try:
                rows = _sb_get("ots_cierres_auditoria", {
                    "select": "*",
                    "order": "intento_at.desc",
                    "limit": 50,
                })
                return pd.DataFrame(rows)
            except Exception:
                return pd.DataFrame()

        _dfa = cargar_auditoria()
        if _dfa.empty:
            st.caption("Sin cierres registrados aún. Cuando corras el comando de arriba, "
                       "cada cierre queda logueado acá.")
        else:
            # Convertir UTC -> hora Chile
            _dfa["intento_at"] = (pd.to_datetime(_dfa["intento_at"],
                                                 errors="coerce", utc=True)
                                  .dt.tz_convert(_CL_TZ)
                                  .dt.strftime("%d/%m %H:%M:%S"))
            _dfa["resultado"] = _dfa["resultado"].map(
                lambda x: f"✅ {x}" if x in ("OK", "DRY_OK") else f"❌ {x}")
            _dfa_show = _dfa[["intento_at", "folio", "resultado", "motivo",
                              "duracion_ms", "ejecutado_por"]].rename(columns={
                "intento_at": "Cuándo",
                "folio": "N° OT",
                "resultado": "Resultado",
                "motivo": "Motivo/detalle",
                "duracion_ms": "ms",
                "ejecutado_por": "Por",
            })
            st.dataframe(_dfa_show, use_container_width=True, hide_index=True)

        st.divider()
        st.markdown("### Cómo usar este panel")
        st.markdown(
            "- **🟢 Verdes**: usar el botón de cierre automático de arriba o descargar "
            "la lista de folios y cerrar manual en Fracttal.\n"
            "- **🟡 Amarillas**: correctivas sin falla completa. Consultar al técnico antes de cerrar.\n"
            "- **🔴 Rojas**: devolvé al técnico — el motivo aparece en la columna. Los datos faltantes son obligatorios."
        )
        st.caption(f"Datos actualizados cada 30 min desde Fracttal API. "
                   f"Fuente: tabla `ots_en_revision` en Supabase.")


# Footer
st.divider()
st.caption(
    f"Fuente: Supabase · vista `v_llamados_sla` desde {FECHA_CORTE} · "
    f"Fuente (robot/directa) desde `llamados_correctivos` · Cache 5 min · "
    f"Última consulta: {datetime.now().strftime('%H:%M:%S')}"
)

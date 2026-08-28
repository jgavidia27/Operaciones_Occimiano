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


def _title_smart(s):
    """Title-case que preserva siglas cortas (LVX, LT, EDS, N°, F.N.A.O., etc.)
    Usado para limpiar textos de Copec/Fracttal que vienen en MAYUSCULAS."""
    if not s or not isinstance(s, str):
        return s or ""
    out = []
    for w in s.split():
        if len(w) <= 3 and w.isupper() and w.isalpha():
            out.append(w)
        elif "°" in w:
            out.append(w[0].upper() + w[1:].lower())
        else:
            out.append(w.capitalize())
    return " ".join(out)


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
        # Si Fracttal reporta la tarea como PAUSED, el técnico detuvo la
        # atención (no está trabajando ahora). Mostrarlo aparte.
        _et = str(row.get("_ot_estado_tarea") or "").strip().upper()
        if _et == "PAUSED":
            return ("Técnico pausó", "⏸️", "#94a3b8",
                    "Técnico pausó la atención en Fracttal")
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

    # Fuente adicional del aviso: solicitudes_trabajo (id_solicitud ↔ wo_folio).
    # Rescata la gran mayoría de OTs COPEC/Shell/Aramco cuyo n_llamado
    # en v_llamados_sla viene NULL. El robot de correo suele quedar
    # sin matchear a la OT, pero solicitudes_trabajo sí registra el par.
    try:
        st_rows = _sb_get("solicitudes_trabajo", {
            "select": "wo_folio,id_solicitud",
            "wo_folio": "not.is.null",
            "fecha_solicitud": f"gte.{fecha_desde}",
            "limit": 5000,
        })
    except Exception:
        st_rows = []
    n_solicitud_map = {r["wo_folio"]: str(r["id_solicitud"])
                       for r in st_rows
                       if r.get("wo_folio") and r.get("id_solicitud") is not None}

    df["falla"] = df["os_fracttal"].map(falla_map)
    df["n_aviso"] = df["os_fracttal"].map(n_aviso_map)
    df["n_solicitud"] = df["os_fracttal"].map(n_solicitud_map)
    df["fuente_bd"] = df["os_fracttal"].map(fuente_map)

    # Coalesce del aviso — PRIORIDAD CORREGIDA:
    #   1) n_aviso de llamados_correctivos → N° REAL del cliente
    #      (Copec 8 dig, Shell 4 dig, Aramco/Cotalker 6 dig)
    #   2) n_solicitud de solicitudes_trabajo → contador interno Occimiano
    #      (fallback cuando el robot no registró el correo)
    # NO usamos v_llamados_sla.n_llamado — es un ROW_NUMBER de la vista,
    # no un identificador del negocio (~4 dígitos incrementales sin sentido).
    def _first_non_null(*vals):
        for v in vals:
            if v is None: continue
            if isinstance(v, float) and pd.isna(v): continue
            s = str(v).strip()
            if s and s.lower() not in ("nan", "none", "null"):
                return s
        return None
    df["n_llamado"] = [
        _first_non_null(na, ns)
        for na, ns in zip(df["n_aviso"], df["n_solicitud"])
    ]

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

/* ── Filtros ejecutivos: chips azul marino sobre blanco ─────────── */
/* Multiselect: label más pequeño y espacio compacto */
[data-testid="stMultiSelect"] label,
[data-testid="stTextInput"] label,
[data-testid="stDateInput"] label,
[data-testid="stSelectbox"] label {
    font-size:.72rem !important; font-weight:600 !important;
    color:#475569 !important; text-transform:uppercase;
    letter-spacing:.04em; margin-bottom:2px !important;
}
/* Chip seleccionado dentro del multiselect (BaseWeb "Tag") */
[data-testid="stMultiSelect"] span[data-baseweb="tag"] {
    background:#1e3a8a !important;      /* navy */
    border-color:#1e3a8a !important;
    color:#fff !important;
    border-radius:4px !important;
    padding:2px 8px !important;
    font-size:.72rem !important;
    font-weight:600 !important;
    line-height:1.15 !important;
    margin:1px 3px 1px 0 !important;
}
[data-testid="stMultiSelect"] span[data-baseweb="tag"] * {
    color:#fff !important; fill:#fff !important;
}
[data-testid="stMultiSelect"] span[data-baseweb="tag"] svg {
    color:#fff !important; fill:#fff !important; opacity:.85;
}
[data-testid="stMultiSelect"] span[data-baseweb="tag"]:hover {
    background:#1e40af !important;      /* hover un tono más claro */
}
/* Contenedor del multiselect: borde suave, más denso */
[data-testid="stMultiSelect"] > div > div {
    min-height:34px !important;
    border-radius:6px !important;
    border-color:#e2e8f0 !important;
    background:#f8fafc !important;
}
/* Inputs de texto y date: mismo look que multiselect */
[data-testid="stTextInput"] input,
[data-testid="stDateInput"] input {
    background:#f8fafc !important;
    border-radius:6px !important;
    font-size:.85rem !important;
}
/* Compactar el bloque completo de filtros */
[data-testid="stHorizontalBlock"] > div:has(> [data-testid="stMultiSelect"]),
[data-testid="stHorizontalBlock"] > div:has(> [data-testid="stTextInput"]),
[data-testid="stHorizontalBlock"] > div:has(> [data-testid="stDateInput"]) {
    padding-top:0 !important;
}
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
# ── Barra ejecutiva compacta: buscador + rango + toggle siempre visibles;
#    el resto de filtros vive dentro de un expander colapsado por defecto.
_fuentes = sorted([f for f in df["fuente"].dropna().unique() if f])
_clientes = sorted(df["cliente"].dropna().unique())
_prios = sorted(df["prioridad"].dropna().unique())
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
_est_default = [e for e in _est_opts if e != "Descartada"]

_hoy_date = datetime.now(_CL_TZ).date()
_max_dt = df["fecha_llamado"].max()
_min_dt = df["fecha_llamado"].min()
_fmax_data = _max_dt.date() if pd.notna(_max_dt) else _hoy_date
_fmax = max(_fmax_data, _hoy_date)
_fmin_data = _min_dt.date() if pd.notna(_min_dt) else _fmax
_fmin = min(_fmin_data, _fmax)

_bar1, _bar2, _bar3 = st.columns([3, 2, 1.5])
with _bar1:
    buscar = st.text_input(
        "Buscar",
        placeholder="OS-XXXXX · N° aviso · código EDS · nombre · falla · técnico · comuna",
        key="q",
    )
with _bar2:
    try:
        fecha_rng = st.date_input(
            "Rango de fechas", (_fmin, _fmax),
            min_value=_fmin, max_value=_fmax, key="fecha_rng_v3",
        )
    except Exception:
        st.session_state.pop("fecha_rng_v3", None)
        fecha_rng = (_fmin, _fmax)
with _bar3:
    st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    _solo_pend = st.toggle("Solo pendientes", key="solo_pend",
                           help="Solo OTs sin atender + técnico atendiendo")

# Resumen inline del estado de los filtros avanzados
def _mini(sel, total):
    if not sel or len(sel) == total:
        return f"Todas ({total})"
    return f"{len(sel)}/{total}"
_fuente_sel_prev = st.session_state.get("fuente_v2", _fuentes)
_cliente_sel_prev = st.session_state.get("cliente_v2", _clientes)
_pri_sel_prev = st.session_state.get("pri_v2", _prios)
_est_sel_prev = st.session_state.get("estado_v2", _est_default)
_resumen_fltr = (
    f"Fuente · {_mini(_fuente_sel_prev, len(_fuentes))}  |  "
    f"Cliente · {_mini(_cliente_sel_prev, len(_clientes))}  |  "
    f"Prioridad · {_mini(_pri_sel_prev, len(_prios))}  |  "
    f"Estado · {_mini(_est_sel_prev, len(_est_opts))}"
)

with st.expander(f"🎛 Filtros avanzados  ·  {_resumen_fltr}", expanded=False):
    _f1, _f2, _f3, _f4 = st.columns([1.3, 1.3, 1.1, 2])
    with _f1:
        fuente_sel = st.multiselect(
            "Fuente", _fuentes, default=_fuentes, key="fuente_v2",
            format_func=lambda f: f"{FUENTE_META.get(f, ('❓','?','',''))[0]} {FUENTE_META.get(f, ('','?','',''))[1]}"
                                  if f in FUENTE_META else f,
        )
    with _f2:
        cliente_sel = st.multiselect("Cliente", _clientes,
                                     default=_clientes, key="cliente_v2")
    with _f3:
        pri_sel = st.multiselect("Prioridad", _prios,
                                 default=_prios, key="pri_v2")
    with _f4:
        est_sel = st.multiselect("Estado / SLA", _est_opts,
                                 default=_est_default, key="estado_v2")

# Fuera del expander leemos el estado (Streamlit persiste por session_state
# gracias a las keys). Si el expander nunca se abrió esta corrida, usamos
# el default para inicializar la variable local.
fuente_sel  = st.session_state.get("fuente_v2", _fuentes)
cliente_sel = st.session_state.get("cliente_v2", _clientes)
pri_sel     = st.session_state.get("pri_v2", _prios)
est_sel     = st.session_state.get("estado_v2", _est_default)

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
                           "📝 Registro (Excel)", "🔍 Cierre Fracttal",
                           "🔗 Enlace Copec", "🔧 Repuestos", "📊 Estadísticas"],
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

    # Orden dinámico:
    #  - Solo pendientes ON  → por urgencia SLA (más próximas a vencer arriba;
    #                          vencidas primero, luego menos holgura, y las
    #                          sin SLA definido al final).
    #  - Solo pendientes OFF → cronológico: más recientes primero.
    if _solo_pend:
        _sec = _df.copy()
        _fl = _sec["fecha_llamado"]
        _um = pd.to_numeric(_sec["tiempo_resp_esp"], errors="coerce")
        _deadline = _fl + pd.to_timedelta(_um.fillna(0), unit="h")
        _rest_s = (_deadline - _now_naive).dt.total_seconds()
        # OTs sin SLA definido → NaN → al final
        _rest_s = _rest_s.where(_fl.notna() & _um.notna() & (_um > 0), other=pd.NA)
        _sec["_holgura_seg"] = _rest_s
        _dff = _sec.sort_values("_holgura_seg", ascending=True, na_position="last")
        _dff = _dff.drop(columns=["_holgura_seg"])
        _orden_msg = "orden: más próximas a vencer arriba (vencidas primero)."
    else:
        # NaT al fondo, resto por fecha_llamado desc (última llegada primero)
        _dff = _df.sort_values("fecha_llamado", ascending=False, na_position="last")
        _orden_msg = "orden: más recientes primero."

    if _lim != "Todo":
        _dff = _dff.head(int(_lim))

    st.caption(f"Mostrando **{len(_dff):,}** de {_n_tot:,} llamados · "
               f"{_orden_msg}")

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

    # Title-case a los campos que vienen en MAYUSCULAS desde Fracttal/Copec
    for _c in ("eds_nombre", "comuna", "equipo", "tecnico_disp", "cliente"):
        if _c in _dft.columns:
            _dft[_c] = _dft[_c].fillna("").astype(str).map(_title_smart)

    _cols = ["os_fracttal","n_llamado","cliente","eds_occim","eds_nombre",
             "comuna","zona","prioridad","Fuente","Estado","Estado Fracttal",
             "F. Llamado","F. Inicio","F. Cierre","Horas resp.","SLA (h)",
             "equipo","tecnico_disp","Observación","facturacion"]
    _ren = {
        "os_fracttal":"N° OT Fracttal", "n_llamado":"N° Aviso",
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
            "N° OT Fracttal": st.column_config.TextColumn(width=105),
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
            _health_col, _health_lbl = "#f97316", "Crítico"  # naranja intenso
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
                        dict(range=[75, 150], color="rgba(249,115,22,0.28)"),   # naranja
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

    # Title-case a los campos que Fracttal/Copec envian en MAYUSCULAS
    for _c in ("Asunto", "EDS_r", "Mecanico", "Atencion", "Facturacion_r"):
        if _c in _dfr.columns:
            _dfr[_c] = _dfr[_c].fillna("—").astype(str).map(
                lambda x: _title_smart(x) if x and x != "—" else "—")

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
# Vista: Repuestos — catálogo consolidado (Occim + Copec)
# ══════════════════════════════════════════════════════════════════════
elif vista == "🔧 Repuestos":

    _DATA_DIR = Path(__file__).parent / "data"

    @st.cache_data(ttl=3600, show_spinner="Cargando catálogo de repuestos…")
    def _cargar_catalogo_repuestos() -> pd.DataFrame:
        """Consolida el catálogo desde los dos Excel oficiales.

        FUENTE PRINCIPAL: el archivo COPEC (Vigente / Inactivo / Próximo)
        → cada fila = 1 par (cod_occim, cod_copec). Un mismo REP puede
        aparecer varias veces si Copec le asignó códigos distintos a lo
        largo del tiempo (ej. REP-0351 vive con código nuevo 504033
        vigente Y con código antiguo 504902 inactivo).

        ENRIQUECIMIENTO: la lista OCCIM aporta precio 1S 2026,
        %variación y nombre limpio por cod_occim.

        EXTRAS (fuente="Occim interno" | "Servicio"): items propios de
        Occim que no están en la lista Copec — se marcan con esa etiqueta
        para poder filtrarlos aparte y que los contadores del bloque
        COPEC (317 Vigentes / 52 Inactivos / 34 Próximos) coincidan
        exactamente con el archivo oficial.
        """
        import re as _re

        _copec_path = _DATA_DIR / "lista_precios_copec.xlsx"
        _occim_path = _DATA_DIR / "lista_precios_occim.xlsx"

        def _extraer_cod_occim(desc):
            if desc is None or (isinstance(desc, float) and pd.isna(desc)):
                return None
            m = _re.search(r"((?:REP|INS)-\d{3,4})", str(desc).upper())
            return m.group(1) if m else None

        def _limpiar_desc(desc, cod=None):
            s = str(desc or "").strip()
            if cod and s.upper().startswith(cod):
                s = s[len(cod):].strip()
            s = _re.sub(r"^\{\s*(?:REP|INS)-\d+\s*\}\s*", "", s).strip()
            return s

        # ── COPEC: 3 bloques horizontales → filas atómicas ───────
        copec_raw = pd.read_excel(_copec_path, sheet_name="Hoja1", header=1)

        def _bloque(df, ix, estado):
            b = df.iloc[:, ix].copy()
            b.columns = ["cod_copec", "desc_raw", "cod_occim_raw",
                         "vigente_bloque", "precio_copec"]
            b = b.dropna(how="all")
            b["cod_occim"] = b.apply(
                lambda r: (str(r["cod_occim_raw"]).strip()
                           if pd.notna(r["cod_occim_raw"])
                           else _extraer_cod_occim(r["desc_raw"])), axis=1)
            b["desc_copec"] = b.apply(
                lambda r: _limpiar_desc(r["desc_raw"], r["cod_occim"]), axis=1)
            b["cod_copec"] = b["cod_copec"].apply(
                lambda v: (str(v).strip().rstrip(".0")
                           if pd.notna(v) else None))
            b["precio_copec"] = pd.to_numeric(
                b["precio_copec"], errors="coerce").round(0)
            b["estado"] = estado
            return b[["cod_occim", "cod_copec", "desc_copec",
                      "precio_copec", "estado"]].dropna(subset=["cod_occim"])

        df_copec = pd.concat([
            _bloque(copec_raw, [0, 1, 2, 3, 4],       "Vigente"),
            _bloque(copec_raw, [7, 8, 9, 10, 11],     "Inactivo"),
            _bloque(copec_raw, [14, 15, 16, 17, 18],  "Próximo"),
        ], ignore_index=True)
        df_copec["fuente"] = "Copec"

        # ── OCCIM: precios y descripciones ────────────────────────
        occ = pd.read_excel(_occim_path, sheet_name="REPUESTOS", header=0)
        occ = occ.rename(columns={
            "Código": "cod_occim", "Nombre": "nombre",
            "Precio 1S 2026": "precio_1s", "Precio 2S 2026": "precio_2s",
            "%Variación": "variacion", "VIGENCIA": "vigencia_comercial",
        })[["cod_occim", "nombre", "precio_1s", "precio_2s",
             "variacion", "vigencia_comercial"]]
        occ["cod_occim"] = occ["cod_occim"].astype(str).str.strip().str.upper()
        for _c in ("precio_1s", "precio_2s", "variacion"):
            occ[_c] = pd.to_numeric(occ[_c], errors="coerce")

        serv = pd.read_excel(_occim_path, sheet_name="SERVICIOS", header=0)
        serv = serv.rename(columns={
            "CÓDIGO": "cod_occim", "ITEM": "nombre",
            "PRECIO 1S 2026": "precio_1s", "PRECIO 2S 2026": "precio_2s",
        })[["cod_occim", "nombre", "precio_1s", "precio_2s"]].dropna(
            subset=["cod_occim"])
        serv["cod_occim"] = serv["cod_occim"].astype(str).str.strip().str.upper()
        for _c in ("precio_1s", "precio_2s"):
            serv[_c] = pd.to_numeric(serv[_c], errors="coerce")
        serv["variacion"] = (
            (serv["precio_2s"] - serv["precio_1s"])
            / serv["precio_1s"].replace(0, pd.NA)
        )

        # Lookup OCCIM (una fila por cod_occim para enriquecer COPEC)
        occ_lookup = occ.set_index("cod_occim")
        occ_lookup = occ_lookup[~occ_lookup.index.duplicated()]

        # Enriquecer cada fila COPEC con info de OCCIM
        def _get(cod, col, default=None):
            if cod in occ_lookup.index:
                v = occ_lookup.at[cod, col]
                if pd.notna(v):
                    return v
            return default
        df_copec["nombre"]    = df_copec["cod_occim"].map(
            lambda c: _get(c, "nombre", None)) \
            .fillna(df_copec["desc_copec"])
        df_copec["precio_1s"] = df_copec["cod_occim"].map(
            lambda c: _get(c, "precio_1s", None))
        df_copec["precio_2s"] = df_copec["cod_occim"].map(
            lambda c: _get(c, "precio_2s", None))
        # Si no hay precio 2S de OCCIM, uso el que trae el archivo COPEC
        df_copec["precio_2s"] = df_copec["precio_2s"].fillna(df_copec["precio_copec"])
        df_copec["variacion"] = df_copec["cod_occim"].map(
            lambda c: _get(c, "variacion", None))

        # ── Items propios de Occim (no aparecen en COPEC) ──────────
        cods_en_copec = set(df_copec["cod_occim"])
        occ_extra = occ[~occ["cod_occim"].isin(cods_en_copec)].copy()
        occ_extra["cod_copec"] = None
        occ_extra["fuente"] = "Occim interno"

        def _estado_extra(row):
            if str(row.get("vigencia_comercial", "")).strip().lower() in ("no", "n"):
                return "Inactivo"
            return "Vigente"
        occ_extra["estado"] = occ_extra.apply(_estado_extra, axis=1)

        # ── Servicios Occim ────────────────────────────────────────
        serv["cod_copec"] = None
        serv["estado"] = "Vigente"
        serv["fuente"] = "Servicio"

        # Concatenar
        cols = ["cod_occim", "cod_copec", "nombre",
                "precio_1s", "precio_2s", "variacion", "estado", "fuente"]
        cons = pd.concat([
            df_copec[cols], occ_extra[cols], serv[cols],
        ], ignore_index=True, sort=False)
        return cons

    try:
        _rep = _cargar_catalogo_repuestos()
    except FileNotFoundError as _e:
        st.error(f"No se encontraron los archivos de precios en `data/`: {_e}")
        st.stop()

    # ── Contadores del bloque COPEC (los que aparecen en el archivo) ─
    _copec = _rep[_rep["fuente"] == "Copec"]
    _n_vig_c = int((_copec["estado"] == "Vigente").sum())
    _n_ina_c = int((_copec["estado"] == "Inactivo").sum())
    _n_pro_c = int((_copec["estado"] == "Próximo").sum())
    _n_copec = len(_copec)
    _n_occ_int = int((_rep["fuente"] == "Occim interno").sum())
    _n_serv    = int((_rep["fuente"] == "Servicio").sum())

    _rk1, _rk2, _rk3, _rk4, _rk5 = st.columns(5)
    _rk1.metric("📦 Copec (total)", f"{_n_copec}",
                help="Suma de Vigentes + Inactivos + Próximos según el archivo COPEC")
    _rk2.metric("✅ Vigentes Copec", f"{_n_vig_c}",
                help="Bloque 'Vigentes' del archivo COPEC")
    _rk3.metric("🟡 Próximos Copec", f"{_n_pro_c}",
                help="Bloque 'Próximos a agregar' del archivo COPEC")
    _rk4.metric("⛔ Inactivos Copec", f"{_n_ina_c}",
                help="Bloque 'Dados de baja' del archivo COPEC")
    _rk5.metric("➕ Occim + Servicios", f"{_n_occ_int + _n_serv}",
                help=f"{_n_occ_int} repuestos internos Occim (no en Copec) + "
                     f"{_n_serv} servicios. Aparecen al marcarlos en el filtro Fuente.")

    st.markdown('<div class="section-hdr">Filtros</div>',
                unsafe_allow_html=True)
    _rf1, _rf2, _rf3 = st.columns([1.3, 1.3, 2.4])
    with _rf1:
        # Vigentes por defecto; Inactivos/Próximos solo si el usuario los marca
        _estados_sel = st.multiselect(
            "Estado", ["Vigente", "Próximo", "Inactivo"],
            default=["Vigente"],
            help="Vigentes por defecto. Marca Próximo/Inactivo para verlos.")
    with _rf2:
        # Por defecto solo COPEC para que los conteos coincidan con el archivo
        _fuentes_sel = st.multiselect(
            "Fuente", ["Copec", "Occim interno", "Servicio"],
            default=["Copec"],
            help="Copec = items que Copec compra (con código Copec). "
                 "Occim interno = repuestos propios de Occim no listados en Copec. "
                 "Servicio = servicios/mano de obra de Occim.")
    with _rf3:
        _rep_buscar = st.text_input(
            "🔍 Buscar por código Occim / Copec / descripción",
            placeholder="Ej: REP-0087, 504033, boquilla, mangueras, etc.",
            key="rep_buscar")

    _view = _rep.copy()
    if _estados_sel:
        _view = _view[_view["estado"].isin(_estados_sel)]
    if _fuentes_sel:
        _view = _view[_view["fuente"].isin(_fuentes_sel)]
    if _rep_buscar:
        q = _rep_buscar.strip().upper()
        _m = (
            _view["cod_occim"].fillna("").str.upper().str.contains(q, na=False, regex=False)
            | _view["cod_copec"].fillna("").str.upper().str.contains(q, na=False, regex=False)
            | _view["nombre"].fillna("").str.upper().str.contains(q, na=False, regex=False)
        )
        _view = _view[_m]

    # Orden: Copec primero (Vigente→Próximo→Inactivo), luego Occim interno, luego Servicio
    _orden_estado = {"Vigente": 0, "Próximo": 1, "Inactivo": 2}
    _orden_fuente = {"Copec": 0, "Occim interno": 1, "Servicio": 2}
    _view = _view.assign(
        _o_f=_view["fuente"].map(_orden_fuente).fillna(9),
        _o_e=_view["estado"].map(_orden_estado).fillna(9),
    ).sort_values(["_o_f", "_o_e", "cod_occim"]).drop(columns=["_o_f", "_o_e"]).reset_index(drop=True)

    _n_tot_rep = len(_rep)
    st.caption(
        f"Mostrando **{len(_view):,}** de {_n_tot_rep} items del catálogo. "
        f"Bloque Copec: {_n_copec} · Occim interno: {_n_occ_int} · Servicios: {_n_serv}."
    )

    # Emojis para el estado (visual)
    _est_emoji = {"Vigente": "✅ Vigente",
                  "Próximo": "🟡 Próximo",
                  "Inactivo": "⛔ Inactivo"}
    _fte_emoji = {"Copec": "🏪 Copec", "Occim interno": "🏭 Occim int.",
                  "Servicio": "🛠 Servicio"}
    _tbl = _view.rename(columns={
        "cod_occim": "Código Occim",
        "cod_copec": "Código Copec",
        "nombre": "Descripción",
        "precio_1s": "Precio 1S 2026",
        "precio_2s": "Precio 2S 2026",
        "variacion": "% Variación",
        "estado": "Estado",
        "fuente": "Fuente",
    }).copy()
    _tbl["Estado"] = _tbl["Estado"].map(lambda x: _est_emoji.get(x, x))
    _tbl["Fuente"] = _tbl["Fuente"].map(lambda x: _fte_emoji.get(x, x))
    _tbl["Código Copec"] = _tbl["Código Copec"].fillna("—")

    st.dataframe(
        _tbl,
        hide_index=True, use_container_width=True, height=560,
        column_config={
            "Código Occim":   st.column_config.TextColumn(width=110),
            "Código Copec":   st.column_config.TextColumn(width=110,
                help="Código interno de Copec para cruzar con su sistema"),
            "Descripción":    st.column_config.TextColumn(width=340),
            "Precio 1S 2026": st.column_config.NumberColumn(
                width=120, format="$%d",
                help="Precio primer semestre 2026 (base para %variación)"),
            "Precio 2S 2026": st.column_config.NumberColumn(
                width=120, format="$%d",
                help="Precio vigente segundo semestre 2026"),
            "% Variación":    st.column_config.NumberColumn(
                width=100, format="%.2f%%",
                help="Variación 1S → 2S 2026 (fracción, se multiplica por 100)"),
            "Estado":         st.column_config.TextColumn(width=110),
            "Fuente":         st.column_config.TextColumn(width=120,
                help="🏪 Copec = catálogo Copec · 🏭 Occim int. = repuesto propio Occim · 🛠 Servicio"),
        },
    )

    # Descarga CSV
    _csv_rep = _view.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ Descargar CSV del filtro",
        _csv_rep,
        file_name=f"repuestos_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )

    st.caption(
        "Consolidado desde las dos listas oficiales: "
        "**Lista de Precios Vigente 2S 2026** (Occim) + "
        "**Lista de Precios COPEC — ENLACE** (con código interno de Copec y estados). "
        "Actualiza reemplazando los archivos en `correctivas_mirror/data/`."
    )


# ══════════════════════════════════════════════════════════════════════
# Vista: Enlace Copec (pool de avisos del portal portalenlace.copec.cl)
# ══════════════════════════════════════════════════════════════════════
if vista == "🔗 Enlace Copec":

    # Mapeo de estados de la API → etiqueta UI (Copec agrupa varios en 1)
    # y color/emoji para el semáforo.
    ESTADO_META = {
        "ASIGNADO_RESOLUTOR":    ("🔵", "Asignado"),
        "ASIGNADO_EMPRESA":      ("🔵", "Asignado"),
        "ASIGNADO_EN_CAMINO":    ("🟡", "En camino"),
        "EN_PROGRESO_EN_CURSO":  ("🟠", "En Progreso"),
        "EN_PROGRESO_EN_CIERRE": ("🔴", "Pendiente cierre"),
        "CERRADO":               ("✅", "Cerrado"),
    }

    @st.cache_data(ttl=120, show_spinner="Cargando avisos Enlace...")
    def cargar_enlace_avisos() -> pd.DataFrame:
        # Paginar (Supabase cap 1000/req). Bajamos activos + últimos 90 días cerrados
        # para no traer todo el histórico.
        all_rows = []
        for offset in range(0, 5000, 1000):
            rows = _sb_get("enlace_avisos", {
                "select": ("id_sap,numero_orden,tipo_aviso,tipo_atencion_mantenimiento,"
                           "estado,prioridad,descripcion_falla,descripcion,"
                           "descripcion_equipo,descripcion_instalacion,eds_codigo,"
                           "nombre_contacto,telefono_contacto,sla,"
                           "fecha_creacion,fecha_ultimo_cambio,sync_at"),
                "order": "fecha_creacion.desc",
                "limit": "1000",
                "offset": str(offset),
            })
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < 1000:
                break
        return pd.DataFrame(all_rows)

    @st.cache_data(ttl=3600)
    def cargar_estaciones_comuna() -> dict:
        """Devuelve {eds_occim: comuna_capitalizada} para etiquetar preventivos."""
        try:
            rows = _sb_get("estaciones_servicio", {"select": "eds_occim,comuna", "limit": "2000"})
        except Exception:
            return {}
        out = {}
        for r in rows:
            c = (r.get("comuna") or "").strip()
            if c and len(c) > 1 and r.get("eds_occim"):
                out[str(r["eds_occim"])] = c.title()
        return out

    @st.cache_data(ttl=120, show_spinner="Cruzando correctivos con Fracttal...")
    def cargar_match_fracttal_correctivo(n_avisos: list[str]) -> dict:
        """Correctivos: intenta primero llamados_correctivos.os_fracttal
        (funciona solo para avisos activos que aún no cerraron). Devuelve
        {n_aviso: os_fracttal} solo para los que sí tienen match ahí."""
        if not n_avisos:
            return {}
        by_aviso = {}
        for i in range(0, len(n_avisos), 200):
            chunk = n_avisos[i:i+200]
            rows = _sb_get("llamados_correctivos", {
                "select": "n_aviso,os_fracttal",
                "n_aviso": f"in.({','.join(chunk)})",
                "limit": "500",
            })
            for r in rows:
                if r.get("n_aviso") and r.get("os_fracttal"):
                    by_aviso[str(r["n_aviso"])] = r["os_fracttal"]
        return by_aviso

    @st.cache_data(ttl=300)
    def cargar_ots_detalle(ids: tuple[str, ...]) -> dict:
        """Trae detalle de OTs Fracttal (responsable, fechas, estado, etc.)
        indexado por id_ot para el panel de detalle expandido."""
        if not ids:
            return {}
        out = {}
        ids_list = [i for i in ids if i]
        for i in range(0, len(ids_list), 200):
            chunk = ids_list[i:i+200]
            rows = _sb_get("ordenes_trabajo", {
                "select": ("id_ot,responsable,estado,estado_tarea,prioridad_calc,"
                           "modalidad_atencion,tipo_falla,causa_raiz,"
                           "fecha_inicio,fecha_finalizacion,tiempo_ejecucion,"
                           "duracion_real_seg,duracion_real_neta_seg,"
                           "comentario_tecnico,tipo_tarea"),
                "id_ot": f"in.({','.join(chunk)})",
                "limit": "500",
            })
            for r in rows:
                out[r["id_ot"]] = r
        return out

    @st.cache_data(ttl=300, show_spinner="Cargando OTs correctivas Fracttal...")
    def cargar_ots_correctivas_por_eds() -> dict:
        """OTs correctivas de Fracttal indexadas por EDS para match por
        (EDS + fecha_creacion ± 3 días) cuando llamados_correctivos.os_fracttal
        está NULL (ocurre en TODOS los correctivos ya cerrados)."""
        all_rows = []
        for off in range(0, 10000, 1000):
            rows = _sb_get("ordenes_trabajo", {
                "select": "id_ot,codigo_eds,fecha_creacion",
                "tipo_tarea": "eq.CORRECTIVA",
                "fecha_creacion": "gte.2026-01-01",
                "order": "fecha_creacion.desc",
                "limit": "1000", "offset": str(off),
            })
            if not rows: break
            all_rows.extend(rows)
            if len(rows) < 1000: break
        by_eds: dict[str, list] = {}
        for r in all_rows:
            eds = r.get("codigo_eds"); fc = r.get("fecha_creacion")
            if not eds or not fc: continue
            by_eds.setdefault(str(eds), []).append((fc[:10], r["id_ot"]))
        return by_eds

    @st.cache_data(ttl=300, show_spinner="Cruzando preventivos con Fracttal...")
    def cargar_ots_preventivas_por_eds() -> dict:
        """Preventivos: (EDS + fecha_creacion OT >= fecha_creacion aviso).
        Devuelve {eds: [(fecha_creacion_ot, id_ot), ...]}. La OT SIEMPRE
        se crea después del aviso Enlace — nunca antes.

        Incluye tipos: PREVENTIVA*, ENTREGA DE INSUMOS Y/O REPUESTOS,
        INSPECCIÓN, GARANTIA. Excluye CORRECTIVA."""
        tipos_exactos = ("ENTREGA DE INSUMOS Y/O REPUESTOS", "INSPECCIÓN",
                         "GARANTIA (sin cobro)", "PREVENTIVA CUATRIMESTRAL")
        all_rows = []
        for off in range(0, 10000, 1000):
            rows = _sb_get("ordenes_trabajo", {
                "select": "id_ot,codigo_eds,fecha_creacion,tipo_tarea",
                "tipo_tarea": "like.PREVENTIVA*",
                "fecha_creacion": "gte.2026-01-01",
                "order": "fecha_creacion.desc",
                "limit": "1000", "offset": str(off),
            })
            if not rows: break
            all_rows.extend(rows)
            if len(rows) < 1000: break
        for tt in tipos_exactos:
            for off in range(0, 5000, 1000):
                rows = _sb_get("ordenes_trabajo", {
                    "select": "id_ot,codigo_eds,fecha_creacion,tipo_tarea",
                    "tipo_tarea": f"eq.{tt}",
                    "fecha_creacion": "gte.2026-01-01",
                    "order": "fecha_creacion.desc",
                    "limit": "1000", "offset": str(off),
                })
                if not rows: break
                all_rows.extend(rows)
                if len(rows) < 1000: break
        by_eds: dict[str, list] = {}
        for r in all_rows:
            eds = r.get("codigo_eds"); fc = r.get("fecha_creacion")
            if not eds or not fc:
                continue
            by_eds.setdefault(str(eds), []).append((fc[:10], r["id_ot"]))
        return by_eds

    @st.cache_data(ttl=60)
    def cargar_enlace_auth_status() -> dict:
        try:
            rows = _sb_get("enlace_auth", {"select": "updated_at,expires_at,last_error", "id": "eq.1"})
            return rows[0] if rows else {}
        except Exception:
            return {}

    # ── Header con estado del sync ──────────────────────────────────
    auth = cargar_enlace_auth_status()
    c1, c2, c3 = st.columns([3, 1, 1])
    with c1:
        st.markdown("### Portal Enlace Copec — Pool de avisos")
        st.caption(
            "Espejo del panel de avisos de https://portalenlace.copec.cl. "
            "Sincroniza cada 15 minutos vía API oficial de Copec."
        )
    with c2:
        if auth.get("last_error"):
            st.error(f"⚠️ Sync falló: {auth['last_error'][:80]}")
            st.caption("Corre `bootstrap_enlace_auth.py` con un token nuevo.")
        elif auth.get("updated_at"):
            try:
                ts = pd.to_datetime(auth["updated_at"]).tz_convert(_CL_TZ)
                _mins = (pd.Timestamp.now(tz=_CL_TZ) - ts).total_seconds() / 60
                _label = ts.strftime("%d/%m %H:%M")
                # GitHub Actions cron no garantiza precisión al minuto: puede
                # variar 15-45 min. Sólo alertamos con delay > 90 min (6x el
                # intervalo esperado de 15 min).
                if _mins > 90:
                    st.metric("Última sync", _label, delta=f"⚠️ hace {int(_mins)} min",
                              delta_color="inverse")
                    st.caption("Cron muy demorado. Revisa GitHub Actions.")
                else:
                    st.metric("Última sync", _label, delta=f"hace {int(_mins)} min",
                              delta_color="off")
            except Exception:
                st.caption(f"Última sync: {auth.get('updated_at')}")
    with c3:
        _do_sync_enlace = st.button("🔄 Actualizar ahora",
                                    use_container_width=True, key="btn_sync_enlace",
                                    help="Fuerza sync manual con la API Copec (~15s).")

    if _do_sync_enlace:
        import sys as _sys
        _need = ("SUPABASE_URL", "SUPABASE_KEY")
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
            st.error(f"Faltan credenciales: {', '.join(_missing)}")
        else:
            _root = str(Path(__file__).parent.parent)
            if _root not in _sys.path:
                _sys.path.insert(0, _root)
            try:
                with st.spinner("Sincronizando con API Copec Enlace…"):
                    import importlib
                    import sync_enlace as _sync_e
                    importlib.reload(_sync_e)
                    _sync_e.main()
                st.cache_data.clear()
                st.success("✅ Datos actualizados desde Enlace Copec.")
                st.rerun()
            except (Exception, SystemExit) as _e:
                st.error(f"No se pudo sincronizar: {_e}")

    df = cargar_enlace_avisos()
    if df.empty:
        st.info(
            "Aún no hay avisos sincronizados. Ejecuta `sync_enlace.py` "
            "(o espera a que corra el workflow de GitHub Actions)."
        )
        st.stop()

    # ── ALERTA CRÍTICA: EDS Zona Norte Copec (10xxx) + EDS 20830 ────
    # Estas EDS tienen regla operativa especial: solo se pueden cerrar
    # en Enlace el ÚLTIMO día del mes (facturación consolidada Copec).
    # Antes del último día del mes, mostramos banner rojo distintivo.
    import calendar as _cal_zn
    _hoy_zn = datetime.now()
    _ult_dia_mes_zn = _cal_zn.monthrange(_hoy_zn.year, _hoy_zn.month)[1]
    _es_ultimo_dia_zn = (_hoy_zn.day == _ult_dia_mes_zn)

    def _es_eds_zona_norte(cod):
        s = str(cod or "").strip()
        return s.startswith("10") or s == "20830"

    df["_zona_norte"] = df["eds_codigo"].map(_es_eds_zona_norte)
    _zn_activas = df[df["_zona_norte"] & (df["estado"] != "CERRADO")]

    if not _zn_activas.empty and not _es_ultimo_dia_zn:
        _n_zn = len(_zn_activas)
        _edss_zn = ", ".join(sorted(_zn_activas["eds_codigo"].dropna().unique().tolist())[:10])
        _more = "…" if _zn_activas["eds_codigo"].nunique() > 10 else ""
        _dias_faltan = _ult_dia_mes_zn - _hoy_zn.day
        st.markdown(
            f'<div style="background:#7f1d1d;color:#fff;'
            f'padding:14px 18px;border-radius:8px;margin:10px 0;'
            f'border:2px solid #dc2626;box-shadow:0 2px 8px rgba(220,38,38,0.3)">'
            f'<div style="font-size:1.05em;font-weight:700">'
            f'🚨 ATENCIÓN — {_n_zn} avisos abiertos de EDS ZONA NORTE (código 10xxx o 20830)'
            f'</div>'
            f'<div style="font-size:0.9em;margin-top:6px;line-height:1.4">'
            f'Estas EDS <b>NO deben cerrarse hoy</b>. Su cierre se hace en bloque el '
            f'<b>último día del mes</b> ({_ult_dia_mes_zn:02d}/{_hoy_zn.month:02d} — faltan {_dias_faltan} días). '
            f'Regla operativa Copec: facturación consolidada zona norte.<br>'
            f'<b>EDS afectadas:</b> {_edss_zn}{_more}'
            f'</div></div>',
            unsafe_allow_html=True,
        )

    # ── Cruce con Fracttal (2 estrategias según tipo_aviso) ─────────
    # Correctivos:
    #   1) Primero llamados_correctivos.n_aviso -> os_fracttal (para activos)
    #   2) Fallback: ordenes_trabajo por (EDS + fecha ±3 días + CORRECTIVA)
    # Preventivos: (EDS + fecha_creacion ±45 días) vs fecha_programada Fracttal
    _corr_map = cargar_match_fracttal_correctivo(
        df[df["tipo_aviso"] == "CORRECTIVO"]["id_sap"].dropna().astype(str).tolist()
    )
    _corr_by_eds = cargar_ots_correctivas_por_eds()
    _prev_by_eds = cargar_ots_preventivas_por_eds()

    from datetime import datetime as _dt
    def _best_match(cands, fecha_iso, ventana_dias):
        """Busca la OT más cercana cuya fecha_creacion sea POSTERIOR o
        IGUAL al aviso Enlace (delta >= 0). Nunca acepta OTs anteriores:
        la OT en Fracttal siempre se genera DESPUÉS del aviso Enlace.
        """
        if not cands or not fecha_iso:
            return None
        try:
            d0 = _dt.strptime(fecha_iso[:10], "%Y-%m-%d")
        except ValueError:
            return None
        best, best_d = None, 10**9
        for fp, id_ot in cands:
            try:
                delta = (_dt.strptime(fp, "%Y-%m-%d") - d0).days
            except ValueError:
                continue
            # OT debe ser POSTERIOR o igual al aviso Enlace (0 = mismo día).
            if 0 <= delta <= ventana_dias and delta < best_d:
                best, best_d = id_ot, delta
        return best

    def _resolve_os(row):
        eds = str(row.get("eds_codigo") or "")
        fc  = row.get("fecha_creacion") or ""
        if row["tipo_aviso"] == "CORRECTIVO":
            match = _corr_map.get(str(row["id_sap"]))
            if match:
                return match
            # OT correctiva se crea el mismo día o hasta 5 días después
            return _best_match(_corr_by_eds.get(eds, []), fc, ventana_dias=5)
        # Preventivos: OT hasta 45 días después del aviso Enlace
        return _best_match(_prev_by_eds.get(eds, []), fc, ventana_dias=45)

    df["os_fracttal"] = df.apply(_resolve_os, axis=1)

    # ── EDS DOBLES: 1 orden Enlace → 2 OTs Fracttal ────────────────
    # Algunas EDS son físicamente dobles (dos islas de lavado: Norte/Sur,
    # Oriente/Poniente, etc.). Enlace emite UNA sola orden preventiva que
    # junta insumos de ambas islas, pero Fracttal genera DOS OTs (una por
    # isla), casi siempre el mismo día. `_best_match` engancha solo una;
    # aquí resolvemos AMBAS para estas EDS. Solo aplica a PREVENTIVAS
    # (las correctivas son por incidente, no se dividen por isla).
    EDS_DOBLES = {"20079", "40046", "40502", "60001", "60079", "60107", "60711", "60783"}

    def _best_match_multi(cands, fecha_iso, ventana_dias, tol=3):
        """Como _best_match pero devuelve TODAS las OTs del clúster de fecha
        más cercano (delta mínimo .. delta mínimo + tol días). Para una EDS
        doble esto captura el par Norte+Sur (misma fecha) sin arrastrar OTs
        del mes anterior/siguiente."""
        if not cands or not fecha_iso:
            return []
        try:
            d0 = _dt.strptime(fecha_iso[:10], "%Y-%m-%d")
        except ValueError:
            return []
        scored = []
        for fp, id_ot in cands:
            try:
                delta = (_dt.strptime(fp, "%Y-%m-%d") - d0).days
            except ValueError:
                continue
            if 0 <= delta <= ventana_dias:
                scored.append((delta, id_ot))
        if not scored:
            return []
        scored.sort()
        best_d = scored[0][0]
        # dedup preservando orden por cercanía
        out = []
        for delta, id_ot in scored:
            if delta <= best_d + tol and id_ot not in out:
                out.append(id_ot)
        return out

    def _resolve_os_all(row):
        eds = str(row.get("eds_codigo") or "")
        if row["tipo_aviso"] == "PREVENTIVO" and eds in EDS_DOBLES:
            lst = _best_match_multi(_prev_by_eds.get(eds, []),
                                    row.get("fecha_creacion") or "", ventana_dias=45)
            if lst:
                return lst
        one = row.get("os_fracttal")
        return [one] if one else []

    df["os_fracttal_all"] = df.apply(_resolve_os_all, axis=1)
    # os_fracttal (single) queda como el primero del clúster, para que sea
    # consistente con os_fracttal_all en el resto de vistas.
    df["os_fracttal"] = df["os_fracttal_all"].map(lambda l: l[0] if l else None)

    # Detalles Fracttal (responsable, fechas, etc.) para el panel expandido
    _ots_ids = tuple(sorted({oid for lst in df["os_fracttal_all"] for oid in lst if oid}))
    _ots_detalle = cargar_ots_detalle(_ots_ids)

    # _title_smart definido a nivel de módulo (arriba, línea ~74)

    # ── Detectar pareos Plan/Repuestos (solo preventivos) ───────────
    # Copec divide cada mantención preventiva en 2 avisos separados:
    #   "Plan Mtto Preventivo..." (o "Plan Mto...", inconsistente Copec)
    #   "Repuestos Mtto Prev..." (o "Repuestos Mto...")
    # Ambos con misma EDS y mismo día. El técnico debe cerrar los DOS.
    def _clase(f: str | None) -> str | None:
        f = (f or "").upper().strip()
        # Acepta variantes "Mto" (1T) / "Mtto" (2T) — Copec no es consistente.
        # "Atención Llamado" también cuenta como PLAN (es la orden principal
        # del servicio para ciertas EDS remotas, sustituye al "Plan Mtto").
        if f.startswith(("PLAN MTTO", "PLAN MTO ",
                         "ATENCIÓN LLAMADO", "ATENCION LLAMADO")):
            return "PLAN"
        if f.startswith(("REPUESTOS MTTO", "REPUESTOS MTO ")):
            return "REPUESTOS"
        return None
    df["_clase"] = df["descripcion_falla"].map(_clase)
    _dia = pd.to_datetime(df["fecha_creacion"], errors="coerce", utc=True) \
              .dt.tz_convert(_CL_TZ).dt.strftime("%Y-%m-%d")
    df["_par_key"] = df["eds_codigo"].fillna("") + "|" + _dia.fillna("")

    # Buscar pares desbalanceados: mismo par_key con Plan y Repuestos
    # en estados distintos (uno más avanzado que el otro).
    _pares_df = df[df["_clase"].notna() & (df["tipo_aviso"] == "PREVENTIVO")].copy()
    _desbal = []
    for pk, g in _pares_df.groupby("_par_key"):
        if pk.endswith("|"):
            continue
        clases = set(g["_clase"])
        if clases != {"PLAN", "REPUESTOS"}:
            continue
        estados_grupo = set(g["estado"])
        if len(estados_grupo) <= 1:
            continue   # todos iguales → no desbalanceado
        plans = g[g["_clase"] == "PLAN"]
        reps  = g[g["_clase"] == "REPUESTOS"]
        plan = plans.iloc[0]
        # OS Fracttal (única para el par): primera no vacía del grupo
        os_fr = ""
        for _v in g["os_fracttal"]:
            if _v:
                os_fr = _v; break
        # Técnico responsable desde la OT Fracttal matcheada
        _tec = ""
        if os_fr and _ots_detalle.get(os_fr):
            _tec = _ots_detalle[os_fr].get("responsable") or ""
        _desbal.append({
            "N° OT Fracttal": os_fr or "⏳ pendiente",
            "Técnico": _tec or "⏳ sin OT",
            "EDS": plan["eds_codigo"],
            "Dirección": _title_smart(plan["descripcion_instalacion"] or ""),
            "Fecha": pk.split("|")[-1],
            "N° MP Fija": " + ".join(str(x) for x in plans["id_sap"]),
            "Estado Plan": " / ".join(sorted({
                ESTADO_META.get(e, ("⚪", e))[0] + " " + ESTADO_META.get(e, ("", e))[1]
                for e in plans["estado"]
            })),
            "N° MP Variable": " + ".join(str(x) for x in reps["id_sap"]),
            "Estado Rep": " / ".join(sorted({
                ESTADO_META.get(e, ("⚪", e))[0] + " " + ESTADO_META.get(e, ("", e))[1]
                for e in reps["estado"]
            })),
        })

    if _desbal:
        st.markdown(
            f'<div style="background:#fff7ed;border-left:4px solid #ea580c;'
            f'padding:12px 16px;border-radius:6px;margin:12px 0">'
            f'<b style="color:#9a3412">⚖️ {len(_desbal)} pares Plan/Repuestos desbalanceados</b>'
            f'<div style="color:#7c2d12;font-size:0.85em;margin-top:4px">'
            f'Copec divide cada mantención preventiva en 2 avisos (Plan + Repuestos). '
            f'Ambos deben cerrarse para que Copec pague. Los que aparecen aquí tienen '
            f'una pata más avanzada que la otra — el técnico debe completar la que quedó atrás.'
            f'</div></div>',
            unsafe_allow_html=True,
        )
        st.dataframe(pd.DataFrame(_desbal), hide_index=True, use_container_width=True,
                     height=min(280, 55 + 35 * len(_desbal)))

    # ── ALERTA: "En Progreso" sin cierre ────────────────────────────
    # EN_PROGRESO_EN_CURSO / EN_PROGRESO_EN_CIERRE que llevan >1 hora abiertas
    # son avisos donde el técnico terminó pero olvidó cerrar en Enlace.
    _ahora = pd.Timestamp.now(tz=_CL_TZ)
    _df_prog = df[df["estado"].str.startswith("EN_PROGRESO", na=False)].copy()
    _alertas_ranking = pd.DataFrame()   # para el ranking al final
    if not _df_prog.empty:
        _df_prog["_fecha_cambio"] = pd.to_datetime(_df_prog["fecha_ultimo_cambio"], errors="coerce", utc=True).dt.tz_convert(_CL_TZ)
        _df_prog["_horas_sin_cerrar"] = ((_ahora - _df_prog["_fecha_cambio"]).dt.total_seconds() / 3600).round(1)
        # Enriquecer con técnico responsable de la OT Fracttal
        _df_prog["_tecnico"] = _df_prog["os_fracttal"].map(
            lambda x: (_ots_detalle.get(x, {}) or {}).get("responsable") if x else None
        )
        _alertas = _df_prog[_df_prog["_horas_sin_cerrar"] > 1].sort_values("_horas_sin_cerrar", ascending=False)
        _alertas_ranking = _alertas   # se usa para el ranking abajo

        if not _alertas.empty:
            # Agrupar por OT Fracttal: cuando MP Fija + MP Variable comparten
            # la misma OT, colapsamos a UNA sola fila con ambos N° avisos.
            # Los avisos sin OT (⏳) se dejan como filas individuales.
            _rows_alr = []
            _con_ot = _alertas[_alertas["os_fracttal"].fillna("") != ""]
            _sin_ot = _alertas[_alertas["os_fracttal"].fillna("") == ""]

            for os_id, grp in _con_ot.groupby("os_fracttal"):
                tipo_up = str(grp.iloc[0].get("tipo_aviso") or "").upper()
                tipo_lbl = "Preventivo (MP)" if tipo_up == "PREVENTIVO" else "Correctivo"
                # Separar M. Fija y M. Variable:
                #  - Correctivo → todo va a M. Fija (Variable vacío)
                #  - Preventivo → Plan(es) en M. Fija, Repuestos(s) en M. Variable
                if tipo_up == "PREVENTIVO":
                    _plans = grp[grp["_clase"] == "PLAN"]
                    _reps  = grp[grp["_clase"] == "REPUESTOS"]
                    m_fija = " + ".join(str(x) for x in _plans["id_sap"]) if not _plans.empty else "—"
                    m_var  = " + ".join(str(x) for x in _reps["id_sap"])  if not _reps.empty  else "—"
                else:
                    m_fija = " + ".join(str(x) for x in grp["id_sap"])
                    m_var  = ""
                # falla + equipo del Plan si existe, sino del primero
                _p = grp[grp["_clase"] == "PLAN"]
                _r = (_p.iloc[0] if not _p.empty else grp.iloc[0])
                hmax = grp["_horas_sin_cerrar"].max()
                _rows_alr.append({
                    "N° OT Fracttal": os_id,
                    "Tipo": tipo_lbl,
                    "Técnico": grp.iloc[0].get("_tecnico") or "⏳ sin OT",
                    "M. Fija":     m_fija,
                    "M. Variable": m_var,
                    "EDS": _r.get("eds_codigo") or "",
                    "Falla": _title_smart(_r.get("descripcion_falla") or ""),
                    "Equipo": _title_smart(_r.get("descripcion_equipo") or ""),
                    "Estado": ESTADO_META.get(_r.get("estado"), ("", _r.get("estado") or ""))[1],
                    "_hmax": hmax,
                    "Sin cerrar": (f"🔴 {hmax:.0f}h" if hmax > 72 else
                                   f"🟠 {hmax:.0f}h" if hmax > 24 else
                                   f"🟡 {hmax:.0f}h"),
                })
            # Avisos sin OT: cada uno es su fila (van a M. Fija o Variable
            # según su clase; correctivos siempre a M. Fija).
            for _, r in _sin_ot.iterrows():
                tipo_up = str(r.get("tipo_aviso") or "").upper()
                tipo_lbl = "Preventivo (MP)" if tipo_up == "PREVENTIVO" else "Correctivo"
                _clase = r.get("_clase")
                if tipo_up == "PREVENTIVO" and _clase == "REPUESTOS":
                    m_fija, m_var = "—", str(r["id_sap"])
                elif tipo_up == "PREVENTIVO":
                    m_fija, m_var = str(r["id_sap"]), "—"
                else:
                    m_fija, m_var = str(r["id_sap"]), ""
                h = r["_horas_sin_cerrar"]
                _rows_alr.append({
                    "N° OT Fracttal": "⏳ pendiente",
                    "Tipo": tipo_lbl,
                    "Técnico": "⏳ sin OT",
                    "M. Fija":     m_fija,
                    "M. Variable": m_var,
                    "EDS": r.get("eds_codigo") or "",
                    "Falla": _title_smart(r.get("descripcion_falla") or ""),
                    "Equipo": _title_smart(r.get("descripcion_equipo") or ""),
                    "Estado": ESTADO_META.get(r.get("estado"), ("", r.get("estado") or ""))[1],
                    "_hmax": h,
                    "Sin cerrar": (f"🔴 {h:.0f}h" if h > 72 else
                                   f"🟠 {h:.0f}h" if h > 24 else
                                   f"🟡 {h:.0f}h"),
                })
            _alr_tab = pd.DataFrame(_rows_alr).sort_values("_hmax", ascending=False).drop(columns=["_hmax"])

            st.markdown(
                f'<div style="background:#fef2f2;border-left:4px solid #dc2626;'
                f'padding:12px 16px;border-radius:6px;margin:12px 0">'
                f'<b style="color:#991b1b">🚨 {len(_alr_tab)} servicios "En Progreso" sin cerrar hace más de 1h</b>'
                f'<div style="color:#7f1d1d;font-size:0.85em;margin-top:4px">'
                f'El técnico terminó la mantención pero no cerró la orden en Enlace. '
                f'Los preventivos se muestran agrupados por OT Fracttal (MP Fija + MP Variable en la misma fila).'
                f'</div></div>',
                unsafe_allow_html=True,
            )
            st.dataframe(_alr_tab, hide_index=True, use_container_width=True,
                         height=min(400, 55 + 35 * len(_alr_tab)),
                         column_config={
                             "N° OT Fracttal":  st.column_config.TextColumn(width=105),
                             "Tipo":            st.column_config.TextColumn(width=130),
                             "Técnico":         st.column_config.TextColumn(width=180),
                             "M. Fija":         st.column_config.TextColumn(width=100,
                                 help="N° aviso Copec de la MP Fija (preventivo) o del correctivo"),
                             "M. Variable":     st.column_config.TextColumn(width=100,
                                 help="N° aviso Copec de la MP Variable (repuestos). Solo aplica a preventivos"),
                             "EDS":             st.column_config.TextColumn(width=70),
                             "Falla":           st.column_config.TextColumn(width=260),
                             "Equipo":          st.column_config.TextColumn(width=140),
                             "Estado":          st.column_config.TextColumn(width=140),
                             "Sin cerrar":      st.column_config.TextColumn(width=90),
                         })

    # ── ALERTA CRÍTICA: OT Finalizada en Fracttal pero abierta en Enlace ─
    # Si Fracttal ya validó (fecha_finalizacion poblada) y Enlace sigue
    # abierto, hay un problema operativo — el técnico olvidó cerrar en
    # Enlace pese a completar todo el flujo en Fracttal.
    _abiertos_enl = df[df["estado"] != "CERRADO"].copy()
    _abiertos_enl = _abiertos_enl[_abiertos_enl["os_fracttal"].fillna("") != ""]
    _rows_desync = []
    _hoy = pd.Timestamp.now(tz=_CL_TZ)
    for _os_id, _grp in _abiertos_enl.groupby("os_fracttal"):
        _ot_info = _ots_detalle.get(_os_id) or {}
        _estado_frac = str(_ot_info.get("estado") or "").strip()
        _ff = _ot_info.get("fecha_finalizacion")
        if _estado_frac != "Finalizadas" or not _ff:
            continue
        try:
            _ff_dt = pd.to_datetime(_ff, errors="coerce", utc=True).tz_convert(_CL_TZ)
        except Exception:
            continue
        if pd.isna(_ff_dt):
            continue
        _dias = int((_hoy - _ff_dt).total_seconds() / 86400)
        # Datos del par (Plan + Repuestos si es preventivo)
        _plans = _grp[_grp["_clase"] == "PLAN"]
        _reps  = _grp[_grp["_clase"] == "REPUESTOS"]
        _r = (_plans.iloc[0] if not _plans.empty else _grp.iloc[0])
        _tipo_up = str(_r.get("tipo_aviso") or "").upper()
        _tipo_lbl = "Preventivo (MP)" if _tipo_up == "PREVENTIVO" else "Correctivo"
        if _tipo_up == "PREVENTIVO":
            _m_fija = " + ".join(str(x) for x in _plans["id_sap"]) if not _plans.empty else "—"
            _m_var  = " + ".join(str(x) for x in _reps["id_sap"])  if not _reps.empty  else "—"
        else:
            _m_fija = " + ".join(str(x) for x in _grp["id_sap"])
            _m_var  = ""
        _estados_enl = " / ".join(sorted({
            ESTADO_META.get(e, ("⚪", e))[0] + " " + ESTADO_META.get(e, ("", e))[1]
            for e in _grp["estado"]
        }))
        _rows_desync.append({
            "N° OT Fracttal": _os_id,
            "Tipo": _tipo_lbl,
            "Técnico": _ot_info.get("responsable") or "—",
            "M. Fija":     _m_fija,
            "M. Variable": _m_var,
            "EDS":         _r.get("eds_codigo") or "",
            "Dirección":   _title_smart(_r.get("descripcion_instalacion") or ""),
            "Estado Enlace":   _estados_enl,
            "Cierre Fracttal": _ff_dt.strftime("%d/%m/%Y %H:%M"),
            "_dias": _dias,
            "Días desde cierre Fracttal": (
                f"🔴 {_dias}d" if _dias > 7 else
                f"🟠 {_dias}d" if _dias > 3 else
                f"🟡 {_dias}d" if _dias > 0 else
                f"🟢 hoy"
            ),
        })

    if _rows_desync:
        _desync_df = pd.DataFrame(_rows_desync).sort_values("_dias", ascending=False).drop(columns=["_dias"])
        st.markdown(
            f'<div style="background:#fef2f2;border-left:4px solid #b91c1c;'
            f'padding:12px 16px;border-radius:6px;margin:12px 0">'
            f'<b style="color:#7f1d1d">⚠️ {len(_desync_df)} OTs Finalizadas en Fracttal '
            f'pero ABIERTAS en Enlace</b>'
            f'<div style="color:#7f1d1d;font-size:0.85em;margin-top:4px">'
            f'Fracttal ya validó administrativamente estas OTs (2ª validación completa), '
            f'pero en Enlace siguen abiertas. Cierre pendiente que urge — Copec no paga '
            f'hasta cerrar Enlace.'
            f'</div></div>',
            unsafe_allow_html=True,
        )
        st.dataframe(_desync_df, hide_index=True, use_container_width=True,
                     height=min(400, 55 + 35 * len(_desync_df)),
                     column_config={
                         "N° OT Fracttal":  st.column_config.TextColumn(width=105),
                         "Tipo":            st.column_config.TextColumn(width=130),
                         "Técnico":         st.column_config.TextColumn(width=180),
                         "M. Fija":         st.column_config.TextColumn(width=100),
                         "M. Variable":     st.column_config.TextColumn(width=100),
                         "EDS":             st.column_config.TextColumn(width=70),
                         "Dirección":       st.column_config.TextColumn(width=240),
                         "Estado Enlace":   st.column_config.TextColumn(width=200),
                         "Cierre Fracttal": st.column_config.TextColumn(width=125),
                         "Días desde cierre Fracttal": st.column_config.TextColumn(width=180,
                             help="🟢 hoy · 🟡 1-3d · 🟠 4-7d · 🔴 >7d"),
                     })

    # ── Filtros ─────────────────────────────────────────────────────
    st.markdown('<div class="section-hdr">Filtros</div>', unsafe_allow_html=True)
    f1, f2, f3, f4 = st.columns([1.2, 1.5, 1.2, 2.5])
    with f1:
        tipos = ["Todos"] + sorted(df["tipo_aviso"].dropna().unique().tolist())
        tipo_sel = st.selectbox("Tipo", tipos, key="enlace_tipo")
    with f2:
        # Agrupamos por label UI (varios estados API mapean al mismo label)
        _labels_unicos = []
        _seen = set()
        for e in df["estado"].dropna().unique():
            lbl = ESTADO_META.get(e, ("⚪", e))[1]
            if lbl not in _seen:
                _seen.add(lbl); _labels_unicos.append(lbl)
        _labels_unicos = sorted(_labels_unicos)
        estado_sel = st.multiselect(
            "Estado Enlace", _labels_unicos,
            default=[l for l in _labels_unicos if l != "Cerrado"],
            format_func=lambda x: f"{next((m[0] for e,m in ESTADO_META.items() if m[1]==x), '⚪')} {x}",
            key="enlace_estado",
        )
    with f3:
        prioridades = sorted([p for p in df["prioridad"].dropna().unique() if p])
        prio_sel = st.multiselect("Prioridad", prioridades, default=prioridades, key="enlace_prio")
    with f4:
        q = st.text_input("Buscar (aviso, EDS, dirección, equipo...)",
                          key="enlace_q", placeholder="Ej: 60066 · lavadora · fichero")

    d = df.copy()
    if tipo_sel != "Todos":
        d = d[d["tipo_aviso"] == tipo_sel]
    if estado_sel:
        estados_api = [e for e, m in ESTADO_META.items() if m[1] in estado_sel]
        d = d[d["estado"].isin(estados_api)]
    if prio_sel:
        # Los preventivos no llevan prioridad en la API (Copec la deja null),
        # así que el filtro solo aplica a correctivos. De lo contrario el
        # filtro descartaría todos los Repuestos preventivos y romperia el
        # pareo Plan+Repuestos.
        d = d[(d["tipo_aviso"] == "PREVENTIVO") | d["prioridad"].isin(prio_sel)]
    if q:
        ql = q.strip().lower()
        mask = pd.Series(False, index=d.index)
        for col in ("id_sap", "numero_orden", "eds_codigo", "descripcion_falla",
                    "descripcion", "descripcion_instalacion", "descripcion_equipo",
                    "os_fracttal"):
            if col in d.columns:
                mask |= d[col].fillna("").astype(str).str.lower().str.contains(ql, na=False)
        d = d[mask]

    # ── KPIs rápidos ────────────────────────────────────────────────
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total", len(d))
    k2.metric("Correctivos", int((d["tipo_aviso"] == "CORRECTIVO").sum()))
    k3.metric("Preventivos", int((d["tipo_aviso"] == "PREVENTIVO").sum()))
    k4.metric("En Progreso", int(d["estado"].str.startswith("EN_PROGRESO", na=False).sum()))
    k5.metric("Cerrados", int((d["estado"] == "CERRADO").sum()))

    # ── Tabla colapsada (pareos Plan+Repuestos = 1 fila) ────────────
    if d.empty:
        st.info("Ningún aviso cumple los filtros.")
    else:
        _comuna_by_eds = cargar_estaciones_comuna()

        def _label_estado_uno(est):
            emo, lbl = ESTADO_META.get(est, ("⚪", est or ""))
            return f"{emo} {lbl}"

        def _estado_fracttal_ui(ot):
            """Traduce estado + estado_tarea + fecha_finalizacion de Fracttal
            a las 3 categorías operativas Occimiano:
              🔵 En Progreso: OT creada pero aún no atendida por el técnico
                              (Fracttal la marca 'Por Iniciar' o 'En Proceso')
              🟡 En Revisión: técnico cerró (DONE) pero admin no ha validado
              ✅ Finalizada:  segunda validación OK (fecha_finalizacion poblada)
              🚫 Excepción:   ERROR DE INGRESO / DUPLICADO / CANCEL / RECHAZ
            """
            if not ot:
                return ""
            estado = (ot.get("estado") or "").strip()
            et     = (ot.get("estado_tarea") or "").strip().upper()
            ff     = ot.get("fecha_finalizacion")
            e_up   = estado.upper()
            if any(x in e_up for x in ("ERROR", "DUPLIC", "CANCEL", "RECHAZ")):
                # 'ERROR DE INGRESO' se muestra como 'Cancelada' (nomenclatura Occimiano)
                if "ERROR" in e_up or "CANCEL" in e_up:
                    return "🚫 Cancelada"
                return f"🚫 {estado.title()}"
            if estado == "Finalizadas" and ff:
                return "✅ Finalizada"
            if et == "DONE":
                return "🟡 En Revisión"
            return "🔵 En Progreso"

        def _estado_grupo(estados):
            """Estado combinado para N avisos (típicamente 1 Plan + 1..N Repuestos).
              - Todos CERRADO   → ✅ Cerrado
              - K sin cerrar    → 🟠/🔴 Cierre pendiente (K)   (rojo si K == total)
            """
            total = len(estados)
            no_cerrados = sum(1 for e in estados if e != "CERRADO")
            if no_cerrados == 0:
                return "✅ Cerrado"
            emoji = "🔴" if no_cerrados == total else "🟠"
            return f"{emoji} Cierre pendiente ({no_cerrados})"

        def _combina_ots(ot_ids):
            """Combina 1..N OTs (EDS doble) en (display, estado_ui, fecha_cierre).
            - display: 'OS-A + OS-B'
            - estado_ui: estado combinado; si difieren, muestra la mezcla
              (ej. una isla Finalizada y otra En Progreso).
            - fecha_cierre: MAX solo si TODAS están finalizadas (si una isla
              sigue abierta, el cierre Fracttal queda pendiente)."""
            ids = [x for x in ot_ids if x]
            if not ids:
                return "⏳ pendiente", "", pd.NaT
            disp = " + ".join(ids)
            estados, fechas, all_fin = [], [], True
            for oid in ids:
                ot = _ots_detalle.get(oid)
                if not ot:
                    all_fin = False
                    continue
                e = _estado_fracttal_ui(ot)
                estados.append(e)
                ff = ot.get("fecha_finalizacion")
                if ff:
                    fechas.append(pd.to_datetime(ff, errors="coerce", utc=True))
                if "Finalizada" not in e:
                    all_fin = False
            if not estados:
                estado_ui = ""
            elif len(set(estados)) == 1:
                estado_ui = estados[0]
            else:
                estado_ui = " / ".join(estados)
            fecha_cierre = max(fechas) if (fechas and all_fin) else pd.NaT
            return disp, estado_ui, fecha_cierre

        def _ots_de_grupo(g):
            """Une (dedup, orden por cercanía) todas las OTs de os_fracttal_all
            del grupo de avisos g."""
            out = []
            for lst in g["os_fracttal_all"]:
                for oid in (lst or []):
                    if oid and oid not in out:
                        out.append(oid)
            return out

        rows_view = []
        _prev = d[d["tipo_aviso"] == "PREVENTIVO"].copy()
        _corr = d[d["tipo_aviso"] != "PREVENTIVO"].copy()

        def _cierre_enlace(estados, fechas_ult):
            """Devuelve la fecha_ultimo_cambio MAX cuando TODOS están cerrados;
            si algún estado != CERRADO, devuelve NaT (no cerrado todavía)."""
            if all(e == "CERRADO" for e in estados):
                fechas = [f for f in fechas_ult if pd.notna(f)]
                return max(fechas) if fechas else pd.NaT
            return pd.NaT

        _consumed = set()
        # Pareo: N Plans + M Repuestos con misma EDS+día se agrupan en 1 fila.
        for pk, g in _prev.groupby("_par_key"):
            if pk.endswith("|"):
                continue
            plans = g[g["_clase"] == "PLAN"]
            reps  = g[g["_clase"] == "REPUESTOS"]
            if len(plans) >= 1 and len(reps) >= 1:
                plan = plans.iloc[0]
                todos_ids = list(plans["id_sap"]) + list(reps["id_sap"])
                todos_estados = list(plans["estado"]) + list(reps["estado"])
                _consumed.update(todos_ids)
                comuna = _comuna_by_eds.get(str(plan["eds_codigo"] or ""), "")
                desc = f"Plan Mtto {comuna}" if comuna else "Plan Mtto Preventivo"
                if len(reps) > 1:
                    desc += f" · ({len(reps)} equipos)"
                n_plan = " + ".join(str(x) for x in plans["id_sap"])
                n_rep  = " + ".join(str(x) for x in reps["id_sap"])
                # OS Fracttal: EDS dobles enganchan 2 OTs (Norte+Sur) → mostrar
                # ambas y combinar estado/cierre. EDS simples: una sola.
                _ot_ids = _ots_de_grupo(g)
                os_fr, estado_frac, fecha_cierre_frac = _combina_ots(_ot_ids)
                # Cierre Enlace: MAX(fecha_ultimo_cambio) si TODOS cerrados
                fechas_ult = [pd.to_datetime(g[g["id_sap"] == i].iloc[0]["fecha_ultimo_cambio"],
                                             errors="coerce", utc=True) for i in todos_ids]
                fecha_cierre_enl = _cierre_enlace(todos_estados, fechas_ult)
                rows_view.append({
                    "_key":         f"PREV|{pk}",
                    "_tipo":        "PREVENTIVO",
                    "_ids":         todos_ids,
                    "Creado":       pd.to_datetime(plan["fecha_creacion"], errors="coerce", utc=True),
                    "N° MP Fija":      n_plan,
                    "N° MP Variable":       n_rep,
                    "N° OT Fracttal":  os_fr or "⏳ pendiente",
                    "Tipo":         "Preventivo",
                    "Prioridad":    "",
                    "Estado Enlace":   _estado_grupo(todos_estados),
                    "Estado Fracttal": estado_frac,
                    "Descripción":  desc,
                    "Equipo":       _title_smart(plan.get("descripcion_equipo") or ""),
                    "EDS":          (f"🚫 {plan.get('eds_codigo')}"
                                     if _es_eds_zona_norte(plan.get("eds_codigo"))
                                     else (plan.get("eds_codigo") or "")),
                    "Dirección":    _title_smart(plan.get("descripcion_instalacion") or ""),
                    "Contacto":     _title_smart(plan.get("nombre_contacto") or ""),
                    "Últ. cambio":  pd.to_datetime(plan["fecha_ultimo_cambio"], errors="coerce", utc=True),
                    "Cierre Fracttal": fecha_cierre_frac,
                    "Cierre Enlace":   fecha_cierre_enl,
                })

        # Preventivos huérfanos (o no clasificados por prefijo raro)
        for _, r in _prev.iterrows():
            if r["id_sap"] in _consumed:
                continue
            comuna = _comuna_by_eds.get(str(r["eds_codigo"] or ""), "")
            clase = r["_clase"]  # puede ser None si el prefijo es raro (ej: "Atención Llamado")
            if clase == "PLAN":
                prefijo = "Plan Mtto"; n_plan = str(r["id_sap"]); n_rep = ""
            elif clase == "REPUESTOS":
                prefijo = "Repuestos Mtto"; n_plan = ""; n_rep = str(r["id_sap"])
            else:
                # Prefijo raro. Preservamos el original truncado y ponemos
                # el id en N° Plan por defecto (nunca dejar ambas vacías).
                falla = r.get("descripcion_falla") or "Preventivo sin clasificar"
                prefijo = falla[:40]
                n_plan = str(r["id_sap"]); n_rep = ""
            desc = f"{prefijo} {comuna} · (sin par)" if comuna else f"{prefijo} · (sin par)"
            no_cerr = 0 if r["estado"] == "CERRADO" else 1
            estado_ui = "✅ Cerrado" if no_cerr == 0 else "🟠 Cierre pendiente (1)"
            # EDS dobles: aunque el aviso Enlace venga sin par Plan/Repuestos,
            # en Fracttal igual puede tener 2 OTs (Norte+Sur) → mostrar ambas.
            _ot_ids = list(r.get("os_fracttal_all") or [])
            os_fr, estado_frac, fecha_cierre_frac = _combina_ots(_ot_ids)
            fecha_cierre_enl = pd.to_datetime(r["fecha_ultimo_cambio"], errors="coerce", utc=True) \
                if r["estado"] == "CERRADO" else pd.NaT
            rows_view.append({
                "_key":         f"SOLO|{r['id_sap']}",
                "_tipo":        "PREVENTIVO_HUERFANO",
                "_ids":         [r["id_sap"]],
                "Creado":       pd.to_datetime(r["fecha_creacion"], errors="coerce", utc=True),
                "N° MP Fija":      n_plan,
                "N° MP Variable":       n_rep,
                "N° OT Fracttal":  os_fr or "⏳ pendiente",
                "Tipo":         "Preventivo",
                "Prioridad":    "",
                "Estado Enlace":   estado_ui,
                "Estado Fracttal": estado_frac,
                "Descripción":  desc,
                "Equipo":       _title_smart(r.get("descripcion_equipo") or ""),
                "EDS":          (f"🚫 {r.get('eds_codigo')}"
                                 if _es_eds_zona_norte(r.get("eds_codigo"))
                                 else (r.get("eds_codigo") or "")),
                "Dirección":    _title_smart(r.get("descripcion_instalacion") or ""),
                "Contacto":     _title_smart(r.get("nombre_contacto") or ""),
                "Últ. cambio":  pd.to_datetime(r["fecha_ultimo_cambio"], errors="coerce", utc=True),
                "Cierre Fracttal": fecha_cierre_frac,
                "Cierre Enlace":   fecha_cierre_enl,
            })

        # Correctivos (1 fila cada uno; sí llevan prioridad; nº en col MP Fija)
        for _, r in _corr.iterrows():
            os_fr = r.get("os_fracttal") or ""
            fecha_cierre_frac = pd.NaT
            estado_frac = ""
            if os_fr and _ots_detalle.get(os_fr):
                _ot = _ots_detalle[os_fr]
                ff = _ot.get("fecha_finalizacion")
                if ff: fecha_cierre_frac = pd.to_datetime(ff, errors="coerce", utc=True)
                estado_frac = _estado_fracttal_ui(_ot)
            fecha_cierre_enl = pd.to_datetime(r["fecha_ultimo_cambio"], errors="coerce", utc=True) \
                if r["estado"] == "CERRADO" else pd.NaT
            rows_view.append({
                "_key":         f"CORR|{r['id_sap']}",
                "_tipo":        "CORRECTIVO",
                "_ids":         [r["id_sap"]],
                "Creado":       pd.to_datetime(r["fecha_creacion"], errors="coerce", utc=True),
                "N° MP Fija":      str(r["id_sap"]),
                "N° MP Variable":       "",
                "N° OT Fracttal":  os_fr or "⏳ pendiente",
                "Tipo":         "Correctivo",
                "Prioridad":    r.get("prioridad") or "",
                "Estado Enlace":   _label_estado_uno(r["estado"]),
                "Estado Fracttal": estado_frac,
                "Descripción":  _title_smart(r.get("descripcion_falla") or ""),
                "Equipo":       _title_smart(r.get("descripcion_equipo") or ""),
                "EDS":          (f"🚫 {r.get('eds_codigo')}"
                                 if _es_eds_zona_norte(r.get("eds_codigo"))
                                 else (r.get("eds_codigo") or "")),
                "Dirección":    _title_smart(r.get("descripcion_instalacion") or ""),
                "Contacto":     _title_smart(r.get("nombre_contacto") or ""),
                "Últ. cambio":  pd.to_datetime(r["fecha_ultimo_cambio"], errors="coerce", utc=True),
                "Cierre Fracttal": fecha_cierre_frac,
                "Cierre Enlace":   fecha_cierre_enl,
            })

        tab = pd.DataFrame(rows_view).sort_values("Creado", ascending=False).reset_index(drop=True)

        # Formatear fechas para display
        tab["_creado_dt"] = tab["Creado"]
        tab["Creado"]      = tab["Creado"].dt.tz_convert(_CL_TZ).dt.strftime("%d/%m %H:%M")
        tab["Últ. cambio"] = tab["Últ. cambio"].dt.tz_convert(_CL_TZ).dt.strftime("%d/%m %H:%M")
        def _fmt_dt(s):
            if pd.isna(s): return ""
            try: return s.tz_convert(_CL_TZ).strftime("%d/%m %H:%M")
            except Exception: return ""
        tab["Cierre Fracttal"] = tab["Cierre Fracttal"].map(_fmt_dt)
        tab["Cierre Enlace"]   = tab["Cierre Enlace"].map(_fmt_dt)

        cols_show = ["Creado", "N° MP Fija", "N° MP Variable", "N° OT Fracttal", "Tipo",
                     "Prioridad", "Estado Enlace", "Estado Fracttal",
                     "Descripción", "EDS", "Dirección",
                     "Últ. cambio", "Cierre Fracttal", "Cierre Enlace",
                     "Equipo", "Contacto"]

        st.markdown(
            f"**{len(tab)} registros** "
            f"<span style='color:#64748b;font-size:0.85em'>· Cliquea una fila para ver el detalle "
            f"(preventivos: Plan + Repuestos)</span>",
            unsafe_allow_html=True,
        )

        event = st.dataframe(
            tab[cols_show],
            hide_index=True, use_container_width=True, height=550,
            on_select="rerun", selection_mode="single-row",
            key="enlace_table",
            column_config={
                "Creado":      st.column_config.TextColumn(width=100),
                "N° MP Fija":     st.column_config.TextColumn(width=90),
                "N° MP Variable":      st.column_config.TextColumn(width=90),
                "N° OT Fracttal": st.column_config.TextColumn(width=165,
                    help="EDS dobles muestran 2 OTs (una por isla: Norte+Sur, Oriente+Poniente…)."),
                "Tipo":        st.column_config.TextColumn(width=100),
                "Prioridad":   st.column_config.TextColumn(width=80),
                "Estado Enlace":   st.column_config.TextColumn(width=200),
                "Estado Fracttal": st.column_config.TextColumn(width=160),
                "Descripción": st.column_config.TextColumn(width=240),
                "Equipo":      st.column_config.TextColumn(width=150),
                "EDS":         st.column_config.TextColumn(width=90,
                    help="🚫 = Zona Norte Copec (10xxx o 20830). NO cerrar en Enlace excepto último día del mes."),
                "Dirección":   st.column_config.TextColumn(width=240),
                "Contacto":    st.column_config.TextColumn(width=140),
                "Últ. cambio": st.column_config.TextColumn(width=100),
                "Cierre Fracttal": st.column_config.TextColumn(width=110),
                "Cierre Enlace":   st.column_config.TextColumn(width=110),
            },
        )

        # ── Detalle expandido ───────────────────────────────────────
        sel = getattr(event, "selection", None)
        sel_rows = sel.get("rows", []) if isinstance(sel, dict) else (getattr(sel, "rows", []) or [])
        if sel_rows:
            row = tab.iloc[sel_rows[0]]
            ids = row["_ids"]
            _detalle = d[d["id_sap"].isin(ids)].copy()

            st.markdown("---")
            st.markdown(f"### Detalle · {row['Descripción']}")

            def _fmt(ts):
                if not ts or pd.isna(ts):
                    return "—"
                try:
                    return pd.to_datetime(ts, errors='coerce', utc=True).tz_convert(_CL_TZ).strftime('%d/%m/%Y %H:%M')
                except Exception:
                    return "—"

            # Renderizamos primero los avisos Enlace (uno o más) lado a lado
            _cols = st.columns(len(_detalle) if len(_detalle) > 1 else 1)
            for i, (_, av) in enumerate(_detalle.iterrows()):
                col = _cols[i] if len(_detalle) > 1 else _cols[0]
                with col:
                    clase = av.get("_clase") or av.get("tipo_aviso")
                    _emo, _lbl = ESTADO_META.get(av["estado"], ("⚪", av["estado"]))
                    os_id = av.get("os_fracttal")
                    ot = _ots_detalle.get(os_id) if os_id else None
                    prio_ui = av.get("prioridad") if av["tipo_aviso"] == "CORRECTIVO" else "—"

                    # Renombrar clase con nomenclatura Occimiano
                    clase_label = {"PLAN": "MP Fija",
                                   "REPUESTOS": "MP Variable"}.get(clase, clase)

                    bloque_enlace = (
                        f"**{clase_label} · N° aviso {av['id_sap']}**  \n"
                        f"{_emo} {_lbl}  ·  **Prioridad:** {prio_ui or '—'}  \n"
                        f"**Falla:** {_title_smart(av.get('descripcion_falla') or '') or '—'}  \n"
                        f"**Descripción:** {_title_smart(av.get('descripcion') or '') or '—'}  \n"
                        f"**Equipo:** {_title_smart(av.get('descripcion_equipo') or '') or '—'}  \n"
                        f"**EDS:** {av.get('eds_codigo') or '—'} · "
                        f"{_title_smart(av.get('descripcion_instalacion') or '')}  \n"
                        f"**Contacto:** {_title_smart(av.get('nombre_contacto') or '') or '—'} "
                        f"{('· ' + av.get('telefono_contacto')) if av.get('telefono_contacto') else ''}  \n"
                        f"**N° orden Enlace:** `{av.get('numero_orden') or '—'}`  \n"
                        f"**Creado:** {_fmt(av.get('fecha_creacion'))}  \n"
                        f"**Últ. cambio:** {_fmt(av.get('fecha_ultimo_cambio'))}  \n"
                        f"**Cierre Enlace:** "
                        f"{_fmt(av.get('fecha_ultimo_cambio')) if av.get('estado') == 'CERRADO' else '— (no cerrado aún)'}"
                    )
                    st.markdown(bloque_enlace)

            # OT Fracttal: una sola por par (mismo os_fracttal para MP Fija + MP Variable)
            _ESTADO_TAREA_ES = {
                "DONE":        "Finalizada",
                "NO_STARTED":  "No iniciada",
                "IN_PROGRESS": "En progreso",
                "PAUSED":      "Pausada",
            }
            os_ids_unicos = sorted({oid for _, av in _detalle.iterrows()
                                    for oid in (av.get("os_fracttal_all") or [])
                                    if oid})
            st.markdown("")
            if not os_ids_unicos:
                st.markdown("**🔧 OT Fracttal:** ⏳ _no creada aún en Fracttal_")
            else:
                for os_id in os_ids_unicos:
                    ot = _ots_detalle.get(os_id)
                    if not ot:
                        st.markdown(f"**🔧 OT Fracttal:** `{os_id}` _(sin detalle disponible)_")
                        continue
                    # Solo mostramos campos con valor (evita filas '—' innecesarias
                    # en preventivas donde modalidad/tipo_falla/causa_raiz son null).
                    lineas = [f"**🔧 OT Fracttal · `{os_id}`**"]
                    et_raw = ot.get("estado_tarea") or ""
                    et_es = _ESTADO_TAREA_ES.get(et_raw, et_raw) if et_raw else "—"
                    lineas.append(f"**Estado OT:** {ot.get('estado') or '—'}  ·  "
                                  f"**Estado tarea:** {et_es}")
                    if ot.get("responsable"):
                        lineas.append(f"**Técnico:** {ot['responsable']}")
                    # Modalidad / Tipo falla / Causa raíz aplican SOLO a correctivas.
                    # En preventivas mostramos una nota unificada; en correctivas
                    # mostramos cada campo con su valor (o "—" si viene vacío).
                    tt = (ot.get("tipo_tarea") or "").upper()
                    if "PREVENTIVA" in tt:
                        lineas.append("**Modalidad / Tipo falla / Causa raíz:** _No aplica (es MP)_")
                    else:
                        lineas.append(f"**Modalidad:** {_title_smart(ot.get('modalidad_atencion') or '') or '—'}")
                        lineas.append(f"**Tipo falla:** {_title_smart(ot.get('tipo_falla') or '') or '—'}")
                        lineas.append(f"**Causa raíz:** {_title_smart(ot.get('causa_raiz') or '') or '—'}")
                    lineas.append(f"**Inicio técnico:** {_fmt(ot.get('fecha_inicio'))}")
                    lineas.append(f"**Finalización:** {_fmt(ot.get('fecha_finalizacion'))}")
                    # Tiempo de ejecución — Fracttal a veces registra "00:00" mal
                    # (técnico no puso tiempo). Estrategia:
                    #   1) delta real entre fecha_inicio y fecha_finalizacion (autoritativo)
                    #   2) tiempo_ejecucion si != 0
                    #   3) duracion_real_neta_seg / duracion_real_seg
                    def _fmt_min(mins):
                        if not mins or mins <= 0: return None
                        h, m = divmod(int(mins), 60)
                        return f"{h}h {m:02d}min" if h else f"{m} min"
                    dur_txt = None
                    fi, ff = ot.get("fecha_inicio"), ot.get("fecha_finalizacion")
                    if fi and ff:
                        try:
                            delta_m = int((pd.to_datetime(ff, utc=True) - pd.to_datetime(fi, utc=True)).total_seconds() / 60)
                            dur_txt = _fmt_min(delta_m)
                        except Exception:
                            dur_txt = None
                    if not dur_txt:
                        te = str(ot.get("tiempo_ejecucion") or "").strip()
                        if te and te != "00:00":
                            try:
                                hh, mm = te.split(":")[:2]
                                dur_txt = _fmt_min(int(hh) * 60 + int(mm))
                            except Exception:
                                dur_txt = te
                    if not dur_txt:
                        for k in ("duracion_real_neta_seg", "duracion_real_seg"):
                            try:
                                s = int(ot.get(k) or 0)
                                if s > 0:
                                    dur_txt = _fmt_min(s // 60); break
                            except (TypeError, ValueError):
                                continue
                    if dur_txt:
                        lineas.append(f"**Tiempo ejecución:** {dur_txt}")
                    if ot.get("comentario_tecnico"):
                        lineas.append(f"**Comentario técnico:** {ot['comentario_tecnico']}")
                    st.markdown("  \n".join(lineas))

    # ── Ranking técnicos con órdenes ABIERTAS (incluye todos los no cerrados) ──
    # Toma avisos != CERRADO, enriquece con técnico (de la OT Fracttal matcheada),
    # y agrupa. La tabla es clickeable → detalle de las OTs del técnico.
    # Los avisos sin OT Fracttal aún matcheada se muestran como métrica aparte.
    _rk_base = df[df["estado"] != "CERRADO"].copy()
    if not _rk_base.empty:
        _rk_base["_tecnico"] = _rk_base["os_fracttal"].map(
            lambda x: (_ots_detalle.get(x, {}) or {}).get("responsable") if x else None
        )
        _rk_base["_fecha_cambio"] = pd.to_datetime(
            _rk_base["fecha_ultimo_cambio"], errors="coerce", utc=True).dt.tz_convert(_CL_TZ)
        _now = pd.Timestamp.now(tz=_CL_TZ)
        _rk_base["_horas"] = ((_now - _rk_base["_fecha_cambio"]).dt.total_seconds() / 3600).round(0)

        # Separar avisos con técnico identificado vs sin OT en Fracttal
        _sin_ot = _rk_base[_rk_base["_tecnico"].isna()]
        _con_tec = _rk_base[_rk_base["_tecnico"].notna()]

        st.markdown("---")
        st.markdown(
            '<div class="section-hdr">🏁 Ranking técnicos con órdenes abiertas</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "Avisos que aún no están cerrados en Enlace y tienen OT en Fracttal, "
            "agrupados por técnico responsable. Cliquea una fila para ver el detalle."
        )

        # Métrica avisos sin OT (no forman parte del ranking)
        if not _sin_ot.empty:
            st.info(
                f"ℹ️ Además hay **{len(_sin_ot)} avisos abiertos sin OT en Fracttal aún** "
                f"({int((_sin_ot['tipo_aviso']=='CORRECTIVO').sum())} correctivos · "
                f"{int((_sin_ot['tipo_aviso']=='PREVENTIVO').sum())} preventivos). "
                f"Aún no se les puede asignar un técnico responsable en este ranking."
            )

        if _con_tec.empty:
            st.info("Ningún técnico tiene órdenes abiertas con OT Fracttal identificada.")
            _rk_grp = pd.DataFrame()
        else:
            _rk_grp = _con_tec.groupby("_tecnico").agg(
                avisos_abiertos=("id_sap", "count"),
                correctivos=("tipo_aviso", lambda s: (s == "CORRECTIVO").sum()),
                preventivos=("tipo_aviso", lambda s: (s == "PREVENTIVO").sum()),
                horas_max=("_horas", "max"),
            ).reset_index().sort_values("avisos_abiertos", ascending=False)
            _rk_grp["horas_max"] = _rk_grp["horas_max"].fillna(0).astype(int)
            _rk_grp = _rk_grp.rename(columns={
                "_tecnico":         "Técnico",
                "avisos_abiertos":  "Avisos abiertos",
                "correctivos":      "Correctivos",
                "preventivos":      "Preventivos",
                "horas_max":        "Máx sin cerrar (h)",
            })

        if _rk_grp.empty:
            _rk_event = None
        else:
            _rk_event = st.dataframe(
                _rk_grp, hide_index=True, use_container_width=True,
                height=min(500, 55 + 35 * len(_rk_grp)),
                on_select="rerun", selection_mode="single-row",
                key="enlace_ranking_tab",
                column_config={
                    "Técnico":            st.column_config.TextColumn(width=260),
                    "Avisos abiertos":    st.column_config.NumberColumn(width=140),
                    "Correctivos":        st.column_config.NumberColumn(width=110),
                    "Preventivos":        st.column_config.NumberColumn(width=110),
                    "Máx sin cerrar (h)": st.column_config.NumberColumn(width=150),
                },
            )

        # ── Detalle del técnico seleccionado ────────────────────────
        _rk_sel = getattr(_rk_event, "selection", None) if _rk_event else None
        _rk_sel_rows = _rk_sel.get("rows", []) if isinstance(_rk_sel, dict) else (getattr(_rk_sel, "rows", []) or [])
        if _rk_sel_rows:
            _tec_sel = _rk_grp.iloc[_rk_sel_rows[0]]["Técnico"]
            _det = _rk_base[_rk_base["_tecnico"] == _tec_sel].copy()
            _det = _det.sort_values("_horas", ascending=False)
            st.markdown(f"#### 📋 Órdenes abiertas de **{_tec_sel}** ({len(_det)})")

            _det_tab = pd.DataFrame({
                "N° OT Fracttal": _det["os_fracttal"].fillna("").map(lambda x: x if x else "⏳ pendiente"),
                "N° aviso": _det["id_sap"].astype(str),
                "Tipo": _det["tipo_aviso"].str.title(),
                "EDS": _det["eds_codigo"].fillna(""),
                "Dirección": _det["descripcion_instalacion"].fillna("").map(_title_smart),
                "Descripción": _det["descripcion_falla"].fillna("").map(_title_smart),
                "Estado Enlace": _det["estado"].map(
                    lambda x: f"{ESTADO_META.get(x, ('⚪', x))[0]} {ESTADO_META.get(x, ('', x))[1]}"
                ),
                "Creado": pd.to_datetime(_det["fecha_creacion"], errors="coerce", utc=True) \
                    .dt.tz_convert(_CL_TZ).dt.strftime("%d/%m/%Y %H:%M"),
                "Últ. cambio": _det["_fecha_cambio"].dt.strftime("%d/%m/%Y %H:%M"),
                "Sin cerrar": _det["_horas"].map(
                    lambda h: (f"🔴 {int(h)}h" if h > 72 else
                               f"🟠 {int(h)}h" if h > 24 else
                               f"🟡 {int(h)}h" if h > 1 else
                               f"🟢 <1h")
                ),
            })
            st.dataframe(
                _det_tab, hide_index=True, use_container_width=True,
                height=min(500, 55 + 35 * len(_det_tab)),
                column_config={
                    "N° OT Fracttal": st.column_config.TextColumn(width=110),
                    "N° aviso":       st.column_config.TextColumn(width=100),
                    "Tipo":           st.column_config.TextColumn(width=100),
                    "EDS":            st.column_config.TextColumn(width=70),
                    "Dirección":      st.column_config.TextColumn(width=240),
                    "Descripción":    st.column_config.TextColumn(width=260),
                    "Estado Enlace":  st.column_config.TextColumn(width=170),
                    "Creado":         st.column_config.TextColumn(width=125),
                    "Últ. cambio":    st.column_config.TextColumn(width=125),
                    "Sin cerrar":     st.column_config.TextColumn(width=95),
                },
            )


# ══════════════════════════════════════════════════════════════════════
# Vista: Validación En Revisión
# ══════════════════════════════════════════════════════════════════════
if vista == "🔍 Cierre Fracttal":

    @st.cache_data(ttl=60, show_spinner="Cargando OTs en revisión...")
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

    _uc1, _uc2, _uc3 = st.columns([3, 1, 1])
    with _uc1:
        st.caption(
            f"🕒 Última sincronización con Fracttal: **{_upd_lbl}**  ·  "
            f"El sync automático corre cada 5 min. Usá los botones para "
            f"actualizar la vista o traer datos frescos desde Fracttal."
        )
    with _uc2:
        _do_refresh = st.button("🔃 Refrescar", use_container_width=True,
            help="Lee lo último desde Supabase — instantáneo (~1s). "
                 "Muestra los datos del sync automático más reciente.")
    with _uc3:
        _do_sync = st.button("🔄 Sync Fracttal", use_container_width=True,
            help="Trae en vivo las OTs desde Fracttal (~2 min). Sólo úsalo si "
                 "necesitas datos ULTRA frescos que aún no capturó el cron.")

    if _do_refresh:
        st.cache_data.clear()
        st.rerun()

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
        # ── Ventana de 24h desde revisión (regla Jesús, 2026-08-28) ───────
        # Una OT no debería cerrarse hasta que pasen ≥24h desde que pasó a
        # revisión. Las que aún no cumplen se muestran en AZUL ("faltan Xh"),
        # orientativo: no bloquea, se pueden seleccionar y cerrar igual. Se
        # calcula en vivo (depende de la hora actual), no en el sync.
        _MIN_HORAS_CIERRE = 24
        _now_utc = pd.Timestamp.now(tz="UTC")
        _anchor = _dfr["review_date"] if "review_date" in _dfr.columns else None
        if _anchor is not None and "final_date" in _dfr.columns:
            _anchor = _anchor.fillna(_dfr["final_date"])
        _rev_dt = pd.to_datetime(_anchor, errors="coerce", utc=True) \
            if _anchor is not None else pd.Series(pd.NaT, index=_dfr.index)
        _dfr["_horas_rev"] = (_now_utc - _rev_dt).dt.total_seconds() / 3600.0
        _dfr["_azul"] = (_dfr["color_semaforo"] == "VERDE") & \
                        _dfr["_horas_rev"].notna() & (_dfr["_horas_rev"] < _MIN_HORAS_CIERRE)

        # KPIs arriba
        _n_total = len(_dfr)
        _n_verde = int((_dfr["color_semaforo"] == "VERDE").sum())
        _n_azul  = int(_dfr["_azul"].sum())
        _n_verde_ready = _n_verde - _n_azul   # verdes que YA cumplen 24h
        _n_amar  = int((_dfr["color_semaforo"] == "AMARILLO").sum())
        _n_rojo  = int((_dfr["color_semaforo"] == "ROJO").sum())
        _n_15    = int((_dfr["dias_en_revision"] >= 15).sum())
        _n_old   = int((_dfr["dias_en_revision"] >= 30).sum())
        _cost_num = pd.to_numeric(_dfr["total_cost"], errors="coerce").fillna(0)
        _monto_v = _cost_num[(_dfr["color_semaforo"] == "VERDE") & (~_dfr["_azul"])].sum()
        _monto_r = _cost_num[_dfr["color_semaforo"] == "ROJO"].sum()

        _k1, _k2, _k3, _k4, _k5, _k6 = st.columns(6)
        _k1.metric("Total pendientes", f"{_n_total}")
        _k2.metric("🟢 Cerrar hoy", f"{_n_verde_ready}",
                   f"${int(_monto_v):,}".replace(",", "."),
                   help="Verdes que ya cumplieron 24h desde revisión (listas para cerrar).")
        _k3.metric("🔵 Esperan 24h", f"{_n_azul}",
                   help="Verdes del mismo día: aún no cumplen 24h desde revisión. "
                        "Orientativo — igual se pueden cerrar si es necesario.")
        _k4.metric("🔴 Devolver", f"{_n_rojo}",
                   f"${int(_monto_r):,} en riesgo".replace(",", "."),
                   delta_color="inverse",
                   help="Monto de las OTs rojas: facturación bloqueada hasta corregirlas.")
        _k5.metric("🟡 Revisar", f"{_n_amar}")
        _k6.metric("⚠️ >30 días", f"{_n_old}",
                   help="OTs con 30 días o más — priorizar cierre urgente")

        # ── SLA Shell: cada OT debe cerrarse antes del día 20 de SU ciclo ──
        # Regla operativa (Jesús, 2026-08-28): la facturación mensual Shell
        # corta el día 20. El plazo NO es un único "día 20" global: depende de
        # cuándo se generó la OT.
        #   • OT generada hasta el día 20 → vence ese mismo día 20.
        #   • OT generada DESPUÉS del 20 → pertenece al ciclo siguiente y vence
        #     el día 20 del mes próximo.
        # Ej.: una OT del 21-08 debe cerrarse antes del 20-09, NO del 20-08.
        # Por eso, pasado el día 20, las OTs nuevas NO están "vencidas".
        _shell_open = _dfr[_dfr["cliente"].astype(str).str.upper().str.contains(
            "SHELL", na=False)].copy() if "cliente" in _dfr.columns else pd.DataFrame()
        if not _shell_open.empty:
            from datetime import date as _date
            _hoy_d = datetime.now().date()

            def _deadline_shell(cd):
                """Día 20 del ciclo al que pertenece la OT según su generación."""
                dt = pd.to_datetime(cd, errors="coerce")
                if pd.isna(dt):
                    return None
                d = dt.date()
                if d.day <= 20:
                    y, m = d.year, d.month
                else:
                    m, y = d.month + 1, d.year
                    if m > 12:
                        m, y = 1, y + 1
                return _date(y, m, 20)

            _shell_open["_deadline"] = _shell_open["creation_date"].map(_deadline_shell)
            _venc = _shell_open[_shell_open["_deadline"].map(
                lambda x: x is not None and x < _hoy_d)]
            _hoy_ot = _shell_open[_shell_open["_deadline"].map(
                lambda x: x is not None and x == _hoy_d)]
            _fut = _shell_open[_shell_open["_deadline"].map(
                lambda x: x is None or x > _hoy_d)]

            def _banner(emo, bg, bd, tx, html):
                st.markdown(
                    f'<div style="background:{bg};color:{tx};'
                    f'padding:12px 16px;border-radius:6px;margin:10px 0;'
                    f'border-left:4px solid {bd}">{emo} {html}</div>',
                    unsafe_allow_html=True,
                )

            # Banner 1 — VENCIDAS / vencen HOY (urgente)
            if len(_venc) or len(_hoy_ot):
                if len(_venc):
                    _fv = ", ".join(f"{d.day:02d}/{d.month:02d}/{d.year}"
                                    for d in sorted({d for d in _venc["_deadline"] if d}))
                    _dias_max = (_hoy_d - min(d for d in _venc["_deadline"] if d)).days
                    _m = (f'<b>Shell · {len(_venc)} OT(s) VENCIDAS</b> — debieron cerrarse '
                          f'el {_fv} y siguen abiertas (hace {_dias_max} día(s)). Cerrar de inmediato.')
                    if len(_hoy_ot):
                        _m += f' Además {len(_hoy_ot)} vence(n) HOY.'
                    _banner("🚨", "#7f1d1d", "#dc2626", "#fff", _m)
                else:
                    _banner("🔴", "#7f1d1d", "#dc2626", "#fff",
                            f'<b>Shell · {len(_hoy_ot)} OT(s) vencen HOY</b> (día 20) — cerrar hoy.')

            # Banner 2 — ciclo actual (aún dentro de plazo)
            if len(_fut):
                _fechas_f = sorted({d for d in _fut["_deadline"] if d})
                _prox = _fechas_f[0] if _fechas_f else None
                if _prox:
                    _dias = (_prox - _hoy_d).days
                    _lim = f"{_prox.day:02d}/{_prox.month:02d}/{_prox.year}"
                    if _dias <= 3:
                        _emo, _bg, _bd, _tx = "🔴", "#fef2f2", "#dc2626", "#991b1b"
                    elif _dias <= 7:
                        _emo, _bg, _bd, _tx = "🟠", "#fff7ed", "#ea580c", "#9a3412"
                    else:
                        _emo, _bg, _bd, _tx = "🟡", "#fefce8", "#ca8a04", "#713f12"
                    _banner(_emo, _bg, _bd, _tx,
                            f'<b>Shell · {len(_fut)} OTs del ciclo actual</b> — deben cerrarse '
                            f'antes del {_lim} (faltan {_dias} días).')
                else:
                    _banner("🟡", "#fefce8", "#ca8a04", "#713f12",
                            f'<b>Shell · {len(_fut)} OTs pendientes de cerrar</b> '
                            f'(sin fecha de generación para calcular el plazo).')

        # ── Alerta: repuestos usados pero NO cargados (pérdida directa) ────
        # 'dice SÍ entregó repuestos pero no cargó ninguno en recursos' →
        # repuestos usados que no se pueden facturar. Es plata que se pierde.
        _mot = _dfr.get("motivo_semaforo", pd.Series([], dtype=str)).astype(str).str.lower()
        _fuga = _dfr[(_dfr["color_semaforo"] == "ROJO") & _mot.str.contains("dice 'si'", na=False)]
        if not _fuga.empty:
            _fol_fuga = ", ".join(
                f"{r['folio']} ({_title_smart(str(r.get('personnel') or ''))})"
                for _, r in _fuga.iterrows())
            st.markdown(
                f'<div style="background:#7f1d1d;color:#fff;padding:12px 16px;'
                f'border-radius:6px;margin:10px 0;border-left:4px solid #dc2626">'
                f'💸 <b>{len(_fuga)} OT(s) con repuestos usados SIN cargar</b> — el técnico '
                f'declaró que usó repuestos pero no los registró en Fracttal. No se pueden '
                f'facturar hasta cargarlos. → {_fol_fuga}.</div>',
                unsafe_allow_html=True)

        # ── Diagnóstico rápido: rojas por causa + carga por técnico ────────
        _rojo_df = _dfr[_dfr["color_semaforo"] == "ROJO"].copy()
        with st.expander(f"📊 Diagnóstico — {len(_rojo_df)} rojas por causa · carga por técnico",
                         expanded=False):
            _dcol1, _dcol2 = st.columns(2)
            with _dcol1:
                st.markdown("**🔴 Rojas por causa** _(qué perseguir)_")
                def _causa(m):
                    m = str(m or "").lower()
                    if "mantención de fondo" in m or "mantencion de fondo" in m:
                        return "Falta mantención lavadora/aspiradora"
                    if "sin recursos" in m:
                        return "Sin recursos cargados"
                    if "dice 'si'" in m:
                        return "Repuestos: dice SÍ, no cargó"
                    if "sin:" in m:
                        return "Correctiva sin falla/causa/detección"
                    if "completitud" in m:
                        return "Incompleta (otro)"
                    return "Otro"
                if _rojo_df.empty:
                    st.caption("Sin OTs rojas 🎉")
                else:
                    _cc = _rojo_df["motivo_semaforo"].map(_causa).value_counts()
                    _cc_df = _cc.rename_axis("Causa").reset_index(name="OTs")
                    st.dataframe(_cc_df, hide_index=True, use_container_width=True,
                                 height=min(240, 60 + 35 * len(_cc_df)))
            with _dcol2:
                st.markdown("**👤 Carga por técnico** _(accountability)_")
                if "personnel" in _dfr.columns:
                    _g = _dfr.assign(
                        _rojo=(_dfr["color_semaforo"] == "ROJO").astype(int)
                    ).groupby("personnel").agg(
                        Pend=("folio", "count"), Rojas=("_rojo", "sum"),
                        DíasMáx=("dias_en_revision", "max")).reset_index()
                    _g = _g[_g["personnel"].astype(str).str.strip() != ""]
                    _g["% rojo"] = (100 * _g["Rojas"] / _g["Pend"]).round(0).astype(int)
                    _g["Técnico"] = _g["personnel"].map(lambda x: _title_smart(str(x)))
                    _g = _g[["Técnico", "Pend", "Rojas", "% rojo", "DíasMáx"]].sort_values(
                        ["Rojas", "Pend"], ascending=False)
                    st.dataframe(_g, hide_index=True, use_container_width=True,
                                 height=min(320, 60 + 35 * len(_g)),
                                 column_config={
                                     "% rojo": st.column_config.NumberColumn(format="%d%%", width=70),
                                 })

        # ── Cruce con Enlace Copec (solo COPEC) ───────────────────────────
        # Une cada OT COPEC con su aviso de Enlace (por EDS + fecha) para ver
        # si el aviso sigue abierto: OT lista en Fracttal pero aviso Enlace
        # abierto = Copec aún no paga. Shell/Aramco no están en Enlace Copec.
        @st.cache_data(ttl=120)
        def _cargar_enlace_idx():
            idx = {}
            try:
                _rows = _sb_get("enlace_avisos", {
                    "select": "eds_codigo,fecha_creacion,estado,numero_orden,tipo_aviso",
                    "order": "fecha_creacion.desc", "limit": "5000"})
            except Exception:
                _rows = []
            for a in _rows:
                eds = str(a.get("eds_codigo") or "").strip()
                fc = a.get("fecha_creacion")
                if not eds or not fc:
                    continue
                idx.setdefault(eds, []).append(
                    (fc[:10], (a.get("estado") or ""), a.get("numero_orden") or "",
                     a.get("tipo_aviso") or ""))
            return idx

        _copec = _dfr[_dfr["cliente"].astype(str).str.upper().str.contains("COPEC", na=False)].copy() \
            if "cliente" in _dfr.columns else pd.DataFrame()
        _copec = _copec[_copec["eds_occim"].astype(str).str.fullmatch(r"\d+", na=False)] \
            if not _copec.empty else _copec
        if not _copec.empty:
            _eidx = _cargar_enlace_idx()
            from datetime import datetime as _dtm
            def _match_aviso(row):
                eds = str(row.get("eds_occim") or "").strip()
                cd = pd.to_datetime(row.get("creation_date"), errors="coerce")
                cands = _eidx.get(eds, [])
                if not cands or pd.isna(cd):
                    return (None, None, None)
                d0 = cd.date()
                best = None
                for fp, est, orden, tipo in cands:
                    try:
                        fd = _dtm.strptime(fp, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    delta = (d0 - fd).days  # aviso es anterior a la OT → delta>=0
                    if -1 <= delta <= 60:
                        if best is None or abs(delta) < best[0]:
                            best = (abs(delta), est, orden, fp)
                if best is None:
                    return (None, None, None)
                return (best[1], best[2], best[3])  # estado, orden, fecha
            _mm = _copec.apply(_match_aviso, axis=1, result_type="expand")
            _copec["_enl_estado"] = _mm[0]
            _copec["_enl_orden"] = _mm[1]
            _copec["_enl_fecha"] = _mm[2]
            _abiertos = _copec[_copec["_enl_estado"].map(
                lambda x: bool(x) and str(x).upper() != "CERRADO")]
            _sin_match = _copec[_copec["_enl_estado"].isna()]
            _n_ab = len(_abiertos)
            with st.expander(
                f"🔗 Cruce con Enlace Copec — {_n_ab} OT(s) con aviso Enlace aún abierto "
                f"({len(_copec)} OTs COPEC · {len(_sin_match)} sin match)", expanded=False):
                st.caption(
                    "Empareja cada OT COPEC con su aviso Enlace por EDS + fecha. "
                    "Un aviso abierto = Copec aún no cierra el servicio (no paga) aunque la "
                    "OT esté lista en Fracttal. Cerrar también en Enlace. "
                    "(Match aproximado; Shell/Aramco no aplican.)")
                _emap = {"VERDE": "🟢", "AMARILLO": "🟡", "ROJO": "🔴"}
                def _enl_ui(x):
                    if x is None or (isinstance(x, float) and pd.isna(x)):
                        return "— sin aviso"
                    return "🟢 cerrado" if str(x).upper() == "CERRADO" else f"🟠 abierto ({x})"
                _cop_show = _copec.copy()
                _cop_show["Fracttal"] = _cop_show["color_semaforo"].map(_emap).fillna("")
                _cop_show["Enlace"] = _cop_show["_enl_estado"].map(_enl_ui)
                _cop_show["Técnico"] = _cop_show["personnel"].map(lambda x: _title_smart(str(x)))
                _cop_show = _cop_show.rename(columns={
                    "folio": "N° OT", "eds_occim": "EDS", "_enl_orden": "N° orden Enlace"})
                _cols_cop = ["Fracttal", "N° OT", "EDS", "Técnico", "Enlace", "N° orden Enlace"]
                _cop_show = _cop_show.sort_values("_enl_estado", na_position="last")
                st.dataframe(_cop_show[_cols_cop], hide_index=True, use_container_width=True,
                             height=min(420, 60 + 35 * len(_cop_show)))

        # Filtros
        st.markdown('<div class="section-hdr">Filtros</div>', unsafe_allow_html=True)
        _f1, _f2, _f3, _f4, _f5 = st.columns([1.2, 1.4, 1.2, 1, 1.5])
        with _f1:
            _f_color = st.multiselect("Semáforo",
                ["VERDE", "AMARILLO", "ROJO"],
                default=["VERDE", "AMARILLO", "ROJO"],
                format_func=lambda x: x.capitalize())
        with _f2:
            _clientes_disp = sorted(c for c in _dfr["cliente"].dropna().unique() if c) \
                if "cliente" in _dfr.columns else []
            _f_cli = st.multiselect("Cliente", _clientes_disp, default=[],
                placeholder="Todos los clientes")
        with _f3:
            _tecnicos_disp = sorted(t for t in _dfr["personnel"].dropna().unique() if t)
            _f_tec = st.multiselect("Técnico", _tecnicos_disp, default=[])
        with _f4:
            _tipos_disp = sorted(t for t in _dfr["tipo"].dropna().unique() if t)
            _f_tipo = st.multiselect("Tipo", _tipos_disp, default=[])
        with _f5:
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
        if _f_cli and "cliente" in _dff.columns:
            _dff = _dff[_dff["cliente"].isin(_f_cli)]
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
        # Orden cronológico REAL por timestamp de review (más viejo primero).
        # dias_en_revision es entero (días) y dejaba empates desordenados.
        _dff = _dff.sort_values("_review_dt", ascending=True, na_position="last")
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
                _ml = motivo.lower()
                if "pendiente menor" in _ml or "pendiente sin detalle" in _ml:
                    _falta = motivo.split("pendiente menor:", 1)[1].strip() if "pendiente menor:" in motivo else ""
                    if _falta:
                        return f"Preventiva {pct}% — se puede cerrar; pendiente menor (seguimiento): {_falta}"
                    return f"Preventiva {pct}% — se puede cerrar; pendiente sin detalle (revisar en seguimiento)"
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
                if "solo falta cambio de aceite" in motivo.lower():
                    return (f"Solo falta cambio de aceite ({pct}%) — "
                            "aceptable si el equipo tuvo poco uso. Puede cerrarse "
                            "tras confirmar con el técnico")
                if "sin: tipo falla" in motivo.lower() or "sin: causa" in motivo.lower() or "sin: deteccion" in motivo.lower():
                    return f"Correctiva incompleta — {motivo}. Pedir al técnico completar. NO cerrar"
                if "cambio" in motivo.lower() or "reemplaz" in motivo.lower():
                    return f"Trabajo menciona cambio de pieza pero no hay repuesto cargado. Validar antes de cerrar"
                return f"Revisar: {motivo}. Confirmar con técnico antes de cerrar"

            if color == "ROJO":
                if "mantención de fondo" in motivo.lower() or "mantencion de fondo" in motivo.lower():
                    _det = _s(r.get("subtareas_pendientes")) or (
                        motivo.split(":", 1)[1].strip() if ":" in motivo else "")
                    return (f"Incompleta ({pct}%) — falta la mantención de fondo de la "
                            f"lavadora/aspiradora: {_det}. NO cerrar")
                if "completitud" in motivo.lower():
                    subs = _s(r.get("subtareas_pendientes"))
                    if subs:
                        return f"Incompleta ({pct}%) — falta: {subs}. NO cerrar"
                    return f"Incompleta ({pct}%) — falta terminar el trabajo en Fracttal. NO cerrar"
                if "sin recursos" in motivo.lower():
                    return "Sin recursos registrados — pedir al técnico cargar mano de obra / repuestos / servicios. NO cerrar"
                if "dice 'si'" in motivo.lower():
                    return ("Incongruencia repuestos — dice que SÍ usó repuestos pero no cargó "
                            "ninguno en recursos. Pedir al técnico que los cargue en Fracttal. NO cerrar")
                return f"Requiere corrección: {motivo}. NO cerrar"

            return motivo or "—"

        _dff = _dff.copy()
        _dff["_resolucion"] = _dff.apply(_resolucion, axis=1)
        # OTs azules (verde pero <24h desde revisión): sobrescribir resolución
        # con el aviso orientativo de "faltan Xh".
        if "_azul" in _dff.columns and _dff["_azul"].any():
            def _azul_txt(r):
                h = r.get("_horas_rev")
                falta = int(round(max(0, _MIN_HORAS_CIERRE - h))) if pd.notna(h) else _MIN_HORAS_CIERRE
                return (f"🔵 Lista, pero faltan ~{falta}h para cumplir 24h desde que pasó a "
                        f"revisión. Orientativo: puedes cerrarla igual si es necesario.")
            _dff.loc[_dff["_azul"], "_resolucion"] = _dff[_dff["_azul"]].apply(_azul_txt, axis=1)

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

        # Normalizar campos que vienen en MAYUSCULAS a title-case,
        # preservando siglas cortas (LVX, LT, EDS, N°, etc).
        def _clean(x):
            if not x or str(x).lower() in ("nan", "none"):
                return "—"
            return _title_smart(str(x))
        for _c in ("tipo", "activo", "_estacion", "personnel", "cliente",
                   "motivo_semaforo", "trabajo_realizado", "descripcion_falla",
                   "repuestos_detalle"):
            if _c in _dff.columns:
                _dff[_c] = _dff[_c].astype(str).apply(_clean)

        st.caption(f"Mostrando **{len(_dff)}** de {_n_total} OTs.")

        # Acciones
        _a1, _a2, _a3 = st.columns([1, 1, 3])
        with _a1:
            # Solo las verdes que YA cumplen 24h desde revisión (excluye azules).
            _folios_verdes = _dfr.loc[
                (_dfr["color_semaforo"] == "VERDE") & (~_dfr["_azul"]), "folio"].tolist()
            if _folios_verdes:
                st.download_button(
                    f"📋 Copiar {len(_folios_verdes)} folios verdes (TXT)",
                    "\n".join(_folios_verdes),
                    file_name=f"folios_verdes_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
                    mime="text/plain",
                    help="Folios verdes que ya cumplieron 24h desde revisión (listos para "
                         "cerrar). Las azules (<24h) no se incluyen. Copiar y pegar en Fracttal.",
                )
        with _a2:
            _csv = _dff.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "⬇️ Exportar Excel/CSV",
                _csv,
                file_name=f"ots_en_revision_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
            )
        with _a3:
            # Mini-gráfico referencial: distribución por semáforo de la vista
            # actual (verde ya-listo / azul <24h / amarillo / rojo).
            _az = _dff["_azul"] if "_azul" in _dff.columns else pd.Series(False, index=_dff.index)
            _cv = int(((_dff["color_semaforo"] == "VERDE") & (~_az)).sum())
            _cz = int(_az.sum())
            _ca = int((_dff["color_semaforo"] == "AMARILLO").sum())
            _cr = int((_dff["color_semaforo"] == "ROJO").sum())
            _segs = [("🟢", _cv, "#16a34a"), ("🔵", _cz, "#2563eb"),
                     ("🟡", _ca, "#ca8a04"), ("🔴", _cr, "#dc2626")]
            _tot_g = sum(s[1] for s in _segs) or 1
            _bar = "".join(
                f'<div style="width:{100*c/_tot_g:.1f}%;background:{col};height:100%"></div>'
                for _e, c, col in _segs if c > 0)
            _leg = " &nbsp;·&nbsp; ".join(
                f'<span style="white-space:nowrap">{e} <b>{c}</b> '
                f'<span style="color:#94a3b8">{100*c/_tot_g:.0f}%</span></span>'
                for e, c, col in _segs)
            st.markdown(
                f'<div style="font-size:0.78em;color:#64748b;margin:2px 0 3px 2px">'
                f'Distribución por semáforo · {_tot_g} OTs</div>'
                f'<div style="display:flex;height:20px;border-radius:5px;overflow:hidden;'
                f'border:1px solid #e2e8f0">{_bar}</div>'
                f'<div style="margin-top:6px;font-size:0.9em">{_leg}</div>',
                unsafe_allow_html=True)

        # ── Vista: Pendientes por cerrar (OT por OT) ──────────────────────
        # Lista TODA OT con trabajo sin terminar (completitud < 100%),
        # independiente de los filtros de arriba, para tener la foto completa
        # de qué quedó pendiente y de quién es responsable. Incluye las
        # preventivas VERDES con pendiente menor (se pueden cerrar, pero el
        # pendiente queda registrado para seguimiento).
        _pct_num = pd.to_numeric(_dfr.get("completed_pct"), errors="coerce").fillna(100)
        _pend = _dfr[_pct_num < 100].copy()
        if not _pend.empty:
            _emoji_sem = {"VERDE": "🟢", "AMARILLO": "🟡", "ROJO": "🔴"}
            _pend["_sem"] = _pend["color_semaforo"].map(_emoji_sem).fillna("")
            _pend["_falto"] = _pend["subtareas_pendientes"].apply(
                lambda s: _s(s) if _s(s) else "Sin detalle (revisar en Fracttal)")
            _pend["_tec"] = _pend["personnel"].apply(lambda x: _title_smart(_s(x)) or "—")
            _pend["_act"] = _pend.apply(
                lambda r: _limpiar_activo(r.get("activo"), r.get("parent_desc")), axis=1)
            _pend["_cli"] = _pend["cliente"].apply(lambda x: _title_smart(_s(x)) or "—")
            _pend = _pend.sort_values(["color_semaforo", "completed_pct"],
                                      ascending=[False, True])
            _pend_show = _pend[["_sem", "folio", "_tec", "_cli", "eds_occim",
                                "_act", "completed_pct", "_falto"]].rename(columns={
                "_sem": " ", "folio": "N° OT", "_tec": "Técnico responsable",
                "_cli": "Cliente", "eds_occim": "Cód. EDS", "_act": "Activo",
                "completed_pct": "%", "_falto": "Qué faltó por realizar",
            })
            _n_rojo_pend = int((_pend["color_semaforo"] == "ROJO").sum())
            with st.expander(
                f"🔧 Pendientes por cerrar — {len(_pend)} OTs con trabajo sin terminar "
                f"({_n_rojo_pend} 🔴 no cerrar)", expanded=False):
                st.caption(
                    "OTs con completitud < 100%. "
                    "🔴 = NO cerrar (falta mantención de fondo de lavadora/aspiradora, "
                    "recursos o es correctiva incompleta) · "
                    "🟢 = se puede cerrar; el pendiente es menor y queda en seguimiento.")
                st.dataframe(
                    _pend_show, hide_index=True, use_container_width=True,
                    height=min(460, 60 + 35 * len(_pend_show)),
                    column_config={
                        " ": st.column_config.TextColumn(width=36),
                        "N° OT": st.column_config.TextColumn(width=90),
                        "Técnico responsable": st.column_config.TextColumn(width=170),
                        "Cliente": st.column_config.TextColumn(width=110),
                        "Cód. EDS": st.column_config.TextColumn(width=80),
                        "Activo": st.column_config.TextColumn(width=200),
                        "%": st.column_config.NumberColumn(width=55, format="%d%%"),
                        "Qué faltó por realizar": st.column_config.TextColumn(width=380),
                    },
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
            # "Resolución" ocupa el lugar de la antigua columna "Motivo"
            # (motivo_semaforo) — se eliminó por redundante.
            "_resolucion":        "Resolución",
            "trabajo_realizado":  "Trabajo realizado (técnico)",
            "entrega_repuestos":  "¿Entregó rep.?",
            "repuestos_detalle":  "Repuestos usados",
            "descripcion_falla":  "Descripción falla",
        }
        _cols_out = [c for c in _COL_MAP if c in _dff.columns]
        _tbl = _dff[_cols_out].rename(columns=_COL_MAP).copy()

        # Emoji en semaforo (solo icono) + emoji en entrega repuestos.
        # Verde con <24h desde revisión → 🔵 (azul, orientativo).
        _emoji = {"VERDE": "🟢", "AMARILLO": "🟡", "ROJO": "🔴"}
        if "_azul" in _dff.columns:
            _tbl["Semáforo"] = [
                "🔵" if az else _emoji.get(c, c or "")
                for c, az in zip(_dff["color_semaforo"].values, _dff["_azul"].values)]
        else:
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
                help="Selecciona las OTs verdes 🟢 (que ya cumplen 24h). Las azules 🔵 "
                     "(<24h desde revisión) quedan fuera; márcalas a mano si es necesario.")
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
                    help="🟢 listo para cerrar · 🔵 verde pero <24h desde revisión "
                         "(orientativo, se puede cerrar igual) · 🟡 revisar · 🔴 no cerrar"),
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
        _sel_all = _edited.loc[_edited["Cerrar"] == True].copy()
        _sel_all[_folio_col] = _sel_all[_folio_col].fillna("")
        _sel_all = _sel_all[_sel_all[_folio_col] != ""]

        # RESTRICCIÓN operativa: EDS de zona norte Copec (código 10xxx) y
        # la EDS 20830 solo pueden cerrarse el ÚLTIMO día del mes.
        # Regla del negocio: cierre mensual en bloque para estas estaciones
        # (facturación consolidada).
        import calendar as _cal_r
        _hoy = datetime.now()
        _ult_dia_mes = _cal_r.monthrange(_hoy.year, _hoy.month)[1]
        _es_ultimo_dia = (_hoy.day == _ult_dia_mes)

        def _es_eds_restringida(cod):
            s = str(cod or "").strip()
            if not s: return False
            return s.startswith("10") or s == "20830"

        # Buscar la columna de EDS en la tabla (puede ser "Cód. EDS" o "eds_occim")
        _eds_col = None
        for _c in ("Cód. EDS", "eds_occim", "Cod. EDS"):
            if _c in _sel_all.columns:
                _eds_col = _c; break

        _folios_seleccionados = []
        _folios_bloqueados = []
        if _eds_col:
            _mask_bloq = _sel_all[_eds_col].map(_es_eds_restringida)
            if not _es_ultimo_dia:
                _folios_bloqueados = _sel_all.loc[_mask_bloq, _folio_col].tolist()
                _folios_seleccionados = _sel_all.loc[~_mask_bloq, _folio_col].tolist()
            else:
                _folios_seleccionados = _sel_all[_folio_col].tolist()
        else:
            _folios_seleccionados = _sel_all[_folio_col].tolist()

        if _folios_bloqueados:
            st.warning(
                f"🚫 **{len(_folios_bloqueados)} OT(s) BLOQUEADAS para cerrar** "
                f"(EDS zona norte 10xxx o EDS 20830 — solo se cierran el último "
                f"día del mes). Excluidas del batch: "
                f"{', '.join(_folios_bloqueados[:10])}"
                f"{'…' if len(_folios_bloqueados) > 10 else ''}"
            )

        if _folios_seleccionados:
            st.success(
                f"**{len(_folios_seleccionados)} OT(s) seleccionadas para cerrar:** "
                f"{', '.join(_folios_seleccionados[:5])}"
                f"{'...' if len(_folios_seleccionados) > 5 else ''}"
            )

            # Generar contenido del .bat
            # Chrome corre con ventana visible pero con flags anti-throttling
            # (--disable-background-timer-throttling etc.) que le permiten al
            # bot correr a velocidad completa aunque minimices la ventana o
            # trabajes en otras apps. NO uses --headless: Fracttal es una SPA
            # que renderiza mal en modo sin GUI y falla el selector del buscador.
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
                    f"descargado · Chrome se abre solo, hace login y cierra las "
                    f"**{len(_folios_seleccionados)}** OTs. "
                    f"**Podés minimizar la ventana y seguir trabajando** — el bot "
                    f"corre igual gracias a los flags anti-throttling. Solo NO "
                    f"cierres la ventana de Chrome mientras trabaja."
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

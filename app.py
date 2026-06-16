import re
import os
import json
import base64
import unicodedata
from pathlib import Path
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime

from auth import init_cookie_manager, is_authenticated, try_login, logout

from api import (
    load_work_orders, load_third_parties,
    load_work_orders_subtasks, load_wo_resources,
    load_meters, load_meters_reading,
    load_work_requests, load_items_catalog,
)
from data import (
    build_work_orders_df, build_third_parties_df, station_summary, CLIENT_COLORS,
    build_kpi_llenado_df, score_llenado_por_ot, score_llenado_por_tecnico,
    build_reincidencias,
    GRUPOS_TERRENO, get_grupo_tecnico, TECNICOS_NO_APLICA,
    build_meters_fichas_df, enrich_fichas_with_readings,
)
from gdrive import (
    load_listado_eds, load_all_llamados, load_llamados_fracttal, kpis_por_eds,
    load_llamados_copec, load_llamados_esmax, load_llamados_shell,
    resolve_llamados_eds_codes,
    load_utilizacion_tiempo, list_utilizacion_sheets,
    load_mtto_realizados_planilla,
    load_base_tecnicos, build_tech_name_maps,
    CATEGORY_COLORS_UTIL, classify_task_line,
    SLA_HOURS, SLA_DEFAULT, TECNICOS_OCCIMIANO_FULL, TECH_NAME_MAP,
)

# ── SUPABASE — nueva capa de datos (reemplaza Fracttal API y Excel) ───────────
from supabase_client import (
    load_work_orders_supabase,
    load_listado_eds_supabase,
    load_tecnicos_supabase,
    load_equipos_supabase,
    load_preventivas_supabase,
    load_all_llamados_supabase,
    load_sla_umbrales_supabase,
    load_cotalker_index_supabase,
    load_ots_en_vivo_supabase,
)
_USE_SUPABASE = True   # ← cambiar a False para volver a Fracttal/Excel

# ── Caché en disco para build_kpi_llenado_df (≈9s sin caché) ────────────────
@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def _cached_build_kpi_llenado(raw_wo: list) -> pd.DataFrame:
    """Wrapper con caché persistente en disco. Sobrevive reinicios de Streamlit."""
    return build_kpi_llenado_df(raw_wo)

# ── Session-state cache helper ───────────────────────────────────────────────
def _sc(key: str, sig: str, builder):
    """
    Guarda en st.session_state[key] el resultado de builder().
    Solo recalcula cuando `sig` cambia (nueva carga de datos).
    Evita recomputar DataFrames pesados en cada rerun por filtros.
    """
    if st.session_state.get(f"_sc_sig_{key}") != sig or key not in st.session_state:
        st.session_state[key]              = builder()
        st.session_state[f"_sc_sig_{key}"] = sig
    return st.session_state[key]


# ── Station name helper ───────────────────────────────────────────────────────
def _get_addr_numbers(text: str) -> set:
    """Extract 3-or-more-digit numbers from an address string."""
    return set(re.findall(r'\b\d{3,}\b', str(text).upper()))


@st.cache_data(show_spinner=False)
def build_station_name_map(df_tp_: pd.DataFrame, df_eds_: pd.DataFrame) -> dict:
    """
    Returns {eds_occim: station_name} by matching Fracttal third-parties
    to the EDS listado via street number in the address field.
    Only produces a mapping when a single candidate is found (safe matches only).
    """
    if df_tp_.empty or df_eds_.empty:
        return {}

    # Index EDS by address numbers for O(1) lookup
    eds_by_num: dict = {}
    for _, row in df_eds_.iterrows():
        for n in _get_addr_numbers(row.get("direccion", "")):
            eds_by_num.setdefault(n, []).append(row)

    result: dict = {}
    for _, tp in df_tp_.iterrows():
        tp_name = str(tp.get("name", "")).strip()
        if not tp_name:
            continue
        tp_nums = _get_addr_numbers(str(tp.get("address", "") or ""))
        if not tp_nums:
            continue

        # Collect EDS candidates that share a street number
        candidates: list = []
        for n in tp_nums:
            for c in eds_by_num.get(n, []):
                if str(c["eds_occim"]) not in {str(x["eds_occim"]) for x in candidates}:
                    candidates.append(c)

        # Narrow by client prefix (COPEC, SHELL, ESMAX …)
        prefix = tp_name.split()[0].upper()
        filtered = [c for c in candidates
                    if str(c.get("cliente", "")).upper().startswith(prefix[:4])]
        final = filtered if filtered else candidates

        if len(final) == 1:
            result[str(final[0]["eds_occim"])] = tp_name

    return result


# ── Wallpaper / Theme ─────────────────────────────────────────────────────────
_APP_DIR = os.path.dirname(os.path.abspath(__file__))

@st.cache_resource(show_spinner=False)
def _load_wallpaper_b64(theme: str) -> str:
    """Carga wallpaper_{theme}.jpg/.png como data-URI base64 (cacheado)."""
    for ext in ("jpg", "jpeg", "png"):
        path = os.path.join(_APP_DIR, f"wallpaper_{theme}.{ext}")
        if os.path.exists(path):
            with open(path, "rb") as fh:
                raw = fh.read()
            mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
            return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    return ""


@st.cache_resource(show_spinner=False)
def _load_logo_b64() -> str:
    """Carga logo.png/.jpg como data-URI base64 (cacheado). PNG tiene prioridad."""
    for name in ("logo_dashboard.jpg", "logo_occim.jpg", "logo_occim.png", "logo.png", "logo.jpg", "logo.jpeg"):
        path = os.path.join(_APP_DIR, name)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                raw = fh.read()
            mime = "image/jpeg" if name.endswith((".jpg", ".jpeg")) else "image/png"
            return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    return ""


def _inject_theme(theme: str) -> None:
    """
    Inyecta CSS completo de tema oscuro/claro con efecto marca de agua.
    El wallpaper aparece muy difuminado (8-12 % de opacidad) bajo el color
    de fondo del tema, exactamente como el cambio de tema en Google Gemini.
    """
    uri = _load_wallpaper_b64(theme)
    wp_layer  = f'url("{uri}")' if uri else "none"

    # ── Paleta según tema ─────────────────────────────────────────────────────
    if theme == "dark":
        bg_rgba       = "10, 18, 38,  0.93"   # overlay 93 % → wallpaper al 7 %
        app_bg_solid  = "#0a1226"
        card_bg       = "#111f38"
        card_border   = "#1e3356"
        text_color    = "#e2e8f0"
        text_muted    = "#94a3b8"
        sidebar_bg    = "#0d1427"
        input_bg      = "#111f38"
        input_border  = "#1e3356"
        tab_bg        = "#111f38"
        tab_color     = "#94a3b8"
        tab_active_c  = "#60a5fa"
        tab_active_bd = "#60a5fa"
        divider_c     = "#1e3356"
        section_color = "#cbd5e1"
        btn_bg        = "#1e3356"
        btn_hover     = "#2d4a73"
        btn_color     = "#e2e8f0"
        metric_bg     = "#111f38"
        toggle_bg     = "rgba(10,18,38,0.75)"
        toggle_active = "rgba(255,255,255,0.18)"
        toggle_icon   = "☀️"           # ícono para pasar a Light
        toggle_href   = "?theme=light"
    else:  # light
        bg_rgba       = "248, 250, 252, 0.88"  # overlay 88 % → wallpaper al 12 %
        app_bg_solid  = "#f8fafc"
        card_bg       = "#ffffff"
        card_border   = "#e2e8f0"
        text_color    = "#1e293b"
        text_muted    = "#64748b"
        sidebar_bg    = "#0d1427"
        input_bg      = "#ffffff"
        input_border  = "#cbd5e1"
        tab_bg        = "#f1f5f9"
        tab_color     = "#64748b"
        tab_active_c  = "#3b82f6"
        tab_active_bd = "#3b82f6"
        divider_c     = "#e2e8f0"
        section_color = "#1e293b"
        btn_bg        = "#f1f5f9"
        btn_hover     = "#e2e8f0"
        btn_color     = "#1e293b"
        metric_bg     = "#ffffff"
        toggle_bg     = "rgba(15,32,68,0.72)"
        toggle_active = "rgba(255,255,255,0.22)"
        toggle_icon   = "🌙"           # ícono para pasar a Dark
        toggle_href   = "?theme=dark"

    st.markdown(
        f"""
        <style>
        /* ══════════════════════════════════════════════════════
           FONDO — color sólido en html/body como base firme
           ══════════════════════════════════════════════════════ */
        html, body {{
            background-color: {app_bg_solid} !important;
        }}

        /* Wallpaper como marca de agua sobre el color sólido */
        .stApp {{
            background-color: transparent !important;
            background-image:
                linear-gradient(rgba({bg_rgba}), rgba({bg_rgba})),
                {wp_layer} !important;
            background-size: cover !important;
            background-position: center top !important;
            background-attachment: fixed !important;
            background-repeat: no-repeat !important;
            min-height: 100vh !important;
        }}

        /* ══════════════════════════════════════════════════════
           CONTENIDO PRINCIPAL
           ══════════════════════════════════════════════════════ */
        [data-testid="stAppViewContainer"] {{
            background-color: transparent !important;
            background-image: none !important;
        }}
        [data-testid="stAppViewContainer"] > section.main {{
            background-color: transparent !important;
            background-image: none !important;
        }}
        [data-testid="stAppViewContainer"] > section.main > div.block-container {{
            background-color: transparent !important;
            background-image: none !important;
            padding-top: 0.5rem !important;
        }}
        /* Selector adicional para Streamlit ≥1.32 */
        [data-testid="stMainBlockContainer"] {{
            padding-top: 0.5rem !important;
        }}
        .main .block-container, div.block-container {{
            padding-top: 0.5rem !important;
        }}

        /* Texto general */
        .stApp, .stApp p, .stApp div, .stApp span,
        .stApp li, .stApp h1, .stApp h2, .stApp h3 {{
            color: {text_color} !important;
        }}
        .stApp .stCaption, .stApp small {{
            color: {text_muted} !important;
        }}

        /* Dividers */
        hr {{ border-color: {divider_c} !important; }}

        /* Section headers del dashboard */
        .section-header {{
            color: {section_color} !important;
            border-bottom-color: {divider_c} !important;
        }}

        /* ── Métricas ────────────────────────────────────────── */
        [data-testid="metric-container"] {{
            background: {metric_bg} !important;
            border: 1px solid {card_border} !important;
            border-radius: 8px !important;
            padding: 14px !important;
        }}
        [data-testid="stMetricValue"] {{ color: {text_color} !important; }}
        [data-testid="stMetricLabel"] {{ color: {text_muted} !important; }}

        /* ── Tabs — colores de tema (tamaño y hover en CSS estático) ─── */
        [data-baseweb="tab-list"] {{
            background: {tab_bg} !important;
            border-radius: 10px 10px 0 0 !important;
        }}
        /* Color base del texto según tema */
        [data-baseweb="tab"] {{
            color: {tab_color} !important;
            background: transparent !important;
        }}
        /* Tab inactivo hover — color de texto según tema */
        [data-testid="stTabs"] button[data-baseweb="tab"]:not([aria-selected="true"]):hover {{
            color: {tab_active_c} !important;
        }}
        /* Tab activo — color y borde según tema */
        [data-baseweb="tab"][aria-selected="true"] {{
            color: {tab_active_c} !important;
            border-bottom: 3px solid {tab_active_bd} !important;
        }}

        /* ── Selectboxes / inputs ─────────────────────────────── */
        [data-baseweb="select"] > div,
        [data-baseweb="input"] > div,
        [data-testid="stTextInput"] > div > div {{
            background: {input_bg} !important;
            border-color: {input_border} !important;
            color: {text_color} !important;
        }}
        [data-baseweb="select"] * {{ color: {text_color} !important; }}

        /* ── Botones ──────────────────────────────────────────── */
        .stButton > button {{
            background: {btn_bg} !important;
            color: {btn_color} !important;
            border: 1px solid {card_border} !important;
        }}
        .stButton > button:hover {{
            background: {btn_hover} !important;
            border-color: {tab_active_bd} !important;
        }}

        /* ── Expanders ────────────────────────────────────────── */
        [data-testid="stExpander"] {{
            background: {card_bg} !important;
            border: 1px solid {card_border} !important;
        }}

        /* ── DataFrames ───────────────────────────────────────── */
        [data-testid="stDataFrame"],
        [data-testid="stDataFrame"] > div,
        [data-testid="stDataFrameResizable"],
        [data-testid="stDataFrameResizable"] > div {{
            background: {card_bg} !important;
            border-radius: 8px !important;
        }}
        /* Forzar colores de celda y encabezado dentro del grid */
        [data-testid="stDataFrame"] [role="gridcell"] {{
            background-color: {card_bg} !important;
            color: {text_color} !important;
        }}
        [data-testid="stDataFrame"] [role="columnheader"],
        [data-testid="stDataFrame"] [role="rowheader"] {{
            background-color: #0C2540 !important;
            color: #ffffff !important;
        }}
        /* Scrollbars en dark mode */
        [data-testid="stDataFrame"] ::-webkit-scrollbar-thumb {{
            background: {card_border} !important;
            border-radius: 4px !important;
        }}
        [data-testid="stDataFrame"] ::-webkit-scrollbar-track {{
            background: {card_bg} !important;
        }}

        /* ── Spinner / info boxes ─────────────────────────────── */
        [data-testid="stAlert"] {{
            background: {card_bg} !important;
            border-color: {card_border} !important;
            color: {text_color} !important;
        }}

        /* ══════════════════════════════════════════════════════
           SIDEBAR
           ══════════════════════════════════════════════════════ */
        [data-testid="stSidebar"],
        [data-testid="stSidebar"] > div,
        [data-testid="stSidebar"] > div > div,
        [data-testid="stSidebar"] section,
        [data-testid="stSidebarContent"] {{
            background: {sidebar_bg} !important;
        }}
        /* Sidebar: texto siempre blanco (anula .stApp span con misma especificidad pero regla posterior) */
        [data-testid="stSidebar"] span,
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] div,
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] li,
        [data-testid="stSidebar"] h1,
        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3 {{
            color: #f1f5f9 !important;
        }}

        /* Desktop: sidebar siempre visible */
        @media screen and (min-width: 768px) {{
            section[data-testid="stSidebar"] {{
                transform: translateX(0) !important;
                visibility: visible !important;
            }}
            [data-testid="stSidebarCollapseButton"] {{
                display: none !important;
            }}
        }}
        /* Mobile: botón hamburguesa siempre visible para abrir/cerrar */
        @media screen and (max-width: 767px) {{
            [data-testid="stSidebarCollapseButton"] {{
                display: flex !important;
            }}
        }}

        </style>
        """,
        unsafe_allow_html=True,
    )


def _inject_toggle(theme: str) -> None:
    """
    Inyecta el toggle ☀️/🌙 en el <body> del parent via iframe JS.
    st.components.v1.html sí ejecuta scripts (a diferencia de st.markdown).
    """
    if theme == "dark":
        bg_css     = "rgba(10,18,38,0.85)"
        active_css = "rgba(255,255,255,0.18)"
    else:
        bg_css     = "rgba(15,32,68,0.75)"
        active_css = "rgba(255,255,255,0.22)"

    sun_active  = "active-theme" if theme == "light" else ""
    moon_active = "active-theme" if theme == "dark"  else ""

    components.html(
        f"""
        <script>
        (function() {{
            var p = window.parent;
            if (!p || !p.document) return;

            // Remove previous toggle if re-render
            var old = p.document.getElementById('occ-theme-toggle');
            if (old) old.remove();

            // Build toggle
            var d = p.document.createElement('div');
            d.id = 'occ-theme-toggle';
            d.style.cssText =
                'position:fixed;top:8px;right:80px;z-index:2147483647;' +
                'display:flex;align-items:center;gap:2px;' +
                'background:{bg_css};border-radius:20px;padding:4px 10px;' +
                'backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);' +
                'box-shadow:0 2px 12px rgba(0,0,0,0.35);';

            function makeLink(href, emoji, title, activeClass) {{
                var a = p.document.createElement('a');
                a.href = href;
                a.title = title;
                a.textContent = emoji;
                a.style.cssText =
                    'text-decoration:none;font-size:1.2rem;padding:3px 8px;' +
                    'border-radius:14px;transition:background 0.15s;';
                if (activeClass) a.style.background = '{active_css}';
                return a;
            }}

            d.appendChild(makeLink('?theme=light', '☀️', 'Tema claro',  '{sun_active}'));
            d.appendChild(makeLink('?theme=dark',  '🌙', 'Tema oscuro', '{moon_active}'));
            p.document.body.appendChild(d);
        }})();
        </script>
        """,
        height=0,
        scrolling=False,
    )


# ── Pantalla de inicio de sesión ──────────────────────────────────────────────
def _get_logo_path() -> str:
    """Retorna la ruta al archivo de logo."""
    for name in ("logo_occim.jpg", "logo_occim.png", "logo.png", "logo.jpg", "logo.jpeg"):
        path = os.path.join(_APP_DIR, name)
        if os.path.exists(path):
            return path
    return ""


def _show_login_page() -> None:
    """Pantalla de login: card blanco centrado sobre fondo oscuro."""

    st.markdown("""
    <style>
    /* Ocultar chrome de Streamlit */
    [data-testid="stSidebar"], [data-testid="stHeader"],
    [data-testid="stToolbar"], [data-testid="stMainMenu"],
    footer, .stDeployButton { display: none !important; }

    /* Fondo oscuro navy */
    .stApp, [data-testid="stAppViewContainer"] {
        background: #0d1427 !important;
    }

    /* Centrar el contenido principal */
    [data-testid="stMain"] > div {
        padding: 6vh 1rem 4vh !important;
    }

    /* ── Card blanco ── */
    [data-testid="stForm"] {
        background: #ffffff !important;
        border-radius: 18px !important;
        border: none !important;
        padding: 2.75rem 2.5rem 2.25rem !important;
        box-shadow: 0 24px 60px rgba(0,0,0,0.45) !important;
        max-width: 420px !important;
        margin: 0 auto !important;
    }

    /* Logo centrado */
    [data-testid="stForm"] [data-testid="stImage"] {
        display: flex !important;
        justify-content: center !important;
        margin-bottom: 0.25rem !important;
    }
    [data-testid="stForm"] [data-testid="stImage"] img {
        max-height: 56px !important;
        width: auto !important;
        object-fit: contain !important;
    }

    /* Labels */
    [data-testid="stForm"] label {
        color: #374151 !important;
        font-size: 0.8rem !important;
        font-weight: 600 !important;
        letter-spacing: 0.4px !important;
        text-transform: uppercase !important;
    }

    /* Inputs */
    [data-testid="stForm"] input {
        background: #f9fafb !important;
        border: 1.5px solid #e5e7eb !important;
        color: #111827 !important;
        border-radius: 8px !important;
        font-size: 0.95rem !important;
    }
    [data-testid="stForm"] input:focus {
        border-color: #0d1427 !important;
        box-shadow: 0 0 0 3px rgba(13,26,39,0.1) !important;
    }
    [data-testid="stForm"] input::placeholder { color: #9ca3af !important; }

    /* Botón submit — navy oscuro */
    [data-testid="stForm"] button[kind="primaryFormSubmit"],
    [data-testid="stForm"] button[data-testid="baseButton-primaryFormSubmit"] {
        background: #0d1427 !important;
        border: none !important;
        color: #fff !important;
        font-weight: 600 !important;
        font-size: 0.95rem !important;
        height: 2.85rem !important;
        border-radius: 9px !important;
        letter-spacing: 0.3px !important;
        transition: background 0.2s !important;
    }
    [data-testid="stForm"] button[kind="primaryFormSubmit"]:hover {
        background: #1a3066 !important;
    }

    /* Error dentro del card */
    [data-testid="stForm"] [data-testid="stAlert"] {
        background: #fef2f2 !important;
        border: 1px solid #fecaca !important;
        border-radius: 8px !important;
        color: #991b1b !important;
    }
    </style>
    """, unsafe_allow_html=True)

    # Columna centrada
    _, _col, _ = st.columns([0.6, 1, 0.6])
    with _col:
        _logo_path = _get_logo_path()

        with st.form("occim_login"):

            # Logo centrado
            _lc1, _lc2, _lc3 = st.columns([1, 2, 1])
            with _lc2:
                if _logo_path:
                    st.image(_logo_path, use_container_width=True)
                else:
                    st.markdown(
                        '<p style="text-align:center;font-size:2rem;font-weight:900;'
                        'color:#0d1427;letter-spacing:3px;margin:0 0 0.5rem;">OCCIM</p>',
                        unsafe_allow_html=True,
                    )

            # Título
            st.markdown("""
            <div style="text-align:center;padding:1rem 0 1.5rem;">
                <p style="color:#0d1427;font-size:1.45rem;font-weight:700;
                    margin:0;line-height:1.2;">Iniciar sesión</p>
            </div>
            """, unsafe_allow_html=True)

            # Error
            if st.session_state.get("_login_failed"):
                st.error("Correo o contraseña incorrectos.", icon="⚠️")

            _login_email = st.text_input(
                "Correo electrónico",
                placeholder="nombre@occimiano.cl",
                key="_lf_email",
            )
            _login_pw = st.text_input(
                "Contraseña",
                placeholder="••••••••",
                type="password",
                key="_lf_pw",
            )
            st.markdown("<div style='height:0.5rem'></div>", unsafe_allow_html=True)
            _submitted = st.form_submit_button(
                "Ingresar  →",
                use_container_width=True,
                type="primary",
            )

        if _submitted:
            if try_login(_login_email, _login_pw):
                st.session_state.pop("_login_failed", None)
                st.rerun()
            else:
                st.session_state["_login_failed"] = True
                st.rerun()


st.set_page_config(
    page_title="Occimiano - Indicadores de Gestión Operacional",
    page_icon="🔧",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Cookie manager — debe inicializarse ANTES de cualquier chequeo de sesión ─
# init_cookie_manager() retorna False en el primer render (cookies aún cargando)
# En ese caso detenemos la ejecución y esperamos el re-run automático del componente
if not init_cookie_manager():
    st.stop()

# ── Logout via query param (?_lo=1) ──────────────────────────────────────────
if st.query_params.get("_lo") == "1":
    logout()
    try:
        del st.query_params["_lo"]
    except Exception:
        pass
    st.rerun()

# ── Autenticación — mostrar login y detener si no hay sesión ─────────────────
if not is_authenticated():
    _show_login_page()
    st.stop()

# ── Tema (dark / light) — leer desde URL query param ─────────────────────────
# Limpiar query param heredado del sistema anterior (URL ?theme=dark/light)
# ya no se usa — el tema vive solo en session_state
if "theme" in st.query_params:
    try:
        del st.query_params["theme"]
    except Exception:
        pass
_current_theme = st.session_state.get("_theme", "light")
_inject_theme(_current_theme)

# ── Indicador discreto de capacidad OTs (esquina sup. derecha) ───────────────
_wo_2026 = st.session_state.get("_wo_2026_count", 0)
if _wo_2026:
    _wo_pct = min(round(_wo_2026 / 20_000 * 100), 100)
    _wo_clr = "#22c55e" if _wo_pct < 70 else ("#f59e0b" if _wo_pct < 90 else "#ef4444")
    components.html(f"""<script>
(function(){{
    var p=window.parent; if(!p||!p.document) return;
    var old=p.document.getElementById('occ-ot-ctr'); if(old) old.remove();
    var d=p.document.createElement('div'); d.id='occ-ot-ctr';
    d.style.cssText='position:fixed;top:9px;right:200px;z-index:2147483646;'
        +'font-size:0.68rem;color:rgba(160,160,160,0.7);'
        +'font-family:monospace;pointer-events:none;letter-spacing:0.02em;';
    d.innerHTML='<span style="color:{_wo_clr};font-weight:600;">{_wo_2026:,}</span>'
        +'<span style="opacity:.55;"> / 20K OTs 2026 &nbsp;·&nbsp; {_wo_pct}%</span>';
    p.document.body.appendChild(d);
}})();
</script>""", height=0)

# ── Badge usuario + Salir inyectado en el body del parent (evita clip) ───────
_auth_email_badge = st.session_state.get("_auth_email", "")
components.html(f"""<script>
(function(){{
    var p = window.parent; if(!p||!p.document) return;
    var old = p.document.getElementById('occ-user-badge'); if(old) old.remove();
    var d = p.document.createElement('div'); d.id='occ-user-badge';
    d.style.cssText='position:fixed;top:7px;right:0.9rem;z-index:2147483640;'
        +'display:flex;align-items:center;gap:8px;'
        +'background:rgba(13,20,39,0.90);border:1px solid rgba(255,255,255,0.15);'
        +'border-radius:20px;padding:4px 6px 4px 12px;'
        +'font-family:system-ui,sans-serif;font-size:0.75rem;'
        +'color:rgba(255,255,255,0.85);white-space:nowrap;'
        +'box-shadow:0 2px 8px rgba(0,0,0,0.35);';
    var sp = p.document.createElement('span');
    sp.textContent = '👤 {_auth_email_badge}';
    var a = p.document.createElement('a');
    a.textContent='Salir'; a.href='?_lo=1';
    a.style.cssText='background:rgba(255,255,255,0.13);'
        +'border:1px solid rgba(255,255,255,0.25);border-radius:12px;'
        +'padding:2px 10px;text-decoration:none;color:#fff;'
        +'font-size:0.72rem;font-weight:500;cursor:pointer;';
    d.appendChild(sp); d.appendChild(a);
    p.document.body.appendChild(d);
}})();
</script>""", height=0)

# Colores de tema para HTML inline — evita hardcodear colores claros en dark mode
_t = {
    "card":     "#111f38"               if _current_theme == "dark" else "#f8fafc",
    "border":   "#1e3356"               if _current_theme == "dark" else "#e2e8f0",
    "text":     "#e2e8f0"               if _current_theme == "dark" else "#1e293b",
    "muted":    "#94a3b8"               if _current_theme == "dark" else "#64748b",
    # Fondos de tarjetas informativas — paleta Occimiano (#0C2540 / #01798A)
    "info_bg":  "rgba(1,121,138,0.15)"  if _current_theme == "dark" else "rgba(1,121,138,0.08)",
    "warn_bg":  "rgba(1,121,138,0.18)"  if _current_theme == "dark" else "rgba(1,121,138,0.10)",
    "err_bg":   "rgba(239,68,68,0.12)"  if _current_theme == "dark" else "#fef2f2",
    "tbl_head": "#0C2540",                                                              # navy Occimiano
    "tbl_head_text": "#ffffff",                                                          # texto blanco sobre navy
    "tbl_alt":  "rgba(1,121,138,0.10)"  if _current_theme == "dark" else "rgba(1,121,138,0.07)",  # teal suave
    "prog_bg":  "#1e3356"               if _current_theme == "dark" else "#e2e8f0",
    "orange_bg":"rgba(249,115,22,0.12)" if _current_theme == "dark" else "#fff7ed",
}


# ── Helper: st.dataframe con colores de tema ──────────────────────────────────
_st_df = st.dataframe  # alias interno — no tocar

def _show_df(df, **kwargs):
    """
    Wrapper de st.dataframe que aplica estilos oscuros cuando el tema es dark.
    Usa pandas Styler para colorear celdas/encabezados — compatible con column_config.
    """
    if _current_theme == "dark":
        try:
            _s = df.style.set_properties(**{
                "background-color": "#111f38",
                "color": "#e2e8f0",
                "border-color": "#1e3356",
            }).set_table_styles([
                {"selector": "th",
                 "props": [("background-color", "#0C2540"),
                           ("color", "#ffffff"),
                           ("font-weight", "700")]},
                {"selector": "tr:nth-child(even) td",
                 "props": [("background-color", "rgba(1,121,138,0.10)")]},
            ])
            _st_df(_s, **kwargs)
        except Exception:
            # fallback si el Styler falla (p.ej. MultiIndex)
            _st_df(df, **kwargs)
    else:
        _st_df(df, **kwargs)


# ── Helper: gráficos Plotly con tema oscuro/claro ─────────────────────────────
def _apply_plot_theme(fig) -> None:
    """Aplica colores del tema actual a un objeto Figure Plotly (in-place).
    También añade bordes oscuros a barras, pies y sunbursts para consistencia visual."""
    _fc  = _t["text"]
    _gc  = _t["border"]
    _pbg = "rgba(255,255,255,0.04)" if _current_theme == "dark" else "rgba(0,0,0,0)"
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor=_pbg,
        font=dict(color=_fc),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color=_fc)),
    )
    fig.update_xaxes(gridcolor=_gc, zerolinecolor=_gc,
                     tickfont=dict(color=_fc), title_font=dict(color=_fc))
    fig.update_yaxes(gridcolor=_gc, zerolinecolor=_gc,
                     tickfont=dict(color=_fc), title_font=dict(color=_fc))
    # Bordes finos uniformes en barras, tortas y sunbursts
    fig.update_traces(selector={"type": "bar"},
                      marker_line_color="#2d3436", marker_line_width=0.8)
    fig.update_traces(selector={"type": "pie"},
                      marker=dict(line=dict(color="#2d3436", width=1.5)))
    fig.update_traces(selector={"type": "sunburst"},
                      marker=dict(line=dict(color="#2d3436", width=0.8)))


def _plot(fig, **kw) -> None:
    """Aplica colores de tema al fondo/ejes/fuente del chart y lo renderiza."""
    _apply_plot_theme(fig)
    st.plotly_chart(fig, **kw)


def _plot_cached(cache_key: str, sig: str, builder, **kw) -> None:
    """
    Crea, aplica tema y cachea gráficos Plotly en session_state.
    `builder` (callable sin args) solo se invoca en cache miss.
    La clave incluye tema y sig → rerenders son instantáneos si nada cambió.
    """
    _k = f"_fig_{cache_key}_{_current_theme}_{sig}"
    if _k not in st.session_state:
        _fig = builder()
        _apply_plot_theme(_fig)
        st.session_state[_k] = _fig
    st.plotly_chart(st.session_state[_k], **kw)

# ── CSS ──────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* Desktop: ocultar header completo (barra Deploy) */
    @media screen and (min-width: 768px) {
        [data-testid="stHeader"] {
            display: none !important;
        }
        [data-testid="stToolbar"] {
            display: none !important;
        }
    }
    /* Mobile: mostrar header con botón ☰ nativo de Streamlit */
    @media screen and (max-width: 767px) {
        [data-testid="stHeader"] {
            display: flex !important;
            background: #0d1427 !important;
            height: 2.8rem !important;
            padding: 0 0.5rem !important;
            align-items: center !important;
            border-bottom: 1px solid rgba(255,255,255,0.12) !important;
        }
        /* Botón ☰ — ícono blanco sobre fondo oscuro */
        [data-testid="stHeader"] button {
            color: #f1f5f9 !important;
            background: rgba(255,255,255,0.08) !important;
            border-radius: 6px !important;
            padding: 6px !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            min-width: 36px !important;
            min-height: 32px !important;
        }
        [data-testid="stHeader"] button svg {
            fill: #f1f5f9 !important;
            color: #f1f5f9 !important;
            width: 20px !important;
            height: 20px !important;
        }
        [data-testid="stHeader"] button svg path {
            fill: #f1f5f9 !important;
            stroke: #f1f5f9 !important;
        }
        /* Ocultar Deploy/Share incluso en móvil */
        [data-testid="stToolbar"],
        [data-testid="stMainMenu"] {
            display: none !important;
        }
    }

    .section-header {
        font-size: 1.1rem; font-weight: 700; color: #1e293b;
        margin: 1rem 0 0.5rem; padding-bottom: 4px;
        border-bottom: 2px solid #e2e8f0;
    }
    [data-testid="stSidebar"],
    [data-testid="stSidebar"] > div,
    [data-testid="stSidebar"] > div > div,
    [data-testid="stSidebar"] section,
    [data-testid="stSidebarContent"] { background: #0d1427; }
    [data-testid="stSidebar"] * { color: #f1f5f9 !important; }
    /* Botones del sidebar — fondo y texto siempre visibles sobre azul oscuro */
    [data-testid="stSidebar"] .stButton > button {
        color: #f1f5f9 !important;
        background: rgba(255,255,255,0.10) !important;
        border: 1px solid rgba(255,255,255,0.22) !important;
        font-weight: 500 !important;
        transition: background 0.18s !important;
    }
    [data-testid="stSidebar"] .stButton > button:hover {
        background: rgba(255,255,255,0.20) !important;
        border-color: rgba(255,255,255,0.38) !important;
    }
    /* ── Sidebar: ancho controlado por _sb_open en session_state ────────────── */
    section[data-testid="stSidebar"] {
        transform: translateX(0) !important;
        visibility: visible !important;
        display: flex !important;
        overflow-x: hidden !important;
        overflow-y: auto !important;
        transition: min-width 0.28s ease, max-width 0.28s ease;
    }
    [data-testid="stSidebarCollapseButton"] { display: none !important; }
    [data-testid="stSidebarContent"] {
        overflow-x: hidden !important;
        overflow-y: auto !important;
        width: 100% !important;
        height: 100% !important;
        max-height: 100vh !important;
        padding-top: 0.5rem !important;
    }
    [data-testid="stSidebarUserContent"] {
        padding-top: 0 !important;
    }

    /* ── Navigation radio buttons styled as menu items ─────────────────────── */
    [data-testid="stSidebar"] [data-testid="stRadio"] > label { display: none; }
    [data-testid="stSidebar"] [data-testid="stRadio"] div[role="radiogroup"] {
        gap: 2px !important;
    }
    [data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] {
        border-radius: 8px !important;
        transition: background 0.15s, box-shadow 0.15s !important;
        width: 100%;
        white-space: nowrap !important;
        overflow: hidden !important;
        cursor: pointer !important;
    }
    [data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:hover {
        background: rgba(59,130,246,0.18) !important;
        box-shadow: inset 3px 0 0 #3b82f6 !important;
    }
    [data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"][aria-checked="true"] {
        background: rgba(59,130,246,0.35) !important;
        box-shadow: inset 3px 0 0 #60a5fa !important;
    }

    /* ── Botones del sidebar: base ──────────────────────────────────────────── */
    [data-testid="stSidebar"] .stButton > button {
        white-space: nowrap !important;
        overflow: hidden !important;
    }

    /* ── Logo: transición de opacidad ─────────────────────────────────────── */
    section[data-testid="stSidebar"] img {
        transition: opacity 0.25s !important;
    }
    /* Multiselect "Select all" → ocultar el inglés y simular "Seleccionar todos" */
    [data-testid="stMultiSelect"] li[role="option"]:first-child > div > span {
        visibility: hidden;
        position: relative;
    }
    [data-testid="stMultiSelect"] li[role="option"]:first-child > div > span::after {
        content: "Seleccionar todos";
        visibility: visible;
        position: absolute;
        left: 0;
    }

    /* Ocultar el iframe del toggle JS — solo sirve para ejecutar el script */
    [data-testid="stCustomComponentV1"],
    [data-testid="stCustomComponentV1"] iframe {
        display: none !important;
        height: 0 !important;
        min-height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        border: none !important;
    }

    /* ── Títulos de página — más compactos que el default de Streamlit ─────── */
    h1,
    [data-testid="stHeading"],
    [data-testid="stHeadingWithActionElements"] h1 {
        font-size: 1.9rem !important;
        font-weight: 800 !important;
        line-height: 1.2 !important;
    }

    /* ══════════════════════════════════════════════════════
       TABS — Tamaño grande + hover luminoso + activo claro
       ══════════════════════════════════════════════════════ */

    /* Contenedor de la lista de tabs */
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        gap: 4px !important;
        padding: 0 4px !important;
    }

    /* Botón base — mucho más grande */
    [data-testid="stTabs"] button[data-baseweb="tab"] {
        font-size: 1.10rem !important;
        font-weight: 700 !important;
        padding: 16px 46px !important;
        border-radius: 10px 10px 0 0 !important;
        letter-spacing: 0.03em !important;
        position: relative !important;
        /* Transición suave para TODOS los efectos */
        transition:
            background   0.20s ease,
            box-shadow   0.20s ease,
            color        0.18s ease,
            transform    0.15s ease,
            border-color 0.18s ease !important;
    }

    /* ── HOVER — iluminación al pasar el mouse (sin hacer clic) ── */
    [data-testid="stTabs"] button[data-baseweb="tab"]:not([aria-selected="true"]):hover {
        background: rgba(59, 130, 246, 0.13) !important;
        /* Glow exterior azul + borde inferior iluminado */
        box-shadow:
            0  -3px 18px  4px rgba(59, 130, 246, 0.35),
            0  -2px  0    0   rgba(99, 162, 255, 0.55) inset !important;
        color: #2563eb !important;
        transform: translateY(-3px) !important;
    }

    /* ── ACTIVO — tab seleccionado ── */
    [data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
        background: rgba(37, 99, 235, 0.12) !important;
        box-shadow:
            0  -4px 22px  6px rgba(37, 99, 235, 0.45),
            0  -3px  0    0   #3b82f6 inset !important;
        color: #1d4ed8 !important;
        border-bottom: 3px solid #3b82f6 !important;
        transform: translateY(-1px) !important;
    }

    /* Dark mode — ajuste de colores para fondo oscuro */
    .stApp[data-theme="dark"] [data-testid="stTabs"] button[data-baseweb="tab"]:not([aria-selected="true"]):hover,
    [data-theme="dark"] [data-testid="stTabs"] button[data-baseweb="tab"]:not([aria-selected="true"]):hover {
        color: #93c5fd !important;
    }
    .stApp[data-theme="dark"] [data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"],
    [data-theme="dark"] [data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
        color: #60a5fa !important;
    }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────
_NAV_PAGES = [
    "🥇  Desempeño Servicio Tecnico",
    "✅  Cumplimiento SLA",
    "🛠️  Mantenciones Preventivas",
    "⛽  Estaciones de Servicio",
    "⌛  Utilización del Tiempo",
]

# ── CSS del sidebar: colapsado / expandido (controlado por _sb_open) ─────────
_sb_open = st.session_state.get("_sb_open", True)
if _sb_open:
    st.markdown("""<style>
section[data-testid="stSidebar"] {
    min-width: 16rem !important; max-width: 16rem !important;
}
[data-testid="stSidebar"] [data-testid="stSidebarContent"],
[data-testid="stSidebar"] [data-testid="stSidebarContent"] > div,
[data-testid="stSidebar"] [data-testid="stSidebarContent"] > div > div {
    padding-left: 0.5rem !important; padding-right: 0.5rem !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] {
    padding: 10px 14px !important; font-size: 1.1rem !important;
    white-space: normal !important; overflow: visible !important;
    height: auto !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] > div:first-child {
    display: flex !important; flex-shrink: 0 !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] > div:last-child {
    max-width: none !important; overflow: visible !important;
    white-space: normal !important; word-break: break-word !important;
}
[data-testid="stSidebar"] .stButton > button {
    max-width: none !important; font-size: 1.0rem !important;
    justify-content: flex-start !important; text-align: left !important;
    padding-left: 14px !important; padding-top: 0.55rem !important;
    padding-bottom: 0.55rem !important;
}
section[data-testid="stSidebar"] img { opacity: 1 !important; }
[data-testid="stSidebar"] small,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
    opacity: 1 !important; font-size: inherit !important; line-height: inherit !important;
}
</style>""", unsafe_allow_html=True)
else:
    st.markdown("""<style>
section[data-testid="stSidebar"] {
    min-width: 6.0rem !important; max-width: 6.0rem !important;
}
[data-testid="stSidebar"] [data-testid="stSidebarContent"],
[data-testid="stSidebar"] [data-testid="stSidebarContent"] > div,
[data-testid="stSidebar"] [data-testid="stSidebarContent"] > div > div {
    padding-left: 0.1rem !important; padding-right: 0.1rem !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] > div:first-child {
    display: none !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] {
    padding: 6px 0 !important; font-size: 1.5rem !important;
    justify-content: center !important; line-height: 1.5 !important;
    height: auto !important; overflow: visible !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] > div:last-child {
    font-size: 1.5rem !important; line-height: 1.5 !important;
    max-width: 1.8em !important; overflow: hidden !important; display: block !important;
    white-space: nowrap !important;
}
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] > div:last-child * {
    font-size: 1.5rem !important; line-height: 1.5 !important;
}
[data-testid="stSidebar"] .stButton > button {
    font-size: 1.25rem !important; justify-content: center !important;
    padding: 0.35rem 0 !important; max-width: none !important;
}
section[data-testid="stSidebar"] img { opacity: 0 !important; }
[data-testid="stSidebar"] small,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
    opacity: 0 !important; font-size: 0 !important;
    line-height: 0 !important; pointer-events: none !important;
}
</style>""", unsafe_allow_html=True)

with st.sidebar:
    # Logo Occimiano (solo en modo expandido)
    if _sb_open:
        _logo_uri = _load_logo_b64()
        if _logo_uri:
            st.markdown(
                f'<div style="text-align:center;padding:10px 4px 4px;background:#0d1427;">'
                f'<img src="{_logo_uri}" style="max-width:100%;max-height:90px;'
                f'object-fit:contain;"/></div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div style="text-align:center;padding:12px 0;">'
                '<span style="font-size:1.6rem;font-weight:900;letter-spacing:2px;color:#fff;">OCCIM</span>'
                '</div>',
                unsafe_allow_html=True,
            )
        st.caption("Indicadores de Gestión Operacional")
    st.divider()

    _page = st.radio("Navegación", _NAV_PAGES, label_visibility="collapsed", key="_nav_radio")

    st.divider()
    if st.button("↺  Actualizar datos" if _sb_open else "↺", use_container_width=True):  # noqa
        # load_work_orders se limpia junto con todo lo demás.
        # Es necesario para que Calidad, Precisión y reincidencias muestren
        # los meses más recientes (junio, etc.) — sin esto, el caché de disco
        # puede tener datos viejos aunque haya OTs nuevas en Fracttal.
        # Tarda ~30-45s en recargarse, pero es necesario para datos actualizados.
        for fn in [load_work_orders,                # ← OTs Fracttal (Calidad + Precisión)
                   load_third_parties,
                   load_work_orders_subtasks, load_wo_resources,
                   load_meters, load_meters_reading, load_work_requests,
                   load_listado_eds,
                   load_llamados_fracttal,
                   load_llamados_copec,
                   load_llamados_shell,
                   load_llamados_esmax,
                   load_all_llamados,
                   load_utilizacion_tiempo, list_utilizacion_sheets,
                   load_mtto_realizados_planilla, load_base_tecnicos,
                   # Supabase (solo las que tienen @st.cache_data)
                   load_work_orders_supabase, load_listado_eds_supabase,
                   load_tecnicos_supabase, load_equipos_supabase,
                   load_sla_umbrales_supabase, load_preventivas_supabase]:
            fn.clear()
        # Limpiar TODOS los caches derivados de session_state
        # Incluye df_kpi_raw y df_ot_all_scores — crítico para que junio aparezca:
        # _wo_sig = str(len(raw_wo)) = "20000" siempre, por lo que sin esta limpieza
        # el caché nunca se invalida y no muestra los meses nuevos.
        for _k in ["_base_sig", "_df_llamados", "_df_tp", "_station_name_map",
                   "eds_fracttal_items", "df_llamados_supa",
                   "_sc_sig_df_llamados_supa"]:
            st.session_state.pop(_k, None)
        # Limpiar caches _sc (df_kpi_raw, df_ot_all_scores, df_wo_base, etc.)
        # También eds_enrich y eds_fracttal_items para que Listado de EDS se recalcule
        for _k in list(st.session_state.keys()):
            if str(_k).startswith(("_sla_proc", "_fig_", "_sc_sig_", "df_kpi",
                                   "df_ot_all", "df_ot_bono", "df_wo", "df_reinc",
                                   "eds_enrich", "eds_fracttal")):
                st.session_state.pop(_k, None)
        st.rerun()
    # ── Modo oscuro / claro ───────────────────────────────────────────────────
    _toggle_lbl_full = "☀️  Modo claro" if _current_theme == "dark" else "🌙  Modo oscuro"
    _toggle_lbl_icon = "☀️" if _current_theme == "dark" else "🌙"
    _toggle_lbl = _toggle_lbl_full if _sb_open else _toggle_lbl_icon
    if st.button(_toggle_lbl, use_container_width=True, key="theme_toggle"):
        _new_theme = "light" if _current_theme == "dark" else "dark"
        st.session_state["_theme"] = _new_theme
        import plotly.graph_objects as _pgo
        _old_pfx = f"_{_current_theme}_"
        _new_pfx = f"_{_new_theme}_"
        _figs_to_rename = {
            k: v for k, v in st.session_state.items()
            if k.startswith("_fig_") and isinstance(v, _pgo.Figure)
        }
        _nt = {
            "text":   "#e2e8f0" if _new_theme == "dark" else "#1e293b",
            "border": "#1e3356" if _new_theme == "dark" else "#e2e8f0",
            "pbg":    "rgba(255,255,255,0.04)" if _new_theme == "dark" else "rgba(0,0,0,0)",
        }
        for _fk, _fv in _figs_to_rename.items():
            _fv.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor=_nt["pbg"],
                font=dict(color=_nt["text"]),
                legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color=_nt["text"])),
            )
            _fv.update_xaxes(gridcolor=_nt["border"], zerolinecolor=_nt["border"],
                             tickfont=dict(color=_nt["text"]), title_font=dict(color=_nt["text"]))
            _fv.update_yaxes(gridcolor=_nt["border"], zerolinecolor=_nt["border"],
                             tickfont=dict(color=_nt["text"]), title_font=dict(color=_nt["text"]))
            _new_fk = _fk.replace(_old_pfx, _new_pfx, 1)
            if _new_fk != _fk:
                st.session_state.pop(_fk, None)
                st.session_state[_new_fk] = _fv
        st.rerun()

    # ── Sesión activa: usuario y cierre de sesión ─────────────────────────────
    _auth_email = st.session_state.get("_auth_email", "")
    _auth_user  = _auth_email.split("@")[0] if _auth_email else "usuario"
    if _sb_open:
        st.markdown(
            f'<div style="font-size:0.7rem;color:rgba(255,255,255,0.35);'
            f'text-align:center;padding:2px 0 4px;">👤 {_auth_user}</div>',
            unsafe_allow_html=True,
        )
    if st.button("⎋  Cerrar sesión" if _sb_open else "⎋", use_container_width=True, key="logout_btn"):
        logout()
        st.rerun()
    if _sb_open:
        st.caption(f"Cache: 30 min · disco  |  {datetime.now().strftime('%H:%M:%S')}")

    if _sb_open:
        st.divider()
        # ── Auditor Dash 1.0: última revisión de calidad de datos ────────────────
        _alerta_json = Path(__file__).parent / "alertas_resultado.json"
        try:
            if _alerta_json.exists():
                _ar = json.loads(_alerta_json.read_text(encoding="utf-8"))
                _ar_estado   = _ar.get("estado", "OK")
                _ar_fecha    = (_ar.get("fecha_ejecucion") or "")[:16].replace("T", " ")
                _ar_nc       = _ar.get("total_criticos", 0)
                _ar_na       = _ar.get("total_advertencias", 0)
                if _ar_estado == "CRÍTICO":
                    _ar_ico = "🔴"
                    _ar_txt = f"{_ar_nc} crítica(s)"
                    _ar_col = "#ef4444"
                elif _ar_estado == "ADVERTENCIA":
                    _ar_ico = "🟡"
                    _ar_txt = f"{_ar_na} advertencia(s)"
                    _ar_col = "#f59e0b"
                else:
                    _ar_ico = "✅"
                    _ar_txt = "Datos OK"
                    _ar_col = "#22c55e"
                st.markdown(
                    f'<div style="font-size:0.68rem;color:#64748b;text-align:center;'
                    f'padding:2px 0 0 0;line-height:1.5;">'
                    f'<span style="color:{_ar_col};">{_ar_ico} {_ar_txt}</span>'
                    f'&nbsp;·&nbsp;{_ar_fecha}</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div style="font-size:0.68rem;color:#475569;text-align:center;'
                    'padding:2px 0 0 0;">⚪ Sin revisión de datos</div>',
                    unsafe_allow_html=True,
                )
        except Exception:
            pass   # nunca romper el sidebar por Auditor Dash 1.0

    _sidebar_load_slot = st.empty()   # placeholder para la barra de carga

# (bloque de toggle móvil eliminado — se usa el botón nativo de Streamlit)

# ── Carga de datos base (barra de progreso en sidebar, no en área principal) ──
# Páginas que necesitan los llamados de Excel (COPEC / ESMAX / Shell).
# En las demás páginas se omite esa carga para no bloquear el arranque.
_PAGES_NEED_LLAMADOS = {_NAV_PAGES[0], _NAV_PAGES[1], _NAV_PAGES[3]}

with _sidebar_load_slot.container():
    _base_prog = st.progress(0, text="📂 Cargando…")
raw_tp = load_third_parties()
with _sidebar_load_slot.container():
    _base_prog = st.progress(50, text="📂 EDS…")

# ── EDS: Supabase en vez de Excel ────────────────────────────────────────────
if _USE_SUPABASE:
    df_eds = load_listado_eds_supabase()
else:
    df_eds = load_listado_eds()

if _page in _PAGES_NEED_LLAMADOS:
    with _sidebar_load_slot.container():
        _base_prog = st.progress(75, text="📂 Llamados…")
    # ── Llamados SLA: Supabase (v_llamados_sla) ──────────────────────────────
    if _USE_SUPABASE:
        _ll_refresh = pd.Timestamp.now().floor("30min").strftime("%Y%m%d%H%M")
        df_llamados = _sc(
            "df_llamados_supa", f"supa_v2_{_ll_refresh}",
            lambda: load_all_llamados_supabase("2026-01-01")
        )
    else:
        df_llamados = load_all_llamados("2026-01-01")
else:
    df_llamados = st.session_state.get("_df_llamados", pd.DataFrame())

_sidebar_load_slot.empty()   # desaparece al terminar

# ── Excepciones SLA globales — aplicadas a df_llamados antes de cualquier cálculo
# OTs justificadas que el Excel marca NO CUMPLE por causas externas a Occimiano.
# Se aplican aquí para que afecten TODA la app (Cumplimiento de SLA + Desempeño Terreno).
if not df_llamados.empty and "cumplimiento" in df_llamados.columns:
    _SLA_EXC_OS  = {"OS-37055", "OS-37448", "OS-37547"}   # folios OS Fracttal
    _SLA_EXC_NUM = {"140785", "143926", "145331"}           # N° llamado ESMAX
    _exc_mask = pd.Series(False, index=df_llamados.index)
    for _col in ["os_fracttal", "OS FRACTTAL"]:
        if _col in df_llamados.columns:
            _exc_mask |= df_llamados[_col].astype(str).str.strip().str.upper().isin(
                {s.upper() for s in _SLA_EXC_OS})
            break
    for _col in ["n_llamado", "LLAMADO", "N° llamado"]:
        if _col in df_llamados.columns:
            _exc_mask |= df_llamados[_col].astype(str).str.strip().isin(_SLA_EXC_NUM)
            break
    if _exc_mask.any():
        df_llamados.loc[_exc_mask, "cumplimiento"] = "CUMPLE"

# ── DataFrames derivados — cacheados en session_state para no recalcular
# en reruns de solo tema (theme toggle). Se invalidan cuando se pulsa "Actualizar".
_base_sig = f"{len(raw_tp)}_{len(df_eds)}_{len(df_llamados)}"
if st.session_state.get("_base_sig") != _base_sig:
    # Datos cambiaron → recalcular
    if not df_llamados.empty and not df_eds.empty:
        df_llamados = resolve_llamados_eds_codes(df_llamados, df_eds)
    df_tp           = build_third_parties_df(raw_tp)
    station_name_map = build_station_name_map(df_tp, df_eds)
    st.session_state["_base_sig"]        = _base_sig
    st.session_state["_df_llamados"]     = df_llamados
    st.session_state["_df_tp"]           = df_tp
    st.session_state["_station_name_map"]= station_name_map
else:
    # Solo rerun de tema → usar caché de session_state
    df_llamados      = st.session_state["_df_llamados"]
    df_tp            = st.session_state["_df_tp"]
    station_name_map = st.session_state["_station_name_map"]

# ── Títulos por página ────────────────────────────────────────────────────────
_PAGE_TITLE = {
    _NAV_PAGES[0]: "Desempeño Servicio Tecnico",
    _NAV_PAGES[1]: "Cumplimiento SLA",
    _NAV_PAGES[2]: "Mantenciones Preventivas",
    _NAV_PAGES[3]: "Estaciones de Servicio",
    _NAV_PAGES[4]: "Utilización del Tiempo",
}
_n_ll = f"{len(df_llamados):,}" if not df_llamados.empty else "–"
_CAPTION = (
    f"Llamados 2026: **{_n_ll}**  |  "
    f"EDS activas: **{int(df_eds['activa'].sum()) if not df_eds.empty else '?'}**"
)


def _hdr(title: str, caption=None) -> None:
    """Renderiza título de página con botón toggle del sidebar al lado derecho."""
    _cap = caption if caption is not None else _CAPTION
    _h_c, _t_c = st.columns([11, 1])
    with _h_c:
        st.title(title)
        if _cap:
            st.caption(_cap)
    with _t_c:
        _open = st.session_state.get("_sb_open", True)
        st.markdown("<div style='padding-top:20px;'>", unsafe_allow_html=True)
        if st.button("◀" if _open else "▶", key="_sb_toggle_main",
                     help="Colapsar/expandir menú lateral"):
            st.session_state["_sb_open"] = not _open
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)


# ── Helper: carga OTs con spinner nativo ────────────────────────────────────
def _load_wo_con_progreso(label: str = "órdenes de trabajo") -> list:
    """
    Carga work orders desde Fracttal.
    - Caché 8 h en disco: si el caché es válido, retorna en < 1 s sin UI.
    - Primera carga o caché expirado: muestra spinner mientras descarga ~30-45 s.
    """
    # Verificar si ya está en caché de disco (el st.cache_data lo gestiona internamente)
    # Usar un st.spinner nativo es más robusto que un thread de progreso animado
    _ph = st.empty()
    with _ph.container():
        with st.spinner(f"⏳ Cargando {label} desde Fracttal… (primera carga puede tardar 1-2 min)"):
            _raw = load_work_orders()
    _ph.empty()
    # Guardar conteo en session_state para el indicador discreto global
    st.session_state["_wo_loaded_count"] = len(_raw)
    st.session_state["_wo_2026_count"] = sum(
        1 for r in _raw
        if str(r.get("creation_date") or "").startswith("2026")
    )
    return _raw

# ══════════════════════════════════════════════════════════════════════════════
# NAVEGACIÓN LATERAL → CONTENIDO CONDICIONAL
# ══════════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# PÁGINA 1: HISTORIAL FRACTTAL
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# PÁGINA 2: CUMPLIMIENTO DE SLA 2026
# ─────────────────────────────────────────────────────────────────────────────
if _page == _NAV_PAGES[1]:
    _hdr(_PAGE_TITLE[_NAV_PAGES[1]])
    st.divider()
    if df_llamados.empty:
        st.warning("No se encontraron llamados 2026. Verifica que Google Drive está sincronizado (G:).")
    else:
        # ── Helpers compartidos de fecha ──────────────────────────────────────
        _MESES_ES_CHART = {
            "01":"Enero","02":"Febrero","03":"Marzo","04":"Abril","05":"Mayo","06":"Junio",
            "07":"Julio","08":"Agosto","09":"Septiembre","10":"Octubre","11":"Noviembre","12":"Diciembre",
        }
        def _ym_a_lbl(ym: str) -> str:
            p = str(ym).split("-")
            return f"{_MESES_ES_CHART.get(p[1], p[1])} {p[0][2:]}" if len(p) == 2 else ym

        _TRIMESTRES_DEF = {
            "T1 · Ene–Mar": [1, 2, 3],
            "T2 · Abr–Jun": [4, 5, 6],
            "T3 · Jul–Sep": [7, 8, 9],
            "T4 · Oct–Dic": [10, 11, 12],
        }
        # Inverso de _MESES_ES_CHART: abreviatura → número de mes (para filtrar por trimestre)
        _MES_ABR_NUM_LL = {v: int(k) for k, v in _MESES_ES_CHART.items()}
        _fl_norm = df_llamados["fecha_llamado"]
        if _fl_norm.dt.tz is not None:
            _fl_norm = _fl_norm.dt.tz_convert(None)
        _meses_con_datos = set(_fl_norm.dt.month.dropna().astype(int).unique())
        _trim_opts = ["Todos"] + [
            k for k, v in _TRIMESTRES_DEF.items()
            if any(m in _meses_con_datos for m in v)
        ]
        _mes_raw = _fl_norm.dt.to_period("M").astype(str)
        _meses_disp_lbl = ["Todos"] + [_ym_a_lbl(m) for m in sorted(_mes_raw.dropna().unique(), reverse=True)]
        _lbl_to_period  = {_ym_a_lbl(m): m for m in sorted(_mes_raw.dropna().unique(), reverse=True)}

        _equipo_to_full: dict[str, list[str]] = {
            grp: [TECH_NAME_MAP.get(m, m) for m in info["miembros"]]
            for grp, info in GRUPOS_TERRENO.items()
        }
        _equipo_opts = ["Todos"] + list(GRUPOS_TERRENO.keys())
        prio_colors = {"P1":"#ef4444","P2":"#f97316","P3":"#eab308","P4":"#22c55e"}
        _cu_colors  = {"CUMPLE":"#22c55e","NO CUMPLE":"#ef4444","SIN DATOS":"#94a3b8","NAN":"#94a3b8"}

        # Helper SLA compartido
        def _get_sla_h(cliente: str, prioridad: str, zona_key: str):
            return (SLA_HOURS.get(str(cliente), SLA_DEFAULT)
                    .get(str(prioridad).upper(), {})
                    .get(str(zona_key), None))

        # ── Sub-pestañas ──────────────────────────────────────────────────────
        _tab_cli, _tab_tec = st.tabs(["👤  Clientes", "🔧  Servicio Técnico"])

        # ══════════════════════════════════════════════════════════════════════
        # SUB-PESTAÑA: CLIENTES
        # ══════════════════════════════════════════════════════════════════════
        with _tab_cli:
            # ── Filtros ───────────────────────────────────────────────────────
            cf1, cf2, cf3, cf4, cf5 = st.columns([1.6, 1.4, 1.6, 1.4, 1.6])
            with cf1:
                sel_trim_c = st.selectbox("Período", _trim_opts, key="cl_trim")
            with cf2:
                if sel_trim_c != "Todos":
                    _trim_m_c = _TRIMESTRES_DEF[sel_trim_c]
                    _meses_c_disp = [l for l in _meses_disp_lbl[1:]  # skip "Todos"
                                     if _MES_ABR_NUM_LL.get(l.split(" ")[0], 0) in _trim_m_c]
                else:
                    _meses_c_disp = _meses_disp_lbl[1:]
                sel_mes_c = st.multiselect("Mes", _meses_c_disp, key="cl_mes",
                                           placeholder="Todos los meses")
            with cf3:
                _cl_opts = ["Todos"] + sorted(df_llamados["cliente"].dropna().unique().tolist())
                sel_cl_c = st.selectbox("Cliente", _cl_opts, key="cl_cli")
            with cf4:
                _pr_opts_c = ["Todas"] + sorted(df_llamados["prioridad"].dropna().unique().tolist())
                sel_pr_c = st.selectbox("Prioridad", _pr_opts_c, key="cl_pr")
            with cf5:
                sel_cu_c = st.selectbox("Cumplimiento SLA", ["Todos","CUMPLE","NO CUMPLE"], key="cl_cu")

            # ── Aplicar filtros ───────────────────────────────────────────────
            df_ll = df_llamados.copy()
            _fl2 = df_ll["fecha_llamado"]
            if _fl2.dt.tz is not None:
                _fl2 = _fl2.dt.tz_convert(None)
            df_ll["_mes"]   = _fl2.dt.to_period("M").astype(str)
            df_ll["_month"] = _fl2.dt.month.astype("Int64")
            if sel_trim_c != "Todos":
                df_ll = df_ll[df_ll["_month"].isin(_TRIMESTRES_DEF[sel_trim_c])]
            if sel_mes_c:
                _periods_c = [_lbl_to_period[l] for l in sel_mes_c if l in _lbl_to_period]
                if _periods_c: df_ll = df_ll[df_ll["_mes"].isin(_periods_c)]
            if sel_cl_c != "Todos":  df_ll = df_ll[df_ll["cliente"] == sel_cl_c]
            if sel_pr_c != "Todas":  df_ll = df_ll[df_ll["prioridad"].str.upper() == sel_pr_c.upper()]
            if sel_cu_c != "Todos":  df_ll = df_ll[df_ll["cumplimiento"] == sel_cu_c]

            # ── KPIs ──────────────────────────────────────────────────────────
            _mes_lbl_tit = f" — {', '.join(sel_mes_c)}" if sel_mes_c else ""
            _cumple_c    = (df_ll["cumplimiento"] == "CUMPLE").sum()
            _nocumple_c  = (df_ll["cumplimiento"] == "NO CUMPLE").sum()
            _pct_c       = round(_cumple_c/(_cumple_c+_nocumple_c)*100,1) if (_cumple_c+_nocumple_c)>0 else 0
            lk1, lk2, lk3, lk4, lk5 = st.columns(5)
            lk1.metric(f"Total llamados{_mes_lbl_tit}", f"{len(df_ll):,}")
            lk2.metric("P1 (máquina detenida)", f"{(df_ll['prioridad'].str.upper()=='P1').sum():,}")
            lk3.metric("Cumple SLA", f"{_cumple_c:,}", delta=f"{_pct_c}%")
            lk4.metric("No cumple SLA", f"{_nocumple_c:,}",
                       delta=f"-{100-_pct_c:.1f}%" if (_cumple_c+_nocumple_c)>0 else None,
                       delta_color="inverse")
            # ── KPI semana actual (Domingo→Sábado, renovación automática) ─────
            _ts_hoy     = pd.Timestamp.now().normalize()
            _dow_s      = (_ts_hoy.dayofweek + 1) % 7          # 0=Dom … 6=Sáb
            _sem_ts     = _ts_hoy - pd.Timedelta(days=_dow_s)  # domingo de esta semana
            _sem_ini    = _sem_ts.date()
            _sem_fin_d  = (_sem_ts + pd.Timedelta(days=6)).date()
            _sem_num    = (_sem_ts + pd.Timedelta(days=1)).isocalendar()[1]
            _meses_abr  = ["Ene","Feb","Mar","Abr","May","Jun",
                           "Jul","Ago","Sep","Oct","Nov","Dic"]
            _sem_lbl    = (f"{_sem_ini.day}–{_sem_fin_d.day} "
                           f"{_meses_abr[_sem_ini.month-1]}")
            _fl_sem     = df_llamados["fecha_llamado"]
            if _fl_sem.dt.tz is not None:
                _fl_sem = _fl_sem.dt.tz_convert(None)
            _mask_sem   = (
                (_fl_sem.dt.date >= _sem_ini) &
                (_fl_sem.dt.date <= _sem_fin_d) &
                (df_llamados["cumplimiento"].isin(["CUMPLE","NO CUMPLE"]))
            )
            _df_sem     = df_llamados[_mask_sem]
            _sem_total  = len(_df_sem)
            _sem_cumple = (_df_sem["cumplimiento"] == "CUMPLE").sum()
            _sem_pct    = round(_sem_cumple / _sem_total * 100, 1) if _sem_total else 0.0
            lk5.metric(
                f"Sem. {_sem_num}  ({_sem_lbl})",
                f"{_sem_pct}%",
                delta=f"{_sem_total} llamados",
                delta_color="off",
            )

            st.divider()
            st.markdown('<div class="section-header">Distribución por prioridad y cliente</div>', unsafe_allow_html=True)
            _ll_sig_c = f"{len(df_llamados)}_{sel_trim_c}_{sel_mes_c}_{sel_cl_c}_{sel_pr_c}_{sel_cu_c}"
            gc1, gc2, gc3 = st.columns([2, 1.1, 2.7])

            with gc1:
                _k = f"_fig_ll_prio_{_current_theme}_{_ll_sig_c}"
                if _k not in st.session_state:
                    _d = df_ll["prioridad"].value_counts().reset_index()
                    _d.columns = ["Prioridad","Llamados"]
                    _f = px.pie(_d, values="Llamados", names="Prioridad", color="Prioridad",
                                color_discrete_map=prio_colors, title="Por prioridad", hole=0.45,
                                category_orders={"Prioridad": ["P1", "P2", "P3", "P4"]})
                    _f.update_traces(
                        marker=dict(line=dict(color="#2d3436", width=1.5)),
                        textfont=dict(size=13),
                    )
                    _f.update_layout(height=400, margin=dict(t=40,b=5,l=5,r=5))
                    _apply_plot_theme(_f); st.session_state[_k] = _f
                st.plotly_chart(st.session_state[_k], width="stretch")

            with gc2:
                _k = f"_fig_ll_cli_{_current_theme}_{_ll_sig_c}"
                if _k not in st.session_state:
                    _d = df_ll["cliente"].value_counts().reset_index()
                    _d.columns = ["Cliente","Llamados"]
                    _f = px.bar(_d, x="Cliente", y="Llamados", color="Cliente",
                                color_discrete_map=CLIENT_COLORS, title="Por cliente", text_auto=True)
                    _f.update_traces(marker_line_color="#2d3436", marker_line_width=0.8,
                                     textfont=dict(size=13))
                    _f.update_layout(showlegend=False, xaxis_title="",
                                     height=400, margin=dict(t=40, b=5))
                    _apply_plot_theme(_f); st.session_state[_k] = _f
                st.plotly_chart(st.session_state[_k], width="stretch")

            with gc3:
                _k = f"_fig_ll_gauge_{_current_theme}_{_ll_sig_c}"
                if _k not in st.session_state:
                    import math as _math

                    _g_clr = (
                        "#22c55e" if _pct_c >= 90 else
                        "#84cc16" if _pct_c >= 75 else
                        "#f97316" if _pct_c >= 60 else "#ef4444"
                    )

                    # ── Gradiente suave rojo→verde (10 zonas sin bordes visibles) ─
                    def _arc(vmin, vmax, r_in, r_out, n=80):
                        a0 = _math.pi*(1 - vmax/100)
                        a1 = _math.pi*(1 - vmin/100)
                        t  = [a0+(a1-a0)*i/(n-1) for i in range(n)]
                        xo = [r_out*_math.cos(a) for a in t]
                        yo = [r_out*_math.sin(a) for a in t]
                        xi = [r_in *_math.cos(a) for a in reversed(t)]
                        yi = [r_in *_math.sin(a) for a in reversed(t)]
                        return xo+xi+[xo[0]], yo+yi+[yo[0]]

                    # Arco exterior gris oscuro (borde)
                    _R_OUT, _R_IN = 1.0, 0.52
                    _zones = [
                        (0,  10, "#c0392b"),
                        (10, 20, "#e74c3c"),
                        (20, 35, "#e67e22"),
                        (35, 50, "#f39c12"),
                        (50, 65, "#f1c40f"),
                        (65, 75, "#d4e157"),
                        (75, 85, "#a5d63a"),
                        (85, 92, "#66bb6a"),
                        (92, 97, "#43a047"),
                        (97,100, "#2e7d32"),
                    ]
                    _fig_gauge = go.Figure()
                    for _vn, _vx, _clr in _zones:
                        _xz, _yz = _arc(_vn, _vx, _R_IN, _R_OUT)
                        _fig_gauge.add_trace(go.Scatter(
                            x=_xz, y=_yz, fill="toself",
                            fillcolor=_clr,
                            line=dict(color=_clr, width=0.5),
                            mode="lines", showlegend=False, hoverinfo="skip"
                        ))

                    # Aro exterior oscuro (delgado)
                    _xout, _yout = _arc(0, 100, _R_OUT, _R_OUT+0.025)
                    _fig_gauge.add_trace(go.Scatter(
                        x=_xout, y=_yout, fill="toself",
                        fillcolor="#2d3436", line=dict(color="#2d3436", width=0),
                        mode="lines", showlegend=False, hoverinfo="skip"
                    ))
                    # Aro interior oscuro (delgado)
                    _xinn, _yinn = _arc(0, 100, _R_IN-0.025, _R_IN)
                    _fig_gauge.add_trace(go.Scatter(
                        x=_xinn, y=_yinn, fill="toself",
                        fillcolor="#2d3436", line=dict(color="#2d3436", width=0),
                        mode="lines", showlegend=False, hoverinfo="skip"
                    ))

                    # ── Aguja delgada y profesional ───────────────────────────
                    _ang = _math.pi*(1 - _pct_c/100)
                    _nl  = 0.78   # largo aguja
                    _bw  = 0.025  # ancho base
                    _al, _ar = _ang+_math.pi/2, _ang-_math.pi/2
                    # Aguja principal
                    _fig_gauge.add_trace(go.Scatter(
                        x=[_bw*_math.cos(_al), _nl*_math.cos(_ang),
                           _bw*_math.cos(_ar), _bw*_math.cos(_al)],
                        y=[_bw*_math.sin(_al), _nl*_math.sin(_ang),
                           _bw*_math.sin(_ar), _bw*_math.sin(_al)],
                        fill="toself", fillcolor="#2d3436",
                        line=dict(color="#2d3436", width=0.5),
                        mode="lines", showlegend=False, hoverinfo="skip"
                    ))
                    # Contrapeso (cola corta)
                    _tail = 0.18
                    _fig_gauge.add_trace(go.Scatter(
                        x=[_bw*_math.cos(_al),
                           -_tail*_math.cos(_ang),
                           _bw*_math.cos(_ar),
                           _bw*_math.cos(_al)],
                        y=[_bw*_math.sin(_al),
                           -_tail*_math.sin(_ang),
                           _bw*_math.sin(_ar),
                           _bw*_math.sin(_al)],
                        fill="toself", fillcolor="#555",
                        line=dict(color="#555", width=0.5),
                        mode="lines", showlegend=False, hoverinfo="skip"
                    ))
                    # Hub: círculo blanco con borde oscuro
                    _hn, _hr_o, _hr_i = 40, 0.10, 0.06
                    for _hr, _fc in [(_hr_o,"#2d3436"),(_hr_i,"#ffffff")]:
                        _fig_gauge.add_trace(go.Scatter(
                            x=[_hr*_math.cos(2*_math.pi*i/_hn) for i in range(_hn+1)],
                            y=[_hr*_math.sin(2*_math.pi*i/_hn) for i in range(_hn+1)],
                            fill="toself", fillcolor=_fc,
                            line=dict(color=_fc), mode="lines",
                            showlegend=False, hoverinfo="skip"
                        ))

                    # ── Ticks ─────────────────────────────────────────────────
                    for _tv in [0, 25, 50, 75, 90, 100]:
                        _ta    = _math.pi*(1 - _tv/100)
                        _is_90 = _tv == 90
                        _tc    = "#e74c3c" if _is_90 else "#ecf0f1"
                        _tw    = 3 if _is_90 else 2
                        _tl    = 1.18 if _is_90 else 1.13
                        _fig_gauge.add_trace(go.Scatter(
                            x=[(_R_OUT+0.07)*_math.cos(_ta), _tl*_math.cos(_ta)],
                            y=[(_R_OUT+0.07)*_math.sin(_ta), _tl*_math.sin(_ta)],
                            line=dict(color=_tc, width=_tw),
                            mode="lines", showlegend=False, hoverinfo="skip"
                        ))
                        _lbl = f"<b>{_tv}%✓</b>" if _is_90 else f"{_tv}%"
                        _fig_gauge.add_annotation(
                            x=1.30*_math.cos(_ta), y=1.30*_math.sin(_ta),
                            text=_lbl, showarrow=False,
                            font=dict(size=10 if _is_90 else 9,
                                      color=_tc)
                        )

                    # ── Textos ────────────────────────────────────────────────
                    _fig_gauge.add_annotation(
                        x=0, y=-0.15,
                        text=f"<b>{_pct_c:.1f}%</b>",
                        showarrow=False, font=dict(size=36, color=_g_clr)
                    )
                    _fig_gauge.add_annotation(
                        x=0, y=-0.38,
                        text=f"{int(_cumple_c)} cumple · {int(_nocumple_c)} no cumple",
                        showarrow=False, font=dict(size=11, color=_t["muted"])
                    )
                    _fig_gauge.add_annotation(
                        x=0, y=1.30,
                        text="<b>Cumplimiento SLA</b>",
                        showarrow=False, font=dict(size=14, color=_t["text"])
                    )

                    _fig_gauge.update_layout(
                        xaxis=dict(range=[-1.2, 1.2], visible=False, scaleanchor="y"),
                        yaxis=dict(range=[-0.50, 1.42], visible=False),
                        height=400,
                        margin=dict(t=5, b=5, l=5, r=5),
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="rgba(0,0,0,0)",
                    )
                    st.session_state[_k] = _fig_gauge
                st.plotly_chart(st.session_state[_k], width="stretch")

            # ── Evolución SLA por mes ─────────────────────────────────────────
            st.divider()
            _cli_lbl_evol = sel_cl_c if sel_cl_c != "Todos" else "todos los clientes"
            st.markdown(
                f'<div class="section-header">📈 Evolución SLA Occimiano por mes — {_cli_lbl_evol}</div>',
                unsafe_allow_html=True,
            )

            _sla_evol_sig = f"{len(df_llamados)}_{sel_trim_c}_{sel_cl_c}_{sel_pr_c}"
            _sla_evol_k   = f"_fig_ll_sla_evol_{_current_theme}_{_sla_evol_sig}"
            if _sla_evol_k not in st.session_state:
                # Base: filtrar por período y cliente pero NO por cumplimiento
                # para medir la tasa SLA real mes a mes
                _ev_base = df_llamados.copy()
                _fl_ev = _ev_base["fecha_llamado"]
                if _fl_ev.dt.tz is not None:
                    _fl_ev = _fl_ev.dt.tz_convert(None)
                _ev_base["_mes"]   = _fl_ev.dt.to_period("M").astype(str)
                _ev_base["_month"] = _fl_ev.dt.month.astype("Int64")
                if sel_trim_c != "Todos":
                    _ev_base = _ev_base[_ev_base["_month"].isin(_TRIMESTRES_DEF[sel_trim_c])]
                if sel_cl_c  != "Todos":
                    _ev_base = _ev_base[_ev_base["cliente"] == sel_cl_c]
                if sel_pr_c  != "Todas":
                    _ev_base = _ev_base[_ev_base["prioridad"].str.upper() == sel_pr_c.upper()]

                _ev_grp = (
                    _ev_base.groupby("_mes").agg(
                        llamados =("_mes", "count"),
                        cumple   =("cumplimiento", lambda x: (x == "CUMPLE").sum()),
                        no_cumple=("cumplimiento", lambda x: (x == "NO CUMPLE").sum()),
                    ).reset_index().sort_values("_mes")
                )
                _ev_grp["pct_sla"] = (
                    (_ev_grp["cumple"] / (_ev_grp["cumple"] + _ev_grp["no_cumple"]) * 100)
                    .where((_ev_grp["cumple"] + _ev_grp["no_cumple"]) > 0, 0)
                    .astype(float).round(1)
                )
                _ev_grp["mes_lbl"] = _ev_grp["_mes"].apply(_ym_a_lbl)

                if not _ev_grp.empty:
                    _fig_sla_evol = make_subplots(specs=[[{"secondary_y": True}]])

                    # Color de barra según cliente seleccionado
                    _bar_color_evol = CLIENT_COLORS.get(sel_cl_c, "#94a3b8")
                    # Texto oscuro sobre amarillo (Shell), blanco sobre el resto
                    _bar_txt_evol = "#1e293b" if sel_cl_c == "SHELL (Enex)" else "#ffffff"

                    # Barras: llamados por mes
                    _fig_sla_evol.add_trace(
                        go.Bar(
                            x=_ev_grp["mes_lbl"], y=_ev_grp["llamados"],
                            name="Llamados correctivos",
                            marker_color=_bar_color_evol, opacity=0.9,
                            text=_ev_grp["llamados"],
                            textposition="inside",
                            textfont=dict(size=13, color=_bar_txt_evol, family="Arial"),
                        ),
                        secondary_y=False,
                    )

                    # Línea: tendencia de llamados — naranja
                    _fig_sla_evol.add_trace(
                        go.Scatter(
                            x=_ev_grp["mes_lbl"], y=_ev_grp["llamados"],
                            name="Tendencia llamados",
                            mode="lines+markers",
                            line=dict(color="#f97316", width=2.5, dash="dot"),
                            marker=dict(size=7, color="#f97316",
                                        line=dict(color="#ffffff", width=1)),
                        ),
                        secondary_y=False,
                    )

                    # Línea: % SLA cumplimiento — verde (eje derecho)
                    _fig_sla_evol.add_trace(
                        go.Scatter(
                            x=_ev_grp["mes_lbl"], y=_ev_grp["pct_sla"],
                            name="% Cumplimiento SLA",
                            mode="lines+markers",
                            line=dict(color="#22c55e", width=3),
                            marker=dict(size=11, color="#22c55e",
                                        line=dict(color="#ffffff", width=2)),
                            customdata=list(zip(
                                _ev_grp["cumple"], _ev_grp["no_cumple"], _ev_grp["llamados"]
                            )),
                            hovertemplate=(
                                "<b>%{x}</b><br>"
                                "% SLA: %{y:.1f}%<br>"
                                "Cumple: %{customdata[0]}<br>"
                                "No cumple: %{customdata[1]}<br>"
                                "Total llamados: %{customdata[2]}<br>"
                                "<extra></extra>"
                            ),
                        ),
                        secondary_y=True,
                    )
                    # Anotaciones con fondo blanco bordeado en verde
                    for _, _ann_row in _ev_grp.iterrows():
                        _fig_sla_evol.add_annotation(
                            x=_ann_row["mes_lbl"],
                            y=_ann_row["pct_sla"],
                            yref="y2",
                            text=f"<b>{_ann_row['pct_sla']:.1f}%</b>  {int(_ann_row['cumple'])} cumple",
                            showarrow=False,
                            yanchor="bottom",
                            yshift=10,
                            font=dict(size=11, color="#16a34a", family="Arial"),
                            bgcolor="rgba(255,255,255,0.88)",
                            bordercolor="#22c55e",
                            borderwidth=1.5,
                            borderpad=4,
                        )

                    _fig_sla_evol.update_layout(
                        title=f"Evolución mensual — Llamados correctivos vs % SLA",
                        height=430,
                        legend=dict(orientation="h", y=1.08, x=0),
                        margin=dict(t=60, b=20),
                        bargap=0.3,
                    )
                    _fig_sla_evol.update_yaxes(title_text="Llamados correctivos", secondary_y=False)
                    _fig_sla_evol.update_yaxes(
                        title_text="% Cumplimiento SLA",
                        secondary_y=True,
                        tickformat=".1f", ticksuffix="%",
                        range=[0, 110],
                    )
                    _apply_plot_theme(_fig_sla_evol)
                    st.session_state[_sla_evol_k] = _fig_sla_evol
                else:
                    st.session_state[_sla_evol_k] = None

            _fig_sla_show = st.session_state.get(_sla_evol_k)
            if _fig_sla_show is not None:
                st.plotly_chart(_fig_sla_show, width="stretch")
            else:
                st.info("Sin datos para el período seleccionado.")

            st.divider()
            st.markdown('<div class="section-header">Evolución mensual de llamados</div>', unsafe_allow_html=True)
            _df_llm = df_ll.dropna(subset=["fecha_llamado"]).copy()
            _df_llm["mes_lbl"] = _df_llm["_mes"].apply(_ym_a_lbl)
            _monthly_c = _df_llm.groupby(["mes_lbl","prioridad"]).size().reset_index(name="llamados")
            _mes_ord_c = [_ym_a_lbl(m) for m in sorted(_df_llm["_mes"].unique())]
            if not _monthly_c.empty:
                _k = f"_fig_ll_monthly_{_current_theme}_{_ll_sig_c}"
                if _k not in st.session_state:
                    _f = px.bar(_monthly_c, x="mes_lbl", y="llamados", color="prioridad",
                                color_discrete_map=prio_colors, title="Llamados por mes y prioridad",
                                barmode="stack", category_orders={"mes_lbl": _mes_ord_c})
                    _f.update_layout(xaxis_title="", yaxis_title="Llamados",
                                     legend_title="Prioridad", xaxis_type="category")
                    _apply_plot_theme(_f); st.session_state[_k] = _f
                st.plotly_chart(st.session_state[_k], width="stretch")

            st.markdown('<div class="section-header">Ranking de EDS por llamados (2026)</div>', unsafe_allow_html=True)

            # Pre-calcular % uso SLA por OT (tiempo real / umbral × 100)
            # para tener un indicador real incluso cuando cumplimiento=0%
            def _td_h_c(v):
                try: return pd.to_timedelta(v).total_seconds()/3600
                except: return float("nan")
            def _zona_c(z):
                z=str(z).upper().strip()
                if any(k in z for k in ["SANTIAGO","METRO","RM","R.M."]): return "Santiago"
                return "Regiones"
            _df_sla_uso = df_ll.copy()
            _df_sla_uso["_h"] = _df_sla_uso.get(
                "tiempo_resp_real", pd.Series(dtype=object, index=_df_sla_uso.index)
            ).apply(_td_h_c)
            _df_sla_uso["_zona"] = _df_sla_uso.get(
                "zona", pd.Series("", index=_df_sla_uso.index)
            ).apply(_zona_c)
            _df_sla_uso["_sla"] = [
                _get_sla_h(c, p, z)
                for c, p, z in zip(
                    _df_sla_uso.get("cliente",  pd.Series("", index=_df_sla_uso.index)),
                    _df_sla_uso.get("prioridad", pd.Series("", index=_df_sla_uso.index)),
                    _df_sla_uso["_zona"],
                )
            ]
            _df_sla_uso["_pct"] = (
                (_df_sla_uso["_h"] / _df_sla_uso["_sla"] * 100)
                .where(
                    _df_sla_uso["_h"].notna() &
                    pd.Series([pd.notna(x) for x in _df_sla_uso["_sla"]], index=_df_sla_uso.index)
                )
            )
            _eds_avg_pct = (
                _df_sla_uso.groupby("eds_occim")["_pct"].mean().round(1)
                .reset_index().rename(columns={"_pct": "pct_sla_uso"})
            )

            _kpis_raw_c = kpis_por_eds(df_ll)
            if not df_eds.empty:
                _eds_act = df_eds[df_eds["activa"]].copy()
                if sel_cl_c != "Todos":
                    _eds_act = _eds_act[_eds_act["cliente"] == sel_cl_c]
                _bc = [c for c in ["eds_occim","eds_cliente","cliente","direccion","comuna","zona_occim","region"]
                       if c in _eds_act.columns]
                _eds_base = _eds_act[_bc].drop_duplicates("eds_occim")
                if not _kpis_raw_c.empty:
                    # Partir desde los llamados reales (no desde el listado completo de EDS)
                    # → garantiza que aparecen todos los llamados aunque su código no esté en df_eds
                    kpis_ll = _kpis_raw_c.merge(
                        _eds_base.drop(columns=["cliente"], errors="ignore"),
                        on="eds_occim", how="left")
                else:
                    kpis_ll = pd.DataFrame()
                for _col in ["total_llamados","p1","p2","p3","p4","cumple","no_cumple"]:
                    if _col in kpis_ll.columns:
                        kpis_ll[_col] = kpis_ll[_col].fillna(0).astype(int)
                if "pct_cumplimiento" in kpis_ll.columns:
                    kpis_ll["pct_cumplimiento"] = kpis_ll["pct_cumplimiento"].fillna(0.0)
                # Solo EDS que han tenido al menos 1 llamado
                if not kpis_ll.empty and "total_llamados" in kpis_ll.columns:
                    kpis_ll = kpis_ll[kpis_ll["total_llamados"] > 0]
                # Unir % promedio de uso SLA (tiempo real / umbral × 100)
                if not kpis_ll.empty:
                    kpis_ll = kpis_ll.merge(_eds_avg_pct, on="eds_occim", how="left")
            else:
                kpis_ll = _kpis_raw_c
                kpis_ll = kpis_ll.merge(_eds_avg_pct, on="eds_occim", how="left")
            if not kpis_ll.empty:
                # Nombre para mostrar: nombre comercial → dirección → código
                kpis_ll["nombre"] = kpis_ll["eds_occim"].astype(str).map(station_name_map)
                kpis_ll["nombre"] = kpis_ll.apply(
                    lambda r: r["nombre"] if pd.notna(r.get("nombre")) and str(r.get("nombre","")).strip()
                    else r.get("direccion",""), axis=1)
                if "ultimo_llamado" in kpis_ll.columns:
                    kpis_ll["ultimo_llamado"] = (pd.to_datetime(kpis_ll["ultimo_llamado"],errors="coerce")
                                                 .dt.strftime("%d/%m/%Y"))
                # Filtro definitivo: solo EDS con al menos 1 llamado real
                # (doble garantía: el filtro previo pudo no alcanzar si total_llamados llegó como NaN)
                if "total_llamados" in kpis_ll.columns:
                    kpis_ll = kpis_ll[kpis_ll["total_llamados"].fillna(0).astype(int) > 0]

                _ll_buscar = st.text_input("🔍 Buscar estación (código, dirección, comuna)",
                                           key="ll_buscar", placeholder="ej: Talagante, 60783, SH_647")
                if _ll_buscar:
                    _q = _ll_buscar.lower()
                    _mask = (
                        kpis_ll["eds_occim"].astype(str).str.lower().str.contains(_q, na=False)
                        | kpis_ll.get("eds_cliente",pd.Series(dtype=str)).fillna("").str.lower().str.contains(_q,na=False)
                        | kpis_ll.get("nombre",pd.Series(dtype=str)).fillna("").str.lower().str.contains(_q,na=False)
                        | kpis_ll.get("direccion",pd.Series(dtype=str)).fillna("").str.lower().str.contains(_q,na=False)
                        | kpis_ll.get("comuna",pd.Series(dtype=str)).fillna("").str.lower().str.contains(_q,na=False)
                    )
                    kpis_ll = kpis_ll[_mask]

                # Columnas a mostrar: solo las relevantes (sin desglose de prioridad)
                _dc = [c for c in ["eds_occim", "nombre", "cliente", "comuna",
                                   "total_llamados", "pct_cumplimiento",
                                   "ultimo_llamado", "ultimo_tecnico"]
                       if c in kpis_ll.columns]
                _kpis_disp = (
                    kpis_ll[_dc]
                    .sort_values("total_llamados", ascending=False)
                    .rename(columns={
                        "eds_occim":        "Cód. Occim",
                        "nombre":           "Nombre / Dirección",
                        "cliente":          "Cliente",
                        "comuna":           "Comuna",
                        "total_llamados":   "Llamados",
                        "pct_cumplimiento": "% Cumpl. SLA",
                        "ultimo_llamado":   "Último Llamado",
                        "ultimo_tecnico":   "Último Técnico",
                    })
                )
                if not _kpis_disp.empty:
                    _show_df(_kpis_disp, width="stretch", hide_index=True,
                        column_config={
                            "Cód. Occim":         st.column_config.TextColumn(width=100),
                            "Nombre / Dirección": st.column_config.TextColumn(width=280),
                            "Cliente":            st.column_config.TextColumn(width=110),
                            "Comuna":             st.column_config.TextColumn(width=120),
                            "Llamados":           st.column_config.NumberColumn(
                                                    format="%d", width=90),
                            "% Cumpl. SLA":       st.column_config.ProgressColumn(
                                                    label="% Cumpl. SLA",
                                                    min_value=0, max_value=100,
                                                    format="%.1f%%"),
                            "Último Llamado":     st.column_config.TextColumn(width=110),
                            "Último Técnico":     st.column_config.TextColumn(width=140),
                        })
                else:
                    st.info("Sin llamados en el período seleccionado.")

            st.divider()
            st.markdown('<div class="section-header">⏱ Cumplimiento de SLA por OT</div>', unsafe_allow_html=True)
            st.caption("Tiempo de resolución real por cada llamado cerrado, comparado con el umbral de SLA según prioridad y zona.")

            def _fmt_horas_ot(h: float) -> str:
                if pd.isna(h) or h < 0: return "—"
                hh, mm = int(h), int((h % 1) * 60)
                return f"{hh}h {mm:02d}m"

            _df_sla_ot = df_ll[df_ll["fecha_llamado"].notna() & df_ll["fecha_atencion"].notna()].copy()
            if not _df_sla_ot.empty:
                def _merge_dt(fecha_ser: pd.Series, hora_col: str) -> pd.Series:
                    fechas = pd.to_datetime(fecha_ser, errors="coerce")
                    if hora_col not in _df_sla_ot.columns: return fechas
                    def _to_hms(h):
                        if pd.isna(h): return ""
                        if hasattr(h, "strftime"): return h.strftime("%H:%M:%S")
                        s = str(h).strip()
                        return "" if s in ("nan","NaT","None","") else s
                    hora_str = _df_sla_ot[hora_col].apply(_to_hms)
                    date_str = fechas.dt.strftime("%Y-%m-%d").fillna("")
                    combined = pd.to_datetime((date_str + " " + hora_str).str.strip(), errors="coerce")
                    return combined.fillna(fechas)

                _dt_ll  = _merge_dt(_df_sla_ot["fecha_llamado"], "hora_llamado")
                _dt_at  = _merge_dt(_df_sla_ot["fecha_atencion"], "hora_fin")
                _df_sla_ot["horas_res"] = ((_dt_at - _dt_ll).dt.total_seconds() / 3600).clip(lower=0)

                def _zona_key_ot(z: str) -> str:
                    z = str(z).upper().strip()
                    if z in ("RM","R.M.") or any(k in z for k in ["SANTIAGO","METRO"]): return "Santiago"
                    if any(k in z for k in ["NORTE","SUR","CENTRO","REGION","REGIONES","RG"]): return "Regiones"
                    return "Santiago"

                _df_sla_ot["zona_ot"] = (
                    _df_sla_ot["zona"].fillna("").astype(str).apply(_zona_key_ot)
                    if "zona" in _df_sla_ot.columns else "Santiago"
                )
                _df_sla_ot["umbral_h"] = [
                    _get_sla_h(c, p, z)
                    for c, p, z in zip(
                        _df_sla_ot.get("cliente", pd.Series([""] * len(_df_sla_ot), index=_df_sla_ot.index)),
                        _df_sla_ot.get("prioridad", pd.Series([""] * len(_df_sla_ot), index=_df_sla_ot.index)),
                        _df_sla_ot["zona_ot"],
                    )
                ]
                _df_sla_ot["umbral_lbl"] = [
                    f"{int(u)}h" if (u is not None and pd.notna(u)) else "—"
                    for u in _df_sla_ot["umbral_h"]
                ]
                _df_sla_ot["tiempo_res"] = [_fmt_horas_ot(h) for h in _df_sla_ot["horas_res"]]
                # % de uso SLA real (tiempo / umbral × 100)
                _df_sla_ot["pct_sla_ot"] = [
                    round(h / u * 100, 1) if (u is not None and pd.notna(u) and u > 0 and pd.notna(h)) else None
                    for h, u in zip(_df_sla_ot["horas_res"], _df_sla_ot["umbral_h"])
                ]
                # estado_sla: usar cumplimiento del Excel como fuente de verdad
                # (evita discrepancias entre la marca del Excel y el cálculo de horas)
                if "cumplimiento" in _df_sla_ot.columns:
                    _df_sla_ot["estado_sla"] = _df_sla_ot["cumplimiento"].apply(
                        lambda v: "✅ Cumple" if str(v).upper() == "CUMPLE"
                                 else ("❌ No cumple" if str(v).upper() == "NO CUMPLE"
                                       else "⚪ Sin datos")
                    )
                else:
                    _df_sla_ot["estado_sla"] = [
                        ("✅ Cumple" if (u is not None and pd.notna(u)) and h <= u else
                         "❌ No cumple" if (u is not None and pd.notna(u)) else "⚪ Sin prioridad")
                        for h, u in zip(_df_sla_ot["horas_res"], _df_sla_ot["umbral_h"])
                    ]
                # Agregar ciudad: primero desde columna "comuna" del propio llamado
                # (Shell ya la incluye en su Excel). Si no existe, buscar en df_eds.
                if "comuna" in _df_sla_ot.columns:
                    _df_sla_ot["ciudad"] = _df_sla_ot["comuna"].fillna("—")
                elif not df_eds.empty and "comuna" in df_eds.columns and "eds_occim" in _df_sla_ot.columns:
                    _eds_ciudad = df_eds[["eds_occim","comuna"]].drop_duplicates("eds_occim")
                    _df_sla_ot = _df_sla_ot.merge(_eds_ciudad, on="eds_occim", how="left")
                    _df_sla_ot["ciudad"] = _df_sla_ot["comuna_y" if "comuna_y" in _df_sla_ot.columns else "comuna"].fillna("—")
                else:
                    _df_sla_ot["ciudad"] = "—"

                # N° Aviso cliente — Aramco: N° Cotalker / COPEC: N° Aviso del email
                _cotalker_idx = load_cotalker_index_supabase()
                if _cotalker_idx and "os_fracttal" in _df_sla_ot.columns:
                    _df_sla_ot["n_cotalker"] = (
                        _df_sla_ot["os_fracttal"]
                        .map(_cotalker_idx)
                        .apply(lambda v: str(v) if pd.notna(v) and str(v) not in ("", "nan") else "")
                    )
                else:
                    _df_sla_ot["n_cotalker"] = ""

                _sla_ot_base = [c for c in ["os_fracttal","n_cotalker","fecha_llamado","fecha_atencion",
                                            "wo_cierre_ot","eds_occim","eds_nombre","cliente","tecnico",
                                            "prioridad","ciudad","zona_ot"] if c in _df_sla_ot.columns]
                _df_sla_ot_disp = _df_sla_ot[_sla_ot_base + ["tiempo_res","umbral_lbl","pct_sla_ot","estado_sla"]].copy()
                if "wo_cierre_ot" in _df_sla_ot_disp.columns:
                    _df_sla_ot_disp["wo_cierre_ot"] = pd.to_datetime(
                        _df_sla_ot_disp["wo_cierre_ot"], errors="coerce"
                    ).dt.strftime("%d/%m/%Y %H:%M").fillna("—")
                _df_sla_ot_disp["fecha_llamado"]  = pd.to_datetime(_df_sla_ot_disp["fecha_llamado"],  errors="coerce").dt.strftime("%d/%m/%Y")
                _df_sla_ot_disp["fecha_atencion"] = pd.to_datetime(_df_sla_ot_disp["fecha_atencion"], errors="coerce").dt.strftime("%d/%m/%Y")
                _df_sla_ot_disp = _df_sla_ot_disp.sort_values("fecha_llamado", ascending=False).rename(
                    columns={"os_fracttal":"OS Fracttal",
                             "fecha_llamado":"Fecha llamado",
                             "fecha_atencion":"Fecha atención",
                             "wo_cierre_ot":"Cierre completo OT",
                             "eds_occim":"Cód. EDS",
                             "eds_nombre":"EDS","cliente":"Cliente","tecnico":"Técnico",
                             "prioridad":"Prioridad","ciudad":"Ciudad","zona_ot":"Zona",
                             "tiempo_res":"Tiempo resolución","umbral_lbl":"Umbral SLA",
                             "pct_sla_ot":"% Uso SLA","estado_sla":"Estado SLA",
                             "n_cotalker":"N° Aviso"})
                st.caption(f"**{len(_df_sla_ot_disp):,}** OTs con fechas de apertura y cierre registradas")
                _show_df(_df_sla_ot_disp, width="stretch", hide_index=True,
                    column_config={
                        "OS Fracttal":          st.column_config.TextColumn(width=110),
                        "N° Aviso":             st.column_config.TextColumn(width=105,
                            help="N° de referencia del cliente: 'No. Aviso' para COPEC / N° Cotalker para ESMAX-Aramco. Vacío = sin referencia registrada."),
                        "Fecha llamado":        st.column_config.TextColumn(width=110),
                        "Fecha atención":       st.column_config.TextColumn(width=110),
                        "Cierre completo OT":   st.column_config.TextColumn(width=140,
                            help="Fecha en que la OT cambió a estado Finalizada (cierre administrativo). Solo informativo — no afecta el cálculo SLA."),
                        "Cód. EDS":          st.column_config.TextColumn(width=100),
                        "EDS":               st.column_config.TextColumn(width=210),
                        "Cliente":           st.column_config.TextColumn(width=90),
                        "Técnico":           st.column_config.TextColumn(width=155),
                        "Prioridad":         st.column_config.TextColumn(width=80),
                        "Ciudad":            st.column_config.TextColumn(width=110),
                        "Zona":              st.column_config.TextColumn(width=85),
                        "Tiempo resolución": st.column_config.TextColumn(width=120),
                        "Umbral SLA":        st.column_config.TextColumn(width=85),
                        "% Uso SLA":         st.column_config.ProgressColumn(
                            label="% Uso SLA", min_value=0, max_value=200, format="%.1f%%",
                            help=">100% = excedió el umbral SLA"),
                        "Estado SLA":        st.column_config.TextColumn(width=110),
                    })
            else:
                st.info("No hay llamados con fechas de apertura y cierre registradas en el período seleccionado.")

        # ══════════════════════════════════════════════════════════════════════
        # SUB-PESTAÑA: SERVICIO TÉCNICO
        # ══════════════════════════════════════════════════════════════════════
        with _tab_tec:
            # ── Filtros fila 1: Empresa / Período / Mes / Prioridad / Cumplimiento ──
            ef1, ef2, ef3, ef4, ef5 = st.columns([1.2, 1.2, 1.8, 1.2, 1.5])
            with ef1:
                sel_emp_t = st.selectbox("Empresa", ["Occimiano", "Elecons", "AUTEC"],
                                         key="tec_emp")
            with ef2:
                sel_trim_t = st.selectbox("Período", _trim_opts, key="tec_trim")
            with ef3:
                if sel_trim_t != "Todos":
                    _trim_m_t = _TRIMESTRES_DEF[sel_trim_t]
                    _meses_t_disp = [l for l in _meses_disp_lbl[1:]
                                     if _MES_ABR_NUM_LL.get(l.split(" ")[0], 0) in _trim_m_t]
                else:
                    _meses_t_disp = _meses_disp_lbl[1:]
                sel_mes_t = st.multiselect("Mes", _meses_t_disp, key="tec_mes",
                                           placeholder="Todos los meses")
            with ef4:
                _pr_opts_t = ["Todas"] + sorted(df_llamados["prioridad"].dropna().unique().tolist())
                sel_pr_t = st.selectbox("Prioridad", _pr_opts_t, key="tec_pr")
            with ef5:
                sel_cu_t = st.selectbox("Cumplimiento SLA", ["Todos","CUMPLE","NO CUMPLE"], key="tec_cu")

            # ── Filtros fila 2: Equipo / Técnico (solo Occimiano) ──────────────
            if sel_emp_t == "Occimiano":
                ef6, ef7, _, _, _ = st.columns([1.5, 1.5, 1.2, 1.2, 1.5])
                with ef6:
                    sel_eq_t = st.selectbox("Equipo", _equipo_opts, key="tec_eq")
                with ef7:
                    _tec_pool_t = sorted(TECNICOS_OCCIMIANO_FULL) if sel_eq_t == "Todos" \
                                  else sorted(_equipo_to_full.get(sel_eq_t, []))
                    sel_tec_t = st.selectbox("Técnico", ["Todos"] + _tec_pool_t,
                                             key=f"tec_tec_{sel_eq_t}")
            else:
                sel_eq_t  = "Todos"
                sel_tec_t = "Todos"

            # ── Aplicar filtros ───────────────────────────────────────────────
            df_lt = df_llamados.copy()
            _fl3 = df_lt["fecha_llamado"]
            if _fl3.dt.tz is not None:
                _fl3 = _fl3.dt.tz_convert(None)
            df_lt["_mes"]   = _fl3.dt.to_period("M").astype(str)
            df_lt["_month"] = _fl3.dt.month.astype("Int64")
            # Empresa primero
            _occa_members_t = {m for g in _equipo_to_full.values() for m in g}
            if sel_emp_t == "Occimiano":
                df_lt = df_lt[df_lt["tecnico"].isin(_occa_members_t)]
            elif sel_emp_t == "Elecons":
                df_lt = df_lt[df_lt["tecnico"].str.contains("ocampo", case=False, na=False)]
            elif sel_emp_t == "AUTEC":
                df_lt = df_lt[df_lt["tecnico"].str.contains("autec", case=False, na=False)]
            if sel_trim_t != "Todos":
                df_lt = df_lt[df_lt["_month"].isin(_TRIMESTRES_DEF[sel_trim_t])]
            if sel_mes_t:
                _periods_t = [_lbl_to_period[l] for l in sel_mes_t if l in _lbl_to_period]
                if _periods_t: df_lt = df_lt[df_lt["_mes"].isin(_periods_t)]
            if sel_eq_t  != "Todos": df_lt = df_lt[df_lt["tecnico"].isin(_equipo_to_full[sel_eq_t])]
            if sel_tec_t != "Todos": df_lt = df_lt[df_lt["tecnico"] == sel_tec_t]
            if sel_pr_t  != "Todas": df_lt = df_lt[df_lt["prioridad"].str.upper() == sel_pr_t.upper()]
            if sel_cu_t  != "Todos": df_lt = df_lt[df_lt["cumplimiento"] == sel_cu_t]

            # ── KPIs ──────────────────────────────────────────────────────────
            _cumple_t   = (df_lt["cumplimiento"] == "CUMPLE").sum()
            _nocumple_t = (df_lt["cumplimiento"] == "NO CUMPLE").sum()
            _pct_t      = round(_cumple_t/(_cumple_t+_nocumple_t)*100,1) if (_cumple_t+_nocumple_t)>0 else 0
            if sel_emp_t == "Occimiano":
                if sel_tec_t != "Todos":
                    _tec_activos_cnt = 1
                elif sel_eq_t != "Todos":
                    _tec_activos_cnt = len(_equipo_to_full.get(sel_eq_t, []))
                else:
                    _tec_activos_cnt = sum(len(v) for v in _equipo_to_full.values())
            else:
                _tec_activos_cnt = int(df_lt["tecnico"].nunique()) if not df_lt.empty else 0
            tk1, tk2, tk3, tk4, tk5 = st.columns(5)
            tk1.metric("Total llamados", f"{len(df_lt):,}")
            tk2.metric("P1 (máquina detenida)", f"{(df_lt['prioridad'].str.upper()=='P1').sum():,}")
            tk3.metric("Cumple SLA", f"{_cumple_t:,}", delta=f"{_pct_t}%")
            tk4.metric("No cumple SLA", f"{_nocumple_t:,}",
                       delta=f"-{100-_pct_t:.1f}%" if (_cumple_t+_nocumple_t)>0 else None,
                       delta_color="inverse")
            tk5.metric("Técnicos activos", str(_tec_activos_cnt))

            st.divider()
            _ll_sig_t = f"{len(df_llamados)}_{sel_emp_t}_{sel_trim_t}_{sel_mes_t}_{sel_eq_t}_{sel_tec_t}_{sel_pr_t}_{sel_cu_t}"

            # ── SECCIÓN: Cumplimiento SLA ─────────────────────────────────────
            st.markdown('<div class="section-header">📊  Cumplimiento SLA</div>', unsafe_allow_html=True)

            # ── Velocímetro (común a todas las empresas) ──────────────────────
            _tgauge_k = f"_fig_tec_gauge_{_current_theme}_{_ll_sig_t}"
            if _tgauge_k not in st.session_state:
                import math as _math_t
                _tg_clr = (
                    "#22c55e" if _pct_t >= 90 else
                    "#84cc16" if _pct_t >= 75 else
                    "#f97316" if _pct_t >= 60 else "#ef4444"
                )
                def _arc_t(vmin, vmax, r_in, r_out, n=80):
                    a0 = _math_t.pi*(1 - vmax/100)
                    a1 = _math_t.pi*(1 - vmin/100)
                    t  = [a0+(a1-a0)*i/(n-1) for i in range(n)]
                    xo = [r_out*_math_t.cos(a) for a in t]
                    yo = [r_out*_math_t.sin(a) for a in t]
                    xi = [r_in *_math_t.cos(a) for a in reversed(t)]
                    yi = [r_in *_math_t.sin(a) for a in reversed(t)]
                    return xo+xi+[xo[0]], yo+yi+[yo[0]]
                _R_OUT_T, _R_IN_T = 1.0, 0.52
                _zones_t = [
                    (0,  10, "#c0392b"),(10, 20, "#e74c3c"),(20, 35, "#e67e22"),
                    (35, 50, "#f39c12"),(50, 65, "#f1c40f"),(65, 75, "#d4e157"),
                    (75, 85, "#a5d63a"),(85, 92, "#66bb6a"),(92, 97, "#43a047"),
                    (97,100, "#2e7d32"),
                ]
                _fig_tgauge = go.Figure()
                for _vn, _vx, _clr in _zones_t:
                    _xz, _yz = _arc_t(_vn, _vx, _R_IN_T, _R_OUT_T)
                    _fig_tgauge.add_trace(go.Scatter(x=_xz, y=_yz, fill="toself",
                        fillcolor=_clr, line=dict(color=_clr, width=0.5),
                        mode="lines", showlegend=False, hoverinfo="skip"))
                for _rb0, _rb1, _rbc in [(_R_OUT_T, _R_OUT_T+0.025,"#2d3436"),
                                          (_R_IN_T-0.025, _R_IN_T,  "#2d3436")]:
                    _xb, _yb = _arc_t(0, 100, _rb0, _rb1)
                    _fig_tgauge.add_trace(go.Scatter(x=_xb, y=_yb, fill="toself",
                        fillcolor=_rbc, line=dict(color=_rbc, width=0),
                        mode="lines", showlegend=False, hoverinfo="skip"))
                _ang_t = _math_t.pi*(1 - _pct_t/100)
                _nl_t, _bw_t = 0.78, 0.025
                _al_t, _ar_t = _ang_t+_math_t.pi/2, _ang_t-_math_t.pi/2
                _fig_tgauge.add_trace(go.Scatter(
                    x=[_bw_t*_math_t.cos(_al_t), _nl_t*_math_t.cos(_ang_t),
                       _bw_t*_math_t.cos(_ar_t), _bw_t*_math_t.cos(_al_t)],
                    y=[_bw_t*_math_t.sin(_al_t), _nl_t*_math_t.sin(_ang_t),
                       _bw_t*_math_t.sin(_ar_t), _bw_t*_math_t.sin(_al_t)],
                    fill="toself", fillcolor="#2d3436", line=dict(color="#2d3436", width=0.5),
                    mode="lines", showlegend=False, hoverinfo="skip"))
                _fig_tgauge.add_trace(go.Scatter(
                    x=[_bw_t*_math_t.cos(_al_t), -0.18*_math_t.cos(_ang_t),
                       _bw_t*_math_t.cos(_ar_t), _bw_t*_math_t.cos(_al_t)],
                    y=[_bw_t*_math_t.sin(_al_t), -0.18*_math_t.sin(_ang_t),
                       _bw_t*_math_t.sin(_ar_t), _bw_t*_math_t.sin(_al_t)],
                    fill="toself", fillcolor="#555", line=dict(color="#555", width=0.5),
                    mode="lines", showlegend=False, hoverinfo="skip"))
                for _hr_t, _fc_t in [(0.10,"#2d3436"),(0.06,"#ffffff")]:
                    _fig_tgauge.add_trace(go.Scatter(
                        x=[_hr_t*_math_t.cos(2*_math_t.pi*i/40) for i in range(41)],
                        y=[_hr_t*_math_t.sin(2*_math_t.pi*i/40) for i in range(41)],
                        fill="toself", fillcolor=_fc_t, line=dict(color=_fc_t),
                        mode="lines", showlegend=False, hoverinfo="skip"))
                for _tv in [0, 25, 50, 75, 90, 100]:
                    _ta_t  = _math_t.pi*(1 - _tv/100)
                    _is90  = _tv == 90
                    _tc_t  = "#e74c3c" if _is90 else "#ecf0f1"
                    _fig_tgauge.add_trace(go.Scatter(
                        x=[(_R_OUT_T+0.07)*_math_t.cos(_ta_t),
                           (1.18 if _is90 else 1.13)*_math_t.cos(_ta_t)],
                        y=[(_R_OUT_T+0.07)*_math_t.sin(_ta_t),
                           (1.18 if _is90 else 1.13)*_math_t.sin(_ta_t)],
                        line=dict(color=_tc_t, width=3 if _is90 else 2),
                        mode="lines", showlegend=False, hoverinfo="skip"))
                    _fig_tgauge.add_annotation(
                        x=1.30*_math_t.cos(_ta_t), y=1.30*_math_t.sin(_ta_t),
                        text=f"<b>{_tv}%✓</b>" if _is90 else f"{_tv}%",
                        showarrow=False,
                        font=dict(size=10 if _is90 else 9, color=_tc_t))
                # Título dinámico según contexto
                if sel_emp_t == "Occimiano":
                    _tg_lbl = (sel_tec_t if sel_tec_t != "Todos"
                               else (sel_eq_t if sel_eq_t != "Todos"
                                     else "Occimiano — todos los equipos"))
                else:
                    _tg_lbl = sel_emp_t
                _fig_tgauge.add_annotation(x=0, y=-0.15,
                    text=f"<b>{_pct_t:.1f}%</b>",
                    showarrow=False, font=dict(size=36, color=_tg_clr))
                _fig_tgauge.add_annotation(x=0, y=-0.38,
                    text=f"{int(_cumple_t)} cumple · {int(_nocumple_t)} no cumple",
                    showarrow=False, font=dict(size=11, color=_t["muted"]))
                _fig_tgauge.add_annotation(x=0, y=1.30,
                    text=f"<b>Cumplimiento SLA</b>",
                    showarrow=False, font=dict(size=14, color=_t["text"]))
                _fig_tgauge.update_layout(
                    xaxis=dict(range=[-1.2, 1.2], visible=False, scaleanchor="y"),
                    yaxis=dict(range=[-0.50, 1.42], visible=False),
                    height=400, margin=dict(t=5, b=5, l=5, r=5),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                )
                st.session_state[_tgauge_k] = _fig_tgauge
            _fig_tgauge_show = st.session_state.get(_tgauge_k)

            # ── Comparativo por empresa (tabla resumen) ───────────────────────
            _df_comp = df_llamados.copy()
            _fl_comp = _df_comp["fecha_llamado"]
            if _fl_comp.dt.tz is not None:
                _fl_comp = _fl_comp.dt.tz_convert(None)
            _df_comp["_month"] = _fl_comp.dt.month.astype("Int64")
            _df_comp["_mes"]   = _fl_comp.dt.to_period("M").astype(str)
            if sel_trim_t != "Todos":
                _df_comp = _df_comp[_df_comp["_month"].isin(_TRIMESTRES_DEF[sel_trim_t])]
            if sel_mes_t:
                _periods_comp = [_lbl_to_period[l] for l in sel_mes_t if l in _lbl_to_period]
                if _periods_comp:
                    _df_comp = _df_comp[_df_comp["_mes"].isin(_periods_comp)]
            if sel_pr_t != "Todas":
                _df_comp = _df_comp[_df_comp["prioridad"].str.upper() == sel_pr_t.upper()]
            _df_occ_c  = _df_comp[_df_comp["tecnico"].isin(_occa_members_t)]
            _df_autec_c = _df_comp[_df_comp["tecnico"].str.contains("autec", case=False, na=False)]
            _df_elec_c  = _df_comp[_df_comp["tecnico"].str.contains("ocampo", case=False, na=False)]
            _comp_rows = []
            for _emp_lbl, _df_e in [("Occimiano", _df_occ_c), ("Elecons", _df_elec_c), ("AUTEC", _df_autec_c)]:
                _cc  = int((_df_e["cumplimiento"] == "CUMPLE").sum())
                _cnc = int((_df_e["cumplimiento"] == "NO CUMPLE").sum())
                _ct  = _cc + _cnc
                _cpct = round(_cc / _ct * 100, 1) if _ct > 0 else 0.0
                _comp_rows.append({
                    "Empresa":    _emp_lbl,
                    "Total":      _ct,
                    "Cumple":     _cc,
                    "No Cumple":  _cnc,
                    "% SLA":      f"{_cpct}%" if _ct > 0 else "—",
                })
            _comp_df = pd.DataFrame(_comp_rows)

            # ── Layout: velocímetro + tabla comparativa empresa ───────────────
            _tc0, _tc1 = st.columns([0.9, 1.6])
            with _tc0:
                if _fig_tgauge_show:
                    st.plotly_chart(_fig_tgauge_show, width="stretch")
                else:
                    st.info("Sin datos para el período seleccionado.")
            with _tc1:
                if not _comp_df.empty:
                    _muted_c = _t["muted"]
                    st.markdown(
                        f"<p style='margin:0 0 6px 0; font-size:13px; font-weight:600;"
                        f" color:{_muted_c};'>Resumen por empresa</p>",
                        unsafe_allow_html=True,
                    )
                    _, _ctbl, _ = st.columns([0.05, 0.9, 0.05])
                    with _ctbl:
                        _show_df(_comp_df, hide_index=True, use_container_width=True,
                            column_config={
                                "Empresa":    st.column_config.TextColumn(width=85),
                                "Total":      st.column_config.NumberColumn(format="%d", width=50),
                                "Cumple":     st.column_config.NumberColumn(format="%d", width=55),
                                "No Cumple":  st.column_config.NumberColumn(format="%d", width=72),
                                "% SLA":      st.column_config.TextColumn(width=55),
                            })

            # ── Mini-tabla resumen (solo Occimiano) ───────────────────────────
            if sel_emp_t == "Occimiano":
                _all_mbs_t2 = {m for g in _equipo_to_full.values() for m in g}
                _df_lt_rs   = df_lt[df_lt["tecnico"].isin(_all_mbs_t2)]
                if sel_tec_t != "Todos":
                    _rs_grp   = [sel_tec_t]
                    _rs_label = "Técnico"
                    _rs_mask  = lambda n: _df_lt_rs[_df_lt_rs["tecnico"] == n]
                elif sel_eq_t != "Todos":
                    _rs_grp   = _equipo_to_full.get(sel_eq_t, [])
                    _rs_label = "Técnico"
                    _rs_mask  = lambda n: _df_lt_rs[_df_lt_rs["tecnico"] == n]
                else:
                    _rs_grp   = list(_equipo_to_full.keys())
                    _rs_label = "Equipo"
                    _rs_mask  = lambda n: _df_lt_rs[_df_lt_rs["tecnico"].isin(_equipo_to_full[n])]
                _rs_rows = []
                for _rn in _rs_grp:
                    _drs = _rs_mask(_rn)
                    _rc  = (_drs["cumplimiento"] == "CUMPLE").sum()
                    _rnc = (_drs["cumplimiento"] == "NO CUMPLE").sum()
                    _rt  = _rc + _rnc
                    _rs_rows.append({
                        _rs_label: _rn,
                        "Total": len(_drs),
                        "Cumple": int(_rc),
                        "No Cumple": int(_rnc),
                        "% SLA": f"{round(_rc/_rt*100,1)}%" if _rt > 0 else "—",
                    })
                if _rs_rows:
                    _show_df(pd.DataFrame(_rs_rows), hide_index=True, width="stretch",
                        column_config={
                            _rs_label:   st.column_config.TextColumn(width=140),
                            "Total":     st.column_config.NumberColumn(format="%d", width=60),
                            "Cumple":    st.column_config.NumberColumn(format="%d", width=70),
                            "No Cumple": st.column_config.NumberColumn(format="%d", width=80),
                            "% SLA":     st.column_config.TextColumn(width=65),
                        })

            # ── GRÁFICO 2: Llamados por técnico (stacked prioridad) ───────────
            st.divider()
            st.markdown('<div class="section-header">👷  Llamados correctivos por técnico</div>', unsafe_allow_html=True)
            _tbar_k = f"_fig_tec_bar_tec_{_current_theme}_{_ll_sig_t}"
            if _tbar_k not in st.session_state:
                _all_mbs3 = {m for g in _equipo_to_full.values() for m in g}
                _df_bar = df_lt[df_lt["tecnico"].isin(_all_mbs3)]
                if not _df_bar.empty:
                    _tec_prio = _df_bar.groupby(["tecnico","prioridad"]).size().reset_index(name="llamados")
                    _tec_ord  = (_tec_prio.groupby("tecnico")["llamados"].sum()
                                 .sort_values(ascending=False).index.tolist())
                    _fig_bar = px.bar(
                        _tec_prio, x="tecnico", y="llamados", color="prioridad",
                        color_discrete_map=prio_colors,
                        title="Llamados correctivos por técnico",
                        barmode="stack",
                        category_orders={"tecnico": _tec_ord},
                        labels={"tecnico":"Técnico","llamados":"Llamados","prioridad":"Prioridad"},
                        text_auto=True,
                    )
                    _fig_bar.update_layout(xaxis_tickangle=-30, legend_title="Prioridad",
                                           xaxis_title="", height=430, margin=dict(t=50,b=80))
                    _apply_plot_theme(_fig_bar)
                    st.session_state[_tbar_k] = _fig_bar
                else:
                    st.session_state[_tbar_k] = None
            _fig_bar_show = st.session_state.get(_tbar_k)
            if _fig_bar_show:
                st.plotly_chart(_fig_bar_show, width="stretch")
            else:
                st.info("Sin datos para el período seleccionado.")

            # ── GRÁFICO 3: Evolución temporal ─────────────────────────────────
            st.divider()
            st.markdown('<div class="section-header">📈  Evolución temporal</div>', unsafe_allow_html=True)
            _tev_grain = st.radio("Granularidad", ["Mensual","Trimestral"],
                                  horizontal=True, key="tec_grain")
            _tev_k = f"_fig_tec_evol_{_current_theme}_{_ll_sig_t}_{_tev_grain}"
            if _tev_k not in st.session_state:
                _all_mbs4 = {m for g in _equipo_to_full.values() for m in g}
                _df_ev = df_lt[df_lt["tecnico"].isin(_all_mbs4)].copy()

                if _tev_grain == "Mensual":
                    _df_ev["periodo_lbl"] = _df_ev["_mes"].apply(_ym_a_lbl)
                    _periodo_ord = [_ym_a_lbl(m) for m in sorted(_df_ev["_mes"].dropna().unique())]
                else:
                    def _trim_lbl(m):
                        try:
                            mn = int(str(m).split("-")[1])
                            t  = (mn - 1) // 3 + 1
                            yr = str(m).split("-")[0][2:]
                            return f"T{t} '{yr}"
                        except Exception:
                            return str(m)
                    _df_ev["periodo_lbl"] = _df_ev["_mes"].apply(_trim_lbl)
                    _uniq_p = sorted(_df_ev["_mes"].dropna().unique())
                    _seen_p = []
                    _periodo_ord = []
                    for _mp in _uniq_p:
                        _lp = _trim_lbl(_mp)
                        if _lp not in _seen_p:
                            _seen_p.append(_lp)
                            _periodo_ord.append(_lp)

                if sel_tec_t != "Todos":
                    _df_ev["_grp_col"] = _df_ev["tecnico"]
                    _grp_title = "Técnico"
                elif sel_eq_t != "Todos":
                    _df_ev["_grp_col"] = _df_ev["tecnico"]
                    _grp_title = "Técnico"
                else:
                    def _tec_to_eq(tec):
                        for _eq, _mbs in _equipo_to_full.items():
                            if tec in _mbs: return _eq
                        return "Otros"
                    _df_ev["_grp_col"] = _df_ev["tecnico"].apply(_tec_to_eq)
                    _grp_title = "Equipo"

                if not _df_ev.empty:
                    _monthly_t = (_df_ev.groupby(["periodo_lbl","_grp_col"])
                                  .size().reset_index(name="llamados")
                                  .rename(columns={"_grp_col": _grp_title}))
                    _fig_ev = px.bar(
                        _monthly_t, x="periodo_lbl", y="llamados", color=_grp_title,
                        title=f"Llamados por {'mes' if _tev_grain == 'Mensual' else 'trimestre'} "
                              f"y {_grp_title.lower()}",
                        barmode="group",
                        category_orders={"periodo_lbl": _periodo_ord},
                        labels={"periodo_lbl": "Período", "llamados": "Llamados"},
                    )
                    _fig_ev.update_layout(xaxis_title="", yaxis_title="Llamados",
                                          xaxis_type="category", height=400,
                                          margin=dict(t=50,b=10))
                    _apply_plot_theme(_fig_ev)
                    st.session_state[_tev_k] = _fig_ev
                else:
                    st.session_state[_tev_k] = None
            _fig_ev_show = st.session_state.get(_tev_k)
            if _fig_ev_show:
                st.plotly_chart(_fig_ev_show, width="stretch")
            else:
                st.info("Sin datos para el período seleccionado.")

            # ── Detalle de llamados ───────────────────────────────────────────
            st.divider()
            st.markdown('<div class="section-header">Detalle de llamados</div>', unsafe_allow_html=True)
            df_det = df_lt.copy()
            if TECNICOS_OCCIMIANO_FULL:
                df_det = df_det[df_det["tecnico"].isin(TECNICOS_OCCIMIANO_FULL)]

            def _td_horas(v):
                if v is None or (hasattr(v,"__class__") and v.__class__.__name__ == "NaTType"):
                    return float("nan")
                try:
                    return pd.to_timedelta(v).total_seconds() / 3600
                except Exception:
                    return float("nan")

            def _fmt_h_min(h) -> str:
                if pd.isna(h) or h < 0: return "⏳ Pendiente"
                hh = int(h); mm = int(round((h % 1) * 60))
                if hh == 0: return f"{mm}min"
                if mm == 0: return f"{hh}h"
                return f"{hh}h {mm:02d}min"

            def _zona_key_det(z: str) -> str:
                z = str(z).upper().strip()
                if z in ("RM","R.M.") or any(k in z for k in ["SANTIAGO","METRO"]): return "Santiago"
                if any(k in z for k in ["NORTE","SUR","CENTRO","REGION","REGIONES","RG"]): return "Regiones"
                return "Santiago"

            df_det["_hrs_real"] = df_det.get("tiempo_resp_real", pd.Series(dtype=object,index=df_det.index)).apply(_td_horas)
            df_det["_zona_key"] = df_det.get("zona", pd.Series("",index=df_det.index)).apply(_zona_key_det)
            df_det["_sla_h"]    = [
                (SLA_HOURS.get(str(c), SLA_DEFAULT).get(str(p).upper(), {}).get(str(z), None))
                for c, p, z in zip(
                    df_det.get("cliente",  pd.Series("", index=df_det.index)),
                    df_det.get("prioridad",pd.Series("", index=df_det.index)),
                    df_det["_zona_key"],
                )
            ]
            df_det["_pct_sla"] = (df_det["_hrs_real"] / df_det["_sla_h"] * 100).where(
                df_det["_hrs_real"].notna() & pd.Series([pd.notna(x) for x in df_det["_sla_h"]], index=df_det.index)
            )
            df_det["Fecha Llamado"]    = pd.to_datetime(df_det["fecha_llamado"], errors="coerce").dt.strftime("%d/%m/%Y")
            df_det["Tiempo Respuesta"] = df_det["_hrs_real"].apply(_fmt_h_min)
            df_det["SLA"]              = [f"{int(h)}h" if pd.notna(h) else "—" for h in df_det["_sla_h"]]
            df_det["% SLA"]            = df_det["_pct_sla"]
            df_det["Cumplimiento"]     = df_det.get("cumplimiento", pd.Series("—", index=df_det.index)).apply(
                lambda v: "✅" if str(v).upper() == "CUMPLE" else ("❌" if str(v).upper() == "NO CUMPLE" else "—")
            )
            _det_cols = [c for c in ["Fecha Llamado","eds_occim","eds_nombre","cliente","prioridad",
                                     "tecnico","Tiempo Respuesta","SLA","% SLA","Cumplimiento","os_fracttal"]
                         if c in df_det.columns]
            df_det_disp = (df_det[_det_cols]
                           .rename(columns={"eds_occim":"Cód. EDS","eds_nombre":"EDS","cliente":"Cliente",
                                            "prioridad":"Prioridad","tecnico":"Técnico","os_fracttal":"OS Fracttal"})
                           .sort_values("Fecha Llamado", ascending=False))
            st.caption(f"**{len(df_det_disp):,}** llamados atendidos por técnicos Occimiano")
            _show_df(df_det_disp, width="stretch", hide_index=True,
                column_config={
                    "Fecha Llamado":    st.column_config.TextColumn(width=105),
                    "Cód. EDS":         st.column_config.TextColumn(width=90),
                    "EDS":              st.column_config.TextColumn(width=220),
                    "Cliente":          st.column_config.TextColumn(width=90),
                    "Prioridad":        st.column_config.TextColumn(width=75),
                    "Técnico":          st.column_config.TextColumn(width=180),
                    "Tiempo Respuesta": st.column_config.TextColumn(width=120),
                    "SLA":              st.column_config.TextColumn(width=65),
                    "% SLA":            st.column_config.ProgressColumn(
                        label="% SLA usado", min_value=0, max_value=200, format="%.1f%%"),
                    "Cumplimiento":     st.column_config.TextColumn(width=90),
                    "OS Fracttal":      st.column_config.TextColumn(width=105),
                })


# ─────────────────────────────────────────────────────────────────────────────
# PÁGINA 3: ESTACIONES DE SERVICIO
# ─────────────────────────────────────────────────────────────────────────────
elif _page == _NAV_PAGES[3]:

    # ── Datos base ──────────────────────────────────────────────────────────
    import datetime as _dt
    import plotly.graph_objects as go
    import plotly.express as px
    _hoy        = pd.Timestamp.now()
    _cur_year   = _hoy.year
    _cur_month  = _hoy.month
    _MES_ES     = {1:"Ene",2:"Feb",3:"Mar",4:"Abr",5:"May",6:"Jun",
                   7:"Jul",8:"Ago",9:"Sep",10:"Oct",11:"Nov",12:"Dic"}
    _MESES_DISP = [f"{_cur_year}-{m:02d}" for m in range(1, _cur_month + 1)]

    # Work orders con eds_occim ──────────────────────────────────────────────
    raw_wo_eds = load_work_orders_supabase()
    _wo_eds_sig = str(len(raw_wo_eds))

    def _build_wo_eds():
        _df = build_work_orders_df(raw_wo_eds)
        _fmap = {
            str(wo.get("wo_folio")): wo.get("groups_2_description")
            for wo in raw_wo_eds
            if wo.get("groups_2_description")
        }
        _df["eds_occim"] = _df["folio"].astype(str).map(_fmap)
        _cd = _df["creation_date"].dt.tz_convert(None) if _df["creation_date"].dt.tz is not None else _df["creation_date"]
        _df["mes_str"] = _cd.dt.to_period("M").astype(str)
        _df["mes_num"] = _cd.dt.month
        _df["year"]    = _cd.dt.year
        return _df

    df_wo_eds_full = _sc("df_wo_eds_v1", _wo_eds_sig, _build_wo_eds)

    # Filtrar a clientes EDS y año actual ────────────────────────────────────
    _EDS_CLIENTS = {"COPEC", "Aramco (Esmax)", "ESMAX (Aramco)", "SHELL (Enex)"}
    df_wo_cur = df_wo_eds_full[
        df_wo_eds_full["client"].isin(_EDS_CLIENTS) &
        (df_wo_eds_full["year"] == _cur_year)
    ].copy()

    # Llamados correctivos ───────────────────────────────────────────────────
    if not df_llamados.empty:
        df_ll_cur = df_llamados[
            df_llamados["cliente"].isin(_EDS_CLIENTS) &
            (df_llamados.get("Año", pd.Series(dtype=int)) == _cur_year
             if "Año" in df_llamados.columns else
             pd.to_datetime(df_llamados["fecha_llamado"], errors="coerce").dt.year == _cur_year)
        ].copy()
        if "Mes" not in df_ll_cur.columns:
            df_ll_cur["Mes"] = pd.to_datetime(df_ll_cur["fecha_llamado"], errors="coerce").dt.month
    else:
        df_ll_cur = pd.DataFrame()

    # EDS master ─────────────────────────────────────────────────────────────
    _CLIENTES_EXCLUIR = {"BLANDFORD","OCCIMIANO","PARTICULAR","TERPEL COL","TERPEL","ABASTIBLE"}
    df_eds_activas = df_eds[df_eds["activa"]].copy()
    df_eds_activas = df_eds_activas[
        ~df_eds_activas["cliente"].str.strip().str.upper().isin(_CLIENTES_EXCLUIR)
    ].copy()

    # Colores empresa ────────────────────────────────────────────────────────
    _CL_COLORS = {
        "COPEC":          {"tab":"🔴","pm":"#CC0000","cm":"#F4A7A9","accent":"#CC0000","label":"COPEC"},
        "SHELL (Enex)":   {"tab":"🟡","pm":"#D4A800","cm":"#FAE57A","accent":"#D4A800","label":"Shell (Enex)"},
        "Aramco (Esmax)": {"tab":"🟢","pm":"#16A34A","cm":"#86EFAC","accent":"#16A34A","label":"Aramco (Esmax)"},
        "ESMAX (Aramco)": {"tab":"🟢","pm":"#16A34A","cm":"#86EFAC","accent":"#16A34A","label":"Aramco (Esmax)"},  # alias legacy
    }

    # ── Título y tabs ────────────────────────────────────────────────────────
    _hdr("Estaciones de Servicio")
    st.divider()

    _tabs_eds = st.tabs([
        "🔴  COPEC",
        "🟢  Aramco (Esmax)",
        "🟡  Shell (Enex)",
    ])

    # ════════════════════════════════════════════════════════════════════════
    # HELPER: dona de tipos de falla + desglose de causas
    # ════════════════════════════════════════════════════════════════════════
    _FALLA_PAL = ["#EF4444","#F59E0B","#6B7280","#3B82F6","#8B5CF6","#10B981","#EC4899","#14B8A6"]

    def _limpiar_tipo(t: str) -> str:
        """Quita prefijos numéricos: '01.- F.N.A.O.' → 'F.N.A.O.'"""
        import re as _re2
        return _re2.sub(r"^\d+\.\-?\s*", "", str(t)).strip()

    def _render_fallas_panel(df_src: "pd.DataFrame", key_sfx: str):
        """Dona con tipos de falla + panel de causas por tipo."""
        if df_src.empty or "failure_type" not in df_src.columns:
            st.caption("Sin datos de tipo de falla disponibles.")
            return
        _df_ft = df_src[df_src["failure_type"].str.strip() != ""].copy()
        if _df_ft.empty:
            st.caption("Sin datos de tipo de falla disponibles.")
            return

        _fallas_cnt = _df_ft["failure_type"].value_counts().head(6)
        _total      = int(_fallas_cnt.sum())
        _has_cause  = "failure_cause" in _df_ft.columns

        _dona_col, _det_col = st.columns([2, 3])

        with _dona_col:
            _labels_short = [_limpiar_tipo(f) for f in _fallas_cnt.index]
            _fig_dona = go.Figure(go.Pie(
                labels=_labels_short,
                values=_fallas_cnt.values,
                hole=0.52,
                marker=dict(colors=_FALLA_PAL[:len(_fallas_cnt)],
                            line=dict(color="rgba(0,0,0,0.08)", width=1)),
                textinfo="percent",
                textposition="outside",
                hovertemplate="%{label}<br>%{value} OTs · %{percent}<extra></extra>",
                sort=False,
            ))
            _fig_dona.update_layout(
                height=300,
                margin=dict(l=0, r=0, t=10, b=10),
                showlegend=True,
                legend=dict(orientation="v", x=1.0, y=0.5,
                            font=dict(size=11), bgcolor="rgba(0,0,0,0)"),
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font=dict(color=_t["text"]),
                annotations=[dict(
                    text=f"<b>{_total}</b><br><span style='font-size:11px'>OTs</span>",
                    x=0.5, y=0.5, showarrow=False,
                    font=dict(size=16, color=_t["text"]),
                )],
            )
            st.plotly_chart(_fig_dona, use_container_width=True, key=f"dona_{key_sfx}")

        with _det_col:
            st.markdown(
                f'<div style="font-size:0.78rem;font-weight:700;color:{_t["muted"]};'
                f'letter-spacing:0.05em;margin-bottom:8px;">DESGLOSE POR TIPO</div>',
                unsafe_allow_html=True,
            )
            for _i, (_ftype, _cnt) in enumerate(_fallas_cnt.items()):
                _pct   = _cnt / _total * 100
                _color = _FALLA_PAL[_i % len(_FALLA_PAL)]
                _short = _limpiar_tipo(_ftype)

                with st.expander(f"{_short}  —  {_cnt} OTs · {_pct:.1f}%"):
                    if _has_cause:
                        _df_tipo  = _df_ft[_df_ft["failure_type"] == _ftype]
                        _causas_v = (
                            _df_tipo[_df_tipo["failure_cause"].str.strip() != ""]
                            ["failure_cause"].value_counts().head(4)
                        )
                        if not _causas_v.empty:
                            for _cn, _cc in _causas_v.items():
                                _cp = _cc / _cnt * 100
                                _bar_w = int(_cp)
                                st.markdown(
                                    f'<div style="padding:3px 6px 3px 10px;">'
                                    f'<div style="display:flex;justify-content:space-between;'
                                    f'font-size:0.76rem;color:{_t["muted"]};">'
                                    f'<span>↳ {_cn}</span>'
                                    f'<span style="font-weight:600;color:{_t["text"]};">'
                                    f'{_cc} ({_cp:.0f}%)</span></div>'
                                    f'<div style="height:3px;border-radius:2px;margin-top:2px;'
                                    f'background:{_color};opacity:0.35;width:{_bar_w}%;"></div>'
                                    f'</div>',
                                    unsafe_allow_html=True,
                                )
                        else:
                            st.caption("Sin causa registrada")

    # ════════════════════════════════════════════════════════════════════════
    # FUNCIÓN: renderizar pestaña por empresa
    # ════════════════════════════════════════════════════════════════════════
    def _render_eds_tab(company: str, tab_idx: int):
        _col = _CL_COLORS[company]
        _ck  = company.replace(" ","_").replace("(","").replace(")","")  # key prefix

        df_ll_c  = df_ll_cur[df_ll_cur["cliente"] == company].copy()  if not df_ll_cur.empty  else pd.DataFrame()
        df_wo_c  = df_wo_cur[df_wo_cur["client"]  == company].copy()
        df_eds_c = df_eds_activas[df_eds_activas["cliente"].str.contains(
            company.split()[0], case=False, na=False)].copy()

        # ── Filtros ──────────────────────────────────────────────────────────
        _frow1, _frow2 = st.columns([2, 3])
        with _frow1:
            _mes_sel = st.multiselect(
                "Mes (dejar vacío = año completo)",
                options=_MESES_DISP,
                default=[],
                key=f"eds_mes_{_ck}",
                format_func=lambda m: f"{_MES_ES[int(m.split('-')[1])]} {m.split('-')[0]}",
            )
        _meses_activos = _mes_sel if _mes_sel else _MESES_DISP

        # Opciones EDS desde df_llamados (tienen eds_occim y eds_nombre)
        if not df_ll_c.empty and "eds_occim" in df_ll_c.columns and "eds_nombre" in df_ll_c.columns:
            _eds_opts_df = (
                df_ll_c[["eds_occim","eds_nombre"]]
                .dropna(subset=["eds_occim","eds_nombre"])
                .drop_duplicates()
                .sort_values("eds_nombre")
            )
            _eds_nombre_to_code = dict(zip(_eds_opts_df["eds_nombre"], _eds_opts_df["eds_occim"]))
            _eds_opts_list = ["Todas"] + list(_eds_opts_df["eds_nombre"])
        else:
            _eds_nombre_to_code = {}
            _eds_opts_list = ["Todas"]
        with _frow2:
            _eds_sel_nombre = st.selectbox(
                "EDS específica",
                _eds_opts_list,
                key=f"eds_sel_{_ck}",
            )
        _eds_sel_code = _eds_nombre_to_code.get(_eds_sel_nombre) if _eds_sel_nombre != "Todas" else None

        # ── Aplicar filtros ──────────────────────────────────────────────────
        def _filtrar_ll(df):
            if df.empty: return df
            df = df[df["mes_str"].isin(_meses_activos)] if "mes_str" in df.columns else df[
                pd.to_datetime(df["fecha_llamado"], errors="coerce").dt.to_period("M").astype(str).isin(_meses_activos)]
            if _eds_sel_code:
                df = df[df["eds_occim"] == _eds_sel_code]
            return df

        def _filtrar_wo(df):
            if df.empty: return df
            df = df[df["mes_str"].isin(_meses_activos)]
            if _eds_sel_code:
                df = df[df["eds_occim"] == _eds_sel_code]
            return df

        # Enrich df_ll_c with mes_str if missing
        if not df_ll_c.empty and "mes_str" not in df_ll_c.columns:
            df_ll_c["mes_str"] = pd.to_datetime(df_ll_c["fecha_llamado"], errors="coerce").dt.to_period("M").astype(str)

        df_ll_f  = _filtrar_ll(df_ll_c)
        df_pm_f  = _filtrar_wo(df_wo_c[df_wo_c["maint_type"] == "Preventiva"])
        df_cm_f  = _filtrar_wo(df_wo_c[df_wo_c["maint_type"] == "Correctiva"])

        # ── KPI cards ────────────────────────────────────────────────────────
        _n_eds_atend   = df_ll_f["eds_occim"].nunique()    if not df_ll_f.empty and "eds_occim" in df_ll_f.columns else 0
        _n_llamados    = df_ll_f["n_llamado"].nunique()    if not df_ll_f.empty and "n_llamado" in df_ll_f.columns else len(df_ll_f)
        _n_pms         = df_pm_f["folio"].nunique()        if not df_pm_f.empty else 0
        _pct_sla       = None
        if not df_ll_f.empty and "cumplimiento" in df_ll_f.columns:
            _tot_sla   = (df_ll_f["cumplimiento"] != "SIN DATOS").sum()
            _cumple    = (df_ll_f["cumplimiento"] == "CUMPLE").sum()
            _pct_sla   = f"{_cumple/_tot_sla*100:.1f}%" if _tot_sla > 0 else "—"
        _ultima_fecha  = None
        if not df_ll_f.empty and "fecha_llamado" in df_ll_f.columns:
            _ult = pd.to_datetime(df_ll_f["fecha_llamado"], errors="coerce").max()
            _ultima_fecha = _ult.strftime("%d/%m/%Y") if pd.notna(_ult) else "—"

        _kc1, _kc2, _kc3, _kc4, _kc5 = st.columns(5)
        _kc1.metric("EDS atendidas",        _n_eds_atend)
        _kc2.metric("Llamados correctivos", _n_llamados)
        _kc3.metric("PMs realizados",       _n_pms)
        _kc4.metric("% SLA cumplido",       _pct_sla if _pct_sla else "—")
        _kc5.metric("Última atención",      _ultima_fecha if _ultima_fecha else "—")

        # ── Gráfico PM vs CM mensual ─────────────────────────────────────────
        # Filtrar por EDS seleccionada (fix: antes usaba datos sin filtrar)
        _pm_chart_src = df_wo_c[df_wo_c["maint_type"] == "Preventiva"].copy()
        _cm_chart_src = df_ll_c.copy()
        if _eds_sel_code:
            if not _pm_chart_src.empty and "eds_occim" in _pm_chart_src.columns:
                _pm_chart_src = _pm_chart_src[_pm_chart_src["eds_occim"] == _eds_sel_code]
            if not _cm_chart_src.empty and "eds_occim" in _cm_chart_src.columns:
                _cm_chart_src = _cm_chart_src[_cm_chart_src["eds_occim"] == _eds_sel_code]

        _pm_by_m = (
            _pm_chart_src[_pm_chart_src["mes_str"].isin(_MESES_DISP) if "mes_str" in _pm_chart_src.columns else pd.Series(True, index=_pm_chart_src.index)]
            .groupby("mes_num")["folio"].nunique()
            .reset_index()
            .rename(columns={"mes_num":"mes","folio":"PM"})
        ) if not _pm_chart_src.empty else pd.DataFrame(columns=["mes","PM"])

        _cm_by_m = (
            _cm_chart_src[_cm_chart_src["Mes"].isin(range(1, _cur_month+1))]
            .groupby("Mes")["n_llamado"].count()
            .reset_index()
            .rename(columns={"Mes":"mes","n_llamado":"CM"})
        ) if not _cm_chart_src.empty and "Mes" in _cm_chart_src.columns else pd.DataFrame(columns=["mes","CM"])

        _chart_base = pd.DataFrame({"mes": list(range(1, _cur_month+1))})
        _chart_df = _chart_base.merge(_pm_by_m, on="mes", how="left").merge(_cm_by_m, on="mes", how="left").fillna(0)
        _chart_df["mes_lbl"] = _chart_df["mes"].map(_MES_ES)

        import plotly.graph_objects as go
        # Etiquetas: mostrar count solo si > 0
        _pm_txt = [str(int(v)) if v > 0 else "" for v in _chart_df["PM"]]
        _cm_txt = [str(int(v)) if v > 0 else "" for v in _chart_df["CM"]]
        _fig_main = go.Figure()
        _fig_main.add_trace(go.Bar(
            x=_chart_df["mes_lbl"], y=_chart_df["PM"],
            name="Preventivo", marker_color=_col["cm"], opacity=0.92,
            text=_pm_txt, textposition="inside", textfont=dict(color="#555555", size=12),
        ))
        _fig_main.add_trace(go.Bar(
            x=_chart_df["mes_lbl"], y=_chart_df["CM"],
            name="Correctivo (llamados)", marker_color=_col["pm"], opacity=0.92,
            text=_cm_txt, textposition="inside", textfont=dict(color="white", size=12),
        ))
        _fig_main.update_layout(
            barmode="stack",
            title=dict(text=f"Mantenciones {_cur_year} — {_col['label'] if _eds_sel_nombre == 'Todas' else _eds_sel_nombre}", font_size=14),
            height=340,
            margin=dict(l=0, r=0, t=40, b=0),
            legend=dict(orientation="h", y=-0.15),
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font_color=_t["text"],
            xaxis=dict(gridcolor=_t["border"]),
            yaxis=dict(gridcolor=_t["border"], title="N° mantenciones"),
        )
        st.plotly_chart(_fig_main, use_container_width=True, key=f"chart_main_{_ck}_{_eds_sel_nombre}")

        # ── SECCIÓN CONDICIONAL ──────────────────────────────────────────────
        if _eds_sel_nombre == "Todas":
            # ── TOP 5 EDS con más llamados ───────────────────────────────────
            st.markdown(
                f'<div style="font-size:1.0rem;font-weight:700;color:{_t["text"]};'
                f'margin:12px 0 8px 0;border-bottom:2px solid {_col["accent"]};'
                f'padding-bottom:4px;">📊 Top 5 EDS · Más llamados correctivos ({", ".join([_MES_ES[int(m.split("-")[1])] for m in _meses_activos])})</div>',
                unsafe_allow_html=True,
            )
            if not df_ll_f.empty and "eds_occim" in df_ll_f.columns:
                _top5_data = (
                    df_ll_f.groupby(["eds_occim","eds_nombre"], dropna=True)
                    .agg(
                        llamados=("n_llamado","count"),
                        ultimo_llamado=("fecha_llamado","max"),
                    )
                    .reset_index()
                    .sort_values("llamados", ascending=False)
                    .head(5)
                )

                _col_chart, _col_detail = st.columns([3, 2])
                with _col_chart:
                    _fig_top5 = go.Figure(go.Bar(
                        x=_top5_data["llamados"],
                        y=_top5_data["eds_nombre"],
                        orientation="h",
                        marker_color=_col["accent"],
                        text=_top5_data["llamados"],
                        textposition="outside",
                    ))
                    _fig_top5.update_layout(
                        height=280, margin=dict(l=0,r=40,t=10,b=0),
                        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                        font_color=_t["text"],
                        xaxis=dict(gridcolor=_t["border"]),
                        yaxis=dict(autorange="reversed"),
                    )
                    st.plotly_chart(_fig_top5, use_container_width=True, key=f"chart_top5_{_ck}")

                with _col_detail:
                    for _, _r5 in _top5_data.iterrows():
                        _ult5 = pd.to_datetime(_r5["ultimo_llamado"], errors="coerce")
                        _ult5_str = _ult5.strftime("%d/%m/%Y") if pd.notna(_ult5) else "—"
                        # Falla más frecuente para esa EDS desde df_wo
                        _fallas5 = pd.Series(dtype=str)
                        if not df_wo_c.empty and "failure_type" in df_wo_c.columns and "eds_occim" in df_wo_c.columns:
                            _fallas5 = df_wo_c[
                                (df_wo_c["eds_occim"] == _r5["eds_occim"]) &
                                (df_wo_c["failure_type"].str.strip() != "")
                            ]["failure_type"].value_counts()
                        _falla_top = _fallas5.index[0] if not _fallas5.empty else "—"
                        st.markdown(
                            f'<div style="background:{_t["card"]};border:1px solid {_t["border"]};'
                            f'border-radius:8px;padding:8px 10px;margin-bottom:6px;font-size:0.80rem;">'
                            f'<b style="color:{_col["accent"]};">{_r5["eds_nombre"]}</b><br>'
                            f'<span style="color:{_t["muted"]};">Llamados: </span><b>{int(_r5["llamados"])}</b> · '
                            f'<span style="color:{_t["muted"]};">Último: </span>{_ult5_str}<br>'
                            f'<span style="color:{_t["muted"]};">Falla frecuente: </span>{_falla_top}'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

                # ── Análisis de tipo de falla (company-level) — Dona ────────
                if not df_wo_c.empty and "failure_type" in df_wo_c.columns:
                    _df_fallas_c = df_wo_c[
                        (df_wo_c["maint_type"] == "Correctiva") &
                        (df_wo_c["failure_type"].str.strip() != "") &
                        (df_wo_c["mes_str"].isin(_meses_activos))
                    ]
                    st.markdown(
                        f'<div style="font-weight:700;font-size:0.95rem;margin:14px 0 8px 0;'
                        f'color:{_t["text"]};">🔩 Tipos de falla — correctivos</div>',
                        unsafe_allow_html=True,
                    )
                    _render_fallas_panel(_df_fallas_c, f"co_{_ck}")
            else:
                st.info("No hay datos de llamados para el período seleccionado.")

        else:
            # ── DETALLE EDS ESPECÍFICA ───────────────────────────────────────
            _eds_row = df_eds_activas[df_eds_activas["eds_occim"] == _eds_sel_code]
            _eds_info = _eds_row.iloc[0] if not _eds_row.empty else None

            if _eds_info is not None:
                st.markdown(
                    f'<div style="background:{_t["card"]};border-left:4px solid {_col["accent"]};'
                    f'border-radius:8px;padding:10px 14px;margin:8px 0 14px 0;font-size:0.85rem;">'
                    f'<b style="font-size:1.0rem;">{_eds_sel_nombre}</b>'
                    f'<span style="color:{_t["muted"]};"> · Cód: {_eds_sel_code}</span><br>'
                    f'<span style="color:{_t["muted"]};">Dirección: </span>{_eds_info.get("nombre","") or _eds_info.get("direccion","—")}'
                    f' · <span style="color:{_t["muted"]};">Comuna: </span>{_eds_info.get("comuna","—")}'
                    f' · <span style="color:{_t["muted"]};">Zona: </span>{_eds_info.get("zona_occim","—")}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            # KPIs específicos EDS
            _sla_eds = "—"
            if not df_ll_f.empty and "cumplimiento" in df_ll_f.columns:
                _tot_e = (df_ll_f["cumplimiento"] != "SIN DATOS").sum()
                _ok_e  = (df_ll_f["cumplimiento"] == "CUMPLE").sum()
                _sla_eds = f"{_ok_e/_tot_e*100:.1f}%" if _tot_e > 0 else "—"

            _avg_resp = "—"
            if not df_ll_f.empty and "horas_resolucion" in df_ll_f.columns:
                _hr = pd.to_numeric(df_ll_f["horas_resolucion"], errors="coerce")
                if _hr.notna().any():
                    _avg_resp = f"{_hr.mean():.1f} h"

            # Regularidad: promedio días entre llamados
            _regularidad = "—"
            if not df_ll_f.empty and len(df_ll_f) > 1 and "fecha_llamado" in df_ll_f.columns:
                _fechas_ord = pd.to_datetime(df_ll_f["fecha_llamado"], errors="coerce").dropna().sort_values()
                if len(_fechas_ord) > 1:
                    _gaps = [(_fechas_ord.iloc[i+1] - _fechas_ord.iloc[i]).days for i in range(len(_fechas_ord)-1)]
                    _avg_gap = sum(_gaps) / len(_gaps)
                    _regularidad = f"c/ {_avg_gap:.0f} días"

            _dc1, _dc2, _dc3, _dc4 = st.columns(4)
            _dc1.metric("Llamados en período", len(df_ll_f))
            _dc2.metric("PMs en período",      df_pm_f["folio"].nunique() if not df_pm_f.empty else 0)
            _dc3.metric("SLA cumplimiento",     _sla_eds)
            _dc4.metric("T° resp. promedio",    _avg_resp)

            _dc5, _dc6 = st.columns(2)
            _dc5.metric("Regularidad llamados", _regularidad)
            if not df_ll_f.empty and "fecha_llamado" in df_ll_f.columns:
                _ult_ll = pd.to_datetime(df_ll_f["fecha_llamado"], errors="coerce").max()
                _dc6.metric("Último llamado", _ult_ll.strftime("%d/%m/%Y") if pd.notna(_ult_ll) else "—")

            # Timeline llamados para esta EDS
            if not df_ll_f.empty and "Mes" in df_ll_f.columns:
                _ll_mes = df_ll_f.groupby("Mes")["n_llamado"].count().reset_index()
                _all_months = pd.DataFrame({"Mes": list(range(1, _cur_month+1))})
                _ll_mes = _all_months.merge(_ll_mes, on="Mes", how="left").fillna(0)
                _ll_mes["lbl"] = _ll_mes["Mes"].map(_MES_ES)
                _fig_tl = go.Figure(go.Bar(
                    x=_ll_mes["lbl"], y=_ll_mes["n_llamado"],
                    marker_color=_col["cm"], name="Llamados",
                ))
                _fig_tl.update_layout(
                    title=dict(text="Llamados correctivos por mes", font_size=13),
                    height=240, margin=dict(l=0,r=0,t=35,b=0),
                    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                    font_color=_t["text"],
                    xaxis=dict(gridcolor=_t["border"]),
                    yaxis=dict(gridcolor=_t["border"]),
                )
                st.plotly_chart(_fig_tl, use_container_width=True, key=f"chart_tl_{_ck}_{_eds_sel_code}")

            # Últimas visitas
            st.markdown(
                f'<div style="font-weight:700;font-size:0.95rem;margin:12px 0 8px 0;'
                f'color:{_t["text"]};">🔧 Últimas atenciones</div>',
                unsafe_allow_html=True,
            )
            if not df_ll_f.empty:
                _ultimas = (
                    df_ll_f.sort_values("fecha_llamado", ascending=False)
                    [["fecha_llamado","tecnico_corto" if "tecnico_corto" in df_ll_f.columns else "tecnico",
                      "prioridad","cumplimiento","horas_resolucion"
                      if "horas_resolucion" in df_ll_f.columns else "cumplimiento"]]
                    .head(10)
                    .rename(columns={
                        "fecha_llamado":"Fecha",
                        "tecnico_corto":"Técnico","tecnico":"Técnico",
                        "prioridad":"Prioridad",
                        "cumplimiento":"SLA",
                        "horas_resolucion":"H. Resp.",
                    })
                )
                _ultimas["Fecha"] = pd.to_datetime(_ultimas["Fecha"], errors="coerce").dt.strftime("%d/%m/%Y")
                _show_df(_ultimas, use_container_width=True, hide_index=True)
            else:
                st.caption("Sin llamados en el período seleccionado.")

            # Análisis de fallas para EDS específica — Dona
            if not df_cm_f.empty and "failure_type" in df_cm_f.columns:
                _df_fallas_eds = df_cm_f[df_cm_f["failure_type"].str.strip() != ""]
                if not _df_fallas_eds.empty:
                    st.markdown(
                        f'<div style="font-weight:700;font-size:0.95rem;margin:14px 0 8px 0;'
                        f'color:{_t["text"]};">🔩 Tipos de falla registrados</div>',
                        unsafe_allow_html=True,
                    )
                    _render_fallas_panel(_df_fallas_eds, f"eds_{_ck}_{_eds_sel_code}")

        # ── Tabla de detalle EDS ─────────────────────────────────────────────
        st.markdown(
            f'<div style="font-weight:700;font-size:0.95rem;margin:18px 0 8px 0;'
            f'color:{_t["text"]};">📋 Listado de estaciones — {_col["label"]}</div>',
            unsafe_allow_html=True,
        )
        # Construir tabla: EDS master + KPIs desde llamados
        _df_tbl = df_eds_c.copy()
        if not df_llamados.empty:
            _kpis_eds = kpis_por_eds(df_llamados)
            if not _kpis_eds.empty and "eds_occim" in _kpis_eds.columns:
                _df_tbl = _df_tbl.merge(_kpis_eds[["eds_occim","total_llamados","p1","ultimo_llamado","ultimo_tecnico"]],
                                         on="eds_occim", how="left")
        # Columnas a mostrar (las que existan)
        _col_map = {
            "eds_occim":       "Cód. Occim",
            "_cod_occim_frac": "Cód. Fracttal",
            "_loc_code":       "LOC Fracttal",
            "nombre":          "Nombre / Dirección",
            "direccion":       "Dirección",
            "comuna":          "Comuna",
            "zona_occim":      "Zona",
            "region":          "Región",
            "total_llamados":  "Llamados",
            "p1":              "P1",
            "ultimo_llamado":  "Último Llamado",
            "ultimo_tecnico":  "Último Técnico",
        }
        _cols_show = [c for c in _col_map if c in _df_tbl.columns]
        _df_display = _df_tbl[_cols_show].rename(columns=_col_map).copy()
        if "Último Llamado" in _df_display.columns:
            _df_display["Último Llamado"] = pd.to_datetime(
                _df_display["Último Llamado"], errors="coerce"
            ).dt.strftime("%d/%m/%Y").fillna("—")
        # Ordenar antes del fillna para evitar mezcla float/str
        _sort_col = "Llamados" if "Llamados" in _df_display.columns else _df_display.columns[0]
        _df_display = _df_display.sort_values(_sort_col, ascending=False, na_position="last")
        _df_display = _df_display.fillna("—")
        _show_df(_df_display, use_container_width=True, hide_index=True)

    # ── Renderizar tabs ──────────────────────────────────────────────────────
    with _tabs_eds[0]:
        _render_eds_tab("COPEC", 0)
    with _tabs_eds[1]:
        _render_eds_tab("Aramco (Esmax)", 1)
    with _tabs_eds[2]:
        _render_eds_tab("SHELL (Enex)", 2)



# ─────────────────────────────────────────────────────────────────────────────
# PÁGINA 4: UTILIZACIÓN DEL TIEMPO
# ─────────────────────────────────────────────────────────────────────────────
elif _page == _NAV_PAGES[4]:
    _hdr(_PAGE_TITLE[_NAV_PAGES[4]])
    # ── Sub-tabs ──────────────────────────────────────────────────────────────
    _util_sub_tab = st.radio(
        "",
        ["📊 Utilización del tiempo", "📡 En Vivo"],
        horizontal=True,
        label_visibility="collapsed",
        key="util_sub_tab",
    )
    st.divider()

    if _util_sub_tab == "📡 En Vivo":
        from datetime import datetime as _dt_vivo

        # ── Título + botón actualizar ─────────────────────────────────────────────
        _col_tit, _col_btn = st.columns([6, 1])
        with _col_tit:
            st.title("📡 En Vivo — Órdenes en Ejecución")
        with _col_btn:
            st.write("")
            if st.button("🔄 Actualizar", key="btn_envivo_refresh", use_container_width=True):
                load_ots_en_vivo_supabase.clear()
                st.rerun()

        _vivo_ts = _dt_vivo.now().strftime("%d/%m/%Y %H:%M")
        st.caption(
            f"Última actualización: **{_vivo_ts}** · "
            "caché renovado cada **2 min** automáticamente · "
            "incluye OTs *En Progreso*, *Por Validar* y *Por Iniciar*"
        )
        st.divider()

        # ── Carga de datos ────────────────────────────────────────────────────────
        with st.spinner("Cargando órdenes en curso…"):
            _raw_vivo = load_ots_en_vivo_supabase()

        if not _raw_vivo:
            st.info("✅ No hay órdenes activas en este momento.")
        else:
            _df_vivo = pd.DataFrame(_raw_vivo)

            # ── Helpers internos ─────────────────────────────────────────────────
            def _vivo_seg_fmt(s):
                try:
                    s = int(float(s or 0))
                    if s <= 0:
                        return "—"
                    h, rem = divmod(s, 3600)
                    m = rem // 60
                    return f"{h}h {m:02d}m" if h else f"{m}m"
                except Exception:
                    return "—"

            def _vivo_avance(row):
                try:
                    r = float(row.get("duracion_real_seg") or 0)
                    e = float(row.get("duracion_estim_seg") or 0)
                    if e <= 0:
                        return None
                    return min(round(r / e * 100, 1), 999.0)
                except Exception:
                    return None

            def _vivo_fmt_dt(x):
                if not x or str(x).strip() in ("", "None", "null"):
                    return "—"
                try:
                    t = pd.Timestamp(str(x))
                    if t.tzinfo:
                        t = t.tz_convert(None)
                    return t.strftime("%d/%m %H:%M")
                except Exception:
                    return "—"

            def _vivo_limpiar_ubi(s):
                """'// COPEC/ COPEC LAMPA/' → 'COPEC LAMPA'"""
                if not s or str(s).strip() in ("", "None"):
                    return "—"
                partes = [p.strip() for p in str(s).split("/") if p.strip()]
                return partes[-1] if partes else str(s)

            # ── Normalizar columnas ───────────────────────────────────────────────
            for _col in ["estado", "estado_tarea", "tipo_tarea", "responsable", "cliente"]:
                if _col in _df_vivo.columns:
                    _df_vivo[_col] = _df_vivo[_col].fillna("—")

            _df_vivo["t_real"]      = _df_vivo["duracion_real_seg"].apply(_vivo_seg_fmt)
            _df_vivo["t_est"]       = _df_vivo["duracion_estim_seg"].apply(_vivo_seg_fmt)
            _df_vivo["avance_pct"]  = _df_vivo.apply(_vivo_avance, axis=1)
            _df_vivo["inicio_fmt"]  = _df_vivo["fecha_inicio"].apply(_vivo_fmt_dt)
            _df_vivo["creacion_fmt"]= _df_vivo["fecha_creacion"].apply(_vivo_fmt_dt)
            _df_vivo["ubi_limpia"]  = _df_vivo["ubicacion"].apply(_vivo_limpiar_ubi)

            _ESTADO_T_ICON = {
                "En Proceso":  "🟢 En Proceso",
                "En Revisión": "🟡 En Revisión",
                "No Iniciada": "⚫ No Iniciada",
                "En Espera":   "🔵 En Espera",
                "Finalizada":  "✅ Finalizada",
            }
            _df_vivo["estado_tarea_lbl"] = _df_vivo["estado_tarea"].map(
                lambda x: _ESTADO_T_ICON.get(x, f"⚪ {x}")
            )

            # ── KPI cards ─────────────────────────────────────────────────────────
            _n_prog = int((_df_vivo["estado"] == "En Progreso").sum())
            _n_val  = int((_df_vivo["estado"] == "Por Validar").sum())
            _n_ini  = int((_df_vivo["estado"] == "Por Iniciar").sum())
            _n_tec  = int(
                _df_vivo.loc[_df_vivo["estado"] == "En Progreso", "responsable"]
                .replace("—", pd.NA).dropna().nunique()
            )
            _n_cm   = int(_df_vivo["tipo_tarea"].str.contains("CORREC", na=False, case=False).sum())
            _n_pm   = int(_df_vivo["tipo_tarea"].str.contains("PREVEN", na=False, case=False).sum())

            _kc1, _kc2, _kc3, _kc4, _kc5, _kc6 = st.columns(6)
            _kc1.metric("🔧 En Progreso", _n_prog)
            _kc2.metric("🔍 Por Validar", _n_val)
            _kc3.metric("⏳ Por Iniciar", _n_ini)
            _kc4.metric("👷 Técnicos activos", _n_tec)
            _kc5.metric("🚨 Correctivas", _n_cm)
            _kc6.metric("🛠️ Preventivas", _n_pm)

            st.divider()

            # ── Filtros ───────────────────────────────────────────────────────────
            _fvc1, _fvc2, _fvc3, _fvc4 = st.columns(4)
            with _fvc1:
                _ev_tipos = ["Todos"] + sorted(_df_vivo["tipo_tarea"].unique().tolist())
                _ev_sel_tipo = st.selectbox("Tipo OT", _ev_tipos, key="ev_tipo")
            with _fvc2:
                _ev_tecs = ["Todos"] + sorted(
                    t for t in _df_vivo["responsable"].unique().tolist() if t != "—"
                )
                _ev_sel_tec = st.selectbox("Técnico", _ev_tecs, key="ev_tec")
            with _fvc3:
                _ev_clis = ["Todos"] + sorted(
                    c for c in _df_vivo["cliente"].unique().tolist() if c != "—"
                )
                _ev_sel_cli = st.selectbox("Cliente", _ev_clis, key="ev_cli")
            with _fvc4:
                _ev_ests = ["Todos"] + sorted(_df_vivo["estado"].unique().tolist())
                _ev_sel_est = st.selectbox("Estado OT", _ev_ests, key="ev_est")

            _df_vf = _df_vivo.copy()
            if _ev_sel_tipo != "Todos":
                _df_vf = _df_vf[_df_vf["tipo_tarea"] == _ev_sel_tipo]
            if _ev_sel_tec != "Todos":
                _df_vf = _df_vf[_df_vf["responsable"] == _ev_sel_tec]
            if _ev_sel_cli != "Todos":
                _df_vf = _df_vf[_df_vf["cliente"] == _ev_sel_cli]
            if _ev_sel_est != "Todos":
                _df_vf = _df_vf[_df_vf["estado"] == _ev_sel_est]

            # ── Tabla principal ───────────────────────────────────────────────────
            st.subheader(f"Órdenes activas — {len(_df_vf):,} OT(s)")

            _vivo_cols_disp = [
                "id_ot", "estado", "estado_tarea_lbl", "tipo_tarea",
                "responsable", "cliente", "nombre_activo", "ubi_limpia",
                "inicio_fmt", "creacion_fmt", "t_real", "t_est", "avance_pct",
            ]
            _df_vivo_show = _df_vf[[c for c in _vivo_cols_disp if c in _df_vf.columns]].copy()
            _df_vivo_show = _df_vivo_show.rename(columns={
                "id_ot":            "OT",
                "estado":           "Estado OT",
                "estado_tarea_lbl": "Estado Tarea",
                "tipo_tarea":       "Tipo",
                "responsable":      "Técnico",
                "cliente":          "Cliente",
                "nombre_activo":    "Activo / Equipo",
                "ubi_limpia":       "Ubicación",
                "inicio_fmt":       "Inicio",
                "creacion_fmt":     "Creación",
                "t_real":           "T. Real",
                "t_est":            "T. Est.",
                "avance_pct":       "Avance %",
            })

            st.dataframe(
                _df_vivo_show,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "OT":           st.column_config.TextColumn(width=100),
                    "Estado OT":    st.column_config.TextColumn(width=110),
                    "Estado Tarea": st.column_config.TextColumn(width=150),
                    "Tipo":         st.column_config.TextColumn(width=120),
                    "Técnico":      st.column_config.TextColumn(width=180),
                    "Cliente":      st.column_config.TextColumn(width=95),
                    "Activo / Equipo": st.column_config.TextColumn(width=240),
                    "Ubicación":    st.column_config.TextColumn(width=200),
                    "Inicio":       st.column_config.TextColumn(width=90),
                    "Creación":     st.column_config.TextColumn(width=90),
                    "T. Real":      st.column_config.TextColumn(width=75),
                    "T. Est.":      st.column_config.TextColumn(width=75),
                    "Avance %":     st.column_config.ProgressColumn(
                        min_value=0, max_value=100, format="%.0f%%", width=100
                    ),
                },
            )

            # ── Desglose por técnico ──────────────────────────────────────────────
            if not _df_vf.empty:
                st.divider()
                st.subheader("👷 Carga actual por técnico")

                _vivo_by_tec = (
                    _df_vf[_df_vf["responsable"] != "—"]
                    .groupby("responsable")
                    .agg(
                        total      =("id_ot",        "count"),
                        en_proceso =("estado_tarea",  lambda x: (x == "En Proceso").sum()),
                        correctivas=("tipo_tarea",
                                     lambda x: x.str.contains("CORREC", na=False, case=False).sum()),
                        preventivas=("tipo_tarea",
                                     lambda x: x.str.contains("PREVEN", na=False, case=False).sum()),
                        clientes   =("cliente",       lambda x: ", ".join(sorted(x[x != "—"].unique()))),
                    )
                    .reset_index()
                    .sort_values("total", ascending=False)
                    .rename(columns={
                        "responsable": "Técnico",
                        "total":       "Total OTs",
                        "en_proceso":  "🟢 En Proceso",
                        "correctivas": "🚨 Correctivas",
                        "preventivas": "🛠️ Preventivas",
                        "clientes":    "Clientes",
                    })
                )
                st.dataframe(
                    _vivo_by_tec,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Técnico":       st.column_config.TextColumn(width=200),
                        "Total OTs":     st.column_config.NumberColumn(width=85),
                        "🟢 En Proceso": st.column_config.NumberColumn(width=100),
                        "🚨 Correctivas":st.column_config.NumberColumn(width=100),
                        "🛠️ Preventivas":st.column_config.NumberColumn(width=100),
                        "Clientes":      st.column_config.TextColumn(width=200),
                    },
                )

        st.stop()

    # ── Carga lazy: solo se ejecuta al visitar esta página ────────────────────
    with st.spinner("📂 Cargando planificación del tiempo…"):
        df_util, util_sheet, util_error = load_utilizacion_tiempo()
        util_sheets  = list_utilizacion_sheets()
        df_mtto_plan = load_mtto_realizados_planilla()

    # Colores de fondo por categoría (sincronizados con CATEGORY_COLORS_UTIL)
    # Adaptados al tema para evitar pasteles claros sobre fondo oscuro
    if _current_theme == "dark":
        _CAT_BG = {
            "Mant. Preventivo":   "#0d2e18",
            "Llamado Correctivo": "#2e0d0d",
            "Instalación":        "#0d1a2e",
            "Capacitación":       "#1e0d2e",
            "Reunión":            "#2e2200",
            "Feriado":            "#1a202c",
            "Inventario":         "#0a2030",
            "Oficina":            "#111f38",
        }
        _FUERA_STG_BG = "#0d2e1a"
    else:
        _CAT_BG = {
            "Mant. Preventivo":   "#dcfce7",
            "Llamado Correctivo": "#fee2e2",
            "Instalación":        "#dbeafe",
            "Capacitación":       "#f3e8ff",
            "Reunión":            "#fef3c7",
            "Feriado":            "#f1f5f9",
            "Inventario":         "#cffafe",
            "Oficina":            "#f8fafc",
        }
        _FUERA_STG_BG = "#d1fae5"

    # ── Selector de período y mes ─────────────────────────────────────────────
    _sheets_opts = util_sheets if util_sheets else ([util_sheet] if util_sheet else [])

    # Mapear nombre de hoja "MAYO 2026" → número de mes
    _MESES_FULL_NUM = {
        "ENERO":1,"FEBRERO":2,"MARZO":3,"ABRIL":4,"MAYO":5,"JUNIO":6,
        "JULIO":7,"AGOSTO":8,"SEPTIEMBRE":9,"OCTUBRE":10,"NOVIEMBRE":11,"DICIEMBRE":12,
    }
    def _sheet_mes_num(s: str) -> int:
        su = str(s).upper()
        for nm, n in _MESES_FULL_NUM.items():
            if nm in su:
                return n
        return 0

    _TRIMESTRES_UTIL = {
        "T1 · Ene–Mar": [1,2,3], "T2 · Abr–Jun": [4,5,6],
        "T3 · Jul–Sep": [7,8,9], "T4 · Oct–Dic": [10,11,12],
    }
    _util_meses_nums = {_sheet_mes_num(s) for s in _sheets_opts}
    _trim_opts_util  = ["Todos"] + [
        k for k,v in _TRIMESTRES_UTIL.items() if any(m in v for m in _util_meses_nums)
    ]

    _uc1, _uc2 = st.columns([1.5, 2.5])
    with _uc1:
        _sel_trim_util = st.selectbox("Período", _trim_opts_util, key="util_trim")
    # Filtrar las hojas disponibles según el trimestre seleccionado
    if _sel_trim_util != "Todos":
        _sheets_filtrados = [s for s in _sheets_opts if _sheet_mes_num(s) in _TRIMESTRES_UTIL[_sel_trim_util]]
    else:
        _sheets_filtrados = _sheets_opts

    # Inicializar session_state con el mes auto-detectado
    if "util_sheet_sel" not in st.session_state or st.session_state["util_sheet_sel"] not in _sheets_filtrados:
        _def_sheet = util_sheet if util_sheet in _sheets_filtrados else (_sheets_filtrados[0] if _sheets_filtrados else "")
        st.session_state["util_sheet_sel"] = _def_sheet

    with _uc2:
        _sel_mes = st.selectbox(
            "📅  Mes",
            options=_sheets_filtrados,
            index=_sheets_filtrados.index(st.session_state["util_sheet_sel"]) if st.session_state["util_sheet_sel"] in _sheets_filtrados else 0,
            key=f"util_sheet_select_{_sel_trim_util}",   # key cambia con trimestre → reset automático
            help="Selecciona el mes a visualizar. Solo aparecen los meses disponibles en el Excel.",
        )
    st.session_state["util_sheet_sel"] = _sel_mes

    # Recargar datos si el mes seleccionado es distinto al auto-detectado
    if _sel_mes and _sel_mes != util_sheet:
        df_util, util_sheet, util_error = load_utilizacion_tiempo(sheet_name=_sel_mes)

    if util_error and df_util.empty:
        st.info(
            "📋 **Utilización del tiempo** — sección en proceso de migración a la nube.\n\n"
            "Esta vista lee un archivo Excel de Google Drive que solo está disponible en la versión "
            "local del dashboard. Estará disponible aquí una vez migrada a Supabase.",
            icon="🚧",
        )
        # ── Mostrar datos de Supabase que SÍ están disponibles ───────────────
        st.markdown("---")
        st.markdown("### 🛠️ Mantenciones Preventivas — estado actual")
        st.caption("Datos disponibles en la nube desde Supabase")
        with st.spinner("Cargando preventivas…"):
            _prev_plan = load_preventivas_supabase()
        if _prev_plan:
            _df_prev_plan = pd.DataFrame(_prev_plan)
            # Parsear fechas — quitar timezone si Supabase las devuelve con offset
            for _c in ["fecha_programada", "fecha_creacion", "fecha_finalizacion"]:
                if _c in _df_prev_plan.columns:
                    _tmp = pd.to_datetime(_df_prev_plan[_c], errors="coerce")
                    if _tmp.dt.tz is not None:
                        _tmp = _tmp.dt.tz_convert(None)
                    _df_prev_plan[_c] = _tmp
            # Filtrar próximas 30 días + pendientes
            _hoy = pd.Timestamp.today().normalize()
            _en_30 = _hoy + pd.Timedelta(days=30)
            if "fecha_programada" in _df_prev_plan.columns:
                _prox = _df_prev_plan[
                    (_df_prev_plan["fecha_programada"] >= _hoy) &
                    (_df_prev_plan["fecha_programada"] <= _en_30)
                ].copy()
            else:
                _prox = pd.DataFrame()
            # KPIs rápidos
            _pk1, _pk2, _pk3 = st.columns(3)
            _pk1.metric("Total OTs preventivas", f"{len(_df_prev_plan):,}")
            _pk2.metric("Próximas 30 días", f"{len(_prox):,}")
            _pend = len(_df_prev_plan[_df_prev_plan.get("estado", pd.Series()).str.lower().str.contains("inici", na=False)]) if "estado" in _df_prev_plan.columns else "—"
            _pk3.metric("No iniciadas", str(_pend))
            # Tabla próximas
            if not _prox.empty:
                st.markdown("**Próximas mantenciones (30 días)**")
                _cols_prox = [c for c in ["id_ot","nombre_tarea","responsable","fecha_programada","estado","estado_tarea"] if c in _prox.columns]
                _prox_disp = _prox[_cols_prox].copy()
                if "fecha_programada" in _prox_disp.columns:
                    _prox_disp["fecha_programada"] = _prox_disp["fecha_programada"].dt.strftime("%d/%m/%Y")
                _show_df(_prox_disp.sort_values("fecha_programada") if "fecha_programada" in _prox_disp.columns else _prox_disp,
                         use_container_width=True, hide_index=True)
            else:
                st.info("No hay mantenciones programadas en los próximos 30 días.")
        else:
            st.warning("No se pudieron cargar datos de Supabase.")
    else:
        if util_error:
            st.warning(f"Aviso: {util_error}")

        _miembros_activos = {m for g in GRUPOS_TERRENO.values() for m in g["miembros"]}
        u_techs = (
            df_util["tecnico"][df_util["tecnico"].isin(_miembros_activos)].nunique()
            if not df_util.empty else len(_miembros_activos)
        )
        u_labs  = int(df_util[~df_util["categoria"].isin(["Feriado"])]["fecha"].nunique()) if not df_util.empty else 0

        uk1, uk2 = st.columns(2)
        uk1.metric("Técnicos activos", str(u_techs))
        uk2.metric("Días laborales", str(u_labs))

        if df_util.empty:
            st.info("No hay datos de asignación para el mes en curso.")
        else:
            # Calcular semana del mes con semanas Domingo–Sábado
            # Semana 1 = desde el 1° del mes hasta el primer Sábado (puede ser 1-2 días)
            # Semana 2 = del siguiente Domingo al siguiente Sábado, etc.
            def _semana_del_mes(dt):
                d = pd.Timestamp(dt)
                first = d.replace(day=1)
                # Offset del 1° respecto a su Domingo (Dom=0, Lun=1, …, Sáb=6)
                first_sun_offset = (first.weekday() + 1) % 7
                elapsed = first_sun_offset + d.day - 1
                return f"Semana {elapsed // 7 + 1}"

            df_util = df_util.copy()
            df_util["semana_mes"] = df_util["fecha"].apply(_semana_del_mes)

            st.divider()

            # ════════════════════════════════════════════════════════════════
            # VISTA 1: Programación del día (filtro fecha + técnico)
            # ════════════════════════════════════════════════════════════════
            st.markdown('<div class="section-header">📅  Programación diaria</div>', unsafe_allow_html=True)

            dates_avail = sorted(df_util["fecha"].dt.normalize().unique())
            # Default = hoy si está en la lista, sino último día disponible
            today_ts = pd.Timestamp.now().normalize()
            default_idx = next(
                (i for i, d in enumerate(dates_avail) if pd.Timestamp(d) == today_ts),
                len(dates_avail) - 1,
            )
            _DIAS_ABBR_ES = {
                "Mon":"Lun","Tue":"Mar","Wed":"Mié",
                "Thu":"Jue","Fri":"Vie","Sat":"Sáb","Sun":"Dom",
            }
            date_labels = [
                f"{_DIAS_ABBR_ES.get(pd.Timestamp(d).strftime('%a'), pd.Timestamp(d).strftime('%a'))} "
                f"{pd.Timestamp(d).strftime('%d/%m/%Y')}"
                for d in dates_avail
            ]
            date_map    = dict(zip(date_labels, dates_avail))

            fd1, fd2 = st.columns([2, 2])
            with fd1:
                sel_fecha_lbl = st.selectbox(
                    "Fecha", date_labels, index=default_idx, key="util_fecha"
                )
            with fd2:
                all_techs = ["Todos los técnicos"] + sorted(df_util["tecnico"].unique().tolist())
                sel_tech_d = st.selectbox("Técnico", all_techs, key="util_tech_d")

            sel_fecha = date_map.get(sel_fecha_lbl)
            df_day = df_util[
                df_util["fecha"].dt.normalize() == pd.Timestamp(sel_fecha).normalize()
            ].copy() if sel_fecha is not None else pd.DataFrame()

            if sel_tech_d != "Todos los técnicos" and not df_day.empty:
                df_day = df_day[df_day["tecnico"] == sel_tech_d]

            if not df_day.empty:
                # Resumen por categorías ese día
                cat_resumen = df_day["categoria"].value_counts()
                fuera_n = df_day["fuera_santiago"].sum()
                pills = " ".join(
                    f'<span style="background:{CATEGORY_COLORS_UTIL.get(c,"#94a3b8")};'
                    f'color:white;padding:3px 10px;border-radius:12px;font-size:0.8rem;margin:2px;">'
                    f'{c} ({n})</span>'
                    for c, n in cat_resumen.items()
                )
                st.markdown(pills, unsafe_allow_html=True)
                if fuera_n:
                    st.caption(f"🟢 {int(fuera_n)} técnico(s) con ruta fuera de Santiago")

                # Tabla detallada
                def _color_row(row):
                    bg = _FUERA_STG_BG if row.get("Zona","").startswith("🟢") else _CAT_BG.get(row.get("Categoría",""), "")
                    return [f"background-color:{bg}"] * len(row) if bg else [""] * len(row)

                disp = pd.DataFrame([{
                    "Técnico":     r["tecnico"],
                    "Categoría":   r["categoria"],
                    "Zona":        "🟢 Fuera Stgo" if r["fuera_santiago"] else "⬜ Santiago",
                    "Programación del día": r["tareas"],
                } for _, r in df_day.iterrows()])

                _show_df(
                    disp.style.apply(_color_row, axis=1),
                    width="stretch", hide_index=True,
                    column_config={
                        "Zona": st.column_config.TextColumn(width=130),
                        "Categoría": st.column_config.TextColumn(width=160),
                        "Programación del día": st.column_config.TextColumn(),
                    },
                )
            else:
                st.info("Sin asignaciones para el día seleccionado.")

            st.divider()

            # ════════════════════════════════════════════════════════════════
            # VISTA 2: Semana del mes por técnico
            # ════════════════════════════════════════════════════════════════
            st.markdown('<div class="section-header">📆  Programación semanal por técnico</div>', unsafe_allow_html=True)

            sw1, sw2 = st.columns([2, 2])
            with sw1:
                semanas_disp = sorted(df_util["semana_mes"].unique())
                sel_semana = st.selectbox("Semana", semanas_disp, key="util_semana")
            with sw2:
                sel_tech_s = st.selectbox(
                    "Técnico", sorted(df_util["tecnico"].unique().tolist()), key="util_tech_s"
                )

            df_sem = df_util[
                (df_util["semana_mes"] == sel_semana) &
                (df_util["tecnico"] == sel_tech_s)
            ].sort_values("fecha")

            if not df_sem.empty:
                DIAS_ES = {"Monday":"Lunes","Tuesday":"Martes","Wednesday":"Miércoles",
                           "Thursday":"Jueves","Friday":"Viernes","Saturday":"Sábado","Sunday":"Domingo"}
                sem_rows = []
                for _, r in df_sem.iterrows():
                    ts = pd.Timestamp(r["fecha"])
                    dia_es = DIAS_ES.get(ts.strftime("%A"), ts.strftime("%A"))
                    sem_rows.append({
                        "Día":        f"{dia_es} {ts.strftime('%d/%m')}",
                        "Categoría":  r["categoria"],
                        "Zona":       "🟢 Fuera Stgo" if r["fuera_santiago"] else "⬜ Santiago",
                        "Programación": r["tareas"],
                    })
                df_sem_disp = pd.DataFrame(sem_rows)

                def _color_sem(row):
                    bg = _FUERA_STG_BG if row.get("Zona","").startswith("🟢") else _CAT_BG.get(row.get("Categoría",""), "")
                    return [f"background-color:{bg}"] * len(row) if bg else [""] * len(row)

                _show_df(
                    df_sem_disp.style.apply(_color_sem, axis=1),
                    width="stretch", hide_index=True,
                    column_config={
                        "Día": st.column_config.TextColumn(width=140),
                        "Categoría": st.column_config.TextColumn(width=160),
                        "Zona": st.column_config.TextColumn(width=130),
                        "Programación": st.column_config.TextColumn(),
                    },
                )
            else:
                st.info(f"No hay asignaciones para {sel_tech_s} en {sel_semana}.")

            st.divider()

            # ════════════════════════════════════════════════════════════════
            # VISTA 3: Distribución mensual + desglose por categoría
            # ════════════════════════════════════════════════════════════════
            st.markdown('<div class="section-header">📊  Distribución mensual por técnico</div>', unsafe_allow_html=True)

            cat_counts = (
                df_util[
                    (df_util["categoria"] != "Feriado") &
                    (df_util["tecnico"].isin(_miembros_activos))
                ]
                .groupby(["tecnico", "categoria"])
                .size()
                .reset_index(name="dias")
            )
            tech_order = (
                cat_counts.groupby("tecnico")["dias"].sum()
                .sort_values(ascending=False).index.tolist()
            )
            _util_k = f"_fig_util_{_current_theme}_{_sel_mes}"
            if _util_k not in st.session_state:
                fig_util = px.bar(
                    cat_counts, x="tecnico", y="dias", color="categoria",
                    color_discrete_map=CATEGORY_COLORS_UTIL,
                    title=f"Días por categoría — {util_sheet}",
                    barmode="stack",
                    category_orders={"tecnico": tech_order},
                    labels={"tecnico": "Técnico", "dias": "Días", "categoria": "Categoría"},
                )
                fig_util.update_layout(xaxis_tickangle=-30, legend_title="Categoría", height=400,
                                       margin=dict(t=50, b=80))
                _apply_plot_theme(fig_util)
                st.session_state[_util_k] = fig_util
            st.plotly_chart(st.session_state[_util_k], width="stretch")

            # Leyenda de colores
            leg_cols = st.columns(len(CATEGORY_COLORS_UTIL))
            for i, (cat, color) in enumerate(CATEGORY_COLORS_UTIL.items()):
                leg_cols[i].markdown(
                    f'<div style="display:flex;align-items:center;gap:4px;">'
                    f'<div style="width:12px;height:12px;background:{color};border-radius:3px;flex-shrink:0"></div>'
                    f'<small style="color:{_t["muted"]};line-height:1.2">{cat}</small></div>',
                    unsafe_allow_html=True,
                )

            st.divider()

            # ── Desglose por categoría ────────────────────────────────────
            st.markdown('<div class="section-header">🔍  Desglose por categoría</div>', unsafe_allow_html=True)
            st.caption("Selecciona una categoría para ver cada tarea individual del mes.")

            dc1, dc2 = st.columns([1, 3])
            with dc1:
                cat_sel = st.radio("Categoría", list(CATEGORY_COLORS_UTIL.keys()), key="util_cat_sel")
                n_cat = len(df_util[df_util["categoria"] == cat_sel])
                cat_color = CATEGORY_COLORS_UTIL.get(cat_sel, "#94a3b8")
                st.markdown(
                    f'<div style="margin-top:8px;padding:8px 12px;background:{cat_color}22;'
                    f'border-left:4px solid {cat_color};border-radius:4px;">'
                    f'<b style="color:{cat_color}">{cat_sel}</b><br>'
                    f'<small>{n_cat} días este mes</small></div>',
                    unsafe_allow_html=True,
                )
            with dc2:
                desglose_rows = []
                for _, r in df_util.iterrows():
                    for line in str(r["tareas"]).split(" | "):
                        line = line.strip()
                        if line and classify_task_line(line) == cat_sel:
                            desglose_rows.append({
                                "Fecha":   (
                                    f"{_DIAS_ABBR_ES.get(pd.Timestamp(r['fecha']).strftime('%a'), pd.Timestamp(r['fecha']).strftime('%a'))} "
                                    f"{pd.Timestamp(r['fecha']).strftime('%d/%m')}"
                                ),
                                "Técnico": r["tecnico"],
                                "Detalle": line,
                                "Zona":    "🟢 Fuera Stgo" if r["fuera_santiago"] else "Santiago",
                            })
                if desglose_rows:
                    df_des = pd.DataFrame(desglose_rows)
                    st.caption(f"**{cat_sel}** — {len(df_des)} tareas en {df_des['Técnico'].nunique()} técnicos")
                    _show_df(df_des, width="stretch", hide_index=True)
                else:
                    st.info(f"No hay tareas de '{cat_sel}' en {util_sheet}.")




# ─────────────────────────────────────────────────────────────────────────────
# PÁGINA 5: DESEMPEÑO TERRENO
# ─────────────────────────────────────────────────────────────────────────────
elif _page == _NAV_PAGES[0]:
    _hdr(_PAGE_TITLE[_NAV_PAGES[0]])
    st.divider()

    # ── Técnicos: Supabase o Excel ────────────────────────────────────────────
    if _USE_SUPABASE:
        df_tecnicos = load_tecnicos_supabase()
    else:
        df_tecnicos = load_base_tecnicos()
    _excel_to_full, _full_to_excel = build_tech_name_maps(df_tecnicos)

    # ── OTs: Supabase (rapido) o Fracttal API (lento) ─────────────────────────
    if _USE_SUPABASE:
        raw_wo = load_work_orders_supabase()
        # Si Supabase devuelve vacio (cache danada), limpiar y reintentar una vez
        if not raw_wo:
            load_work_orders_supabase.clear()
            raw_wo = load_work_orders_supabase()
        # Si sigue vacio, caer a Fracttal como fallback
        if not raw_wo:
            st.warning("Supabase no retorno OTs — usando Fracttal API como respaldo")
            raw_wo = _load_wo_con_progreso("ordenes de trabajo — Desempeno ST")
    else:
        raw_wo = _load_wo_con_progreso("ordenes de trabajo — Desempeno ST")
    _wo_sig = str(len(raw_wo))

    _ph_proc = st.empty()
    if st.session_state.get("_sc_sig_df_wo_base") != _wo_sig:
        with _ph_proc.container():
            with st.spinner(f"Procesando {len(raw_wo):,} ordenes de trabajo..."):
                df_wo = _sc("df_wo_base", _wo_sig, lambda: build_work_orders_df(raw_wo))
    else:
        df_wo = _sc("df_wo_base", _wo_sig, lambda: build_work_orders_df(raw_wo))
    _ph_proc.empty()

    # ── Utilidades compartidas ───────────────────────────────────────────────
    _SCORE_COLOR = {
        "verde":    "#22c55e",
        "amarillo": "#f59e0b",
        "rojo":     "#ef4444",
    }

    # ── Constantes de bono ────────────────────────────────────────────────────
    _BONO_TOTAL      = 500_000   # CLP/trimestre · pool equipo completo (seniors incluidos)
    _W_SLA           = 0.40      # 40 %
    _W_CAL           = 0.30      # 30 %
    _W_PREC          = 0.30      # 30 %  (trimestral, igual que SLA y Calidad)
    _MAX_SLA_CLP     = int(_BONO_TOTAL * _W_SLA)   # 200.000
    _MAX_CAL_CLP     = int(_BONO_TOTAL * _W_CAL)   # 150.000
    _MAX_PREC_TRM    = int(_BONO_TOTAL * _W_PREC)  # 150.000 (trimestral)

    def _score_level(score: float) -> tuple[str, str]:
        """Color/label informativo para score promedio (display, no bono)."""
        if score >= 90: return _SCORE_COLOR["verde"],    "✅ Excelente"
        if score >= 70: return _SCORE_COLOR["amarillo"], "⚠️ Regular"
        return               _SCORE_COLOR["rojo"],       "❌ Bajo"

    def _bono_sla(pct: float) -> tuple[int, str, str, int]:
        """
        Retorna (% bono, etiqueta, color, CLP/trimestre) según escala KPI Productividad SLA.
        Escala oficial — 40% bono = $200.000/trim pool (5 niveles):
          ≥ 95%        → 100% → $200.000
          93 a <95%    →  90% → $180.000
          90 a <93%    →  80% → $160.000
          85 a <90%    →  50% → $100.000
          < 85%        →   0% →       $0
        """
        m = _MAX_SLA_CLP  # 140.000
        if pct >= 95: return 100, "100%", _SCORE_COLOR["verde"],    m
        if pct >= 93: return  90,  "90%", "#16a34a",                int(m * .90)
        if pct >= 90: return  80,  "80%", "#4ade80",                int(m * .80)
        if pct >= 85: return  50,  "50%", _SCORE_COLOR["amarillo"], int(m * .50)
        return                  0, "Sin bono", _SCORE_COLOR["rojo"], 0

    def _bono_calidad(n_fallas: int, n_pms: int = 0) -> tuple[int, str, str, int]:
        """
        Retorna (% bono, etiqueta, color, CLP/trimestre) según exactitud porcentual.
        Exactitud = (PMs sin reincidencia) / total PMs × 100.
        Escala oficial Tasa de Reproceso — 30% bono = $150.000/trim pool (6 niveles):
          ≥ 98%        → 100% → $150.000
          96 a <98%    →  90% → $135.000
          94 a <96%    →  80% → $120.000
          92 a <94%    →  70% → $105.000
          90 a <92%    →  60% →  $90.000
          < 90%        →   0% →       $0
        """
        if n_pms > 0:
            exactitud = (1 - n_fallas / n_pms) * 100
        else:
            exactitud = 100.0 if n_fallas == 0 else 0.0

        m = _MAX_CAL_CLP   # 105.000
        if exactitud >= 98: return 100, f"{exactitud:.1f}% — 100%", _SCORE_COLOR["verde"],    m
        if exactitud >= 96: return  90, f"{exactitud:.1f}% —  90%", "#16a34a",                int(m * .90)
        if exactitud >= 94: return  80, f"{exactitud:.1f}% —  80%", "#4ade80",                int(m * .80)
        if exactitud >= 92: return  70, f"{exactitud:.1f}% —  70%", "#65a30d",                int(m * .70)
        if exactitud >= 90: return  60, f"{exactitud:.1f}% —  60%", _SCORE_COLOR["amarillo"], int(m * .60)
        return                       0, f"{exactitud:.1f}% —   0%", _SCORE_COLOR["rojo"],     0

    def _bono_prec(exactitud_pct: float) -> tuple[int, str, str, int]:
        """
        Retorna (% bono, etiqueta, color, CLP/trimestre) según cumplimiento de OTs correctas.
        Escala oficial Precisión Fracttal — 30% bono terreno = $150.000/trim pool (7 niveles):
          ≥ 95%  → 100% → $150.000/trim
          ≥ 90%  →  90% → $135.000/trim
          ≥ 85%  →  80% → $120.000/trim
          ≥ 80%  →  70% → $105.000/trim
          ≥ 75%  →  60% →  $90.000/trim
          ≥ 70%  →  50% →  $75.000/trim
          < 70%  →   0% →       $0
        """
        _m = _MAX_PREC_TRM
        if exactitud_pct >= 95: return 100, f"{exactitud_pct:.1f}% — ${_m:,.0f}/trim",        _SCORE_COLOR["verde"],    _m
        if exactitud_pct >= 90: return  90, f"{exactitud_pct:.1f}% — ${int(_m*.90):,}/trim",   "#16a34a",                int(_m * .90)
        if exactitud_pct >= 85: return  80, f"{exactitud_pct:.1f}% — ${int(_m*.80):,}/trim",   "#4ade80",                int(_m * .80)
        if exactitud_pct >= 80: return  70, f"{exactitud_pct:.1f}% — ${int(_m*.70):,}/trim",   "#65a30d",                int(_m * .70)
        if exactitud_pct >= 75: return  60, f"{exactitud_pct:.1f}% — ${int(_m*.60):,}/trim",   _SCORE_COLOR["amarillo"], int(_m * .60)
        if exactitud_pct >= 70: return  50, f"{exactitud_pct:.1f}% — ${int(_m*.50):,}/trim",   "#f97316",                int(_m * .50)
        return                           0, f"{exactitud_pct:.1f}% — $0",                       _SCORE_COLOR["rojo"],     0

    st.markdown(
        f'<div style="background:{_t["info_bg"]};border-left:4px solid #3b82f6;'
        f'border-radius:8px;padding:12px 16px;margin-bottom:12px;color:{_t["text"]};">'
        '<b>🏆 Desempeño Terreno</b> — Bono <b>$500.000 CLP/trimestre</b> (imponible) · '
        'pool compartido por todos los integrantes del equipo (seniors incluidos). '
        'Evaluado en 3 KPIs: '
        '<b>Productividad SLA</b> 40% ($200.000 pool) · '
        '<b>Efectividad MP</b> 30% ($150.000 pool) · '
        '<b>Precisión Fracttal</b> 30% ($150.000 pool). '
        f'<span style="font-size:0.82rem;color:{_t["muted"]};">'
        'Eq.1 Luis Pinto · Eq.2 Victor Bahamonde · Eq.3 Juan Gallardo · '
        'Eq.4 Carlos Avila · Eq.5 Luis Lopez</span></div>',
        unsafe_allow_html=True,
    )

    # ── Mapeo compartido: clave grupo → etiqueta display (usado en las 3 tabs) ──
    # Las claves de GRUPOS_TERRENO ya son el nombre del senior, por lo que
    # _EQUIPO_LABEL es una identidad — se mantiene para compatibilidad con el resto
    # del código sin necesidad de refactorizar cada uso.
    _EQUIPO_LABEL = {k: k for k in GRUPOS_TERRENO}
    _LABEL_TO_GRUPO = {v: k for k, v in _EQUIPO_LABEL.items()}

    def _norm_n(s: str) -> str:
        return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().strip().lower()

    # Índice: TODAS las combinaciones de 2 palabras de cada short-name del grupo
    # Esto cubre nombres chilenos de 2, 3 y 4 partes (ej. "Iván Ignacio Vergara Ferrari" → "Ignacio Ferrari")
    _GRUPOS_NORM: dict[str, str] = {}
    for _grp_k, _grp_v in GRUPOS_TERRENO.items():
        for _mb in _grp_v["miembros"]:
            _GRUPOS_NORM[_norm_n(_mb)] = _grp_k
            _pts = _mb.split()
            for _i in range(len(_pts)):
                for _j in range(_i + 1, len(_pts)):
                    _GRUPOS_NORM[_norm_n(f"{_pts[_i]} {_pts[_j]}")] = _grp_k

    # Conjunto normalizado de exclusiones (No aplica / AUTEC)
    _NO_APLICA_NORM: set[str] = set()
    for _na in TECNICOS_NO_APLICA:
        _NO_APLICA_NORM.add(_norm_n(_na))
        _na_pts = _na.split()
        for _i in range(len(_na_pts)):
            for _j in range(_i + 1, len(_na_pts)):
                _NO_APLICA_NORM.add(_norm_n(f"{_na_pts[_i]} {_na_pts[_j]}"))

    def _get_equipo(t) -> str:
        """Nombre completo (API o Excel) → clave grupo. Prueba todas las combinaciones de 2 palabras."""
        if not isinstance(t, str) or not t.strip():
            return "Sin equipo"
        norm = _norm_n(t.strip())
        if norm in _GRUPOS_NORM:
            return _GRUPOS_NORM[norm]
        parts = t.strip().split()
        for _i in range(len(parts)):
            for _j in range(_i + 1, len(parts)):
                alias = _norm_n(f"{parts[_i]} {parts[_j]}")
                if alias in _GRUPOS_NORM:
                    return _GRUPOS_NORM[alias]
        return "Sin equipo"

    def _es_excluido(t) -> bool:
        """True si el técnico está en TECNICOS_NO_APLICA."""
        if not isinstance(t, str) or not t.strip():
            return False
        norm = _norm_n(t.strip())
        if norm in _NO_APLICA_NORM:
            return True
        parts = t.strip().split()
        for _i in range(len(parts)):
            for _j in range(_i + 1, len(parts)):
                if _norm_n(f"{parts[_i]} {parts[_j]}") in _NO_APLICA_NORM:
                    return True
        return False

    # ── Pre-computar columna "equipo" en df_wo (una sola vez por carga) ──────
    # Evita 6+ apply(_get_equipo) sobre las filas en cada rerun de filtros.
    # Optimización: _get_equipo solo se llama para los ~20 técnicos únicos,
    # luego se mapea vectorizado a las 20k filas → mismo resultado, ~1000× más rápido.
    def _build_wo_eq():
        _tmp = st.session_state["df_wo_base"].copy()
        _unique = _tmp["technician"].dropna().unique()
        _tech_equipo = {t: _get_equipo(t) for t in _unique}
        _tmp["equipo"] = _tmp["technician"].map(_tech_equipo).fillna("Sin equipo")
        return _tmp

    _ph_eq = st.empty()
    if st.session_state.get("_sc_sig_df_wo_eq") != _wo_sig + "_eq":
        with _ph_eq.container():
            with st.spinner("⚙️ Clasificando técnicos por equipo…"):
                df_wo = _sc("df_wo_eq", _wo_sig + "_eq", _build_wo_eq)
    else:
        df_wo = _sc("df_wo_eq", _wo_sig + "_eq", _build_wo_eq)
    _ph_eq.empty()

    # ── Helper compartido por los 3 tabs: semanas domingo→sábado ─────────────
    import calendar as _cal
    from datetime import date as _date, timedelta as _td

    def _semanas_del_mes(ym: str) -> list[tuple]:
        """Retorna lista de (label, start_date, end_date) para el mes 'YYYY-MM'."""
        y, m = int(ym[:4]), int(ym[5:7])
        first = _date(y, m, 1)
        last  = _date(y, m, _cal.monthrange(y, m)[1])
        semanas, num, cur = [], 1, first
        while cur <= last:
            wd = cur.weekday()           # Mon=0 … Sat=5, Sun=6
            days_to_sat = 0 if wd == 5 else (6 if wd == 6 else 5 - wd)
            end = min(cur + _td(days=days_to_sat), last)
            semanas.append((
                f"Semana {num}  ({cur.strftime('%d/%m')} – {end.strftime('%d/%m')})",
                cur, end,
            ))
            num += 1
            cur = end + _td(days=1)
        return semanas

    tab_sla, tab_cal, tab_prec, tab_bono = st.tabs([
        "📊  Desempeño SLA",
        "🎯  Efectividad MP",
        "📋  Precisión Fracttal",
        "💲  Resumen Bono",
    ])

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 1 — PRODUCTIVIDAD (SLA)
    # ══════════════════════════════════════════════════════════════════════════
    with tab_sla:
        _tsla_desc, _tsla_ley = st.columns([3, 1])
        with _tsla_ley:
            st.markdown(
                f'<div style="background:{_t["card"]};border:1px solid {_t["border"]};'
                f'border-radius:8px;padding:10px 12px;font-size:0.76rem;color:{_t["text"]};'
                f'line-height:1.75;margin-top:2px;">'
                f'<div style="font-weight:700;color:{_t["muted"]};font-size:0.78rem;'
                f'margin-bottom:4px;letter-spacing:0.04em;">📊 ESCALA BONO SLA</div>'
                f'<span style="color:#22c55e;">≥ 95%</span> → <b>100%</b> · $200.000/trim<br>'
                f'<span style="color:#16a34a;">≥ 93%</span> → <b>90%</b> · $180.000/trim<br>'
                f'<span style="color:#4ade80;">≥ 90%</span> → <b>80%</b> · $160.000/trim<br>'
                f'<span style="color:#f59e0b;">≥ 85%</span> → <b>50%</b> · $100.000/trim<br>'
                f'<span style="color:#ef4444;">&lt; 85%</span> → <b>Sin bono</b>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with _tsla_desc:
            st.markdown(
                f'<div style="background:{_t["warn_bg"]};border-left:4px solid #01798A;'
                f'border-radius:8px;padding:10px 16px;margin-bottom:12px;color:{_t["text"]};">'
                '<b>KPI 1.1 — Productividad (SLA)</b> — % de llamados correctivos cerrados '
                'dentro del tiempo comprometido. Binario por llamado: cumple ✅ o no cumple ❌.<br>'
                # ── 3 tablas compactas por cliente ────────────────────────────────────────
                f'<div style="display:flex;gap:8px;margin-top:10px;">'

                # ── COPEC ──────────────────────────────────────────────────────────────────
                f'<div style="flex:1;min-width:0;border-radius:6px;overflow:hidden;'
                f'border:1px solid {_t["border"]};">'
                f'<div style="background:#CC0000;color:#fff;font-size:0.72rem;font-weight:700;'
                f'padding:3px 8px;text-align:center;letter-spacing:0.04em;">🔴 COPEC</div>'
                f'<table style="font-size:0.72rem;border-collapse:collapse;width:100%;">'
                f'<tr style="background:#0C2540;color:#fff;">'
                f'<th style="padding:2px 7px;font-weight:600;">Prio</th>'
                f'<th style="padding:2px 7px;font-weight:600;text-align:center;">Stgo</th>'
                f'<th style="padding:2px 7px;font-weight:600;text-align:center;">Reg.</th></tr>'
                f'<tr style="background:{_t["card"]};color:{_t["text"]};">'
                f'<td style="padding:2px 7px;font-weight:700;">P1</td>'
                f'<td style="padding:2px 7px;text-align:center;">18 h</td>'
                f'<td style="padding:2px 7px;text-align:center;">24 h</td></tr>'
                f'<tr style="background:{_t["tbl_alt"]};color:{_t["text"]};">'
                f'<td style="padding:2px 7px;font-weight:700;">P2</td>'
                f'<td style="padding:2px 7px;text-align:center;">24 h</td>'
                f'<td style="padding:2px 7px;text-align:center;">48 h</td></tr>'
                f'<tr style="background:{_t["card"]};color:{_t["text"]};">'
                f'<td style="padding:2px 7px;font-weight:700;">P3</td>'
                f'<td style="padding:2px 7px;text-align:center;">36 h</td>'
                f'<td style="padding:2px 7px;text-align:center;">72 h</td></tr>'
                f'<tr style="background:{_t["tbl_alt"]};color:{_t["text"]};">'
                f'<td style="padding:2px 7px;font-weight:700;">P4</td>'
                f'<td style="padding:2px 7px;text-align:center;" colspan="2">96 h</td></tr>'
                f'</table></div>'

                # ── ARAMCO (Esmax) ─────────────────────────────────────────────────────────
                f'<div style="flex:1;min-width:0;border-radius:6px;overflow:hidden;'
                f'border:1px solid {_t["border"]};">'
                f'<div style="background:#16A34A;color:#fff;font-size:0.72rem;font-weight:700;'
                f'padding:3px 8px;text-align:center;letter-spacing:0.04em;">🟢 Aramco (Esmax)</div>'
                f'<table style="font-size:0.72rem;border-collapse:collapse;width:100%;">'
                f'<tr style="background:#0C2540;color:#fff;">'
                f'<th style="padding:2px 7px;font-weight:600;">Prio</th>'
                f'<th style="padding:2px 7px;font-weight:600;text-align:center;">Stgo</th>'
                f'<th style="padding:2px 7px;font-weight:600;text-align:center;">Reg.</th></tr>'
                f'<tr style="background:{_t["card"]};color:{_t["text"]};">'
                f'<td style="padding:2px 7px;font-weight:700;">P1</td>'
                f'<td style="padding:2px 7px;text-align:center;" colspan="2">24 h</td></tr>'
                f'<tr style="background:{_t["tbl_alt"]};color:{_t["text"]};">'
                f'<td style="padding:2px 7px;font-weight:700;">P2</td>'
                f'<td style="padding:2px 7px;text-align:center;" colspan="2">48 h</td></tr>'
                f'<tr style="background:{_t["card"]};color:{_t["text"]};">'
                f'<td style="padding:2px 7px;font-weight:700;">P3</td>'
                f'<td style="padding:2px 7px;text-align:center;" colspan="2">72 h</td></tr>'
                f'<tr style="background:{_t["tbl_alt"]};color:{_t["text"]};">'
                f'<td style="padding:2px 7px;font-weight:700;">P4</td>'
                f'<td style="padding:2px 7px;text-align:center;" colspan="2">100 h</td></tr>'
                f'</table></div>'

                # ── SHELL (Enex) ───────────────────────────────────────────────────────────
                f'<div style="flex:1;min-width:0;border-radius:6px;overflow:hidden;'
                f'border:1px solid {_t["border"]};">'
                f'<div style="background:#D4A800;color:#fff;font-size:0.72rem;font-weight:700;'
                f'padding:3px 8px;text-align:center;letter-spacing:0.04em;">🟡 Shell (Enex)</div>'
                f'<table style="font-size:0.72rem;border-collapse:collapse;width:100%;">'
                f'<tr style="background:#0C2540;color:#fff;">'
                f'<th style="padding:2px 7px;font-weight:600;">Prio</th>'
                f'<th style="padding:2px 7px;font-weight:600;text-align:center;">Stgo</th>'
                f'<th style="padding:2px 7px;font-weight:600;text-align:center;">Reg.</th></tr>'
                f'<tr style="background:{_t["card"]};color:{_t["text"]};">'
                f'<td style="padding:2px 7px;font-weight:700;">P1</td>'
                f'<td style="padding:2px 7px;text-align:center;" colspan="2">24 h</td></tr>'
                f'<tr style="background:{_t["tbl_alt"]};color:{_t["text"]};">'
                f'<td style="padding:2px 7px;font-weight:700;">P2</td>'
                f'<td style="padding:2px 7px;text-align:center;" colspan="2">48 h</td></tr>'
                f'<tr style="background:{_t["card"]};color:{_t["text"]};">'
                f'<td style="padding:2px 7px;font-weight:700;">P3</td>'
                f'<td style="padding:2px 7px;text-align:center;" colspan="2">72 h</td></tr>'
                f'<tr style="background:{_t["tbl_alt"]};color:{_t["text"]};">'
                f'<td style="padding:2px 7px;font-weight:700;">P4</td>'
                f'<td style="padding:2px 7px;text-align:center;" colspan="2">96 h</td></tr>'
                f'</table></div>'

                f'</div></div>',
                unsafe_allow_html=True,
            )

        # ── Umbrales SLA por zona — fallback generico (usar SLA_HOURS por cliente cuando sea posible)
        # Los valores reales por contrato estan en gdrive.SLA_HOURS y en Supabase.sla_umbrales_horas
        # Este dict se usa solo cuando no hay cumplimiento explicito en el dato fuente
        _SLA_ZONA = {
            ("P1", "Santiago"): 18, ("P1", "Regiones"): 24,   # COPEC mas estricto
            ("P2", "Santiago"): 24, ("P2", "Regiones"): 48,   # Correcto segun contrato
            ("P3", "Santiago"): 36, ("P3", "Regiones"): 72,   # Correcto segun contrato
            ("P4", "Santiago"): 96, ("P4", "Regiones"): 96,
        }
        _SLA_DEFAULT = {"P1": 24, "P2": 48, "P3": 72, "P4": 96}  # Valores genericos conservadores

        if df_llamados.empty:
            st.warning("No se pudieron cargar los llamados de Google Drive.")
        else:
            # ── Preprocessing cacheado en session_state ───────────────────────
            # Clave: tamaño de df_llamados; si cambian los datos se recalcula.
            # v3 — usa hora exacta (gdrive.py combina Fecha+Hora) y cumplimiento del Excel
            _sla_key = f"_sla_proc_v5_{len(df_llamados)}"
            if _sla_key not in st.session_state:
                _src = df_llamados[
                    df_llamados["fecha_atencion"].notna() &
                    df_llamados["fecha_llamado"].notna()
                ].copy()

                # ── Horas de resolución ─────────────────────────────────────────
                # Prioridad 1: tiempo_resp_real del Excel (TMPO.RESP.REAL) — valor exacto
                # Prioridad 2: diferencia entre fecha_atencion y fecha_llamado
                #              (ya incluye hora exacta tras la corrección en gdrive.py)
                if "tiempo_resp_real" in _src.columns:
                    _src["horas_resolucion"] = (
                        pd.to_timedelta(_src["tiempo_resp_real"], errors="coerce")
                        .dt.total_seconds() / 3600
                    ).clip(lower=0).round(2)
                    _miss = _src["horas_resolucion"].isna()
                    if _miss.any():
                        _src.loc[_miss, "horas_resolucion"] = (
                            (pd.to_datetime(_src.loc[_miss, "fecha_atencion"]) -
                             pd.to_datetime(_src.loc[_miss, "fecha_llamado"]))
                            .dt.total_seconds() / 3600
                        ).clip(lower=0).round(2)
                else:
                    _src["horas_resolucion"] = (
                        (pd.to_datetime(_src["fecha_atencion"]) -
                         pd.to_datetime(_src["fecha_llamado"]))
                        .dt.total_seconds() / 3600
                    ).clip(lower=0).round(2)

                _src["prioridad"] = _src["prioridad"].fillna("").str.strip().str.upper()

                # ── Zona normalizada (consistente con gdrive._compute_sla) ───────
                def _norm_zona_sla(z: str) -> str:
                    z = str(z).strip().upper()
                    if not z or z == "NAN":
                        return "Santiago"
                    # RM y variantes = Región Metropolitana → Santiago
                    if z in ("RM", "R.M.", "R.M") or any(k in z for k in ("SANTIAGO", "METRO")):
                        return "Santiago"
                    # Norte, Sur, Centro, Regiones explícitas → Regiones
                    return "Regiones"

                if "zona" in _src.columns:
                    _src["zona_norm"] = _src["zona"].fillna("").astype(str).apply(_norm_zona_sla)
                else:
                    _src["zona_norm"] = "Santiago"

                # ── Excepciones SLA justificadas (no modifican el Excel) ─────────
                # OTs que el Excel marca "NO CUMPLE" pero fueron justificadas
                # posteriormente por causas fuera de la responsabilidad de Occimiano.
                # Se registran aquí para no tocar el archivo fuente de Google Drive.
                # Formato: "OS-XXXXX" — el match usa normalización (sin espacios, sin puntos).
                _SLA_OVERRIDE_CUMPLE: set = {
                    # Mayo 2026 — ESMAX/Aramco — causas externas justificadas
                    "OS-37055",
                    "OS-37448",
                    "OS-37547",
                }
                # Normalizar los folios de override para comparación robusta
                def _norm_folio(s: str) -> str:
                    return str(s).strip().upper().replace(" ", "").replace(".", "")
                _override_norm = {_norm_folio(f) for f in _SLA_OVERRIDE_CUMPLE}

                # ── cumple_sla ──────────────────────────────────────────────────
                # Fuente primaria: columna "cumplimiento" del Excel (STATUS CUMPLIMIENTO /
                # Status Cumplimiento) — ya normalizada a "CUMPLE" / "NO CUMPLE" en gdrive.py.
                # Fallback: comparar horas_resolucion vs umbral SLA por prioridad y zona.
                if "cumplimiento" in _src.columns:
                    _src["cumple_sla"] = _src["cumplimiento"].map(
                        {"CUMPLE": True, "NO CUMPLE": False}
                    )
                    # Aplicar excepciones: buscar en os_fracttal Y en n_llamado como fallback
                    _override_mask = pd.Series(False, index=_src.index)
                    if "os_fracttal" in _src.columns:
                        _override_mask |= (
                            _src["os_fracttal"].astype(str).apply(_norm_folio).isin(_override_norm)
                        )
                    # Forzar cumple=True solo en los que el Excel dice NO CUMPLE
                    # (no toca los que ya eran CUMPLE)
                    _nc_mask = _src["cumple_sla"] == False
                    _src.loc[_override_mask & _nc_mask, "cumple_sla"] = True
                    # Completar filas sin cumplimiento explícito con cálculo
                    _fallback_mask = _src["cumple_sla"].isna()
                    if _fallback_mask.any():
                        for _pri in ("P1", "P2", "P3", "P4"):
                            for _zona in ("Santiago", "Regiones"):
                                _mask = (_fallback_mask &
                                         (_src["prioridad"] == _pri) &
                                         (_src["zona_norm"] == _zona))
                                if _mask.any():
                                    _umb = _SLA_ZONA.get((_pri, _zona), _SLA_DEFAULT.get(_pri, 24))
                                    _src.loc[_mask, "cumple_sla"] = (
                                        _src.loc[_mask, "horas_resolucion"] <= _umb
                                    )
                else:
                    _src["cumple_sla"] = pd.NA
                    for _pri in ("P1", "P2", "P3", "P4"):
                        for _zona in ("Santiago", "Regiones"):
                            _mask = (_src["prioridad"] == _pri) & (_src["zona_norm"] == _zona)
                            if _mask.any():
                                _umb = _SLA_ZONA.get((_pri, _zona), _SLA_DEFAULT.get(_pri, 24))
                                _src.loc[_mask, "cumple_sla"] = (
                                    _src.loc[_mask, "horas_resolucion"] <= _umb
                                )

                # Mes y fecha base
                _src["fecha_llamado_dt"] = pd.to_datetime(_src["fecha_llamado"], errors="coerce")
                _src["mes"] = _src["fecha_llamado_dt"].dt.to_period("M").astype(str)

                # Normalizar nombres técnicos cortos → nombres completos
                _src["tecnico"] = _src["tecnico"].apply(
                    lambda t: _excel_to_full.get(str(t).strip(), str(t).strip())
                    if isinstance(t, str) and t.strip() else t
                )

                # Mapear técnico → equipo y filtrar excluidos (cacheado aquí →
                # no se recalcula en cada cambio de filtro, solo al cargar datos nuevos)
                _src["equipo"] = _src["tecnico"].apply(_get_equipo)
                _src = _src[~_src["tecnico"].apply(_es_excluido)].copy()
                _src["equipo_label"] = _src["equipo"].map(_EQUIPO_LABEL).fillna(_src["equipo"])

                st.session_state[_sla_key] = _src

            df_sla_src = st.session_state[_sla_key].copy()

            # ── Excepciones SLA — forzar cumple=True para OTs justificadas ──
            # df_llamados ya tiene cumplimiento="CUMPLE" para estas OTs (override global).
            # Este bloque sincroniza cumple_sla en caso de que el caché v5 todavía
            # no se haya reconstruido con los datos corregidos.
            _SLA_EXC_NUM = {"140785", "143926", "145331"}
            if "cumple_sla" in df_sla_src.columns:
                _ov_mask = pd.Series(False, index=df_sla_src.index)
                for _col in ["n_llamado", "LLAMADO", "N° llamado"]:
                    if _col in df_sla_src.columns:
                        _ov_mask |= df_sla_src[_col].astype(str).str.strip().isin(_SLA_EXC_NUM)
                        break
                for _col in ["os_fracttal", "OS FRACTTAL"]:
                    if _col in df_sla_src.columns:
                        _ov_mask |= df_sla_src[_col].astype(str).str.strip().str.upper().isin(
                            {"OS-37055", "OS-37448", "OS-37547"})
                        break
                df_sla_src.loc[_ov_mask, "cumple_sla"] = True

            # ── Filtros ───────────────────────────────────────────────────────
            # Mapeo abreviatura → número de mes (para filtro trimestral)
            _MES_ABR_NUM = {"Ene":1,"Feb":2,"Mar":3,"Abr":4,"May":5,"Jun":6,
                            "Jul":7,"Ago":8,"Sep":9,"Oct":10,"Nov":11,"Dic":12}
            _TRIMESTRES_SLA = {
                "T1 · Ene–Mar": [1,2,3], "T2 · Abr–Jun": [4,5,6],
                "T3 · Jul–Sep": [7,8,9], "T4 · Oct–Dic": [10,11,12],
            }
            _meses_sla = sorted(df_sla_src["mes"].dropna().unique(), reverse=True)
            # mes está en formato "YYYY-MM" → extraer número de mes directamente
            _meses_sla_nums = {int(str(m).split("-")[1]) for m in _meses_sla if "-" in str(m)}
            _trim_opts_sla = ["Todos"] + [
                k for k,v in _TRIMESTRES_SLA.items() if any(m in v for m in _meses_sla_nums)
            ]

            _sf0, _sf1, _sf2, _sf3, _sf4 = st.columns([1.5, 1.4, 1.6, 2, 2])
            with _sf0:
                _trim_sla = st.selectbox("Período", _trim_opts_sla, key="sla_trim")
            with _sf1:
                # Filtrar meses al trimestre seleccionado
                if _trim_sla != "Todos":
                    _trim_m_sla = _TRIMESTRES_SLA[_trim_sla]
                    _meses_sla_disp = [m for m in _meses_sla
                                       if int(str(m).split("-")[1]) in _trim_m_sla]
                else:
                    _meses_sla_disp = _meses_sla
                _MESES_FULL_SLA = {
                    "01":"Enero","02":"Febrero","03":"Marzo","04":"Abril","05":"Mayo","06":"Junio",
                    "07":"Julio","08":"Agosto","09":"Septiembre","10":"Octubre","11":"Noviembre","12":"Diciembre",
                }
                def _ym_sla_lbl(ym):
                    p = str(ym).split("-")
                    return f"{_MESES_FULL_SLA.get(p[1], p[1])} {p[0][2:]}" if len(p) == 2 else ym
                _lbl_to_per_sla  = {_ym_sla_lbl(m): m for m in _meses_sla}
                _meses_sla_disp_lbl = [_ym_sla_lbl(m) for m in _meses_sla_disp]
                _mes_sla_lbl = st.multiselect("Mes", _meses_sla_disp_lbl, key="sla_mes",
                                              placeholder="Todos los meses")
                _mes_sla = [_lbl_to_per_sla[l] for l in _mes_sla_lbl if l in _lbl_to_per_sla]
            with _sf2:
                if len(_mes_sla) == 1:
                    _sems = _semanas_del_mes(_mes_sla[0])
                    _sem_opts = ["Todas"] + [s[0] for s in _sems]
                else:
                    _sem_opts = ["Todas"]
                _sem_sla = st.selectbox("Semana", _sem_opts, key="sla_semana")
            with _sf3:
                _equipos_con_datos = df_sla_src["equipo"].unique()
                _equipos_disp = ["Todos"] + [
                    lbl for grp, lbl in _EQUIPO_LABEL.items()
                    if grp in _equipos_con_datos
                ]
                _equipo_sla = st.selectbox("Equipo", _equipos_disp, key="sla_equipo")
            with _sf4:
                if _equipo_sla != "Todos":
                    _grp_key_filt = _LABEL_TO_GRUPO.get(_equipo_sla, "")
                    # Nombres desde el dato real (igual que Precisión) — incluye al senior
                    _tec_sla_opts = ["Todos"] + sorted(
                        t for t in df_sla_src[df_sla_src["equipo"] == _grp_key_filt]["tecnico"].dropna().unique()
                        if not _es_excluido(t)
                    )
                else:
                    _tec_sla_opts = ["Todos"] + sorted(
                        t for t in df_sla_src["tecnico"].dropna().unique()
                        if not _es_excluido(t) and _get_equipo(t) != "Sin equipo"
                    )
                _tec_sla_sel = st.selectbox("Técnico", _tec_sla_opts, key="sla_tecnico")

            _df_sla = df_sla_src.copy()
            # Período trimestral — mes está en formato "YYYY-MM", extraer número de mes
            if _trim_sla != "Todos":
                _trim_months_sla = _TRIMESTRES_SLA[_trim_sla]
                _df_sla = _df_sla[
                    _df_sla["mes"].apply(
                        lambda m: int(str(m).split("-")[1]) if "-" in str(m) else 0
                    ).isin(_trim_months_sla)
                ]
            if _mes_sla:  # lista no vacía = selección específica
                _df_sla = _df_sla[_df_sla["mes"].astype(str).isin([str(m) for m in _mes_sla])]
            if _sem_sla != "Todas" and len(_mes_sla) == 1:
                _sem_match = next((s for s in _sems if s[0] == _sem_sla), None)
                if _sem_match:
                    _sem_start, _sem_end = _sem_match[1], _sem_match[2]
                    _df_sla = _df_sla[
                        (_df_sla["fecha_llamado_dt"].dt.date >= _sem_start) &
                        (_df_sla["fecha_llamado_dt"].dt.date <= _sem_end)
                    ]
            if _equipo_sla != "Todos":
                _grp_sla = _LABEL_TO_GRUPO.get(_equipo_sla, _equipo_sla)
                _df_sla = _df_sla[_df_sla["equipo"] == _grp_sla]
            if _tec_sla_sel != "Todos":
                _df_sla = _df_sla[_df_sla["tecnico"] == _tec_sla_sel]

            st.divider()

            _df_con_pri = _df_sla[_df_sla["cumple_sla"].notna()].copy()
            _total_ll   = len(_df_sla)
            _con_pri    = len(_df_con_pri)
            _sin_pri    = _total_ll - _con_pri
            _pct_cumple = (float(_df_con_pri["cumple_sla"].sum()) / _con_pri * 100) if _con_pri > 0 else 0.0
            _bono_global, _bono_global_lbl, _, _bono_global_clp = _bono_sla(_pct_cumple)

            # ── Debug temporal: mostrar cuántas excepciones se aplicaron ────────
            _ov_aplicadas = int(_ov_mask.sum()) if "_ov_mask" in dir() else 0
            if _ov_aplicadas > 0:
                st.caption(f"ℹ️ {_ov_aplicadas} excepción(es) SLA aplicada(s) (causas externas justificadas)")

            _sm1, _sm2, _sm3, _sm4 = st.columns(4)
            _sm1.metric("Llamados cerrados", f"{_total_ll:,}")
            _sm2.metric("Con prioridad asignada", f"{_con_pri:,}")
            _cumple_abs = int(_df_con_pri["cumple_sla"].sum()) if not _df_con_pri.empty else 0
            _sm3.metric(
                "% Cumple SLA",
                f"{_pct_cumple:.1f}%",
                delta=f"{_cumple_abs:,} / {_con_pri:,} llamados",
                delta_color="off",
                help=f"Cálculo ponderado: {_cumple_abs:,} llamados que cumplen ÷ {_con_pri:,} total = {_pct_cumple:.1f}%. "
                     "No es promedio de % por cliente — cada llamado pesa igual independiente del cliente.",
            )
            _sm4.metric(
                "KPI Productividad (40%)",
                f"${_bono_global_clp:,.0f}",
                delta=f"{_bono_global_lbl} bono · max ${_MAX_SLA_CLP:,}",
                delta_color="off",
            )

            if _con_pri > 0:
                st.divider()
                # ── Tarjetas por equipo ───────────────────────────────────────
                st.markdown('<div class="section-header">👥 Cumplimiento SLA por equipo</div>',
                            unsafe_allow_html=True)
                _eq_sum = _df_con_pri[_df_con_pri["equipo"] != "Sin equipo"].groupby("equipo").agg(
                    total=("cumple_sla", "count"),
                    cumple=("cumple_sla", "sum"),
                    horas_prom=("horas_resolucion", "mean"),
                ).reset_index()
                _eq_sum["pct_sla"]      = (_eq_sum["cumple"] / _eq_sum["total"] * 100).round(1)
                _eq_sum["horas_prom"]   = _eq_sum["horas_prom"].round(1)
                _eq_sum["senior"]       = _eq_sum["equipo"].apply(
                    lambda g: GRUPOS_TERRENO.get(g, {}).get("senior", "")
                )
                _eq_sum["equipo_label"] = _eq_sum["equipo"].map(_EQUIPO_LABEL).fillna(_eq_sum["equipo"])

                _cols_eq = st.columns(max(1, len(_eq_sum)))
                for _col_ui, _row_eq in zip(_cols_eq, _eq_sum.itertuples()):
                    _bono_val, _bono_lbl, _col_eq, _bono_clp = _bono_sla(_row_eq.pct_sla)
                    # Título: técnico específico si está seleccionado y esta tarjeta tiene sus datos
                    if _tec_sla_sel != "Todos":
                        _sla_titulo    = _tec_sla_sel
                        _sla_subtitulo = f"Equipo: {_row_eq.equipo_label}"
                    else:
                        _sla_titulo    = _row_eq.equipo_label
                        _sla_subtitulo = f"Senior: {_row_eq.senior}"
                    _col_ui.markdown(
                        f'<div style="background:{_t["card"]};border:2px solid {_col_eq}33;'
                        f'border-radius:8px;padding:14px 16px;text-align:center;">'
                        f'<div style="font-weight:700;font-size:1rem;color:{_t["text"]};">'
                        f'{_sla_titulo}</div>'
                        f'<div style="font-size:0.8rem;color:{_t["muted"]};margin-bottom:8px;">'
                        f'{_sla_subtitulo}</div>'
                        f'<div style="font-size:1.6rem;font-weight:800;color:{_col_eq};">'
                        f'{_row_eq.pct_sla:.1f}% SLA</div>'
                        f'<div style="background:{_col_eq};color:#fff;border-radius:4px;'
                        f'padding:2px 8px;font-size:0.82rem;font-weight:700;'
                        f'margin:6px auto 4px;display:inline-block;">'
                        f'{_bono_lbl} → ${_bono_clp:,.0f}</div>'
                        f'<div style="font-size:0.78rem;color:{_t["muted"]};">'
                        f'{int(_row_eq.cumple)}/{int(_row_eq.total)} llamados '
                        f'· prom {_row_eq.horas_prom}h</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                # ── Evolución mensual del cumplimiento SLA ────────────────────
                st.divider()
                st.markdown('<div class="section-header">📈 Evolución mensual — Cumplimiento SLA</div>',
                            unsafe_allow_html=True)

                # Helper local (mismo que _m2l de Precisión — definido aquí para evitar NameError)
                _MN_SLA = {1:"Ene",2:"Feb",3:"Mar",4:"Abr",5:"May",6:"Jun",
                           7:"Jul",8:"Ago",9:"Sep",10:"Oct",11:"Nov",12:"Dic"}
                def _sla_m2l(ym):
                    p = str(ym).split("-")
                    return f"{_MN_SLA.get(int(p[1]),p[1])} '{p[0][2:]}" if len(p)==2 else str(ym)

                _sla_hist = _df_con_pri.copy()
                if not _sla_hist.empty and "fecha_llamado" in _sla_hist.columns:
                    _sla_hist["_mes"] = pd.to_datetime(
                        _sla_hist["fecha_llamado"], errors="coerce"
                    ).dt.to_period("M").astype(str)
                    _sla_trend = (
                        _sla_hist.groupby("_mes")
                        .agg(total=("cumple_sla","count"), cumple=("cumple_sla","sum"),
                             horas_prom=("horas_resolucion","mean"))
                        .reset_index().sort_values("_mes")
                    )
                    _sla_trend["pct_sla"]   = (_sla_trend["cumple"] / _sla_trend["total"] * 100).round(1)
                    _sla_trend["pct_nc"]    = (100 - _sla_trend["pct_sla"]).round(1)
                    _sla_trend["horas_prom"] = _sla_trend["horas_prom"].round(1)
                    _sla_trend["mes_lbl"]   = _sla_trend["_mes"].apply(_sla_m2l)

                    _sla_ev_sig = f"_fig_sla_ev_{_current_theme}_{_wo_sig}_{_equipo_sla}_{_tec_sla_sel}_{len(_df_con_pri)}"
                    if _sla_ev_sig not in st.session_state:
                        _fig_sla_ev = make_subplots(specs=[[{"secondary_y": True}]])
                        _fig_sla_ev.add_trace(go.Bar(
                            x=_sla_trend["mes_lbl"], y=_sla_trend["total"],
                            name="Llamados cerrados", marker_color="#94a3b8", opacity=0.85,
                            text=_sla_trend["total"], textposition="inside",
                            textfont=dict(size=11, color="#ffffff"),
                        ), secondary_y=False)
                        _fig_sla_ev.add_trace(go.Scatter(
                            x=_sla_trend["mes_lbl"], y=_sla_trend["pct_sla"],
                            name="% Cumple SLA", mode="lines+markers",
                            line=dict(color="#22c55e", width=3),
                            marker=dict(size=10, color="#22c55e", line=dict(color="#fff", width=2)),
                        ), secondary_y=True)
                        for _, _r in _sla_trend.iterrows():
                            _col_ann = "#22c55e" if _r["pct_sla"] >= 95 else ("#f59e0b" if _r["pct_sla"] >= 85 else "#ef4444")
                            _fig_sla_ev.add_annotation(
                                x=_r["mes_lbl"], y=_r["pct_sla"], yref="y2",
                                text=f"<b>{_r['pct_sla']:.1f}%</b>  {int(_r['cumple'])}/{int(_r['total'])}",
                                showarrow=False, yanchor="bottom", yshift=10,
                                font=dict(size=11, color=_col_ann),
                                bgcolor="rgba(255,255,255,0.88)",
                                bordercolor=_col_ann, borderwidth=1.5, borderpad=4,
                            )
                        _fig_sla_ev.add_trace(go.Scatter(
                            x=_sla_trend["mes_lbl"], y=_sla_trend["pct_nc"],
                            name="% No cumple", mode="lines+markers",
                            line=dict(color="#ef4444", width=2, dash="dot"),
                            marker=dict(size=7, color="#ef4444"),
                            visible="legendonly",
                        ), secondary_y=True)
                        _fig_sla_ev.add_hline(y=95, line_dash="dash", line_color="#22c55e",
                                              annotation_text="Meta 95%",
                                              annotation_position="top left", line_width=1.5,
                                              secondary_y=True)
                        _fig_sla_ev.update_layout(
                            height=380, margin=dict(t=40, b=20),
                            legend=dict(orientation="h", y=1.08, x=0), bargap=0.3,
                        )
                        _fig_sla_ev.update_yaxes(title_text="Llamados", secondary_y=False)
                        _fig_sla_ev.update_yaxes(
                            title_text="% Cumplimiento SLA", secondary_y=True,
                            tickformat=".1f", ticksuffix="%", range=[0, 115]
                        )
                        _apply_plot_theme(_fig_sla_ev)
                        st.session_state[_sla_ev_sig] = _fig_sla_ev
                    st.plotly_chart(st.session_state[_sla_ev_sig], width="stretch")

                # ── Ranking de técnicos ───────────────────────────────────────
                st.divider()
                st.markdown('<div class="section-header">👷 Ranking de técnicos — Cumplimiento SLA</div>',
                            unsafe_allow_html=True)
                _tec_sla_rank = _df_con_pri.groupby(["tecnico", "equipo"]).agg(
                    llamados=("cumple_sla", "count"),
                    cumple=("cumple_sla",   "sum"),
                    horas_prom=("horas_resolucion", "mean"),
                ).reset_index()
                _tec_sla_rank["pct_sla"]    = (_tec_sla_rank["cumple"] / _tec_sla_rank["llamados"] * 100).round(1)
                _tec_sla_rank["horas_prom"] = _tec_sla_rank["horas_prom"].round(1)
                _tec_sla_rank = _tec_sla_rank.sort_values("pct_sla", ascending=True)
                _tec_sla_rank["nivel"] = _tec_sla_rank["pct_sla"].apply(
                    lambda s: f"100% — ${_MAX_SLA_CLP:,} (≥95%)"         if s >= 95
                    else (f"90% — ${int(_MAX_SLA_CLP*.90):,} (93-95%)"    if s >= 93
                    else (f"80% — ${int(_MAX_SLA_CLP*.80):,} (90-93%)"    if s >= 90
                    else (f"50% — ${int(_MAX_SLA_CLP*.50):,} (85-90%)"    if s >= 85
                    else "Sin bono (<85%)")))
                )
                _tec_sla_rank["equipo_label"] = _tec_sla_rank["equipo"].map(_EQUIPO_LABEL).fillna(_tec_sla_rank["equipo"])
                _tec_sla_rank["label"] = (
                    _tec_sla_rank["tecnico"] + " (" + _tec_sla_rank["equipo_label"] + ")"
                )
                _tec_sla_rank["texto"] = _tec_sla_rank.apply(
                    lambda r: f"{r['pct_sla']:.1f}% → {_bono_sla(r['pct_sla'])[1]}", axis=1
                )

                _sla_rank_sig = f"{_wo_sig}_{_equipo_sla}_{_tec_sla_sel}_{_mes_sla}_{_trim_sla}_{_sem_sla}"
                _sla_rank_k   = f"_fig_sla_ranking_{_current_theme}_{_sla_rank_sig}"
                if _sla_rank_k not in st.session_state:
                    _fig_tec_sla = px.bar(
                        _tec_sla_rank,
                        x="pct_sla", y="label",
                        orientation="h",
                        color="nivel",
                        color_discrete_map={
                            f"100% — ${_MAX_SLA_CLP:,} (≥95%)":       "#1a6b2e",   # verde oscuro
                            f"90% — ${int(_MAX_SLA_CLP*.90):,} (93-95%)": "#27ae60",   # verde
                            f"80% — ${int(_MAX_SLA_CLP*.80):,} (90-93%)": "#7dbb1a",   # verde lima
                            f"50% — ${int(_MAX_SLA_CLP*.50):,} (85-90%)": "#f39c12",   # naranja
                            "Sin bono (<85%)":                              "#e74c3c",   # rojo
                        },
                        category_orders={"nivel": [
                            f"100% — ${_MAX_SLA_CLP:,} (≥95%)",
                            f"90% — ${int(_MAX_SLA_CLP*.90):,} (93-95%)",
                            f"80% — ${int(_MAX_SLA_CLP*.80):,} (90-93%)",
                            f"50% — ${int(_MAX_SLA_CLP*.50):,} (85-90%)",
                            "Sin bono (<85%)",
                        ]},
                        text="texto",
                        labels={"pct_sla": "% Cumplimiento SLA", "label": ""},
                    )
                    _fig_tec_sla.add_vline(x=95, line_dash="dash", line_color="#27ae60",
                                           annotation_text="100% bono (≥95%)",
                                           annotation_position="top right", line_width=1.5)
                    _fig_tec_sla.add_vline(x=85, line_dash="dot", line_color="#e74c3c",
                                           annotation_text="Mín. 85%",
                                           annotation_position="top left", line_width=1.5)
                    _fig_tec_sla.update_traces(textposition="outside", textfont=dict(size=12))
                    _fig_tec_sla.update_layout(
                        height=max(300, len(_tec_sla_rank) * 45 + 80),
                        margin=dict(t=20, b=20, l=10, r=160),
                        xaxis=dict(range=[0, 118]),
                        yaxis=dict(categoryorder="array",
                                   categoryarray=_tec_sla_rank["label"].tolist()),
                        legend_title="Bono SLA",
                    )
                    _apply_plot_theme(_fig_tec_sla)
                    st.session_state[_sla_rank_k] = _fig_tec_sla
                st.plotly_chart(st.session_state[_sla_rank_k], width="stretch")

                # ── Detalle SLA por técnico (tabla dinámica por mes / semana) ─
                st.divider()
                st.markdown('<div class="section-header">👤 Detalle SLA por técnico</div>',
                            unsafe_allow_html=True)

                _MES_NUM_ABR = {1:"Ene",2:"Feb",3:"Mar",4:"Abr",5:"May",6:"Jun",
                                7:"Jul",8:"Ago",9:"Sep",10:"Oct",11:"Nov",12:"Dic"}

                def _mes_lbl_tec(ym: str) -> str:
                    p = str(ym).split("-")
                    return f"{_MES_NUM_ABR.get(int(p[1]), p[1])} '{p[0][2:]}" if len(p)==2 else ym

                _df_tec = _df_con_pri[_df_con_pri["equipo"] != "Sin equipo"].copy()

                # ── Determinar granularidad ───────────────────────────────────
                _use_semanas = len(_mes_sla) == 1

                if _use_semanas:
                    # Una semana por columna (solo cuando hay exactamente 1 mes)
                    _sems_tec = _semanas_del_mes(_mes_sla[0])
                    def _get_sem(dt):
                        if pd.isna(dt): return None
                        d = dt.date() if hasattr(dt, "date") else dt
                        for _lbl, _s, _e in _sems_tec:
                            if _s <= d <= _e: return _lbl
                        return None
                    _df_tec["_periodo"] = _df_tec["fecha_llamado_dt"].apply(_get_sem)
                    _df_tec = _df_tec[_df_tec["_periodo"].notna()]
                    _cols_orden = [s[0] for s in _sems_tec]
                    _subttl = f"Vista semanal — {_mes_lbl_tec(_mes_sla[0])}"
                else:
                    # Un mes por columna
                    _df_tec["_periodo"] = _df_tec["mes"].astype(str)
                    _cols_orden = sorted(_df_tec["_periodo"].dropna().unique())
                    _subttl = "Vista mensual"

                # ── Agrupar por técnico + período ─────────────────────────────
                _tec_g = (
                    _df_tec.groupby(["equipo", "tecnico", "_periodo"])
                    .agg(total=("cumple_sla","count"), cumple=("cumple_sla","sum"))
                    .reset_index()
                )
                _tec_g["pct"] = (
                    _tec_g["cumple"] / _tec_g["total"] * 100
                ).round(1).where(_tec_g["total"] > 0)

                # Total global por técnico (última columna)
                _tec_tot = (
                    _df_tec.groupby(["equipo","tecnico"])
                    .agg(total=("cumple_sla","count"), cumple=("cumple_sla","sum"))
                    .reset_index()
                )
                _tec_tot["pct"] = (_tec_tot["cumple"] / _tec_tot["total"] * 100).round(1)

                # ── Pivot ────────────────────────────────────────────────────
                if not _tec_g.empty:
                    _pivot = _tec_g.pivot_table(
                        index=["equipo","tecnico"], columns="_periodo",
                        values="pct", aggfunc="first"
                    ).reset_index()
                    _pivot.columns.name = None

                    # Unir total
                    _pivot = _pivot.merge(
                        _tec_tot[["equipo","tecnico","pct"]].rename(columns={"pct":"Total"}),
                        on=["equipo","tecnico"], how="left"
                    )

                    # Renombrar columnas de período
                    if not _use_semanas:
                        _pivot = _pivot.rename(columns={m: _mes_lbl_tec(m) for m in _cols_orden})
                        _cols_disp_tec = [_mes_lbl_tec(m) for m in _cols_orden
                                          if _mes_lbl_tec(m) in _pivot.columns]
                    else:
                        _cols_disp_tec = [c for c in _cols_orden if c in _pivot.columns]

                    # Añadir etiqueta de equipo
                    _pivot["equipo_lbl"] = _pivot["equipo"].map(_EQUIPO_LABEL).fillna(_pivot["equipo"])
                    _pivot = _pivot.rename(columns={"tecnico": "Técnico"})
                    _pivot = _pivot.sort_values(["equipo_lbl", "Técnico"])

                    _cols_final = ["equipo_lbl", "Técnico"] + _cols_disp_tec + ["Total"]
                    _cols_final = [c for c in _cols_final if c in _pivot.columns]

                    # column_config dinámico
                    _col_cfg_tec = {
                        "equipo_lbl": st.column_config.TextColumn("Equipo", width=130),
                        "Técnico":    st.column_config.TextColumn(width=180),
                        "Total":      st.column_config.ProgressColumn(
                            "Total", min_value=0, max_value=100, format="%.1f%%"),
                    }
                    for _pc in _cols_disp_tec:
                        _col_cfg_tec[_pc] = st.column_config.ProgressColumn(
                            _pc, min_value=0, max_value=100, format="%.1f%%"
                        )

                    st.caption(f"**{_subttl}** · % Cumplimiento SLA por técnico y período")
                    _show_df(
                        _pivot[_cols_final], hide_index=True, width="stretch",
                        column_config=_col_cfg_tec,
                    )
                else:
                    st.info("Sin datos suficientes para generar el detalle por técnico.")

                # ── Tabla detalle llamados ────────────────────────────────────
                st.divider()
                st.markdown('<div class="section-header">📋 Detalle de llamados</div>',
                            unsafe_allow_html=True)

                _df_sla_disp = _df_sla.copy()

                # a) Fecha exacta (dd/MM/yyyy)
                if "fecha_llamado_dt" in _df_sla_disp.columns:
                    _df_sla_disp["_fecha_exacta"] = (
                        _df_sla_disp["fecha_llamado_dt"]
                        .dt.strftime("%d/%m/%Y").fillna("—")
                    )
                    # b) Hora inicio SLA (HH:MM) desde la misma columna datetime
                    _df_sla_disp["_hora_inicio"] = (
                        _df_sla_disp["fecha_llamado_dt"]
                        .dt.strftime("%H:%M").fillna("—")
                    )
                elif "fecha_llamado" in _df_sla_disp.columns:
                    _fl = pd.to_datetime(_df_sla_disp["fecha_llamado"], errors="coerce")
                    _df_sla_disp["_fecha_exacta"] = _fl.dt.strftime("%d/%m/%Y").fillna("—")
                    _df_sla_disp["_hora_inicio"]  = _fl.dt.strftime("%H:%M").fillna("—")
                else:
                    _df_sla_disp["_fecha_exacta"] = "—"
                    _df_sla_disp["_hora_inicio"]  = "—"

                # b) Hora cierre (de fecha_atencion)
                if "fecha_atencion" in _df_sla_disp.columns:
                    _df_sla_disp["_hora_cierre"] = (
                        pd.to_datetime(_df_sla_disp["fecha_atencion"], errors="coerce")
                        .dt.strftime("%H:%M").fillna("—")
                    )
                elif "hora_fin" in _df_sla_disp.columns:
                    _df_sla_disp["_hora_cierre"] = (
                        _df_sla_disp["hora_fin"].apply(
                            lambda t: t.strftime("%H:%M") if hasattr(t, "strftime") else "—"
                        )
                    )
                else:
                    _df_sla_disp["_hora_cierre"] = "—"

                # c) Observación del técnico — join con df_wo por os_fracttal = folio
                _note_map = (
                    df_wo.dropna(subset=["note"])
                    .set_index("folio")["note"]
                    .to_dict()
                ) if not df_wo.empty and "note" in df_wo.columns else {}

                if "os_fracttal" in _df_sla_disp.columns and _note_map:
                    _df_sla_disp["_observacion"] = (
                        _df_sla_disp["os_fracttal"]
                        .astype(str).str.strip()
                        .map(_note_map)
                        .fillna("—")
                    )
                else:
                    _df_sla_disp["_observacion"] = "—"

                # d) EDS código y OT
                # eds_occim ya existe; os_fracttal ya existe
                _df_sla_disp["cumple_sla"] = _df_sla_disp["cumple_sla"].apply(
                    lambda x: "✅ Sí" if x is True else ("❌ No" if x is False else "—")
                )

                _cols_final = [c for c in [
                    "os_fracttal", "eds_occim",
                    "equipo_label", "tecnico", "cliente", "eds_nombre",
                    "_fecha_exacta", "_hora_inicio", "_hora_cierre",
                    "prioridad", "zona_norm",
                    "horas_resolucion", "cumple_sla",
                    "_observacion",
                ] if c in _df_sla_disp.columns]

                _df_sla_disp = _df_sla_disp[_cols_final].copy()
                _df_sla_disp.rename(columns={
                    "os_fracttal":     "OT (OS-XXXXX)",
                    "eds_occim":       "Cód. EDS",
                    "equipo_label":    "Equipo",
                    "tecnico":         "Técnico",
                    "cliente":         "Cliente",
                    "eds_nombre":      "Estación",
                    "_fecha_exacta":   "Fecha atención",
                    "_hora_inicio":    "Hora inicio SLA",
                    "_hora_cierre":    "Hora cierre OT",
                    "prioridad":       "Prioridad",
                    "zona_norm":       "Zona",
                    "horas_resolucion":"Horas resolución",
                    "cumple_sla":      "Cumple SLA",
                    "_observacion":    "Observación técnico",
                }, inplace=True)

                _show_df(
                    _df_sla_disp,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "OT (OS-XXXXX)":       st.column_config.TextColumn(width=120),
                        "Cód. EDS":            st.column_config.TextColumn(width=90),
                        "Fecha atención":      st.column_config.TextColumn(width=105),
                        "Hora inicio SLA":     st.column_config.TextColumn(width=110),
                        "Hora cierre OT":      st.column_config.TextColumn(width=105),
                        "Horas resolución":    st.column_config.NumberColumn(format="%.1f h"),
                        "Observación técnico": st.column_config.TextColumn(width=300),
                        "Estación":            st.column_config.TextColumn(width=200),
                    },
                )


    # ══════════════════════════════════════════════════════════════════════════
    # TAB 2 — CALIDAD (REINCIDENCIAS)
    # ══════════════════════════════════════════════════════════════════════════
    with tab_cal:
        _tcal_desc, _tcal_ley = st.columns([3, 1])
        with _tcal_ley:
            st.markdown(
                f'<div style="background:{_t["card"]};border:1px solid {_t["border"]};'
                f'border-radius:8px;padding:10px 12px;font-size:0.76rem;color:{_t["text"]};'
                f'line-height:1.75;margin-top:2px;">'
                f'<div style="font-weight:700;color:{_t["muted"]};font-size:0.78rem;'
                f'margin-bottom:4px;letter-spacing:0.04em;">📊 ESCALA BONO EFECTIVIDAD MP</div>'
                f'<span style="color:#22c55e;">≥ 98%</span> → <b>100%</b> · $105.000/trim<br>'
                f'<span style="color:#16a34a;">≥ 96%</span> → <b>90%</b> · $94.500/trim<br>'
                f'<span style="color:#4ade80;">≥ 94%</span> → <b>80%</b> · $84.000/trim<br>'
                f'<span style="color:#65a30d;">≥ 92%</span> → <b>70%</b> · $73.500/trim<br>'
                f'<span style="color:#f59e0b;">≥ 90%</span> → <b>60%</b> · $63.000/trim<br>'
                f'<span style="color:#ef4444;">&lt; 90%</span> → <b>Sin bono</b>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with _tcal_desc:
            st.markdown(
                f'<div style="background:{_t["warn_bg"]};border-left:4px solid #01798A;'
                f'border-radius:8px;padding:10px 16px;margin-bottom:8px;color:{_t["text"]};">'
                '<b>KPI 1.2 — Efectividad MP</b> — Fallas post-preventiva: correctivo generado '
                'dentro de los <b>5 días siguientes</b> a un mantenimiento preventivo en el '
                'mismo equipo. El error se imputa al técnico que realizó el <b>preventivo</b> '
                '(debió detectar o resolver el problema en la mantención). '
                'Excepción: causa del cliente → no aplica.</div>',
                unsafe_allow_html=True,
            )
        st.markdown(
            f'<div style="background:{_t["err_bg"]};border-left:4px solid #ef4444;'
            f'border-radius:8px;padding:10px 16px;margin-bottom:12px;font-size:0.9rem;color:{_t["text"]};">'
            '<b>⚠️ Criterio de atribución — Sin información:</b> '
            'Si dentro del rango de 5 días el técnico del correctivo <b>no registra el tipo de falla</b>, '
            'deja el campo vacío o selecciona "Sin Información", '
            '<b>el error se considera atribuible al técnico que realizó el preventivo.</b> '
            'Al no existir una declaración explícita de causa externa (F.N.A.O), '
            'Occimiano no puede quedar exento de responsabilidad por omisión de información. '
            'La única forma de excluir un correctivo es que el técnico declare formalmente '
            '<b>"F.N.A.O — Falla No Atribuible a Occimiano"</b> con su respectiva justificación.</div>',
            unsafe_allow_html=True,
        )

        # Cachear en session_state — no recomputa al cambiar filtros
        # .copy() evita mutar el objeto cacheado al añadir columnas (fecha_cm_dt, mes)
        # NOTA: solo COPEC/ESMAX/SHELL entran al cálculo de reincidencias.
        # Abastible y otros clientes se excluyen aquí para no contaminar el KPI.
        _CLIENTES_SLA = {"COPEC", "Aramco (Esmax)", "ESMAX (Aramco)", "SHELL (Enex)"}
        _df_wo_reinc = df_wo[df_wo["client"].isin(_CLIENTES_SLA)].copy() \
                       if "client" in df_wo.columns else df_wo
        try:
            if st.session_state.get("_sc_sig_df_reinc") != _wo_sig:
                with st.spinner("⚙️ Calculando reincidencias…"):
                    df_reinc = _sc("df_reinc", _wo_sig,
                                    lambda: build_reincidencias(_df_wo_reinc, _excel_to_full)).copy()
            else:
                df_reinc = _sc("df_reinc", _wo_sig,
                                lambda: build_reincidencias(_df_wo_reinc, _excel_to_full)).copy()
        except Exception as _e_cal:
            st.error(f"⚠️ Error al cargar datos de reincidencias: {_e_cal}")
            st.exception(_e_cal)
            st.stop()

        # Filtro desde enero 2026 — KPI aplica solo al año en curso
        if not df_reinc.empty and "fecha_cm" in df_reinc.columns:
            df_reinc = df_reinc[
                pd.to_datetime(df_reinc["fecha_cm"], errors="coerce") >= pd.Timestamp("2026-01-01")
            ].copy()
        elif not df_reinc.empty and "fecha2" in df_reinc.columns:
            df_reinc = df_reinc[
                pd.to_datetime(df_reinc["fecha2"], errors="coerce") >= pd.Timestamp("2026-01-01")
            ].copy()

        # Compatibilidad: si llegara un df con columnas viejas (caché stale), renombrar
        if not df_reinc.empty and "fecha2" in df_reinc.columns:
            df_reinc = df_reinc.rename(columns={
                "folio1": "folio_pm", "fecha1": "fecha_pm",
                "folio2": "folio_cm", "fecha2": "fecha_cm",
                "tecnico2": "tecnico_cm",
            })

        # ── Filtros PRIMERO — necesarios para que las tarjetas reflejen el período ──
        if df_reinc.empty or "fecha_cm" not in df_reinc.columns:
            st.success("✅ Sin fallas post-preventiva detectadas en el período disponible.")
            _df_rc      = pd.DataFrame()
            _mes_rc     = []        # lista vacía → coherente con st.multiselect
            _trim_rc    = "Todos"
            _sem_rc     = "Todas"
            _eq_rc      = "Todos"
            _tec_rc_sel = "Todos"
            _n_bruto = _n_excl = _n_fnao = _n_sin_info = _n_sin_dato = _n_fao = _n_espec = 0
        else:
            df_reinc["fecha_cm_dt"] = pd.to_datetime(df_reinc["fecha_cm"], errors="coerce")
            df_reinc["mes"] = df_reinc["fecha_cm_dt"].dt.to_period("M").astype(str)

            _meses_rc = sorted(df_reinc["mes"].dropna().unique(), reverse=True)
            _meses_rc_nums = {int(str(m).split("-")[1]) for m in _meses_rc if "-" in str(m)}
            _TRIMESTRES_RC = {
                "T1 · Ene–Mar": [1,2,3], "T2 · Abr–Jun": [4,5,6],
                "T3 · Jul–Sep": [7,8,9], "T4 · Oct–Dic": [10,11,12],
            }
            _trim_opts_rc = ["Todos"] + [
                k for k, v in _TRIMESTRES_RC.items() if any(m in v for m in _meses_rc_nums)
            ]
            _grupos_con_datos = set(df_reinc["grupo_responsable"].dropna().unique())
            _rc0, _rc1, _rc2, _rc3, _rc4 = st.columns([1.5, 1.4, 1.5, 2, 2])
            with _rc0:
                _trim_rc = st.selectbox("Período", _trim_opts_rc, key="rc_trim")
            with _rc1:
                if _trim_rc != "Todos":
                    _trim_m_rc = _TRIMESTRES_RC[_trim_rc]
                    _meses_rc_disp = [m for m in _meses_rc
                                      if int(str(m).split("-")[1]) in _trim_m_rc]
                else:
                    _meses_rc_disp = _meses_rc
                _MESES_FULL_RC = {
                    "01":"Enero","02":"Febrero","03":"Marzo","04":"Abril","05":"Mayo","06":"Junio",
                    "07":"Julio","08":"Agosto","09":"Septiembre","10":"Octubre","11":"Noviembre","12":"Diciembre",
                }
                def _ym_rc_lbl(ym):
                    p = str(ym).split("-")
                    return f"{_MESES_FULL_RC.get(p[1], p[1])} {p[0][2:]}" if len(p) == 2 else ym
                _lbl_to_per_rc  = {_ym_rc_lbl(m): m for m in _meses_rc}
                _meses_rc_disp_lbl = [_ym_rc_lbl(m) for m in _meses_rc_disp]
                _mes_rc_lbl = st.multiselect("Mes", _meses_rc_disp_lbl, key="rc_mes",
                                             placeholder="Todos los meses")
                _mes_rc = [_lbl_to_per_rc[l] for l in _mes_rc_lbl if l in _lbl_to_per_rc]
            with _rc2:
                if len(_mes_rc) == 1:
                    _sems_rc = _semanas_del_mes(_mes_rc[0])
                    _sem_rc_opts = ["Todas"] + [s[0] for s in _sems_rc]
                else:
                    _sem_rc_opts = ["Todas"]
                _sem_rc = st.selectbox("Semana", _sem_rc_opts, key="rc_semana")
            with _rc3:
                _eq_rc_opts = ["Todos"] + [
                    _EQUIPO_LABEL[g]
                    for g in GRUPOS_TERRENO
                    if g in _grupos_con_datos and g in _EQUIPO_LABEL
                ]
                _eq_rc = st.selectbox("Equipo", _eq_rc_opts, key="rc_equipo")
            with _rc4:
                if _eq_rc != "Todos":
                    _grp_rc_k = _LABEL_TO_GRUPO.get(_eq_rc)
                    # Nombres desde el dato real (igual que Precisión) — incluye al senior
                    _tec_rc_opts = ["Todos"] + sorted(
                        t for t in df_reinc[df_reinc["grupo_responsable"] == _grp_rc_k]["tecnico_responsable"].dropna().unique()
                        if not _es_excluido(t)
                    )
                else:
                    _tec_rc_opts = ["Todos"] + sorted(
                        t for t in df_reinc["tecnico_responsable"].dropna().unique()
                        if not _es_excluido(t) and _get_equipo(t) != "Sin equipo"
                    )
                _tec_rc_sel = st.selectbox("Técnico", _tec_rc_opts, key="rc_tecnico")

            _df_rc = df_reinc.copy()
            if _trim_rc != "Todos":
                _trim_months_rc = _TRIMESTRES_RC[_trim_rc]
                _df_rc = _df_rc[
                    _df_rc["mes"].apply(
                        lambda m: int(str(m).split("-")[1]) if "-" in str(m) else 0
                    ).isin(_trim_months_rc)
                ]
            if _mes_rc:
                _df_rc = _df_rc[_df_rc["mes"].astype(str).isin([str(m) for m in _mes_rc])]
            if _sem_rc != "Todas" and len(_mes_rc) == 1:
                _sem_rc_match = next((s for s in _sems_rc if s[0] == _sem_rc), None)
                if _sem_rc_match:
                    _sem_rc_start, _sem_rc_end = _sem_rc_match[1], _sem_rc_match[2]
                    _df_rc = _df_rc[
                        (_df_rc["fecha_cm_dt"].dt.date >= _sem_rc_start) &
                        (_df_rc["fecha_cm_dt"].dt.date <= _sem_rc_end)
                    ]
            if _eq_rc != "Todos":
                _grp_rc_k = _LABEL_TO_GRUPO.get(_eq_rc)
                if _grp_rc_k:
                    _df_rc = _df_rc[_df_rc["grupo_responsable"] == _grp_rc_k]
            if _tec_rc_sel != "Todos":
                _df_rc = _df_rc[_df_rc["tecnico_responsable"] == _tec_rc_sel]

            # ── Trazabilidad: contar ANTES de excluir por tipo de falla ──────────
            # nunique(folio_cm) = OTs correctivas únicas → 1 OT = 1 error
            # (sin importar cuántas piezas o ítems tenga adentro esa OT)
            _n_bruto = _df_rc["folio_cm"].nunique() if "folio_cm" in _df_rc.columns else len(_df_rc)
            if "falla_tipo" in _df_rc.columns:
                _cnt_falla  = _df_rc["falla_tipo"].value_counts().to_dict()
                _n_fnao     = int(_cnt_falla.get("fnao",     0))
                _n_sin_info = int(_cnt_falla.get("sin_info", 0))
                _n_sin_dato = int(_cnt_falla.get("sin_dato", 0))
                _n_fao      = int(_cnt_falla.get("fao",      0))
                _n_espec    = int(_cnt_falla.get("especial", 0))
                _n_excl     = _n_fnao + _n_espec   # excluir F.N.A.O y Trabajos Especiales
                # F.N.A.O: causa externa confirmada por el técnico → no responsabilidad Occimiano.
                # Trabajos Especiales (03.-): no son fallas post-PM → no son reincidencias.
                # Sin info / sin dato = duda recae en Occimiano → SÍ es error del técnico.
                _df_rc = _df_rc[~_df_rc["falla_tipo"].isin(["fnao", "especial"])]
            else:
                _n_fnao = _n_sin_info = _n_sin_dato = _n_fao = _n_espec = _n_excl = 0

        # ── KPIs globales: Total PMs y PMs sin reincidencia ──────────────────────
        _df_pm_filt = df_wo[
            (df_wo["maint_type"] == "Preventiva") &
            (~df_wo["technician"].apply(_es_excluido)) &
            (df_wo["equipo"] != "Sin equipo")
        ].copy()
        # Columna auxiliar: nombre normalizado (sin acentos, sin espacios dobles, minúsculas).
        # Fracttal puede guardar el nombre con doble espacio o acentos distintos al TECH_NAME_MAP.
        # _get_equipo() ya usa NFD+combinaciones para asignar "equipo" correctamente,
        # pero la comparación por técnico individual necesita la misma tolerancia.
        _df_pm_filt["_tech_norm"] = _df_pm_filt["technician"].fillna("").apply(
            lambda s: " ".join(_norm_n(s).split())
        )
        # Aplicar mismo filtro de equipo/técnico que _df_rc
        if _eq_rc != "Todos":
            _grp_pm = _LABEL_TO_GRUPO.get(_eq_rc, _eq_rc)
            _df_pm_filt = _df_pm_filt[_df_pm_filt["equipo"] == _grp_pm]
        if _tec_rc_sel != "Todos":
            _full_tec_pm   = TECH_NAME_MAP.get(_tec_rc_sel, _tec_rc_sel)
            _full_tec_pm_n = " ".join(_norm_n(_full_tec_pm).split())
            _df_pm_filt = _df_pm_filt[_df_pm_filt["_tech_norm"] == _full_tec_pm_n]
        # Aplicar mismo filtro de período/mes que _df_rc
        if _df_pm_filt["creation_date"].dt.tz is not None:
            _pm_dates = _df_pm_filt["creation_date"].dt.tz_convert(None)
        else:
            _pm_dates = _df_pm_filt["creation_date"]
        if _trim_rc != "Todos":
            _df_pm_filt = _df_pm_filt[_pm_dates.dt.month.isin(_TRIMESTRES_RC[_trim_rc])]
            _pm_dates = _pm_dates[_df_pm_filt.index]
        if _mes_rc:
            _df_pm_filt = _df_pm_filt[
                _pm_dates.reindex(_df_pm_filt.index).dt.to_period("M").astype(str).isin(_mes_rc)
            ]
        # nunique("folio") = órdenes de trabajo únicas (1 OT puede tener N equipos)
        _n_pm_total    = _df_pm_filt["folio"].nunique() if "folio" in _df_pm_filt.columns else len(_df_pm_filt)
        _n_pm_con_reinc = (
            _df_rc["folio_pm"].nunique()
            if not _df_rc.empty and "folio_pm" in _df_rc.columns else 0
        )
        _n_pm_sin_reinc = max(0, _n_pm_total - _n_pm_con_reinc)

        # ── Tarjetas de bono por equipo — calculadas con el df ya filtrado ──────
        _EQUIPO_LABEL_CAL = _EQUIPO_LABEL
        _periodo_lbl = ", ".join(_mes_rc) if _mes_rc else "todos los datos disponibles"

        # Técnico específico → 1 tarjeta (su equipo)
        # Equipo específico  → 1 tarjeta (ese equipo)
        # Todos              → 5 tarjetas (todos los equipos)
        if _tec_rc_sel != "Todos":
            _tec_grp_key = _get_equipo(_tec_rc_sel)
            _eq_label_cal_iter = {
                k: v for k, v in _EQUIPO_LABEL_CAL.items() if k == _tec_grp_key
            } or _EQUIPO_LABEL_CAL
        elif _eq_rc != "Todos":
            _grp_rc_key = _LABEL_TO_GRUPO.get(_eq_rc)
            _eq_label_cal_iter = {
                k: v for k, v in _EQUIPO_LABEL_CAL.items() if k == _grp_rc_key
            } or _EQUIPO_LABEL_CAL
        else:
            _eq_label_cal_iter = _EQUIPO_LABEL_CAL

        # Clientes evaluados en reincidencias (COPEC/ESMAX/SHELL) — para nota de transparencia
        _CLIENTES_SLA_RC = {"COPEC", "Aramco (Esmax)", "ESMAX (Aramco)", "SHELL (Enex)"}

        _cal_cols = st.columns(len(_eq_label_cal_iter))
        for _ci, (_gk, _gl) in enumerate(zip(_eq_label_cal_iter.keys(), _eq_label_cal_iter.values())):
            _senior_cal = GRUPOS_TERRENO.get(_gk, {}).get("senior", "")
            # PMs de este equipo (denominador total — todos los clientes)
            _pm_equipo = df_wo[
                (df_wo["maint_type"] == "Preventiva") &
                (df_wo["equipo"] == _gk)
            ]
            _n_pm = _pm_equipo["folio"].nunique() if "folio" in _pm_equipo.columns else len(_pm_equipo)
            # PMs de este equipo SOLO en clientes evaluados (COPEC/ESMAX/SHELL) — para transparencia
            _pm_sla = _pm_equipo[_pm_equipo["client"].isin(_CLIENTES_SLA_RC)] \
                if "client" in _pm_equipo.columns else pd.DataFrame()
            _n_pm_sla = _pm_sla["folio"].nunique() if not _pm_sla.empty and "folio" in _pm_sla.columns else 0
            # Fallas post-PM del período filtrado
            # nunique(folio_cm) = OTs correctivas únicas = 1 OT = 1 error
            if not _df_rc.empty and "folio_cm" in _df_rc.columns:
                _fallas_eq = _df_rc[_df_rc["grupo_responsable"] == _gk]["folio_cm"].nunique()
            else:
                _fallas_eq = 0
            _bpct, _blbl, _bcol, _bclp = _bono_calidad(int(_fallas_eq), int(_n_pm))
            # Ratio: cuántas PMs por cada correctiva generada
            if _fallas_eq > 0:
                _ratio = round(_n_pm / _fallas_eq, 1)
                _ratio_lbl = f"1 correctiva cada <b>{_ratio:,.0f}</b> PMs"
                _ratio_clr = "#22c55e" if _ratio >= 50 else ("#f59e0b" if _ratio >= 20 else "#ef4444")
            else:
                _ratio_lbl = "Sin correctivas ✅"
                _ratio_clr = "#22c55e"
            # Título: técnico si está filtrado (ya solo hay 1 tarjeta), equipo si es "Todos"
            if _tec_rc_sel != "Todos":
                _cal_titulo    = _tec_rc_sel
                _cal_subtitulo = f"Equipo: {_gl}"
            else:
                _cal_titulo    = _gl
                _cal_subtitulo = f"Senior: {_senior_cal}"

            # % exactitud para mostrar prominente
            _exactitud_cal = round((1 - _fallas_eq / _n_pm) * 100, 1) if _n_pm > 0 else 100.0

            # Nota de cobertura: si 100% y pocos PMs SLA, advertir
            if _exactitud_cal == 100.0 and _n_pm_sla == 0:
                _cobertura_nota = (
                    f'<div style="font-size:0.65rem;color:#f59e0b;margin-top:3px;">'
                    f'⚠️ 0 PMs en COPEC/ESMAX/SHELL — sin cobertura de evaluación</div>'
                )
            elif _exactitud_cal == 100.0:
                _cobertura_nota = (
                    f'<div style="font-size:0.65rem;color:{_t["muted"]};margin-top:3px;">'
                    f'Verificados {_n_pm_sla} PMs en COPEC/ESMAX/SHELL</div>'
                )
            else:
                _cobertura_nota = (
                    f'<div style="font-size:0.65rem;color:{_t["muted"]};margin-top:3px;">'
                    f'{_n_pm_sla} PMs en COPEC/ESMAX/SHELL</div>'
                )

            _cal_cols[_ci].markdown(
                f'<div style="background:{_t["card"]};border:2px solid {_bcol}33;'
                f'border-radius:8px;padding:14px 16px;text-align:center;">'
                # Nombre equipo / técnico
                f'<div style="font-weight:700;font-size:0.95rem;color:{_t["text"]};">{_cal_titulo}</div>'
                f'<div style="font-size:0.78rem;color:{_t["muted"]};margin-bottom:8px;">{_cal_subtitulo}</div>'
                # % cumplimiento — dato principal
                f'<div style="font-size:2rem;font-weight:800;color:{_bcol};line-height:1.1;">'
                f'{_exactitud_cal:.1f}%</div>'
                f'<div style="font-size:0.72rem;color:{_t["muted"]};margin-bottom:6px;">exactitud</div>'
                # Chip bono
                f'<div style="background:{_bcol};color:#fff;border-radius:4px;'
                f'padding:2px 8px;font-size:0.80rem;font-weight:700;'
                f'margin:0 auto 6px;display:inline-block;">'
                f'{_blbl} → ${_bclp:,.0f}</div>'
                # Fallas y ratio — dato secundario
                f'<div style="font-size:0.73rem;color:{_t["muted"]};margin-top:4px;">'
                f'<b style="color:{_bcol if _fallas_eq == 0 else _t["text"]};">{_fallas_eq}</b> '
                f'fallas post-PM &nbsp;·&nbsp; {_n_pm} PMs</div>'
                f'<div style="font-size:0.72rem;color:{_ratio_clr};font-weight:600;margin-top:2px;">'
                f'{_ratio_lbl}</div>'
                # Cobertura de evaluación
                + _cobertura_nota +
                f'</div>',
                unsafe_allow_html=True,
            )

        st.caption(
            f"Exactitud = PMs sin reincidencia / total PMs × 100 — escala trimestral · "
            f"≥98%→${_MAX_CAL_CLP:,} · "
            f"≥96%→${int(_MAX_CAL_CLP*.90):,} · "
            f"≥94%→${int(_MAX_CAL_CLP*.80):,} · "
            f"≥92%→${int(_MAX_CAL_CLP*.70):,} · "
            f"≥90%→${int(_MAX_CAL_CLP*.60):,} · "
            f"<90%→$0 · Período: **{_periodo_lbl}**. "
            f"Reincidencias evaluadas solo en COPEC, ESMAX y SHELL."
        )

        # ── Expander de verificación de datos ─────────────────────────────────
        with st.expander("🔬 Verificar datos del algoritmo (diagnóstico)", expanded=False):
            st.markdown(
                f'<div style="font-size:0.82rem;color:{_t["muted"]};margin-bottom:8px;">'
                f'Esto permite confirmar que el algoritmo está evaluando correctamente. '
                f'Si un equipo muestra <b>0 PMs en COPEC/ESMAX/SHELL</b>, el 100% es por '
                f'falta de cobertura (ese equipo no atiende esos clientes), no por excelencia real.'
                f'</div>',
                unsafe_allow_html=True,
            )
            # Construir tabla diagnóstico por equipo
            _diag_rows = []
            _prev_sla = _df_wo_reinc[_df_wo_reinc["maint_type"] == "Preventiva"].copy() \
                if not _df_wo_reinc.empty else pd.DataFrame()
            _corr_sla = _df_wo_reinc[_df_wo_reinc["maint_type"] == "Correctiva"].copy() \
                if not _df_wo_reinc.empty else pd.DataFrame()

            _pm_sla_codes = set(_prev_sla["equipment_code"].dropna().unique()) \
                if not _prev_sla.empty else set()
            _cm_sla_codes = set(_corr_sla["equipment_code"].dropna().unique()) \
                if not _corr_sla.empty else set()
            _codigos_comunes = _pm_sla_codes & _cm_sla_codes

            for _gk2, _gl2 in _eq_label_cal_iter.items():
                _pm_eq2 = _prev_sla[_prev_sla["equipo"] == _gk2] \
                    if not _prev_sla.empty and "equipo" in _prev_sla.columns else pd.DataFrame()
                _n_pm_eq2    = len(_pm_eq2)
                _n_pm_eq2_ok = int((_pm_eq2["equipment_code"].str.strip() != "").sum()) \
                    if not _pm_eq2.empty else 0
                _fallas_diag = _df_rc[_df_rc["grupo_responsable"] == _gk2]["folio_cm"].nunique() \
                    if not _df_rc.empty and "folio_cm" in _df_rc.columns else 0
                _pm_codes_eq2 = set(_pm_eq2["equipment_code"].dropna().unique()) \
                    if not _pm_eq2.empty else set()
                _cms_matcheables = len(_corr_sla[_corr_sla["equipment_code"].isin(_pm_codes_eq2)]) \
                    if not _corr_sla.empty and _pm_codes_eq2 else 0
                _diag_rows.append({
                    "Equipo":                    _gl2,
                    "PMs COPEC/ESMAX/SHELL":     _n_pm_eq2,
                    "PMs con código activo":      _n_pm_eq2_ok,
                    "CMs matcheables":            _cms_matcheables,
                    "Reincidencias detectadas":   _fallas_diag,
                    "Cobertura real":             "✅" if _n_pm_eq2 > 0 else "⚠️ Sin evaluación",
                })
            _df_diag = pd.DataFrame(_diag_rows)

            # Resumen global
            _n_prev_total_sla = len(_prev_sla)
            _n_corr_total_sla = len(_corr_sla)
            _n_codigos_comunes = len(_codigos_comunes)

            _c1d, _c2d, _c3d = st.columns(3)
            _c1d.metric("PMs COPEC/ESMAX/SHELL", _n_prev_total_sla)
            _c2d.metric("CMs COPEC/ESMAX/SHELL", _n_corr_total_sla)
            _c3d.metric("Equipos con código común (matcheables)", _n_codigos_comunes)

            if _n_codigos_comunes == 0 and _n_prev_total_sla > 0 and _n_corr_total_sla > 0:
                st.error(
                    "⚠️ **Sin códigos comunes entre PMs y CMs** — el `equipment_code` "
                    "(campo `codigo_activo` en Supabase) no coincide entre preventivas y "
                    "correctivas. El algoritmo no puede detectar reincidencias aunque existan. "
                    "Revisar que `codigo_activo` esté poblado en ambos tipos de OT."
                )
            elif _n_prev_total_sla == 0:
                st.warning(
                    "⚠️ No se encontraron PMs de COPEC/ESMAX/SHELL en el período. "
                    "El 100% es por falta de datos, no por excelencia."
                )
            else:
                st.success(
                    f"✅ El algoritmo tiene cobertura: {_n_prev_total_sla} PMs y "
                    f"{_n_corr_total_sla} CMs de COPEC/ESMAX/SHELL, "
                    f"{_n_codigos_comunes} equipos matcheables."
                )

            st.dataframe(
                _df_diag,
                use_container_width=True,
                hide_index=True,
            )

            # Mostrar en qué meses hay reincidencias (si existen)
            if not df_reinc.empty and "fecha_cm" in df_reinc.columns:
                _df_reinc_mes = df_reinc.copy()
                _df_reinc_mes["mes"] = pd.to_datetime(
                    _df_reinc_mes["fecha_cm"], errors="coerce"
                ).dt.to_period("M").astype(str)
                _reinc_por_mes = _df_reinc_mes["mes"].value_counts().sort_index()
                if not _reinc_por_mes.empty:
                    st.markdown(
                        f'<div style="font-size:0.8rem;color:{_t["muted"]};margin-top:6px;">'
                        f'<b>Reincidencias detectadas por mes (todos los equipos):</b> '
                        + " · ".join(f"{m}: {n}" for m, n in _reinc_por_mes.items())
                        + f'</div>',
                        unsafe_allow_html=True,
                    )
            elif df_reinc.empty:
                st.markdown(
                    f'<div style="font-size:0.8rem;color:{_t["muted"]};margin-top:6px;">'
                    f'df_reinc vacío — no se detectó ninguna reincidencia en todo el período cargado.'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        # ── Gráfico: PMs realizados vs Correctivos ≤5d ───────────────────────
        st.divider()
        st.markdown('<div class="section-header">📊  PMs realizados vs Correctivos ≤ 5 días</div>', unsafe_allow_html=True)

        _cal_pm_sig = f"{_wo_sig}_{_eq_rc}_{_tec_rc_sel}_{_mes_rc}_{_trim_rc}_{_sem_rc}"
        _cal_pm_k   = f"_fig_cal_pm_rc_{_current_theme}_{_cal_pm_sig}"
        if _cal_pm_k not in st.session_state:
            # ── Construir tabla base según nivel de filtro ────────────────────
            if _tec_rc_sel != "Todos":
                _n_rc_chart = _df_rc["folio_cm"].nunique() if not _df_rc.empty and "folio_cm" in _df_rc.columns else 0
                _pm_base = [{"_x": _tec_rc_sel, "total": _n_pm_total, "errores": _n_rc_chart}]
                _pm_x_col = "Técnico"
                _pm_title = f"Exactitud preventivas — {_tec_rc_sel}"
            elif _eq_rc != "Todos":
                _grp_k_pm = _LABEL_TO_GRUPO.get(_eq_rc, _eq_rc)
                _mbs_pm   = GRUPOS_TERRENO.get(_grp_k_pm, {}).get("miembros", [])
                _pm_base  = []
                for _mb_pm in _mbs_pm:
                    _full_pm   = TECH_NAME_MAP.get(_mb_pm, _mb_pm)
                    _full_pm_n = " ".join(_norm_n(_full_pm).split())
                    _tmp_pm    = _df_pm_filt[_df_pm_filt["_tech_norm"] == _full_pm_n]
                    _cnt_pm  = _tmp_pm["folio"].nunique() if "folio" in _tmp_pm.columns else len(_tmp_pm)
                    _cnt_rc  = (_df_rc[_df_rc["tecnico_resp_short"] == _mb_pm]["folio_cm"].nunique()
                                if not _df_rc.empty and "tecnico_resp_short" in _df_rc.columns else 0)
                    _pm_base.append({"_x": _mb_pm, "total": _cnt_pm, "errores": _cnt_rc})
                _pm_x_col = "Técnico"
                _pm_title = f"Exactitud preventivas — {_eq_rc}"
            else:
                _pm_base = []
                for _gk_pm in _EQUIPO_LABEL.keys():
                    _tmp_pm2 = _df_pm_filt[_df_pm_filt["equipo"] == _gk_pm]
                    _cnt_pm  = _tmp_pm2["folio"].nunique() if "folio" in _tmp_pm2.columns else len(_tmp_pm2)
                    _cnt_rc = (_df_rc[_df_rc["grupo_responsable"] == _gk_pm]["folio_cm"].nunique()
                               if not _df_rc.empty and "grupo_responsable" in _df_rc.columns else 0)
                    _pm_base.append({"_x": _gk_pm, "total": _cnt_pm, "errores": _cnt_rc})
                _pm_x_col = "Equipo"
                _pm_title = "Exactitud preventivas — todos los equipos"

            if _pm_base:
                # Calcular % y construir datos apilados
                _stk_rows = []
                for _r in _pm_base:
                    _tot  = _r["total"]
                    _err  = _r["errores"]
                    _ok   = max(0, _tot - _err)
                    _pct_e = round(_err / _tot * 100, 2) if _tot > 0 else 0.0
                    _pct_o = round(100 - _pct_e, 2)
                    _stk_rows.append({
                        _pm_x_col: _r["_x"],
                        "✅ Sin reincidencia": _ok,
                        "⚠️ Correctivos ≤5d": _err,
                        "_pct_err": _pct_e,
                        "_pct_ok":  _pct_o,
                        "_total":   _tot,
                    })
                _stk_df = pd.DataFrame(_stk_rows)

                _melted = _stk_df.melt(
                    id_vars=[_pm_x_col, "_pct_err", "_pct_ok", "_total"],
                    value_vars=["✅ Sin reincidencia", "⚠️ Correctivos ≤5d"],
                    var_name="Tipo", value_name="OTs",
                )

                _fig_pm_rc = px.bar(
                    _melted, x=_pm_x_col, y="OTs", color="Tipo",
                    barmode="stack",
                    title=_pm_title,
                    color_discrete_map={
                        "✅ Sin reincidencia": "#22c55e",
                        "⚠️ Correctivos ≤5d": "#ef4444",
                    },
                    labels={_pm_x_col: "", "OTs": "Órdenes de trabajo"},
                    custom_data=["_pct_err", "_pct_ok", "_total"],
                )
                # Texto dentro de cada segmento: número absoluto
                _fig_pm_rc.update_traces(
                    texttemplate="%{y:,}",
                    textposition="inside",
                    insidetextanchor="middle",
                )
                # Anotaciones encima de cada barra con % error y % exactitud
                for _, _row in _stk_df.iterrows():
                    _xval    = _row[_pm_x_col]
                    _pct_e   = _row["_pct_err"]
                    _pct_o   = _row["_pct_ok"]
                    _tot     = _row["_total"]
                    _fig_pm_rc.add_annotation(
                        x=_xval, y=_tot,
                        text=f"<b style='color:#ef4444'>{_pct_e:.2f}% error</b>"
                             f"  |  <b style='color:#22c55e'>{_pct_o:.2f}% exactitud</b>",
                        showarrow=False,
                        yanchor="bottom",
                        yshift=6,
                        font=dict(size=11),
                    )
                _fig_pm_rc.update_layout(
                    legend_title="", height=430,
                    margin=dict(t=60, b=20),
                    uniformtext_minsize=9, uniformtext_mode="hide",
                )
                _apply_plot_theme(_fig_pm_rc)
                st.session_state[_cal_pm_k] = _fig_pm_rc
            else:
                st.session_state[_cal_pm_k] = None

        _fig_pm_show = st.session_state.get(_cal_pm_k)
        if _fig_pm_show is not None:
            st.plotly_chart(_fig_pm_show, width="stretch")
        else:
            st.info("Sin datos para el período y filtros seleccionados.")

        if not _df_rc.empty or _n_bruto > 0:
            st.divider()

            _total_rc  = _df_rc["folio_cm"].nunique() if "folio_cm" in _df_rc.columns else len(_df_rc)
            _tec_rc    = int(_df_rc["es_reincidencia_tecnico"].sum()) if not _df_rc.empty else 0
            _espec_rc  = int((_df_rc["falla_tipo"] == "especial").sum()) if ("falla_tipo" in _df_rc.columns and not _df_rc.empty) else 0
            _sin_cl    = int((_df_rc["causa_clasif"] == "sin_clasificar").sum()) if not _df_rc.empty else 0

            # ── Fila 1: contexto general de PMs ──────────────────────────────
            _rp1, _rp2 = st.columns(2)
            _rp1.metric(
                "PMs realizados en el período",
                f"{_n_pm_total:,}",
                delta=f"Mantenimientos preventivos ejecutados",
                delta_color="off",
            )
            _rp2.metric(
                "PMs sin reincidencia ✅",
                f"{_n_pm_sin_reinc:,}",
                delta=f"{round(_n_pm_sin_reinc / _n_pm_total * 100, 1) if _n_pm_total > 0 else 0}% sin correctivo en 5 días",
                delta_color="off",
            )

            st.divider()

            # ── Fila 2: desglose de fallas ────────────────────────────────────
            _rm1, _rm2, _rm3, _rm4 = st.columns(4)
            _rm1.metric("Total fallas post-PM detectadas", f"{_n_bruto:,}",
                        delta=f"{_n_excl:,} F.N.A.O excluidas — ver detalle ↓", delta_color="off")
            _rm2.metric("F.A.O — Error del técnico", f"{_tec_rc:,}",
                        delta="⚠️ Afecta KPI Calidad" if _tec_rc > 0 else "✅ Sin afectación",
                        delta_color="off")
            _rm3.metric("Trabajos Especiales", f"{_espec_rc:,}",
                        delta="No imputan KPI", delta_color="off")
            _rm4.metric("Sin causa registrada", f"{_sin_cl:,}",
                        delta="⚠️ Penaliza KPI Precisión" if _sin_cl > 0 else None,
                        delta_color="off")

            # ── Cuadro de trazabilidad completo ─────────────────────────────────
            with st.expander(
                f"🔍 Trazabilidad — desglose de las {_n_bruto:,} fallas post-PM detectadas",
                expanded=False,
            ):
                st.markdown(
                    f"""
| Clasificación (columna "Falla" en Fracttal) | Cant. | ¿Imputa KPI? | Criterio |
|---|---|---|---|
| 🔴 F.A.O — Falla Atribuible a Occimiano | **{_n_fao:,}** | ✅ Sí | Técnico confirmó que es error de Occimiano |
| ⚫ Sin Información (opción "04.-") | **{_n_sin_info:,}** | ✅ Sí | Sin justificación → la duda recae en Occimiano |
| ⚫ Sin dato (campo completamente vacío) | **{_n_sin_dato:,}** | ✅ Sí | Sin justificación → la duda recae en Occimiano |
| 🔵 Trabajos Especiales | **{_n_espec:,}** | ⚠️ No | Categoría explícita, no imputa KPI Calidad |
| 🟢 F.N.A.O — No Atribuible a Occimiano | **{_n_fnao:,}** | ❌ Excluida | Técnico declaró EXPLÍCITAMENTE causa externa |
| **TOTAL detectadas** | **{_n_bruto:,}** | | |
| **TOTAL analizadas** | **{_total_rc:,}** | | = F.A.O + Sin info + Sin dato + Trab. Especiales |
| **TOTAL excluidas** | **{_n_excl:,}** | | = Solo F.N.A.O (causa externa confirmada) |
                    """,
                    unsafe_allow_html=False,
                )
                if _n_sin_dato > 0 or _n_sin_info > 0:
                    st.info(
                        f"ℹ️ {_n_sin_info + _n_sin_dato:,} OTs tienen el campo 'Falla' vacío o sin información. "
                        "Al no poder descartar responsabilidad de Occimiano, se imputan al técnico del PM. "
                        "Esto también penaliza el KPI de Precisión Fracttal."
                    )

            _df_rc_tec = _df_rc[_df_rc["es_reincidencia_tecnico"]] if not _df_rc.empty else pd.DataFrame()
            # ── Evolución mensual MC/MP — doble eje ──────────────────────────
            st.divider()
            _scope_lbl = (
                _tec_rc_sel if _tec_rc_sel != "Todos"
                else (_eq_rc if _eq_rc != "Todos" else "todos los equipos")
            )
            st.markdown(
                f'<div class="section-header">📈 Evolución mensual MC/MP ≤ 5 días — {_scope_lbl}</div>',
                unsafe_allow_html=True,
            )

            _cal_sig = f"{_wo_sig}_{_eq_rc}_{_tec_rc_sel}_{_mes_rc}_{_trim_rc}_{_sem_rc}"
            _cal_k   = f"_fig_cal_evol_{_current_theme}_{_cal_sig}"
            if _cal_k not in st.session_state:
                # PMs desde enero 2026 en adelante (KPI aplica a partir de 2026)
                _pm_m = df_wo[df_wo["maint_type"] == "Preventiva"].copy()
                _pm_m_dates = _pm_m["creation_date"].dt.tz_convert(None) \
                    if _pm_m["creation_date"].dt.tz is not None \
                    else _pm_m["creation_date"]
                _pm_m = _pm_m[_pm_m_dates >= pd.Timestamp("2026-01-01")]
                # Columna auxiliar para comparación tolerante de nombres de técnico
                _pm_m["_tech_norm"] = _pm_m["technician"].fillna("").apply(
                    lambda s: " ".join(_norm_n(s).split())
                )
                if _eq_rc != "Todos":
                    _grp_pm_evol = _LABEL_TO_GRUPO.get(_eq_rc, _eq_rc)
                    _pm_m = _pm_m[_pm_m["equipo"] == _grp_pm_evol]
                if _tec_rc_sel != "Todos":
                    _full_tec_evol   = TECH_NAME_MAP.get(_tec_rc_sel, _tec_rc_sel)
                    _full_tec_evol_n = " ".join(_norm_n(_full_tec_evol).split())
                    _pm_m = _pm_m[_pm_m["_tech_norm"] == _full_tec_evol_n]

                # ── Drill-down: 1 mes seleccionado → semanas; resto → meses ──────────
                _drill_weekly = (len(_mes_rc) == 1)

                if _drill_weekly:
                    # ── MODO SEMANAL ────────────────────────────────────────────────
                    _drm      = _mes_rc[0]                    # e.g. "2026-06"
                    _sems_ev  = _semanas_del_mes(_drm)        # [(label, start, end), ...]
                    _MESES_ES3 = {"01":"Ene","02":"Feb","03":"Mar","04":"Abr",
                                  "05":"May","06":"Jun","07":"Jul","08":"Ago",
                                  "09":"Sep","10":"Oct","11":"Nov","12":"Dic"}
                    _drm_p    = _drm.split("-")
                    _drm_lbl  = f"{_MESES_ES3.get(_drm_p[1], _drm_p[1])} '{_drm_p[0][2:]}"

                    # Filtrar PMs al mes seleccionado
                    _pm_dates3 = (_pm_m["creation_date"].dt.tz_convert(None)
                                  if _pm_m["creation_date"].dt.tz is not None
                                  else _pm_m["creation_date"])
                    _pm_m = _pm_m[_pm_dates3.dt.to_period("M").astype(str) == _drm].copy()
                    _pm_m["_fecha_d"] = (_pm_m["creation_date"].dt.tz_convert(None)
                                         if _pm_m["creation_date"].dt.tz is not None
                                         else _pm_m["creation_date"]).dt.date

                    # Asignador de semana
                    def _asigna_sem_ev(d):
                        for _sl, _ss, _se in _sems_ev:
                            if _ss <= d <= _se:
                                return _sl
                        return None

                    _pm_m["_slot"] = _pm_m["_fecha_d"].apply(_asigna_sem_ev)
                    _pm_by_slot = (_pm_m.dropna(subset=["_slot"])
                                   .groupby("_slot")["folio"].nunique()
                                   .reset_index(name="pms"))

                    # Errores por semana (usar _df_rc ya filtrado por equipo/tec/mes)
                    if (not _df_rc.empty and "fecha_cm_dt" in _df_rc.columns
                            and "folio_cm" in _df_rc.columns):
                        _rc_drill = _df_rc.copy()
                        _rc_drill["_fecha_d"] = pd.to_datetime(
                            _rc_drill["fecha_cm_dt"], errors="coerce"
                        ).dt.date
                        _rc_drill["_slot"] = _rc_drill["_fecha_d"].apply(_asigna_sem_ev)
                        _rc_by_slot = (_rc_drill.dropna(subset=["_slot"])
                                       .groupby("_slot")["folio_cm"].nunique()
                                       .reset_index(name="errores"))
                    else:
                        _rc_by_slot = pd.DataFrame(columns=["_slot", "errores"])

                    # Garantizar todas las semanas del mes (aunque tengan 0)
                    _all_slots = [_sl for _sl, _, _ in _sems_ev]
                    _ev = pd.DataFrame({"_slot": _all_slots})
                    _ev = _ev.merge(_pm_by_slot, on="_slot", how="left").fillna({"pms": 0})
                    _ev = _ev.merge(_rc_by_slot, on="_slot", how="left").fillna({"errores": 0})
                    _ev["pms"]     = _ev["pms"].astype(int)
                    _ev["errores"] = _ev["errores"].astype(int)
                    _ev["pct_err"] = (_ev["errores"] / _ev["pms"] * 100).round(2).where(_ev["pms"] > 0, 0)
                    _ev["pct_ok"]  = (100 - _ev["pct_err"]).round(2)
                    _ev["mes_lbl"] = _ev["_slot"]
                    _titulo_evol   = f"Evolución semanal — {_drm_lbl} · PMs ejecutados vs % error MC/MP"

                else:
                    # ── MODO MENSUAL ─────────────────────────────────────────────────
                    if _pm_m["creation_date"].dt.tz is not None:
                        _pm_m["_mes_pm"] = _pm_m["creation_date"].dt.tz_convert(None).dt.to_period("M").astype(str)
                    else:
                        _pm_m["_mes_pm"] = _pm_m["creation_date"].dt.to_period("M").astype(str)
                    # nunique("folio") = órdenes únicas por mes, no filas (1 OT puede tener N equipos)
                    _pm_by_mes = _pm_m.groupby("_mes_pm")["folio"].nunique().reset_index(name="pms")

                    # Errores únicos (folio_cm) por mes (desde _df_rc ya filtrado)
                    if not _df_rc.empty and "folio_cm" in _df_rc.columns and "mes" in _df_rc.columns:
                        _rc_by_mes = (
                            _df_rc.groupby("mes")["folio_cm"].nunique()
                            .reset_index(name="errores")
                            .rename(columns={"mes": "_mes_pm"})
                        )
                    else:
                        _rc_by_mes = pd.DataFrame(columns=["_mes_pm", "errores"])

                    # Unir por mes
                    _ev = _pm_by_mes.merge(_rc_by_mes, on="_mes_pm", how="left").fillna({"errores": 0})
                    _ev["errores"] = _ev["errores"].astype(int)
                    _ev["pct_err"] = (_ev["errores"] / _ev["pms"] * 100).round(2).where(_ev["pms"] > 0, 0)
                    _ev["pct_ok"]  = (100 - _ev["pct_err"]).round(2)
                    _ev = _ev.sort_values("_mes_pm")

                    # Etiquetas de mes en español
                    _MESES_ES2 = {"01":"Ene","02":"Feb","03":"Mar","04":"Abr","05":"May","06":"Jun",
                                  "07":"Jul","08":"Ago","09":"Sep","10":"Oct","11":"Nov","12":"Dic"}
                    def _mes_lbl2(ym):
                        p = str(ym).split("-")
                        return f"{_MESES_ES2.get(p[1], p[1])} '{p[0][2:]}" if len(p) == 2 else ym
                    _ev["mes_lbl"] = _ev["_mes_pm"].apply(_mes_lbl2)
                    _titulo_evol = "Evolución mensual — PMs ejecutados vs % error MC/MP"

                if not _ev.empty:
                    _fig_evol = make_subplots(specs=[[{"secondary_y": True}]])

                    # Barras: PMs realizados (eje izquierdo)
                    _fig_evol.add_trace(
                        go.Bar(
                            x=_ev["mes_lbl"], y=_ev["pms"],
                            name="PMs realizados",
                            marker_color="#94a3b8",
                            opacity=0.9,
                            text=_ev["pms"],
                            textposition="inside",
                            textfont=dict(size=13, color="#ffffff", family="Arial"),
                        ),
                        secondary_y=False,
                    )

                    # Línea tendencia PMs — amarillo
                    _fig_evol.add_trace(
                        go.Scatter(
                            x=_ev["mes_lbl"], y=_ev["pms"],
                            name="Tendencia PMs",
                            mode="lines+markers",
                            line=dict(color="#fbbf24", width=2.5, dash="dot"),
                            marker=dict(size=7, color="#fbbf24",
                                        line=dict(color="#ffffff", width=1)),
                        ),
                        secondary_y=False,
                    )

                    # Línea: % error (eje derecho) — rojo, sin texto (anotaciones separadas)
                    _fig_evol.add_trace(
                        go.Scatter(
                            x=_ev["mes_lbl"], y=_ev["pct_err"],
                            name="% Error MC/MP",
                            mode="lines+markers",
                            line=dict(color="#ef4444", width=3),
                            marker=dict(size=11, color="#ef4444",
                                        line=dict(color="#ffffff", width=2)),
                            customdata=list(zip(_ev["errores"], _ev["pms"])),
                            hovertemplate=(
                                "<b>%{x}</b><br>"
                                "% Error: %{y:.2f}%<br>"
                                "Errores: %{customdata[0]}<br>"
                                "PMs totales: %{customdata[1]}<br>"
                                "<extra></extra>"
                            ),
                        ),
                        secondary_y=True,
                    )
                    # Anotaciones con cuadro blanco bordeado en rojo — mismo patrón SLA
                    for _, _erow in _ev.iterrows():
                        _fig_evol.add_annotation(
                            x=_erow["mes_lbl"],
                            y=_erow["pct_err"],
                            yref="y2",
                            text=f"<b>{_erow['pct_err']:.2f}%</b>  {int(_erow['errores'])} err",
                            showarrow=False,
                            yanchor="bottom",
                            yshift=10,
                            font=dict(size=11, color="#b91c1c", family="Arial"),
                            bgcolor="rgba(255,255,255,0.88)",
                            bordercolor="#ef4444",
                            borderwidth=1.5,
                            borderpad=4,
                        )

                    # Línea: % exactitud (activable desde leyenda)
                    _fig_evol.add_trace(
                        go.Scatter(
                            x=_ev["mes_lbl"], y=_ev["pct_ok"],
                            name="% Exactitud",
                            mode="lines+markers",
                            line=dict(color="#22c55e", width=2, dash="dot"),
                            marker=dict(size=6, color="#22c55e"),
                            visible="legendonly",
                        ),
                        secondary_y=True,
                    )

                    _fig_evol.update_layout(
                        title=_titulo_evol,
                        height=430,
                        legend=dict(orientation="h", y=1.08, x=0),
                        margin=dict(t=60, b=20),
                        bargap=0.3,
                    )
                    _fig_evol.update_yaxes(title_text="PMs realizados", secondary_y=False)
                    _fig_evol.update_yaxes(
                        title_text="% Error MC/MP",
                        secondary_y=True,
                        tickformat=".2f",
                        ticksuffix="%",
                        range=[0, max(_ev["pct_err"].max() * 2, 1)],
                    )
                    _apply_plot_theme(_fig_evol)
                    st.session_state[_cal_k] = _fig_evol
                else:
                    st.session_state[_cal_k] = None

            _fig_evol_show = st.session_state.get(_cal_k)
            if _fig_evol_show is not None:
                st.plotly_chart(_fig_evol_show, width="stretch")
            else:
                st.info("Sin datos suficientes para mostrar la evolución.")

            st.divider()
            st.markdown('<div class="section-header">📋 Detalle de fallas post-preventiva</div>',
                        unsafe_allow_html=True)
            st.caption(
                "Cada fila = correctivo generado 1–5 días después de un preventivo en el mismo equipo. "
                "El técnico responsable es quien realizó el preventivo."
            )

            _rc_cols_base = [
                "equipment", "client", "station",
                "folio_pm", "folio_cm",
                "fecha_pm", "fecha_cm",
                "tecnico_resp_short", "tecnico_cm", "grupo_responsable",
                "dias_entre",
                "falla_raw", "falla_tipo",
                "causa_raiz", "causa_clasif",
                "es_reincidencia_tecnico",
                "observacion",
            ]
            # falla_raw / falla_tipo solo existen si el DF fue generado con la nueva lógica
            _rc_disp = _df_rc[[c for c in _rc_cols_base if c in _df_rc.columns]].copy()
            _rc_disp["fecha_pm"] = pd.to_datetime(_rc_disp["fecha_pm"]).dt.strftime("%d/%m/%Y")
            _rc_disp["fecha_cm"] = pd.to_datetime(_rc_disp["fecha_cm"]).dt.strftime("%d/%m/%Y")

            # ── Columna Observación — generada con valores raw de falla_tipo ────
            _OBS_MAP = {
                "fao":      "Error del técnico del PM — falla confirmada como F.A.O",
                "sin_info": "⚠️ Atribuible por omisión: técnico del correctivo seleccionó 'Sin Información' — no se puede descartar responsabilidad de Occimiano",
                "sin_dato": "⚠️ Atribuible por omisión: campo 'Falla' dejado vacío en Fracttal — no se puede descartar responsabilidad de Occimiano",
                "especial": "Trabajo Especial — no imputa KPI Calidad",
                "fnao":     "Excluida — F.N.A.O declarada por el técnico",
            }
            if "falla_tipo" in _rc_disp.columns:
                _rc_disp["observacion"] = _rc_disp["falla_tipo"].map(_OBS_MAP).fillna("")

            _rc_disp["es_reincidencia_tecnico"] = _rc_disp["es_reincidencia_tecnico"].apply(
                lambda x: "🔴 Técnico" if x else "🟢 No técnico"
            )
            _rc_disp["causa_clasif"] = _rc_disp["causa_clasif"].apply(
                lambda x: "🔴 Sin causa" if x == "sin_clasificar"
                else ("🟡 Técnico" if x == "tecnico" else "🟢 Cliente")
            )
            if "falla_tipo" in _rc_disp.columns:
                _rc_disp["falla_tipo"] = _rc_disp["falla_tipo"].map({
                    "fao":      "🔴 F.A.O",
                    "fnao":     "🟢 F.N.A.O",
                    "sin_info": "⚫ Sin info",
                    "especial": "🔵 Esp.",
                    "sin_dato": "⚫ Sin dato",
                }).fillna(_rc_disp["falla_tipo"])
            # Mapear grupo interno → label de equipo (único incluso con mismo senior)
            _rc_disp["grupo_responsable"] = _rc_disp["grupo_responsable"].map(
                _EQUIPO_LABEL
            ).fillna(_rc_disp["grupo_responsable"])

            _rc_col_names = [
                "Equipo", "Cliente", "Estación",
                "OT MP", "OT MC",
                "Fecha MP", "Fecha MC",
                "Técnico MP", "Técnico MC", "T.Senior",
                "Días MP→MC",
            ]
            if "falla_raw" in _rc_disp.columns:
                _rc_col_names += ["Tipo Falla", "Clasif. Falla"]
            _rc_col_names += ["Causa raíz", "Clasif. causa", "Responsabilidad"]
            if "observacion" in _rc_disp.columns:
                _rc_col_names += ["Observación"]
            _rc_disp.columns = _rc_col_names

            _show_df(
                _rc_disp,
                width="stretch",
                hide_index=True,
                column_config={
                    "Días MP→MC":    st.column_config.NumberColumn(format="%d días"),
                    "Tipo Falla":    st.column_config.TextColumn(width=120),
                    "Clasif. Falla": st.column_config.TextColumn(width=100),
                    "Causa raíz":    st.column_config.TextColumn(width=220),
                    "Técnico MC":    st.column_config.TextColumn(width=160),
                    "Técnico MP":    st.column_config.TextColumn(width=160),
                    "Observación":   st.column_config.TextColumn(
                        width=340,
                        help="Criterio por el cual esta falla se considera atribuible o no al técnico del preventivo",
                    ),
                },
            )

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 3 — PRECISIÓN INFO OPERATIVA (KPI LLENADO)
    # ══════════════════════════════════════════════════════════════════════════
    with tab_prec:
        _tprec_desc, _tprec_ley = st.columns([3, 1])
        with _tprec_ley:
            st.markdown(
                f'<div style="background:{_t["card"]};border:1px solid {_t["border"]};'
                f'border-radius:8px;padding:10px 12px;font-size:0.76rem;color:{_t["text"]};'
                f'line-height:1.75;margin-top:2px;">'
                f'<div style="font-weight:700;color:{_t["muted"]};font-size:0.78rem;'
                f'margin-bottom:4px;letter-spacing:0.04em;">📊 ESCALA BONO PRECISIÓN</div>'
                f'<span style="color:#22c55e;">≥ 95%</span> → <b>100%</b> · $105.000/trim<br>'
                f'<span style="color:#16a34a;">≥ 90%</span> → <b>90%</b> · $94.500/trim<br>'
                f'<span style="color:#4ade80;">≥ 85%</span> → <b>80%</b> · $84.000/trim<br>'
                f'<span style="color:#65a30d;">≥ 80%</span> → <b>70%</b> · $73.500/trim<br>'
                f'<span style="color:#f59e0b;">≥ 75%</span> → <b>60%</b> · $63.000/trim<br>'
                f'<span style="color:#f97316;">≥ 70%</span> → <b>50%</b> · $52.500/trim<br>'
                f'<span style="color:#ef4444;">&lt; 70%</span> → <b>Sin bono</b><br>'
                f'<div style="border-top:1px solid {_t["border"]};margin:5px 0;padding-top:4px;'
                f'font-size:0.72rem;color:{_t["muted"]};">'
                f'4 componentes × 25 pts:<br>⏱ Tiempo · 🔍 Causa · 🔢 Numeral · 🎯 Modalidad de atención</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with _tprec_desc:
            st.markdown(
                f'<div style="background:{_t["warn_bg"]};border-left:4px solid #01798A;'
                f'border-radius:8px;padding:12px 16px;margin-bottom:12px;color:{_t["text"]};">'
                '<b>KPI Precisión Fracttal</b> — Representa el <b>30% del bono de desempeño</b> '
                '(<b>$105.000 bruto/trimestre</b> máximo, pago trimestral). '
                'Mide <b>4 componentes</b> por OT (25 pts c/u = 100 total): '
                '<b>Tiempo de ejecución</b>, '
                '<b>Causa raíz</b> (solo MC), '
                '<b>Numeral registrado</b> y '
                '<b>Método de detección de falla</b>. '
                f'<span style="font-size:0.82rem;color:{_t["muted"]};">'
                'Una OT es "mala" si falla en <b>cualquiera</b> de los 4 — aunque solo falle 1. '
                'El bono se mide por <b>% de OTs buenas</b>, no por suma de errores.</span></div>',
                unsafe_allow_html=True,
            )

        # ── Construir DataFrame de KPI (cacheado en session_state) ──────────────
        # Solo se recalcula cuando cambia raw_wo (nueva carga desde Fracttal).
        # Se añade columna "equipo" al cachear para evitar apply(_get_equipo) repetidos.
        # Filtrado de excluidos y columnas derivadas también se calculan aquí →
        # no se repiten en cada rerun (solo al cambiar raw_wo).
        def _build_kpi_raw_cached():
            _df = _cached_build_kpi_llenado(raw_wo)
            if not _df.empty:
                _df["equipo"] = _df["tecnico"].apply(_get_equipo)
                _df = _df[~_df["tecnico"].apply(_es_excluido)].copy()
                # Solo tipos de mantenimiento evaluables: Correctiva y Preventiva (cualquier variante).
                # Excluidos: Entrega de Insumos, Inspección, Pendiente, Garantía, etc.
                _tipo_upper = _df["maint_type"].str.upper()
                _df = _df[
                    _tipo_upper.str.contains("CORRECTIVA", na=False) |
                    _tipo_upper.str.contains("PREVENTIVA", na=False)
                ].copy()
                # Solo datos desde 2026 — el KPI de Precisión aplica a partir de ese año
                _df = _df[_df["creation_date"].dt.tz_convert(None).dt.year >= 2026].copy()
                _df["mes"] = (
                    _df["creation_date"].dt.tz_convert(None)
                    .dt.to_period("M").astype(str)
                )
                _df["creation_date_local"] = (
                    _df["creation_date"].dt.tz_convert(None).dt.date
                )
            return _df
        try:
            df_kpi_raw = _sc("df_kpi_raw", _wo_sig, _build_kpi_raw_cached)
        except Exception as _e_prec:
            st.error(f"⚠️ Error al cargar datos de KPI de llenado: {_e_prec}")
            st.exception(_e_prec)
            st.stop()

        if df_kpi_raw.empty:
            st.error("No se pudieron cargar datos de work orders para el KPI.")
            st.stop()

        # ── Filtros de mes disponibles ────────────────────────────────────────────
        meses_disp = sorted(df_kpi_raw["mes"].dropna().unique(), reverse=True)

        if not meses_disp:
            st.warning("No hay OTs con fecha de creación disponibles para calcular el KPI.")
            st.stop()

        # ── Pre-computar scores de TODAS las OTs una sola vez por carga ──────────
        # Cada cambio de filtro (mes/semana/equipo/técnico) solo hace un slice
        # sobre este DataFrame ya calculado → órdenes de magnitud más rápido.
        def _build_ot_all():
            _df = score_llenado_por_ot(df_kpi_raw)
            if not _df.empty:
                _df["equipo"] = _df["tecnico"].apply(_get_equipo)
                _df["mes"] = (
                    _df["creation_date"].dt.tz_convert(None)
                    .dt.to_period("M").astype(str)
                )
                _df["creation_date_local"] = (
                    _df["creation_date"].dt.tz_convert(None).dt.date
                )
            return _df
        df_ot_all = _sc("df_ot_all_scores", _wo_sig, _build_ot_all)

        _meses_prec_nums = {int(str(m).split("-")[1]) for m in meses_disp if "-" in str(m)}
        _TRIMESTRES_PREC = {
            "T1 · Ene–Mar": [1,2,3], "T2 · Abr–Jun": [4,5,6],
            "T3 · Jul–Sep": [7,8,9], "T4 · Oct–Dic": [10,11,12],
        }
        _trim_opts_prec = ["Todos"] + [
            k for k, v in _TRIMESTRES_PREC.items() if any(m in v for m in _meses_prec_nums)
        ]
        kf0, kf1, kf2, kf3, kf4 = st.columns([1.5, 1.4, 1.5, 2, 2])
        with kf0:
            _trim_prec = st.selectbox("Período", _trim_opts_prec, key="kpi_trim")
        with kf1:
            if _trim_prec != "Todos":
                _trim_months_prec = _TRIMESTRES_PREC[_trim_prec]
                _meses_disp_filt = [
                    m for m in meses_disp
                    if "-" in str(m) and int(str(m).split("-")[1]) in _trim_months_prec
                ]
            else:
                _meses_disp_filt = meses_disp
            _meses_base = list(_meses_disp_filt if _meses_disp_filt else meses_disp)
            _MESES_FULL_PREC = {
                "01":"Enero","02":"Febrero","03":"Marzo","04":"Abril","05":"Mayo","06":"Junio",
                "07":"Julio","08":"Agosto","09":"Septiembre","10":"Octubre","11":"Noviembre","12":"Diciembre",
            }
            def _ym_prec_lbl(ym):
                p = str(ym).split("-")
                return f"{_MESES_FULL_PREC.get(p[1], p[1])} {p[0][2:]}" if len(p) == 2 else ym
            _lbl_to_per_prec = {_ym_prec_lbl(m): m for m in meses_disp}
            _meses_base_lbl  = [_ym_prec_lbl(m) for m in _meses_base]
            _meses_sel_lbl = st.multiselect(
                "Mes a evaluar",
                _meses_base_lbl,
                default=[],
                key="kpi_mes",
                placeholder="Todos los meses",
            )
            _meses_sel_raw = [_lbl_to_per_prec[l] for l in _meses_sel_lbl if l in _lbl_to_per_prec]
            # Lista de meses seleccionados; vacío = Todos
            _meses_prec = _meses_sel_raw if _meses_sel_raw else _meses_base
            mes_sel = _meses_prec[0] if len(_meses_prec) == 1 else "Todos"
            # Helper local para etiqueta de mes (p.ej. "2026-05" → "May '26")
            _MN_PREC = {1:"Ene",2:"Feb",3:"Mar",4:"Abr",5:"May",6:"Jun",
                        7:"Jul",8:"Ago",9:"Sep",10:"Oct",11:"Nov",12:"Dic"}
            def _prec_m2l(ym):
                p = str(ym).split("-")
                return f"{_MN_PREC.get(int(p[1]),p[1])} '{p[0][2:]}" if len(p)==2 else str(ym)
            # Etiqueta legible para display
            if not _meses_sel_raw:
                _mes_lbl_prec = "Todos los meses"
            elif len(_meses_sel_raw) <= 3:
                _mes_lbl_prec = ", ".join([_prec_m2l(m) for m in _meses_sel_raw])
            else:
                _mes_lbl_prec = f"{len(_meses_sel_raw)} meses seleccionados"

        with kf2:
            # Semana solo disponible cuando se selecciona 1 solo mes
            if len(_meses_sel_raw) == 1:
                _sems_prec = _semanas_del_mes(_meses_prec[0])
                _sem_prec_opts = ["Todas"] + [s[0] for s in _sems_prec]
            else:
                _sems_prec = []
                _sem_prec_opts = ["Todas"]
            _sem_prec = st.selectbox("Semana", _sem_prec_opts, key="kpi_semana",
                                     disabled=(len(_meses_sel_raw) != 1))
        with kf3:
            # Usar _EQUIPO_LABEL — labels: "Carlos Avila" (Coquimbo), "Luis Lopez" (Concepción)
            _eq_prec_opts = ["Todos"] + list(_EQUIPO_LABEL.values())
            equipo_kpi = st.selectbox("Equipo", _eq_prec_opts, key="kpi_equipo")
        with kf4:
            if equipo_kpi != "Todos":
                _grp_kpi = _LABEL_TO_GRUPO.get(equipo_kpi)
                # IMPORTANTE: usar nombres completos desde df_kpi_raw (misma fuente que el filtro).
                # Antes se usaban los nombres cortos de GRUPOS_TERRENO["miembros"] (ej "Jorge Rodriguez"),
                # pero df_ot_scores["tecnico"] contiene nombres completos ("Jorge Raúl  Rodríguez Fuentes")
                # → el filtro nunca coincidía → "No hay OTs cerradas".
                _tec_kpi_opts = ["Todos"] + sorted(
                    t for t in df_kpi_raw[df_kpi_raw["equipo"] == _grp_kpi]["tecnico"].dropna().unique()
                    if not _es_excluido(t)
                )
            else:
                _tec_kpi_opts = ["Todos"] + sorted(
                    t for t in df_kpi_raw["tecnico"].dropna().unique()
                    if not _es_excluido(t) and _get_equipo(t) != "Sin equipo"
                )
            tec_kpi_sel = st.selectbox("Técnico", _tec_kpi_opts, key="kpi_tecnico")

        # ── Filtrar scores pre-computados (sin recalcular) ───────────────────────
        # df_ot_all ya tiene scores calculados para todas las OTs.
        # Solo se hace un slice en memoria → cambios de filtro son instantáneos.
        _meses_prec_str = [str(m) for m in _meses_prec]
        df_ot_scores = df_ot_all[
            df_ot_all["mes"].astype(str).isin(_meses_prec_str)
        ].copy()
        if _sem_prec != "Todas":
            _sem_prec_match = next((s for s in _sems_prec if s[0] == _sem_prec), None)
            if _sem_prec_match:
                _sem_prec_start, _sem_prec_end = _sem_prec_match[1], _sem_prec_match[2]
                df_ot_scores = df_ot_scores[
                    (df_ot_scores["creation_date_local"] >= _sem_prec_start) &
                    (df_ot_scores["creation_date_local"] <= _sem_prec_end)
                ]
        if equipo_kpi != "Todos":
            _grp_kpi = _LABEL_TO_GRUPO.get(equipo_kpi)
            if _grp_kpi:
                df_ot_scores = df_ot_scores[df_ot_scores["equipo"] == _grp_kpi]
        if tec_kpi_sel != "Todos":
            df_ot_scores = df_ot_scores[df_ot_scores["tecnico"] == tec_kpi_sel]

        df_tec_scores = score_llenado_por_tecnico(df_ot_scores)

        st.divider()

        # ── Tarjetas de bono por equipo (sin título, justo bajo los filtros) ─────
        if not df_tec_scores.empty and not df_ot_scores.empty:
            # Técnico específico → 1 tarjeta (su equipo)
            # Equipo específico  → 1 tarjeta (ese equipo)
            # Todos              → 5 tarjetas (todos los equipos)
            if tec_kpi_sel != "Todos":
                _tec_grp_prec = _get_equipo(tec_kpi_sel)
                _EQUIPO_LABEL_PREC = {k: v for k, v in _EQUIPO_LABEL.items() if k == _tec_grp_prec} or _EQUIPO_LABEL
            elif equipo_kpi != "Todos":
                _grp_kpi_prec = _LABEL_TO_GRUPO.get(equipo_kpi)
                _EQUIPO_LABEL_PREC = {k: v for k, v in _EQUIPO_LABEL.items() if k == _grp_kpi_prec} or _EQUIPO_LABEL
            else:
                _EQUIPO_LABEL_PREC = _EQUIPO_LABEL
            _prec_eq_cols = st.columns(len(_EQUIPO_LABEL_PREC))
            for _pi, (_pgk, _pgl) in enumerate(
                zip(_EQUIPO_LABEL_PREC.keys(), _EQUIPO_LABEL_PREC.values())
            ):
                _senior_prec = GRUPOS_TERRENO.get(_pgk, {}).get("senior", "")
                _tecs_grp = df_tec_scores[
                    df_tec_scores["tecnico"].apply(_get_equipo) == _pgk
                ] if not df_tec_scores.empty else pd.DataFrame()
                _n_tecs_prec   = len(_tecs_grp)
                _ots_llenadas  = int(_tecs_grp["ots_evaluadas"].sum()) if _n_tecs_prec > 0 else 0
                _ots_erradas   = int(_tecs_grp["n_errores"].sum())     if _n_tecs_prec > 0 else 0
                _ots_correctas = _ots_llenadas - _ots_erradas
                _cumpl_pct     = (_ots_correctas / _ots_llenadas * 100) if _ots_llenadas > 0 else 100.0
                _err_total_dim = int(_tecs_grp["err_total_dim"].sum()) if "err_total_dim" in _tecs_grp.columns else _ots_erradas
                _err_t = int(_tecs_grp["err_tiempo"].sum())    if "err_tiempo"    in _tecs_grp.columns else 0
                _err_c = int(_tecs_grp["err_causa"].sum())     if "err_causa"     in _tecs_grp.columns else 0
                _err_n = int(_tecs_grp["err_numeral"].sum())   if "err_numeral"   in _tecs_grp.columns else 0
                _err_d = int(_tecs_grp["err_deteccion"].sum()) if "err_deteccion" in _tecs_grp.columns else 0
                _bono_sum_prec = int(_tecs_grp["bono_semanal"].sum()) if _n_tecs_prec > 0 else 0
                _con_bono_prec = int(_tecs_grp["umbral_bono"].sum())  if _n_tecs_prec > 0 else 0
                _pbpct, _pblbl, _pbcol, _pbclp = _bono_prec(_cumpl_pct)

                # Barra de progreso de cumplimiento
                _bar_w = min(100, max(0, _cumpl_pct))
                _bar_col = _pbcol

                # Cuando hay un técnico específico seleccionado y esta tarjeta tiene sus datos,
                # mostrar el nombre del técnico como título y el equipo como contexto.
                if tec_kpi_sel != "Todos" and _n_tecs_prec > 0:
                    _card_titulo    = tec_kpi_sel
                    _card_subtitulo = f"Equipo: {_pgl}"
                else:
                    _card_titulo    = _pgl
                    _card_subtitulo = f"Senior: {_senior_prec}"

                _prec_eq_cols[_pi].markdown(
                    f'<div style="background:{_t["card"]};border:2px solid {_pbcol}33;'
                    f'border-radius:8px;padding:12px 14px;text-align:center;">'
                    # Nombre técnico o equipo
                    f'<div style="font-weight:700;font-size:0.92rem;color:{_t["text"]};">{_card_titulo}</div>'
                    f'<div style="font-size:0.75rem;color:{_t["muted"]};margin-bottom:6px;">{_card_subtitulo}</div>'
                    # ── CUMPLIMIENTO — protagonista ──
                    f'<div style="font-size:2rem;font-weight:800;line-height:1.1;color:{_pbcol};">'
                    f'{_cumpl_pct:.1f}%</div>'
                    f'<div style="font-size:0.68rem;font-weight:600;letter-spacing:0.05em;'
                    f'text-transform:uppercase;color:{_t["muted"]};margin-bottom:6px;">cumplimiento</div>'
                    # Barra de cumplimiento
                    f'<div style="background:{_t["prog_bg"]};border-radius:4px;height:8px;margin-bottom:6px;">'
                    f'<div style="background:{_bar_col};width:{_bar_w:.0f}%;height:8px;border-radius:4px;"></div></div>'
                    # Chip bono
                    f'<div style="background:{_pbcol};color:#fff;border-radius:4px;'
                    f'padding:3px 10px;font-size:0.82rem;font-weight:700;'
                    f'margin:0 auto 8px auto;display:inline-block;">{_pblbl}</div>'
                    # Separador sutil
                    f'<div style="border-top:1px solid {_t["muted"]}22;margin:6px 0;"></div>'
                    # Fila: OTs llenadas / erradas / correctas  (secundario, más pequeño)
                    f'<div style="display:flex;justify-content:center;gap:8px;margin-bottom:4px;">'
                    f'<div style="text-align:center;">'
                    f'<div style="font-size:1.0rem;font-weight:700;color:{_t["text"]};">{_ots_llenadas}</div>'
                    f'<div style="font-size:0.60rem;color:{_t["muted"]};">llenadas</div></div>'
                    f'<div style="font-size:0.9rem;color:{_t["muted"]};padding-top:3px;">→</div>'
                    f'<div style="text-align:center;">'
                    f'<div style="font-size:1.0rem;font-weight:700;color:#ef4444;">{_ots_erradas}</div>'
                    f'<div style="font-size:0.60rem;color:{_t["muted"]};">con error</div></div>'
                    f'<div style="font-size:0.9rem;color:{_t["muted"]};padding-top:3px;">+</div>'
                    f'<div style="text-align:center;">'
                    f'<div style="font-size:1.0rem;font-weight:700;color:#22c55e;">{_ots_correctas}</div>'
                    f'<div style="font-size:0.60rem;color:{_t["muted"]};">correctas</div></div>'
                    f'</div>'
                    # Desglose errores individuales (informativo)
                    f'<div style="font-size:0.64rem;color:{_t["muted"]};margin-bottom:5px;">'
                    f'Err.: {_err_total_dim} &nbsp;'
                    f'(⏱{_err_t} 🔍{_err_c} 🔢{_err_n} 🎯{_err_d})</div>'
                    # Con bono
                    f'<div style="font-size:0.68rem;color:{_t["muted"]};margin-top:2px;">'
                    f'{_con_bono_prec}/{_n_tecs_prec} con bono · ${_bono_sum_prec:,.0f}/sem</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            st.caption(
                "**Llenadas** = OTs evaluadas en el período (base 100%)  ·  "
                "**Con error** = OTs con ≥1 componente malo  ·  "
                "**Cumplimiento** = OTs correctas / OTs llenadas  ·  "
                "**Err. individuales** = suma de fallos por dimensión (informativo, no mide el KPI)  ·  "
                "Escala (por técnico/trimestre): ≥95%→$105K · ≥90%→$94.5K · ≥85%→$84K · "
                "≥80%→$73.5K · ≥75%→$63K · ≥70%→$52.5K · <70%→$0"
            )

        st.divider()

        # ── KPIs del mes ──────────────────────────────────────────────────────────
        if df_ot_scores.empty:
            st.warning(f"No hay OTs cerradas en **{_mes_lbl_prec}** con los filtros aplicados.")
        else:
            total_ots_mes = len(df_ot_scores)
            score_global = df_ot_scores["score_total"].mean()
            pct_tiempo = (df_ot_scores["score_tiempo"] >= 25).mean() * 100
            pct_causa  = df_ot_scores["causa_ok"].mean() * 100
            pct_numeral = df_ot_scores["numeral_ok"].mean() * 100
            tecnicos_con_bono = (df_tec_scores["umbral_bono"]).sum() if not df_tec_scores.empty else 0
            total_tecnicos = len(df_tec_scores)

            color_global, lbl_global = _score_level(score_global)

            gk1, gk2, gk3, gk4, gk5 = st.columns(5)
            gk1.metric("Score global del mes", f"{score_global:.1f} / 100",
                       delta=lbl_global, delta_color="off")
            gk2.metric("OTs evaluadas", f"{total_ots_mes:,}")
            gk3.metric("Tiempo OK (MP: 80% estim.)", f"{pct_tiempo:.1f}%",
                       delta=f"{'✅' if pct_tiempo >= 80 else '⚠️'}")
            gk4.metric("Causa raíz OK", f"{pct_causa:.1f}%",
                       delta=f"{'✅' if pct_causa >= 80 else '⚠️'}")
            gk5.metric("Técnicos con bono (≥90% exactitud)", f"{tecnicos_con_bono} / {total_tecnicos}")

            # ── Barra de puntaje global con desglose ──────────────────────────────
            st.divider()
            st.markdown('<div class="section-header">📊 Desglose del puntaje por dimensión</div>',
                        unsafe_allow_html=True)
            st.caption(
                f"Promedio mensual de **{total_ots_mes:,}** OTs  |  "
                f"Técnicos: **{total_tecnicos}**  |  Período: **{_mes_lbl_prec}**"
            )

            dim_avg = {
                "⏱ Tiempo ejecución (25 pts)":     df_ot_scores["score_tiempo"].mean(),
                "🔍 Causa raíz (25 pts)":           df_ot_scores["score_causa"].mean(),
                "🔢 Numeral registrado (25 pts)":   df_ot_scores["score_numeral"].mean(),
                "🎯 Modalidad de atención (25 pts)": df_ot_scores["score_deteccion"].mean()
                    if "score_deteccion" in df_ot_scores.columns else 0,
            }
            dim_max = {k: 25 for k in dim_avg}
            dim_colors = ["#f59e0b", "#3b82f6", "#22c55e", "#a855f7"]

            dim_df = pd.DataFrame([
                {
                    "Dimensión": k,
                    "Puntaje promedio": round(v, 1),
                    "Máximo": dim_max[k],
                    "% de máximo": round(v / dim_max[k] * 100, 1),
                }
                for k, v in dim_avg.items()
            ])

            _prec_sig = f"{_wo_sig}_{equipo_kpi}_{tec_kpi_sel}_{'|'.join(_meses_prec_str)}_{_sem_prec}_{_trim_prec}"
            dc1, dc2 = st.columns([3, 2])
            with dc1:
                _dim_k = f"_fig_prec_dim_{_current_theme}_{_prec_sig}"
                if _dim_k not in st.session_state:
                    fig_dim = px.bar(
                        dim_df, x="Puntaje promedio", y="Dimensión",
                        orientation="h",
                        color="Dimensión",
                        color_discrete_sequence=dim_colors,
                        text=dim_df["Puntaje promedio"].apply(lambda v: f"{v:.1f}"),
                        labels={"Puntaje promedio": "Puntaje promedio (escala de cada dimensión)"},
                    )
                    fig_dim.update_traces(textposition="outside")
                    fig_dim.update_layout(
                        showlegend=False, height=200,
                        margin=dict(t=10, b=10, l=10, r=60),
                        xaxis=dict(range=[0, 40]),
                        yaxis={"categoryorder": "array",
                               "categoryarray": list(reversed(list(dim_avg.keys())))},
                    )
                    _apply_plot_theme(fig_dim)
                    st.session_state[_dim_k] = fig_dim
                st.plotly_chart(st.session_state[_dim_k], width="stretch")
            with dc2:
                for row in dim_df.itertuples():
                    pct = row._4  # % de máximo
                    color = "#22c55e" if pct >= 99 else ("#f59e0b" if pct >= 90 else "#ef4444")
                    st.markdown(
                        f'<div style="margin-bottom:10px;">'
                        f'<div style="display:flex;justify-content:space-between;'
                        f'font-size:0.82rem;color:{_t["muted"]};margin-bottom:2px;">'
                        f'<span>{row.Dimensión}</span>'
                        f'<span style="color:{color};font-weight:700">{pct:.0f}%</span></div>'
                        f'<div style="background:{_t["prog_bg"]};border-radius:4px;height:10px;">'
                        f'<div style="background:{color};width:{min(pct,100):.0f}%;'
                        f'height:10px;border-radius:4px;"></div></div></div>',
                        unsafe_allow_html=True,
                    )

            # (Tarjetas de bono movidas arriba, justo bajo los filtros)

            # ══════════════════════════════════════════════════════════════════
            # SECCIÓN: CAUSA RAÍZ
            # ══════════════════════════════════════════════════════════════════
            st.divider()
            st.markdown('<div class="section-header">🔍  Causa Raíz — Correctivos</div>',
                        unsafe_allow_html=True)

            # ── Leyenda expandible ────────────────────────────────────────────
            with st.expander("📖  Leyenda de categorías válidas (F.N.A.O / F.A.O)", expanded=False):
                _leg1, _leg2 = st.columns(2)
                with _leg1:
                    st.markdown("**🟢 F.N.A.O — Falla No Atribuible a Occimiano**")
                    st.markdown("*Causas válidas (SI CORRESPONDE):*")
                    st.markdown("""
- `01.1.- DAÑO CAUSADO POR CLIENTE`
- `01.2.- MAL USO U OMISION EDS` *(sin sal, llave cerrada, etc.)*
- `01.3.- FICHERO (MOJADO/DAÑADO)`
- `02.3.- FICHERO / FALLA PROGRAMACION`
- `02.4.- REPUESTOS /OTROS` *(solo si la falla proviene del concesionario)*
- `03.1.- DAÑOS EN ESTRUCTURAS/GASFITERÍA/OOCC`
""")
                    st.markdown("*❌ No corresponde usar con F.N.A.O:*")
                    st.markdown("""
- `01.4.- REPUESTOS (DESGASTE)/OTROS` → debe ir en F.A.O
- `01.5.- ERROR 01 ELECTRICO` → debe ir en F.A.O
- `01.6.- ERROR 03 AGUA` → debe ir en F.A.O
- `01.7.- OTROS`, `02.7.- OTROS` → dato vago, no aceptado
- `02.2.- BY PASS / MOTORES` → debe ir en F.A.O
- `02.5.- ERROR 01 ELECTRICO` → debe ir en F.A.O
- `02.6.- ERROR 03 AGUA` → debe ir en F.A.O
- Números (147, 148, 150…) → no corresponde, dato no claro
""")
                with _leg2:
                    st.markdown("**🔴 F.A.O — Falla Atribuible a Occimiano**")
                    st.markdown("*Causas válidas para F.A.O:*")
                    st.markdown("""
- `01.4.- REPUESTOS (DESGASTE)/OTROS`
- `01.5.- ERROR 01 ELECTRICO`
- `01.6.- ERROR 03 AGUA`
- `02.2.- BY PASS / MOTORES`
- `02.5.- ERROR 01 ELECTRICO`
- `02.6.- ERROR 03 AGUA`
- `02.4.- REPUESTOS /OTROS` *(con falla comprobada)*
""")
                    st.markdown("**⚫ Siempre se considera ERROR:**")
                    st.markdown("""
- Campo **"Falla"** vacío o con `04.- SIN INFORMACION`
- Campo **"Causas de la Falla"** vacío o con `SIN CLASIFICAR`
- Causas con **solo números** (147, 148, 150, 160, etc.)
- Usar `01.7.- OTROS` o `02.7.- OTROS` sin especificar
""")

            # ── Helpers de mes compartidos por Causa Raíz y Tiempo ──────────
            import re as _re
            _MN2 = {1:"Ene",2:"Feb",3:"Mar",4:"Abr",5:"May",6:"Jun",
                    7:"Jul",8:"Ago",9:"Sep",10:"Oct",11:"Nov",12:"Dic"}
            def _m2l(ym):
                p = str(ym).split("-")
                return f"{_MN2.get(int(p[1]),p[1])} '{p[0][2:]}" if len(p)==2 else str(ym)

            # ── Gráfico evolución mensual Causa Raíz ─────────────────────────
            st.markdown("**Evolución mensual — % correctivos con Causa Raíz correctamente llenada**")

            def _causa_raiz_ok(causa: str) -> bool:
                """True si la causa raíz está correctamente llenada."""
                causa = str(causa or "").strip()
                if not causa or causa.upper() in ("SIN CLASIFICAR", "NONE", "-", "", "NAN"):
                    return False
                if _re.match(r"^\d+$", causa):       # solo números → error
                    return False
                if _re.match(r"^0[12]\.7", causa.upper()):  # OTROS → dato vago
                    return False
                if _re.match(r"^\d{2}\.\d", causa):  # código válido
                    return True
                return False

            # df_kpi_raw tiene columna "causa_raiz_raw" (no "failure_cause")
            _df_cr_base = df_kpi_raw[
                df_kpi_raw.get("es_correctiva", pd.Series(dtype=bool, index=df_kpi_raw.index))
                == True
            ].copy() if "es_correctiva" in df_kpi_raw.columns else df_kpi_raw.copy()

            if equipo_kpi != "Todos":
                _grp_cr = _LABEL_TO_GRUPO.get(equipo_kpi, equipo_kpi)
                _df_cr_base = _df_cr_base[_df_cr_base["equipo"] == _grp_cr]
            if tec_kpi_sel != "Todos":
                _df_cr_base = _df_cr_base[_df_cr_base["tecnico"] == tec_kpi_sel]

            if not _df_cr_base.empty and "causa_raiz_raw" in _df_cr_base.columns:
                _df_cr_base = _df_cr_base.copy()
                _df_cr_base["_causa_ok"] = _df_cr_base["causa_raiz_raw"].apply(_causa_raiz_ok)

                _cr_mes = (
                    _df_cr_base.groupby("mes")
                    .agg(total=("_causa_ok","count"), ok=("_causa_ok","sum"))
                    .reset_index().sort_values("mes")
                )
                _cr_mes["pct_ok"]  = (_cr_mes["ok"] / _cr_mes["total"] * 100).round(1)
                _cr_mes["pct_err"] = (100 - _cr_mes["pct_ok"]).round(1)
                _cr_mes["mes_lbl"] = _cr_mes["mes"].apply(_m2l)

                _cr_sig = f"_fig_cr_{_current_theme}_{_wo_sig}_{equipo_kpi}_{tec_kpi_sel}"
                if _cr_sig not in st.session_state:
                    _fig_cr = make_subplots(specs=[[{"secondary_y": True}]])
                    _fig_cr.add_trace(go.Bar(
                        x=_cr_mes["mes_lbl"], y=_cr_mes["total"],
                        name="OTs correctivas", marker_color="#94a3b8", opacity=0.85,
                        text=_cr_mes["total"], textposition="inside",
                        textfont=dict(size=11, color="#ffffff"),
                    ), secondary_y=False)
                    _fig_cr.add_trace(go.Scatter(
                        x=_cr_mes["mes_lbl"], y=_cr_mes["pct_ok"],
                        name="% Causa correcta", mode="lines+markers",
                        line=dict(color="#22c55e", width=3),
                        marker=dict(size=10, color="#22c55e", line=dict(color="#fff", width=2)),
                    ), secondary_y=True)
                    # Anotaciones con caja blanca
                    for _, _rr in _cr_mes.iterrows():
                        _fig_cr.add_annotation(
                            x=_rr["mes_lbl"], y=_rr["pct_ok"], yref="y2",
                            text=f"<b>{_rr['pct_ok']:.1f}%</b>  {int(_rr['ok'])}/{int(_rr['total'])}",
                            showarrow=False, yanchor="bottom", yshift=10,
                            font=dict(size=11, color="#16a34a"),
                            bgcolor="rgba(255,255,255,0.88)",
                            bordercolor="#22c55e", borderwidth=1.5, borderpad=4,
                        )
                    _fig_cr.add_trace(go.Scatter(
                        x=_cr_mes["mes_lbl"], y=_cr_mes["pct_err"],
                        name="% Error causa", mode="lines+markers",
                        line=dict(color="#ef4444", width=2, dash="dot"),
                        marker=dict(size=7, color="#ef4444"),
                        visible="legendonly",
                    ), secondary_y=True)
                    _fig_cr.update_layout(
                        height=380, margin=dict(t=40, b=20),
                        legend=dict(orientation="h", y=1.08, x=0), bargap=0.3,
                    )
                    _fig_cr.update_yaxes(title_text="OTs correctivas", secondary_y=False)
                    _fig_cr.update_yaxes(title_text="% Causa raíz OK", secondary_y=True,
                                         tickformat=".1f", ticksuffix="%", range=[0, 110])
                    _apply_plot_theme(_fig_cr)
                    st.session_state[_cr_sig] = _fig_cr
                st.plotly_chart(st.session_state.get(_cr_sig), width="stretch")

                # ── Tabla detalle de Causa Raíz ───────────────────────────────
                with st.expander(f"📋 Detalle de OTs — Causa Raíz ({len(_df_cr_base):,} correctivos)", expanded=False):
                    _det_cr = _df_cr_base[[c for c in
                        ["folio","tecnico","creation_date","maint_type",
                         "causa_raiz_raw","causa_clasif","_causa_ok"]
                        if c in _df_cr_base.columns]].copy()
                    _det_cr["creation_date"] = pd.to_datetime(_det_cr["creation_date"], errors="coerce")\
                        .dt.tz_convert(None).dt.strftime("%d/%m/%Y")
                    _det_cr["Estado"] = _det_cr["_causa_ok"].apply(
                        lambda v: "✅ Correcto" if v else "❌ Error")
                    _det_cr = _det_cr.drop(columns=["_causa_ok"], errors="ignore").rename(columns={
                        "folio":"OT","tecnico":"Técnico","creation_date":"Fecha",
                        "maint_type":"Tipo","causa_raiz_raw":"Causa Raíz",
                        "causa_clasif":"Clasificación"
                    }).sort_values("Fecha", ascending=False)
                    _show_df(_det_cr, hide_index=True, width="stretch",
                        column_config={
                            "OT":            st.column_config.TextColumn(width=110),
                            "Técnico":       st.column_config.TextColumn(width=190),
                            "Fecha":         st.column_config.TextColumn(width=100),
                            "Tipo":          st.column_config.TextColumn(width=180),
                            "Causa Raíz":    st.column_config.TextColumn(width=280),
                            "Clasificación": st.column_config.TextColumn(width=110),
                            "Estado":        st.column_config.TextColumn(width=110),
                        })
            else:
                st.info("Sin datos de OTs correctivas para el filtro actual.")

            # ══════════════════════════════════════════════════════════════════
            # SECCIÓN: TIEMPO DE EJECUCIÓN
            # ══════════════════════════════════════════════════════════════════
            st.divider()
            st.markdown('<div class="section-header">⏱  Tiempo de Ejecución — Preventivos</div>',
                        unsafe_allow_html=True)

            # ── Leyenda expandible ────────────────────────────────────────────
            with st.expander("📖  Regla de cumplimiento de tiempo (80% de la duración estimada)", expanded=False):
                st.markdown("""
**¿Cómo funciona?**

Cada mantenimiento preventivo tiene una **duración estimada** (programada en Fracttal).
El técnico debe ejecutar la tarea respetando ese tiempo mínimo con una tolerancia del **20%**.

| Duración estimada | Mínimo aceptable (80%) | Ejemplo |
|---|---|---|
| 00:40 (40 min) | 00:32 (32 min) | Si el técnico tardó 35 min → ✅ Cumple |
| 00:30 (30 min) | 00:24 (24 min) | Si el técnico tardó 05 min → ❌ No cumple |
| 01:00 (60 min) | 00:48 (48 min) | Si el técnico tardó 50 min → ✅ Cumple |

**¿Qué se mide?**
- **`Tiempo de Ejecución`** (campo `tasks_duration` de Fracttal) vs **`Duración Estimada`** (campo `duration`)
- Si `Tiempo Ejecución ≥ Duración Estimada × 80%` → **CUMPLE**
- Si `Tiempo Ejecución < Duración Estimada × 80%` → **ERROR** (posible quick-tick)
- Si no hay duración estimada → **Sin datos** (no penaliza)

**¿Por qué el 20% de holgura?**
El técnico puede llegar y encontrar la máquina en buen estado, reduciendo el tiempo real,
pero no puede hacerlo en 1 o 5 minutos si el estándar es 40 minutos.
""")

            # ── Gráfico evolución mensual Tiempo de Ejecución ────────────────
            st.markdown("**Evolución mensual — % preventivos con tiempo de ejecución correcto**")

            _df_te_base = df_kpi_raw[df_kpi_raw["maint_type"] != "CORRECTIVA"].copy() \
                          if "maint_type" in df_kpi_raw.columns else df_kpi_raw.copy()

            # Aplicar filtros de equipo/técnico
            if equipo_kpi != "Todos":
                _grp_te = _LABEL_TO_GRUPO.get(equipo_kpi, equipo_kpi)
                _df_te_base = _df_te_base[_df_te_base["equipo"] == _grp_te]
            if tec_kpi_sel != "Todos":
                _df_te_base = _df_te_base[_df_te_base["tecnico"] == tec_kpi_sel]

            if not _df_te_base.empty and "estimated_sec" in _df_te_base.columns:
                # Solo OTs con duración estimada > 0
                _df_te = _df_te_base[_df_te_base["estimated_sec"] > 0].copy()
                _df_te["_te_ok"] = _df_te["duration_sec"] >= _df_te["estimated_sec"] * 0.80

                _te_mes = (
                    _df_te.groupby("mes")
                    .agg(total=("_te_ok","count"), ok=("_te_ok","sum"))
                    .reset_index()
                    .sort_values("mes")
                )
                _te_mes["pct_ok"]  = (_te_mes["ok"] / _te_mes["total"] * 100).round(1)
                _te_mes["pct_err"] = (100 - _te_mes["pct_ok"]).round(1)
                _te_mes["mes_lbl"] = _te_mes["mes"].apply(_m2l)

                _te_sig = f"_fig_te_{_current_theme}_{_wo_sig}_{equipo_kpi}_{tec_kpi_sel}"
                if _te_sig not in st.session_state:
                    _fig_te = make_subplots(specs=[[{"secondary_y": True}]])
                    _fig_te.add_trace(go.Bar(
                        x=_te_mes["mes_lbl"], y=_te_mes["total"],
                        name="OTs preventivas", marker_color="#94a3b8", opacity=0.85,
                        text=_te_mes["total"], textposition="inside",
                        textfont=dict(size=11, color="#ffffff"),
                    ), secondary_y=False)
                    _fig_te.add_trace(go.Scatter(
                        x=_te_mes["mes_lbl"], y=_te_mes["pct_ok"],
                        name="% Tiempo correcto (≥80%)", mode="lines+markers",
                        line=dict(color="#3b82f6", width=3),
                        marker=dict(size=10, color="#3b82f6", line=dict(color="#fff", width=2)),
                    ), secondary_y=True)
                    # Anotaciones con caja blanca
                    for _, _tr in _te_mes.iterrows():
                        _fig_te.add_annotation(
                            x=_tr["mes_lbl"], y=_tr["pct_ok"], yref="y2",
                            text=f"<b>{_tr['pct_ok']:.1f}%</b>  {int(_tr['ok'])}/{int(_tr['total'])}",
                            showarrow=False, yanchor="bottom", yshift=10,
                            font=dict(size=11, color="#1d4ed8"),
                            bgcolor="rgba(255,255,255,0.88)",
                            bordercolor="#3b82f6", borderwidth=1.5, borderpad=4,
                        )
                    _fig_te.add_trace(go.Scatter(
                        x=_te_mes["mes_lbl"], y=_te_mes["pct_err"],
                        name="% Tiempo insuficiente", mode="lines+markers",
                        line=dict(color="#ef4444", width=2, dash="dot"),
                        marker=dict(size=7, color="#ef4444"),
                        visible="legendonly",
                    ), secondary_y=True)
                    _fig_te.update_layout(
                        height=380, margin=dict(t=40, b=20),
                        legend=dict(orientation="h", y=1.08, x=0), bargap=0.3,
                    )
                    _fig_te.update_yaxes(title_text="OTs preventivas", secondary_y=False)
                    _fig_te.update_yaxes(
                        title_text="% Tiempo correcto", secondary_y=True,
                        tickformat=".1f", ticksuffix="%", range=[0, 110]
                    )
                    _apply_plot_theme(_fig_te)
                    st.session_state[_te_sig] = _fig_te
                st.plotly_chart(st.session_state.get(_te_sig), width="stretch")

                # ── Tabla detalle de Tiempo de Ejecución ─────────────────────
                def _fmt_seg(s):
                    if pd.isna(s) or s == 0: return "—"
                    s = int(s); h, m = s//3600, (s%3600)//60
                    return f"{h:02d}:{m:02d}"

                _det_te = _df_te.copy()
                _det_te["_minimo_sec"] = (_det_te["estimated_sec"] * 0.80).round(0)
                _det_te["_pct_ej"]     = (_det_te["duration_sec"] / _det_te["estimated_sec"] * 100).round(1)
                _det_te["creation_date"] = pd.to_datetime(_det_te["creation_date"], errors="coerce")\
                    .dt.tz_convert(None).dt.strftime("%d/%m/%Y")

                _det_te_disp = _det_te[[c for c in
                    ["folio","tecnico","creation_date","maint_type",
                     "estimated_sec","_minimo_sec","duration_sec","_pct_ej","_te_ok"]
                    if c in _det_te.columns]].copy()
                _det_te_disp["T. Estimado"]   = _det_te_disp["estimated_sec"].apply(_fmt_seg)
                _det_te_disp["Mín. 80%"]       = _det_te_disp["_minimo_sec"].apply(_fmt_seg)
                _det_te_disp["T. Ejecución"]   = _det_te_disp["duration_sec"].apply(_fmt_seg)
                _det_te_disp["% Ejecutado"]    = _det_te_disp["_pct_ej"]
                _det_te_disp["Estado"]         = _det_te_disp["_te_ok"].apply(
                    lambda v: "✅ Cumple" if v else "❌ No cumple")
                _det_te_disp = _det_te_disp.drop(
                    columns=["estimated_sec","_minimo_sec","duration_sec","_pct_ej","_te_ok"],
                    errors="ignore"
                ).rename(columns={
                    "folio":"OT","tecnico":"Técnico","creation_date":"Fecha","maint_type":"Tipo"
                }).sort_values("Fecha", ascending=False)

                with st.expander(f"📋 Detalle de OTs — Tiempo de Ejecución ({len(_det_te_disp):,} preventivos con estimado)", expanded=False):
                    _show_df(_det_te_disp, hide_index=True, width="stretch",
                        column_config={
                            "OT":          st.column_config.TextColumn(width=110),
                            "Técnico":     st.column_config.TextColumn(width=190),
                            "Fecha":       st.column_config.TextColumn(width=100),
                            "Tipo":        st.column_config.TextColumn(width=200),
                            "T. Estimado": st.column_config.TextColumn(width=100,
                                help="Duración programada en Fracttal (HH:MM)"),
                            "Mín. 80%":    st.column_config.TextColumn(width=90,
                                help="Tiempo mínimo aceptable = 80% del estimado"),
                            "T. Ejecución":st.column_config.TextColumn(width=110,
                                help="Tiempo real que tardó el técnico"),
                            "% Ejecutado": st.column_config.ProgressColumn(
                                label="% Ejecutado", min_value=0, max_value=150, format="%.1f%%",
                                help="T.Ejecución / T.Estimado × 100. Verde si ≥80%"),
                            "Estado":      st.column_config.TextColumn(width=110),
                        })
            else:
                st.info("Sin datos de duración estimada disponibles para el filtro actual.")

            # ══════════════════════════════════════════════════════════════════
            # SECCIÓN: REGISTRO DE NUMERALES
            # ══════════════════════════════════════════════════════════════════
            st.divider()
            st.markdown('<div class="section-header">🔢  Registro de Numerales — OTs con número de ficha</div>',
                        unsafe_allow_html=True)
            st.caption(
                "Porcentaje de OTs donde el técnico registró un número de ficha (≥4 dígitos) "
                "en la nota o task_note. Aplica a todos los tipos de OT."
            )

            _df_num_base = df_ot_scores.copy()
            if not _df_num_base.empty:
                _num_ok  = int(_df_num_base["numeral_ok"].sum())
                _num_tot = len(_df_num_base)
                _num_pct = _num_ok / _num_tot * 100 if _num_tot > 0 else 0.0

                # ── Evolución mensual de registro de numerales ────────────────
                _df_num_hist = df_ot_all.copy()
                if equipo_kpi != "Todos":
                    _grp_num = _LABEL_TO_GRUPO.get(equipo_kpi, equipo_kpi)
                    _df_num_hist = _df_num_hist[_df_num_hist["equipo"] == _grp_num]
                if tec_kpi_sel != "Todos":
                    _df_num_hist = _df_num_hist[_df_num_hist["tecnico"] == tec_kpi_sel]
                _df_num_hist = _df_num_hist[_df_num_hist["mes"].isin(meses_disp[:6])].copy()

                nc1, nc2 = st.columns([1, 3])
                with nc1:
                    _color_num = "#22c55e" if _num_pct >= 90 else ("#f59e0b" if _num_pct >= 70 else "#ef4444")
                    st.markdown(
                        f'<div style="background:{_t["card"]};border:2px solid {_color_num}33;'
                        f'border-radius:10px;padding:20px;text-align:center;">'
                        f'<div style="font-size:2.2rem;font-weight:800;color:{_color_num};">'
                        f'{_num_pct:.1f}%</div>'
                        f'<div style="font-size:0.85rem;color:{_t["muted"]};margin-top:4px;">'
                        f'{_num_ok:,} / {_num_tot:,} OTs</div>'
                        f'<div style="font-size:0.8rem;color:{_t["muted"]};">con numeral registrado</div>'
                        f'<div style="font-size:0.75rem;color:{_t["muted"]};margin-top:8px;">'
                        f'Meta: ≥90%</div></div>',
                        unsafe_allow_html=True
                    )
                with nc2:
                    if not _df_num_hist.empty and "numeral_ok" in _df_num_hist.columns:
                        _num_mes = (
                            _df_num_hist.groupby("mes")
                            .agg(total=("numeral_ok","count"), ok=("numeral_ok","sum"))
                            .reset_index().sort_values("mes")
                        )
                        _num_mes["pct_ok"]  = (_num_mes["ok"]  / _num_mes["total"] * 100).round(1)
                        _num_mes["pct_err"] = (100 - _num_mes["pct_ok"]).round(1)
                        _num_mes["mes_lbl"] = _num_mes["mes"].apply(_m2l)

                        _num_sig = f"_fig_num_{_current_theme}_{_wo_sig}_{equipo_kpi}_{tec_kpi_sel}"
                        if _num_sig not in st.session_state:
                            _fig_num = go.Figure()
                            _fig_num.add_trace(go.Bar(
                                x=_num_mes["mes_lbl"], y=_num_mes["pct_ok"],
                                name="Registró numeral", marker_color="#22c55e", opacity=0.9,
                                text=_num_mes.apply(
                                    lambda r: f"{r['pct_ok']:.1f}%<br>{int(r['ok'])}/{int(r['total'])}",
                                    axis=1),
                                textposition="inside",
                                textfont=dict(size=11, color="#ffffff"),
                            ))
                            _fig_num.add_trace(go.Bar(
                                x=_num_mes["mes_lbl"], y=_num_mes["pct_err"],
                                name="Sin numeral", marker_color="#ef4444", opacity=0.9,
                                text=_num_mes["pct_err"].apply(lambda v: f"{v:.1f}%"),
                                textposition="inside",
                                textfont=dict(size=11, color="#ffffff"),
                            ))
                            _fig_num.add_hline(y=90, line_dash="dash", line_color="#22c55e",
                                               annotation_text="Meta 90%",
                                               annotation_position="top left", line_width=1.5)
                            _fig_num.update_layout(
                                barmode="stack", height=280,
                                margin=dict(t=20, b=20, l=10, r=20),
                                yaxis=dict(range=[0, 105], ticksuffix="%", title="% OTs"),
                                legend=dict(orientation="h", y=1.12, x=0),
                                bargap=0.3,
                            )
                            _apply_plot_theme(_fig_num)
                            st.session_state[_num_sig] = _fig_num
                        st.plotly_chart(st.session_state[_num_sig], width="stretch")

            # ══════════════════════════════════════════════════════════════════
            # SECCIÓN: MODALIDAD DE ATENCIÓN
            # ══════════════════════════════════════════════════════════════════
            st.divider()
            st.markdown('<div class="section-header">🎯  Modalidad de Atención — OTs con campo registrado</div>',
                        unsafe_allow_html=True)
            st.caption(
                "Porcentaje de OTs donde el técnico registró la modalidad de atención "
                "(Atendido Presencial / Vía Remota / Con su MP / Llamado Duplicado). "
                "Si queda como **SIN CLASIFICAR** = error de llenado."
            )

            if "deteccion_ok" in df_ot_scores.columns and not df_ot_scores.empty:
                _det_ok   = int(df_ot_scores["deteccion_ok"].sum())
                _det_tot  = len(df_ot_scores)
                _det_pct  = _det_ok / _det_tot * 100 if _det_tot > 0 else 0.0

                _df_det_hist = df_ot_all.copy()
                if equipo_kpi != "Todos":
                    _grp_det = _LABEL_TO_GRUPO.get(equipo_kpi, equipo_kpi)
                    _df_det_hist = _df_det_hist[_df_det_hist["equipo"] == _grp_det]
                if tec_kpi_sel != "Todos":
                    _df_det_hist = _df_det_hist[_df_det_hist["tecnico"] == tec_kpi_sel]
                _df_det_hist = _df_det_hist[_df_det_hist["mes"].isin(meses_disp[:6])].copy()

                dc1, dc2 = st.columns([1, 3])
                with dc1:
                    _color_det = "#22c55e" if _det_pct >= 90 else ("#f59e0b" if _det_pct >= 70 else "#ef4444")
                    st.markdown(
                        f'<div style="background:{_t["card"]};border:2px solid {_color_det}33;'
                        f'border-radius:10px;padding:20px;text-align:center;">'
                        f'<div style="font-size:2.2rem;font-weight:800;color:{_color_det};">'
                        f'{_det_pct:.1f}%</div>'
                        f'<div style="font-size:0.85rem;color:{_t["muted"]};margin-top:4px;">'
                        f'{_det_ok:,} / {_det_tot:,} OTs</div>'
                        f'<div style="font-size:0.8rem;color:{_t["muted"]};">con método registrado</div>'
                        f'<div style="font-size:0.75rem;color:{_t["muted"]};margin-top:8px;">'
                        f'Meta: ≥90%</div></div>',
                        unsafe_allow_html=True
                    )
                with dc2:
                    if not _df_det_hist.empty and "deteccion_ok" in _df_det_hist.columns:
                        _det_mes = (
                            _df_det_hist.groupby("mes")
                            .agg(total=("deteccion_ok", "count"), ok=("deteccion_ok", "sum"))
                            .reset_index().sort_values("mes")
                        )
                        _det_mes["pct_ok"]  = (_det_mes["ok"]  / _det_mes["total"] * 100).round(1)
                        _det_mes["pct_err"] = (100 - _det_mes["pct_ok"]).round(1)
                        _det_mes["mes_lbl"] = _det_mes["mes"].apply(_m2l)

                        _det_sig = f"_fig_det_{_current_theme}_{_wo_sig}_{equipo_kpi}_{tec_kpi_sel}"
                        if _det_sig not in st.session_state:
                            _fig_det = go.Figure()
                            _fig_det.add_trace(go.Bar(
                                x=_det_mes["mes_lbl"], y=_det_mes["pct_ok"],
                                name="Registró método", marker_color="#a855f7", opacity=0.9,
                                text=_det_mes.apply(
                                    lambda r: f"{r['pct_ok']:.1f}%<br>{int(r['ok'])}/{int(r['total'])}",
                                    axis=1),
                                textposition="inside",
                                textfont=dict(size=11, color="#ffffff"),
                            ))
                            _fig_det.add_trace(go.Bar(
                                x=_det_mes["mes_lbl"], y=_det_mes["pct_err"],
                                name="Sin clasificar", marker_color="#ef4444", opacity=0.9,
                                text=_det_mes["pct_err"].apply(lambda v: f"{v:.1f}%"),
                                textposition="inside",
                                textfont=dict(size=11, color="#ffffff"),
                            ))
                            _fig_det.add_hline(y=90, line_dash="dash", line_color="#a855f7",
                                               annotation_text="Meta 90%",
                                               annotation_position="top left", line_width=1.5)
                            _fig_det.update_layout(
                                barmode="stack", height=280,
                                margin=dict(t=20, b=20, l=10, r=20),
                                yaxis=dict(range=[0, 105], ticksuffix="%", title="% OTs"),
                                legend=dict(orientation="h", y=1.12, x=0),
                                bargap=0.3,
                            )
                            _apply_plot_theme(_fig_det)
                            st.session_state[_det_sig] = _fig_det
                        st.plotly_chart(st.session_state[_det_sig], width="stretch")

                # ── Ranking de técnicos por campo de detección ─────────────────
                if not df_tec_scores.empty and "err_deteccion" in df_tec_scores.columns:
                    _det_rank = df_tec_scores[
                        df_tec_scores["tecnico"].isin(TECNICOS_OCCIMIANO_FULL)
                    ][["tecnico","ots_evaluadas","err_deteccion","pct_deteccion_ok"]].copy()
                    _det_rank = _det_rank.sort_values("err_deteccion", ascending=False)
                    _det_rank["cumple"] = _det_rank["pct_deteccion_ok"].apply(
                        lambda p: "✅" if p >= 90 else ("⚠️" if p >= 70 else "❌"))
                    _det_rank_disp = _det_rank.rename(columns={
                        "tecnico":          "Técnico",
                        "ots_evaluadas":    "OTs evaluadas",
                        "err_deteccion":    "OTs sin método",
                        "pct_deteccion_ok": "% Con método",
                        "cumple":           "Estado",
                    })
                    with st.expander("👤 Detalle por técnico — Modalidad de atención", expanded=False):
                        _show_df(_det_rank_disp, hide_index=True, width="stretch",
                            column_config={
                                "OTs evaluadas": st.column_config.NumberColumn(format="%d"),
                                "OTs sin método":st.column_config.NumberColumn(
                                    format="%d",
                                    help="OTs donde dejó el campo como 'SIN CLASIFICAR'"),
                                "% Con método":  st.column_config.ProgressColumn(
                                    min_value=0, max_value=100, format="%.1f%%"),
                            })

            # ── Ranking de técnicos ───────────────────────────────────────────────
            st.divider()
            st.markdown('<div class="section-header">🏆 Ranking de técnicos — Índice de llenado</div>',
                        unsafe_allow_html=True)

            if not df_tec_scores.empty:
                # Filtrar solo técnicos activos del organigrama (Libro4 / GRUPOS_TERRENO)
                df_rank = df_tec_scores[
                    df_tec_scores["tecnico"].isin(TECNICOS_OCCIMIANO_FULL)
                ].copy()
                # Asegurar columna err_total_dim (puede faltar en caches pre-migración)
                if "err_total_dim" not in df_rank.columns:
                    df_rank["err_total_dim"] = df_rank["n_errores"]
                if "exactitud_pct" not in df_rank.columns:
                    df_rank["exactitud_pct"] = (
                        (1 - df_rank["n_errores"] / df_rank["ots_evaluadas"].clip(lower=1)) * 100
                    ).round(1)
                # Nivel de bono — escala trimestral 7 niveles
                def _nivel_exactitud(e: float) -> str:
                    if e >= 95: return "≥95% — $105.000/trim"
                    if e >= 90: return "≥90% — $94.500/trim"
                    if e >= 85: return "≥85% — $84.000/trim"
                    if e >= 80: return "≥80% — $73.500/trim"
                    if e >= 75: return "≥75% — $63.000/trim"
                    if e >= 70: return "≥70% — $52.500/trim"
                    return "<70% — Sin bono"
                df_rank["nivel"] = df_rank["exactitud_pct"].apply(_nivel_exactitud)
                # Etiqueta en barra: "50 OTs c/error · 200 err.ind."
                df_rank["lbl_barra"] = df_rank.apply(
                    lambda r: f"{int(r['n_errores'])} OT · {int(r['err_total_dim'])} err.",
                    axis=1)
                # Ordenar de menor a mayor exactitud (peores arriba → visualmente más impactante)
                df_rank_plot = df_rank.sort_values("exactitud_pct", ascending=True)

                _rank_k = f"_fig_prec_rank_{_current_theme}_{_prec_sig}"
                if _rank_k not in st.session_state:
                    fig_rank = px.bar(
                        df_rank_plot,
                        x="n_errores", y="tecnico",
                        orientation="h",
                        color="nivel",
                        color_discrete_map={
                            "≥95% — $105.000/trim": _SCORE_COLOR["verde"],
                            "≥90% — $94.500/trim":  "#16a34a",
                            "≥85% — $84.000/trim":  "#4ade80",
                            "≥80% — $73.500/trim":  "#65a30d",
                            "≥75% — $63.000/trim":  _SCORE_COLOR["amarillo"],
                            "≥70% — $52.500/trim":  "#f97316",
                            "<70% — Sin bono":      _SCORE_COLOR["rojo"],
                        },
                        text=df_rank_plot["lbl_barra"],
                        labels={"n_errores": "OTs con error (≥1 dimensión incorrecta)", "tecnico": ""},
                        category_orders={"nivel": [
                            "≥95% — $105.000/trim", "≥90% — $94.500/trim",
                            "≥85% — $84.000/trim",  "≥80% — $73.500/trim",
                            "≥75% — $63.000/trim",  "≥70% — $52.500/trim",
                            "<70% — Sin bono",
                        ]},
                        custom_data=["err_total_dim", "exactitud_pct"],
                    )
                    fig_rank.update_traces(
                        textposition="outside",
                        hovertemplate=(
                            "<b>%{y}</b><br>"
                            "OTs con error: %{x}<br>"
                            "Errores individuales: %{customdata[0]}<br>"
                            "Exactitud: %{customdata[1]:.1f}%"
                            "<extra></extra>"
                        ),
                    )
                    _max_err = max(int(df_rank_plot["n_errores"].max()) + 2, 6)
                    fig_rank.update_layout(
                        height=max(300, len(df_rank_plot) * 40 + 80),
                        margin=dict(t=20, b=20, l=10, r=100),
                        xaxis=dict(range=[0, _max_err], tickformat="d",
                                   title="OTs con error (X) — barra muestra X; etiqueta X·Y"),
                        legend_title="Bono (exactitud)",
                    )
                    _apply_plot_theme(fig_rank)
                    st.session_state[_rank_k] = fig_rank
                st.plotly_chart(st.session_state[_rank_k], width="stretch")
                st.caption(
                    "**Barra** = OTs con ≥1 error (X)  ·  "
                    "**Etiqueta** = X OTs con error · Y errores individuales  ·  "
                    "**Color** = nivel de bono según exactitud %"
                )

                _tec_base = df_tec_scores[df_tec_scores["tecnico"].isin(TECNICOS_OCCIMIANO_FULL)].copy()

                # Guards para columnas nuevas (caches pre-migración)
                _guards = {
                    "err_total_dim":      lambda b: b["n_errores"],
                    "err_tiempo":         lambda b: 0,
                    "err_causa":          lambda b: 0,
                    "err_numeral":        lambda b: 0,
                    "err_deteccion":      lambda b: 0,
                    "ots_correctas":      lambda b: b["ots_evaluadas"] - b["n_errores"],
                    "tiempo_ok_count":    lambda b: (b["ots_evaluadas"] * b["pct_tiempo_ok"]    / 100).round(0).astype(int),
                    "causa_ok_count":     lambda b: (b["ots_evaluadas"] * b["pct_causa_ok"]     / 100).round(0).astype(int),
                    "numeral_ok_count":   lambda b: (b["ots_evaluadas"] * b["pct_numeral_ok"]   / 100).round(0).astype(int),
                    "deteccion_ok_count": lambda b: (b["ots_evaluadas"] * b.get("pct_deteccion_ok", pd.Series(0, index=b.index)) / 100).round(0).astype(int),
                    "pct_deteccion_ok":   lambda b: pd.Series(0.0, index=b.index),
                }
                for _cg, _fb in _guards.items():
                    if _cg not in _tec_base.columns:
                        try: _tec_base[_cg] = _fb(_tec_base)
                        except Exception: _tec_base[_cg] = 0
                if "exactitud_pct" not in _tec_base.columns:
                    _tec_base["exactitud_pct"] = (
                        (1 - _tec_base["n_errores"] / _tec_base["ots_evaluadas"].clip(lower=1)) * 100
                    ).round(1)

                # Formato X/Y (Z%) por dimensión
                def _fmt_dim(ok, total, pct):
                    return f"{int(ok)}/{int(total)} ({pct:.1f}%)"

                _tec_base["col_tiempo"]    = _tec_base.apply(lambda r: _fmt_dim(r["tiempo_ok_count"],    r["ots_evaluadas"], r["pct_tiempo_ok"]),    axis=1)
                _tec_base["col_causa"]     = _tec_base.apply(lambda r: _fmt_dim(r["causa_ok_count"],     r["ots_evaluadas"], r["pct_causa_ok"]),     axis=1)
                _tec_base["col_numeral"]   = _tec_base.apply(lambda r: _fmt_dim(r["numeral_ok_count"],   r["ots_evaluadas"], r["pct_numeral_ok"]),   axis=1)
                _tec_base["col_deteccion"] = _tec_base.apply(lambda r: _fmt_dim(r["deteccion_ok_count"], r["ots_evaluadas"], r["pct_deteccion_ok"]), axis=1)

                st.markdown('<div class="section-header">📋 Resumen por técnico</div>',
                            unsafe_allow_html=True)
                tec_disp = _tec_base[[
                    "tecnico", "ots_evaluadas", "ots_correctas", "n_errores", "err_total_dim",
                    "col_tiempo", "col_causa", "col_numeral", "col_deteccion",
                    "exactitud_pct", "bono_label",
                ]].copy()

                tec_disp.columns = [
                    "Técnico", "OTs evaluadas", "OTs sin error", "OTs con error", "Errores individuales",
                    "⏱ Tiempo OK", "🔍 Causa OK", "🔢 Numeral OK", "🎯 Modalidad OK",
                    "Exactitud %", "Bono semanal",
                ]

                _show_df(
                    tec_disp, width="stretch", hide_index=True,
                    column_config={
                        "OTs evaluadas":       st.column_config.NumberColumn(format="%d"),
                        "OTs sin error":       st.column_config.NumberColumn(
                            help="OTs donde los 4 componentes estuvieron correctos.", format="%d"),
                        "OTs con error":       st.column_config.NumberColumn(
                            help="OTs con al menos 1 componente incorrecto — estas cuentan para el KPI.", format="%d"),
                        "Errores individuales":st.column_config.NumberColumn(
                            help="Suma de fallos por dimensión (una OT puede aportar hasta 4).", format="%d"),
                        "⏱ Tiempo OK":         st.column_config.TextColumn(help="OTs con tiempo correcto / total (%)"),
                        "🔍 Causa OK":          st.column_config.TextColumn(help="OTs con causa raíz válida / total (%)"),
                        "🔢 Numeral OK":        st.column_config.TextColumn(help="OTs con numeral registrado / total (%)"),
                        "🎯 Modalidad OK":      st.column_config.TextColumn(help="OTs con método de detección ≠ SIN CLASIFICAR / total (%)"),
                        "Exactitud %":         st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f%%"),
                    },
                )

            # ── Evolución mensual (últimos 6 meses) ───────────────────────────────
            st.divider()
            st.markdown('<div class="section-header">📈 Evolución mensual del índice de llenado</div>',
                        unsafe_allow_html=True)

            ultimos_6 = meses_disp[:6]  # ya están ordenados desc
            # Usar df_ot_all pre-computado: solo slice, sin recalcular scores
            ot_hist = df_ot_all[df_ot_all["mes"].isin(ultimos_6)].copy()
            if equipo_kpi != "Todos":
                _grp_kpi_h = _LABEL_TO_GRUPO.get(equipo_kpi)
                if _grp_kpi_h:
                    ot_hist = ot_hist[ot_hist["equipo"] == _grp_kpi_h]
            if tec_kpi_sel != "Todos":
                ot_hist = ot_hist[ot_hist["tecnico"] == tec_kpi_sel]
            # df_ot_all ya trae columna "mes" — no hace falta recalcular
            if not ot_hist.empty:
                trend = (
                    ot_hist.groupby("mes")
                    .agg(
                        score_global= ("score_total",  "mean"),
                        pct_tiempo=   ("score_tiempo", lambda x: (x >= 34).mean() * 100),
                        pct_causa=    ("causa_ok",     lambda x: x.mean() * 100),
                        pct_numeral=  ("numeral_ok",   lambda x: x.mean() * 100),
                        ots=          ("folio",        "count"),
                    )
                    .reset_index()
                    .sort_values("mes")
                )
                trend = trend.round(1)

                _trend_k = f"_fig_prec_trend_{_current_theme}_{_wo_sig}_{equipo_kpi}_{tec_kpi_sel}"
                if _trend_k not in st.session_state:
                    fig_trend = px.line(
                        trend, x="mes", y="score_global",
                        markers=True, text="score_global",
                        labels={"mes": "Mes", "score_global": "Score global (0-100)"},
                        title="Score global de llenado por mes",
                    )
                    fig_trend.update_traces(
                        textposition="top center",
                        line_color="#3b82f6",
                        marker=dict(size=8),
                    )
                    fig_trend.add_hline(y=100, line_dash="dash", line_color=_SCORE_COLOR["verde"],
                                        annotation_text="Score perfecto (0 errores)")
                    fig_trend.update_layout(
                        yaxis=dict(range=[0, 105]),
                        height=320,
                        margin=dict(t=40, b=30),
                    )
                    _apply_plot_theme(fig_trend)
                    st.session_state[_trend_k] = fig_trend
                st.plotly_chart(st.session_state[_trend_k], width="stretch")

            # ── Drill-down: OTs individuales de un técnico ────────────────────────
            st.divider()
            st.markdown('<div class="section-header">🔍 Detalle de OTs por técnico</div>',
                        unsafe_allow_html=True)
            st.caption("Selecciona un técnico para ver el historial completo de sus OTs del mes.")

            if not df_tec_scores.empty:
                tec_drill = st.selectbox(
                    "Técnico",
                    df_tec_scores["tecnico"].tolist(),
                    key="kpi_drill_tec",
                )
                df_drill = df_ot_scores[df_ot_scores["tecnico"] == tec_drill].copy()
                df_drill = df_drill.sort_values("score_total", ascending=True)

                if not df_drill.empty:
                    avg_drill = df_drill["score_total"].mean()
                    color_d, lbl_d = _score_level(avg_drill)
                    st.markdown(
                        f'<div style="background:{color_d}18;border-left:4px solid {color_d};'
                        f'border-radius:8px;padding:10px 14px;margin-bottom:8px;">'
                        f'<b style="color:{color_d}">{tec_drill}</b> — '
                        f'Score promedio: <b>{avg_drill:.1f}/100</b> &nbsp; {lbl_d} &nbsp; | &nbsp; '
                        f'<b>{len(df_drill)}</b> OTs en {_mes_lbl_prec}</div>',
                        unsafe_allow_html=True,
                    )

                    # ── Construir columnas de display una por una (sin riesgo de desajuste) ──

                    drill_disp = df_drill.copy()

                    # Scores por componente (con guard para cache viejo)
                    _s = drill_disp["score_tiempo"]  if "score_tiempo"    in drill_disp.columns else pd.Series(0, index=drill_disp.index)
                    _c = drill_disp["score_causa"]   if "score_causa"     in drill_disp.columns else pd.Series(0, index=drill_disp.index)
                    _n = drill_disp["score_numeral"] if "score_numeral"   in drill_disp.columns else pd.Series(0, index=drill_disp.index)
                    _d = drill_disp["score_deteccion"] if "score_deteccion" in drill_disp.columns else pd.Series(0, index=drill_disp.index)
                    _ok_bits = ((_s >= 25).astype(int) + (_c >= 25).astype(int) +
                                (_n >= 25).astype(int) + (_d >= 25).astype(int))

                    # Columna 1: Cumple
                    drill_disp["_cumple"] = _ok_bits.apply(lambda n: "✅ Cumple" if n == 4 else "❌ Con error")

                    # Columna 2: X/4
                    drill_disp["_x4"] = _ok_bits.apply(
                        lambda n: f"{n}/4 {'✅' if n == 4 else ('⚠️' if n >= 2 else '❌')}")

                    # Columna 3: Tipo (title case)
                    drill_disp["_tipo"] = drill_disp["maint_type"].fillna("").str.title()

                    # Columna 4: ⏱ Tiempo — valor en minutos + ✅/❌
                    _em = drill_disp["elapsed_min"] if "elapsed_min" in drill_disp.columns else pd.Series(0.0, index=drill_disp.index)
                    drill_disp["_col_tiempo"] = drill_disp.apply(
                        lambda r: f"{'✅' if _s[r.name] >= 25 else '❌'} {_em[r.name]:.0f} min",
                        axis=1)

                    # Columna 5: 🔍 Causa raíz — texto + ✅/❌
                    def _fmt_causa(r):
                        es_corr = r.get("es_correctiva", True)
                        raw = str(r.get("causa_raiz_raw","") or "").strip()
                        ok  = _c[r.name] >= 25
                        if not es_corr:
                            return "✅ PM (no aplica)"
                        if ok:
                            return f"✅ {raw[:38]}" if raw else "✅ Registrada"
                        return f"❌ {raw[:35]}" if raw else "❌ Sin causa"
                    drill_disp["_col_causa"] = drill_disp.apply(_fmt_causa, axis=1)

                    # Columna 6: 🔢 Numeral — ✅/❌ + indicación
                    drill_disp["_col_numeral"] = drill_disp.apply(
                        lambda r: "✅ Registrado" if r.get("numeral_ok", False) else "❌ Sin numeral",
                        axis=1)

                    # Columna 7: 🎯 Modalidad — valor registrado + ✅/❌
                    def _fmt_modal(r):
                        raw = str(r.get("deteccion_raw","") or "").strip()
                        ok  = _d[r.name] >= 25
                        if ok and raw:
                            return f"✅ {raw[:38]}"
                        return "❌ Sin clasificar"
                    if "deteccion_raw" in drill_disp.columns:
                        drill_disp["_col_modal"] = drill_disp.apply(_fmt_modal, axis=1)
                    else:
                        drill_disp["_col_modal"] = "❌ Sin clasificar"

                    # Columna 8: 💬 Observación — descripción de qué falló
                    _nombres_comp = {0: "Tiempo", 1: "Causa raíz", 2: "Numeral", 3: "Modalidad"}
                    _scores_comp  = [_s, _c, _n, _d]
                    def _obs(r):
                        fallos = [_nombres_comp[i] for i, sc in enumerate(_scores_comp)
                                  if sc[r.name] < 25]
                        if not fallos:
                            return "✅ Registro perfecto"
                        return "⚠️ No cumple: " + ", ".join(fallos)
                    drill_disp["_obs"] = drill_disp.apply(_obs, axis=1)

                    # Columna 9: Fecha cierre
                    drill_disp["_fecha"] = pd.to_datetime(
                        drill_disp["final_date"], errors="coerce"
                    ).dt.strftime("%d/%m/%Y")

                    # Selección ordenada y limpia — sin riesgo de mezclar columnas
                    drill_disp = drill_disp[[
                        "_cumple", "_x4", "folio", "station", "_tipo",
                        "score_total",
                        "_col_tiempo", "_col_causa", "_col_numeral", "_col_modal",
                        "_obs", "_fecha",
                    ]].copy()

                    drill_disp.columns = [
                        "Cumple", "X/4", "OT", "Estación", "Tipo",
                        "Score",
                        "⏱ Tiempo", "🔍 Causa raíz", "🔢 Numeral", "🎯 Modalidad",
                        "💬 Observación", "Fecha",
                    ]

                    # Alertas rápidas (sin quick_tick_label)
                    _qt_criticas  = int((_s < 1).sum())
                    _qt_rapidas   = int(((_s >= 1) & (_s < 25)).sum())
                    _sin_causa    = int((_c == 0).sum())
                    _sin_modal    = int((_d == 0).sum())

                    if _qt_criticas or _qt_rapidas:
                        st.markdown(
                            f'<div style="background:{_t["err_bg"]};border-left:4px solid #ef4444;'
                            f'border-radius:6px;padding:8px 14px;margin-bottom:8px;font-size:0.85rem;color:{_t["text"]};">'
                            f'⚠️ Tiempo insuficiente: <b>{_qt_criticas} OTs</b> con tiempo mínimo · '
                            f'<b>{_qt_rapidas} OTs</b> con tiempo parcial</div>',
                            unsafe_allow_html=True,
                        )
                    if _sin_causa:
                        st.markdown(
                            f'<div style="background:{_t["orange_bg"]};border-left:4px solid #01798A;'
                            f'border-radius:6px;padding:8px 14px;margin-bottom:8px;font-size:0.85rem;color:{_t["text"]};">'
                            f'📋 <b>{_sin_causa} correctiva(s) sin causa raíz</b> registrada</div>',
                            unsafe_allow_html=True,
                        )
                    if _sin_modal:
                        st.markdown(
                            f'<div style="background:{_t["warn_bg"]};border-left:4px solid #a855f7;'
                            f'border-radius:6px;padding:8px 14px;margin-bottom:8px;font-size:0.85rem;color:{_t["text"]};">'
                            f'🎯 <b>{_sin_modal} OT(s) sin modalidad de atención</b> — campo dejado en "Sin clasificar"</div>',
                            unsafe_allow_html=True,
                        )

                    _show_df(
                        drill_disp,
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "Cumple":         st.column_config.TextColumn(width=100,
                                help="✅ = 4/4 componentes OK · ❌ = ≥1 falló"),
                            "X/4":            st.column_config.TextColumn(width=80,
                                help="Componentes correctos de los 4 posibles"),
                            "OT":             st.column_config.TextColumn(width=110),
                            "Estación":       st.column_config.TextColumn(width=200),
                            "Tipo":           st.column_config.TextColumn(width=130),
                            "Score":          st.column_config.ProgressColumn(
                                min_value=0, max_value=100, format="%.1f"),
                            "⏱ Tiempo":       st.column_config.TextColumn(width=110,
                                help="Minutos con Fracttal abierto · ✅ cumple el umbral"),
                            "🔍 Causa raíz":  st.column_config.TextColumn(width=240,
                                help="Causa registrada por el técnico · PM no requiere causa"),
                            "🔢 Numeral":     st.column_config.TextColumn(width=130,
                                help="Si registró o no un número de ficha ≥4 dígitos en la nota"),
                            "🎯 Modalidad":   st.column_config.TextColumn(width=230,
                                help="Modalidad de atención · ❌ = dejó SIN CLASIFICAR"),
                            "💬 Observación": st.column_config.TextColumn(width=260,
                                help="Resumen de cumplimiento de la OT"),
                            "Fecha":          st.column_config.TextColumn(width=90),
                        },
                    )


    # ══════════════════════════════════════════════════════════════════════════
    # TAB 4 — RESUMEN BONO
    # ══════════════════════════════════════════════════════════════════════════
    with tab_bono:
        _bi_info, _bi_escala = st.columns([3, 2])
        with _bi_info:
            st.markdown(
                f'<div style="background:{_t["info_bg"]};border-left:4px solid #f59e0b;'
                f'border-radius:8px;padding:12px 16px;margin-bottom:14px;color:{_t["text"]};">'
                f'<b>🏆 Resumen de Bono Trimestral</b> — Estimación consolidada por técnico y equipo.<br>'
                f'<span style="font-size:0.85rem;">Pool equipo: <b>$500.000 CLP/trimestre</b> · '
                f'Dividido entre todos los integrantes (seniors incluidos) → monto por persona varía según tamaño del equipo.<br>'
                f'50% por KPIs individuales · 50% por KPIs del equipo · '
                f'Ponderación: SLA <b>40%</b> · Efectividad MP <b>30%</b> · Precisión Fracttal <b>30%</b></span>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with _bi_escala:
            st.markdown(
                f'<div style="background:{_t["card"]};border:1px solid {_t["border"]};'
                f'border-radius:8px;padding:12px 14px;font-size:0.78rem;color:{_t["text"]};'
                f'line-height:1.7;margin-bottom:14px;">'
                f'<div style="font-weight:700;font-size:0.82rem;margin-bottom:6px;'
                f'color:{_t["muted"]};letter-spacing:0.04em;">📊 ESCALA DE BONOS</div>'
                f'<div style="margin-bottom:5px;">'
                f'<span style="font-weight:600;">SLA</span> '
                f'<span style="color:{_t["muted"]};">40% · $200.000/trim (pool)</span><br>'
                f'<span style="color:#22c55e;">≥95%→100%</span> &nbsp;'
                f'<span style="color:#16a34a;">93→90%</span> &nbsp;'
                f'<span style="color:#4ade80;">90→80%</span> &nbsp;'
                f'<span style="color:#f59e0b;">85→50%</span> &nbsp;'
                f'<span style="color:#ef4444;">&lt;85%→0%</span>'
                f'</div>'
                f'<div style="margin-bottom:5px;">'
                f'<span style="font-weight:600;">Calidad MP</span> '
                f'<span style="color:{_t["muted"]};">30% · $150.000/trim (pool)</span><br>'
                f'<span style="color:#22c55e;">≥98%→100%</span> &nbsp;'
                f'<span style="color:#16a34a;">96→90%</span> &nbsp;'
                f'<span style="color:#4ade80;">94→80%</span> &nbsp;'
                f'<span style="color:#65a30d;">92→70%</span> &nbsp;'
                f'<span style="color:#f59e0b;">90→60%</span> &nbsp;'
                f'<span style="color:#ef4444;">&lt;90%→0%</span>'
                f'</div>'
                f'<div>'
                f'<span style="font-weight:600;">Precisión</span> '
                f'<span style="color:{_t["muted"]};">30% · $150.000/trim (pool)</span><br>'
                f'<span style="color:#22c55e;">≥95%→100%</span> &nbsp;'
                f'<span style="color:#16a34a;">90→90%</span> &nbsp;'
                f'<span style="color:#4ade80;">85→80%</span> &nbsp;'
                f'<span style="color:#65a30d;">80→70%</span> &nbsp;'
                f'<span style="color:#f59e0b;">75→60%</span> &nbsp;'
                f'<span style="color:#f97316;">70→50%</span> &nbsp;'
                f'<span style="color:#ef4444;">&lt;70%→0%</span>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ── Período ───────────────────────────────────────────────────────────
        _TRIM_DICT_BONO = {
            "T1 · Ene–Mar": [1, 2, 3], "T2 · Abr–Jun": [4, 5, 6],
            "T3 · Jul–Sep": [7, 8, 9], "T4 · Oct–Dic": [10, 11, 12],
        }
        _current_month = 6   # Junio 2026
        _default_trim_bono = next(
            (k for k, v in _TRIM_DICT_BONO.items() if _current_month in v),
            "T2 · Abr–Jun",
        )
        _bf0, _bf1, _bf2, _bf3 = st.columns([2, 3, 2, 2])
        with _bf0:
            _trim_bono = st.selectbox(
                "Trimestre/Período",
                list(_TRIM_DICT_BONO.keys()),
                index=list(_TRIM_DICT_BONO.keys()).index(_default_trim_bono),
                key="bono_trim",
            )
        _trim_months_bono = _TRIM_DICT_BONO[_trim_bono]

        # Todos los meses disponibles en los datos para el filtro de drill-down
        _all_meses_bono = sorted(
            set(
                list(
                    pd.period_range(
                        f"2026-{_trim_months_bono[0]:02d}",
                        f"2026-{_trim_months_bono[-1]:02d}",
                        freq="M",
                    ).astype(str)
                )
            )
        )
        with _bf1:
            _meses_bono_sel = st.multiselect(
                "Mes específico (dejar vacío = todo el trimestre)",
                options=_all_meses_bono,
                default=[],
                key="bono_mes_drill",
            )
        _meses_bono_activos = _meses_bono_sel if _meses_bono_sel else _all_meses_bono

        # ── Filtros equipo / técnico ──────────────────────────────────────────
        _eq_opts_bono = ["Todos"] + [_EQUIPO_LABEL.get(k, k) for k in GRUPOS_TERRENO]
        with _bf2:
            _bono_eq_sel = st.selectbox("Equipo", _eq_opts_bono, key="bono_equipo")
        # Obtener clave del equipo seleccionado
        _bono_eq_key = next(
            (k for k in GRUPOS_TERRENO if _EQUIPO_LABEL.get(k, k) == _bono_eq_sel),
            None,
        ) if _bono_eq_sel != "Todos" else None
        # Opciones de técnico: miembros del equipo seleccionado (o todos si equipo = Todos)
        if _bono_eq_key:
            _tec_opts_bono = ["Todos"] + [
                m for m in GRUPOS_TERRENO[_bono_eq_key].get("miembros", [])
                if not _es_excluido(TECH_NAME_MAP.get(m, m))
            ]
        else:
            _tec_opts_bono = ["Todos"]
        with _bf3:
            _bono_tec_sel = st.selectbox(
                "Técnico",
                _tec_opts_bono,
                key="bono_tecnico",
                disabled=(_bono_eq_sel == "Todos"),
            )

        # ── Cargar datos de SLA ───────────────────────────────────────────────
        _sla_key_bono = f"_sla_proc_v5_{len(df_llamados)}"
        _df_sla_bono = st.session_state.get(_sla_key_bono, pd.DataFrame())
        if not _df_sla_bono.empty and "mes" in _df_sla_bono.columns:
            _df_sla_bono = _df_sla_bono[
                _df_sla_bono["mes"].astype(str).isin(_meses_bono_activos)
            ].copy()
        # Columna normalizada para matching robusto (igual que MP) — evita fallos por tildes
        if not _df_sla_bono.empty and "_tech_norm" not in _df_sla_bono.columns:
            _df_sla_bono["_tech_norm"] = _df_sla_bono["tecnico"].fillna("").apply(
                lambda s: " ".join(_norm_n(s).split())
            )

        # ── Cargar datos de Precisión ─────────────────────────────────────────
        def _build_ot_all_bono():
            _raw = _cached_build_kpi_llenado(raw_wo)
            if _raw.empty:
                return pd.DataFrame()
            _df = score_llenado_por_ot(_raw)
            if not _df.empty:
                _df["equipo"] = _df["tecnico"].apply(_get_equipo)
                _df["_tech_norm"] = _df["tecnico"].fillna("").apply(
                    lambda s: " ".join(_norm_n(s).split())
                )
                if "creation_date" in _df.columns:
                    _cd = (
                        _df["creation_date"].dt.tz_convert(None)
                        if _df["creation_date"].dt.tz is not None
                        else _df["creation_date"]
                    )
                    _df["mes"] = _cd.dt.to_period("M").astype(str)
            return _df

        # Clave DISTINTA a "df_ot_all_scores" (usada por tab Precisión) para evitar
        # que el builder del KPI screen sobrescriba estos datos sin _tech_norm.
        _df_ot_bono = _sc("df_ot_bono_scores", _wo_sig, _build_ot_all_bono)
        if not _df_ot_bono.empty and "mes" in _df_ot_bono.columns:
            _df_ot_bono_filt = _df_ot_bono[
                _df_ot_bono["mes"].astype(str).isin(_meses_bono_activos)
            ].copy()
        else:
            _df_ot_bono_filt = pd.DataFrame()

        # ── Cargar datos de MP (reincidencias) ────────────────────────────────
        _df_reinc_bono = st.session_state.get("df_reinc", pd.DataFrame()).copy()
        if not _df_reinc_bono.empty and "falla_tipo" in _df_reinc_bono.columns:
            _df_reinc_bono = _df_reinc_bono[
                ~_df_reinc_bono["falla_tipo"].isin(["fnao", "especial"])
            ].copy()
        # Filtro de período
        if not _df_reinc_bono.empty and "fecha_cm" in _df_reinc_bono.columns:
            _reinc_fecha = pd.to_datetime(_df_reinc_bono["fecha_cm"], errors="coerce")
            _df_reinc_bono = _df_reinc_bono[
                _reinc_fecha.dt.month.isin(_trim_months_bono)
            ].copy()
            if _meses_bono_sel:
                if "mes" not in _df_reinc_bono.columns:
                    _df_reinc_bono["mes"] = (
                        pd.to_datetime(_df_reinc_bono["fecha_cm"], errors="coerce")
                        .dt.to_period("M").astype(str)
                    )
                _df_reinc_bono = _df_reinc_bono[
                    _df_reinc_bono["mes"].astype(str).isin(_meses_bono_activos)
                ].copy()

        # PMs del período para denominador
        _df_pm_bono = df_wo[
            (df_wo["maint_type"] == "Preventiva") &
            (~df_wo["technician"].apply(_es_excluido)) &
            (df_wo["equipo"] != "Sin equipo")
        ].copy()
        if not _df_pm_bono.empty:
            _pm_dates_bono = (
                _df_pm_bono["creation_date"].dt.tz_convert(None)
                if _df_pm_bono["creation_date"].dt.tz is not None
                else _df_pm_bono["creation_date"]
            )
            _df_pm_bono = _df_pm_bono[_pm_dates_bono.dt.month.isin(_trim_months_bono)].copy()
            if _meses_bono_sel:
                _pm_mes = (
                    _df_pm_bono["creation_date"].dt.tz_convert(None)
                    if _df_pm_bono["creation_date"].dt.tz is not None
                    else _df_pm_bono["creation_date"]
                )
                _df_pm_bono = _df_pm_bono[
                    _pm_mes.dt.to_period("M").astype(str).isin(_meses_bono_activos)
                ].copy()
            _df_pm_bono["_tech_norm"] = _df_pm_bono["technician"].fillna("").apply(
                lambda s: " ".join(_norm_n(s).split())
            )

        # ── Función helper: calcular KPIs para un técnico específico ────────
        def _kpi_para_tecnico(tech_full: str, equipo_key: str):
            """
            Retorna dict con SLA, MP y Prec para un técnico dado.
            tech_full: nombre completo (API)
            """
            _tn = " ".join(_norm_n(tech_full).split())

            # SLA — matching normalizado (igual que MP) para tolerar diferencias de tildes
            if not _df_sla_bono.empty and "_tech_norm" in _df_sla_bono.columns:
                _sla_t = _df_sla_bono[_df_sla_bono["_tech_norm"] == _tn]
                _n_sla_total = len(_sla_t)
                _n_sla_ok = int(_sla_t["cumple_sla"].fillna(False).sum()) if "cumple_sla" in _sla_t.columns else 0
                _pct_sla = (_n_sla_ok / _n_sla_total * 100) if _n_sla_total > 0 else None
            else:
                _n_sla_total = _n_sla_ok = 0
                _pct_sla = None

            # MP
            _pm_t = _df_pm_bono[_df_pm_bono["_tech_norm"] == _tn] if not _df_pm_bono.empty else pd.DataFrame()
            _n_pm_t = _pm_t["folio"].nunique() if not _pm_t.empty and "folio" in _pm_t.columns else 0
            if not _df_reinc_bono.empty and "tecnico_resp_short" in _df_reinc_bono.columns:
                _short = next(
                    (k for k, v in TECH_NAME_MAP.items() if v == tech_full),
                    tech_full,
                )
                _fallas_t = _df_reinc_bono[
                    _df_reinc_bono["tecnico_resp_short"] == _short
                ]
                _n_fallas_t = _fallas_t["folio_cm"].nunique() if "folio_cm" in _fallas_t.columns else len(_fallas_t)
            else:
                _n_fallas_t = 0

            # Prec — misma fórmula que KPI screen: % OTs con 4/4 componentes correctos
            # (binario all-or-nothing: score_total == 100 → correcta, cualquier fallo → mala)
            # matching normalizado para tolerar diferencias de tildes (igual que MP)
            if not _df_ot_bono_filt.empty and "_tech_norm" in _df_ot_bono_filt.columns:
                _prec_t = _df_ot_bono_filt[_df_ot_bono_filt["_tech_norm"] == _tn]
                _n_ots_prec = len(_prec_t)
                if _n_ots_prec > 0 and "score_total" in _prec_t.columns:
                    _n_correctas_t = int((_prec_t["score_total"] >= 100).sum())
                    _pct_prec = _n_correctas_t / _n_ots_prec * 100
                else:
                    _pct_prec = None
                    _n_ots_prec = 0
                    _n_correctas_t = 0
            else:
                _pct_prec = None
                _n_ots_prec = 0
                _n_correctas_t = 0

            return {
                "n_sla_ok": _n_sla_ok, "n_sla_total": _n_sla_total, "pct_sla": _pct_sla,
                "n_fallas": _n_fallas_t, "n_pm": _n_pm_t,
                "pct_prec": _pct_prec, "n_ots_prec": _n_ots_prec, "n_correctas_prec": _n_correctas_t,
            }

        def _kpi_para_equipo(equipo_key: str):
            """Agrega KPIs para todos los técnicos del equipo."""
            # SLA
            if not _df_sla_bono.empty and "equipo" in _df_sla_bono.columns:
                _sla_e = _df_sla_bono[_df_sla_bono["equipo"] == equipo_key]
                _n_sla_total = len(_sla_e)
                _n_sla_ok = int(_sla_e["cumple_sla"].fillna(False).sum()) if "cumple_sla" in _sla_e.columns else 0
                _pct_sla = (_n_sla_ok / _n_sla_total * 100) if _n_sla_total > 0 else None
            else:
                _n_sla_total = _n_sla_ok = 0
                _pct_sla = None

            # MP
            _pm_e = _df_pm_bono[_df_pm_bono["equipo"] == equipo_key] if not _df_pm_bono.empty else pd.DataFrame()
            _n_pm_e = _pm_e["folio"].nunique() if not _pm_e.empty and "folio" in _pm_e.columns else 0
            if not _df_reinc_bono.empty and "grupo_responsable" in _df_reinc_bono.columns:
                _fallas_e = _df_reinc_bono[_df_reinc_bono["grupo_responsable"] == equipo_key]
                _n_fallas_e = _fallas_e["folio_cm"].nunique() if "folio_cm" in _fallas_e.columns else len(_fallas_e)
            else:
                _n_fallas_e = 0

            # Prec — misma fórmula que KPI screen: % OTs con 4/4 componentes correctos
            if not _df_ot_bono_filt.empty and "equipo" in _df_ot_bono_filt.columns:
                _prec_e = _df_ot_bono_filt[_df_ot_bono_filt["equipo"] == equipo_key]
                _n_ots_e = len(_prec_e)
                if _n_ots_e > 0 and "score_total" in _prec_e.columns:
                    _n_correctas_e = int((_prec_e["score_total"] >= 100).sum())
                    _pct_prec = _n_correctas_e / _n_ots_e * 100
                else:
                    _pct_prec = None
                    _n_ots_e = 0
                    _n_correctas_e = 0
            else:
                _pct_prec = None
                _n_ots_e = 0
                _n_correctas_e = 0

            return {
                "n_sla_ok": _n_sla_ok, "n_sla_total": _n_sla_total, "pct_sla": _pct_sla,
                "n_fallas": _n_fallas_e, "n_pm": _n_pm_e,
                "pct_prec": _pct_prec, "n_ots_prec": _n_ots_e, "n_correctas_prec": _n_correctas_e,
            }

        # ── Helpers de formato HTML para celdas ──────────────────────────────
        def _cel_sla(n_ok, n_tot, pct):
            if pct is None or n_tot == 0:
                return f'<span style="color:{_t["muted"]};font-size:0.78rem;">Sin datos</span>'
            nivel, lbl, col, _ = _bono_sla(pct)
            return (
                f'<span style="font-size:0.82rem;">{n_ok}/{n_tot} = {pct:.1f}%</span><br>'
                f'<span style="color:{col};font-weight:700;font-size:0.80rem;">→ {nivel}%</span>'
            )

        def _cel_mp(n_fallas, n_pm):
            if n_pm == 0:
                return f'<span style="color:{_t["muted"]};font-size:0.78rem;">Sin PMs</span>'
            exactitud = (1 - n_fallas / n_pm) * 100
            nivel, _, col, _ = _bono_calidad(n_fallas, n_pm)
            return (
                f'<span style="font-size:0.82rem;">{n_pm - n_fallas}/{n_pm} = {exactitud:.1f}%</span><br>'
                f'<span style="color:{col};font-weight:700;font-size:0.80rem;">→ {nivel}%</span>'
            )

        def _cel_prec(pct, n_correctas=None, n_ots=None):
            if pct is None:
                return f'<span style="color:{_t["muted"]};font-size:0.78rem;">Sin datos</span>'
            nivel, _, col, _ = _bono_prec(pct)
            if n_correctas is not None and n_ots is not None and n_ots > 0:
                lbl = f'{n_correctas}/{n_ots} = {pct:.1f}%'
            else:
                lbl = f'{pct:.1f}%'
            return (
                f'<span style="font-size:0.82rem;">{lbl}</span><br>'
                f'<span style="color:{col};font-weight:700;font-size:0.80rem;">→ {nivel}%</span>'
            )

        def _cel_pond(pond_pct, col):
            return (
                f'<span style="color:{col};font-weight:800;font-size:1.0rem;">{pond_pct:.1f}%</span>'
            )

        def _clp_fmt(v):
            return f'${v:,.0f}'.replace(',', '.')

        def _nivel_color(nivel_int):
            if nivel_int >= 90:
                return "#22c55e"
            if nivel_int >= 70:
                return "#4ade80"
            if nivel_int >= 50:
                return "#f59e0b"
            if nivel_int > 0:
                return "#f97316"
            return "#ef4444"

        # ── Mostrar tabla por equipo ──────────────────────────────────────────
        _no_data_global = (
            _df_sla_bono.empty and _df_ot_bono_filt.empty and _df_reinc_bono.empty
        )
        if _no_data_global and _df_pm_bono.empty:
            st.info(
                "Los datos del período aún no están disponibles. "
                "Visita las pestañas SLA, Efectividad MP y Precisión para que se carguen, "
                "luego vuelve aquí."
            )
        else:
            for _grp_key, _grp_info in GRUPOS_TERRENO.items():
                # ── Filtro equipo ─────────────────────────────────────────────
                if _bono_eq_key and _grp_key != _bono_eq_key:
                    continue

                _senior = _grp_info.get("senior", _grp_key)
                _miembros_short = _grp_info.get("miembros", [])
                # Convertir short names → full names
                _miembros_full = [TECH_NAME_MAP.get(m, m) for m in _miembros_short]
                # Filtrar excluidos
                _miembros_full = [
                    t for t in _miembros_full if not _es_excluido(t)
                ]
                if not _miembros_full:
                    continue

                # ── Filtro técnico ────────────────────────────────────────────
                if _bono_tec_sel != "Todos":
                    _bono_tec_full = TECH_NAME_MAP.get(_bono_tec_sel, _bono_tec_sel)
                    _miembros_full = [t for t in _miembros_full if t == _bono_tec_full]
                    if not _miembros_full:
                        continue

                st.markdown(
                    f'<div style="font-size:1.05rem;font-weight:700;color:{_t["text"]};'
                    f'margin:18px 0 8px 0;border-bottom:2px solid {_t["border"]};'
                    f'padding-bottom:4px;">🔧 Equipo {_EQUIPO_LABEL.get(_grp_key, _grp_key)}'
                    f' <span style="font-size:0.82rem;color:{_t["muted"]};font-weight:400;">'
                    f'— Senior: {_senior}</span></div>',
                    unsafe_allow_html=True,
                )

                # Calcular KPIs por técnico y para el equipo
                _tec_kpis = {t: _kpi_para_tecnico(t, _grp_key) for t in _miembros_full}
                _eq_kpi = _kpi_para_equipo(_grp_key)

                # ── Bono por persona: pool / n_integrantes (seniors incluidos) ──
                # 50 % individual (KPIs propios) · 50 % equipo (KPIs agregados)
                _n_pool       = len(_miembros_full)
                _pp_max       = int(_BONO_TOTAL / _n_pool) if _n_pool > 0 else _BONO_TOTAL
                _pp_ind       = int(_pp_max * 0.50)   # parte individual
                _pp_eq        = int(_pp_max * 0.50)   # parte equipo
                _MAX_IND_SLA  = int(_pp_ind * 0.40)
                _MAX_IND_MP   = int(_pp_ind * 0.30)
                _MAX_IND_PREC = int(_pp_ind * 0.30)
                _MAX_EQ_SLA   = int(_pp_eq  * 0.40)
                _MAX_EQ_MP    = int(_pp_eq  * 0.30)
                _MAX_EQ_PREC  = int(_pp_eq  * 0.30)
                # Callcenter: monto fijo por persona por trimestre (sin medición aún)
                _BONO_CC = 100_000

                # ── Construir tabla HTML ──────────────────────────────────────
                _hdr_teal = "#01798A"
                _hdr_text = "#ffffff"
                _row_bg_alt = _t["card"]
                _row_bg     = _t.get("prog_bg", "#f3f4f6") if _t["card"] != "#ffffff" else "#f9fafb"

                # Cabecera — CSS last-child da fondo teal a columna EQUIPO
                _html = (
                    f'<style>.bono-tbl tbody tr td:last-child{{'
                    f'background:rgba(1,121,138,0.11)!important;}}</style>'
                    f'<div style="overflow-x:auto;margin-bottom:16px;">'
                    f'<table class="bono-tbl" style="width:100%;border-collapse:collapse;'
                    f'font-size:0.83rem;color:{_t["text"]};">'
                    f'<thead><tr>'
                    f'<th style="background:{_hdr_teal};color:{_hdr_text};padding:8px 10px;'
                    f'text-align:left;min-width:160px;border-radius:6px 0 0 0;">KPI</th>'
                )
                for _tf in _miembros_full:
                    _short_name = next(
                        (k for k, v in TECH_NAME_MAP.items() if v == _tf), _tf
                    )
                    _html += (
                        f'<th style="background:{_hdr_teal};color:{_hdr_text};'
                        f'padding:8px 10px;text-align:center;min-width:130px;">'
                        f'{_short_name}</th>'
                    )
                _html += (
                    f'<th style="background:{_hdr_teal};color:{_hdr_text};'
                    f'padding:8px 10px;text-align:center;min-width:130px;'
                    f'border-radius:0 6px 0 0;font-style:italic;">EQUIPO</th>'
                    f'</tr></thead><tbody>'
                )

                def _tr_bg(i):
                    return _row_bg if i % 2 == 0 else _row_bg_alt

                # Fila 1: SLA
                _html += (
                    f'<tr style="background:{_tr_bg(0)};">'
                    f'<td style="padding:8px 10px;font-weight:600;border-bottom:1px solid {_t["border"]};">'
                    f'Desempeño SLA <span style="color:{_t["muted"]};font-size:0.76rem;">(40%)</span></td>'
                )
                for _tf in _miembros_full:
                    _k = _tec_kpis[_tf]
                    _html += (
                        f'<td style="padding:8px 10px;text-align:center;'
                        f'border-bottom:1px solid {_t["border"]};">'
                        f'{_cel_sla(_k["n_sla_ok"], _k["n_sla_total"], _k["pct_sla"])}</td>'
                    )
                _html += (
                    f'<td style="padding:8px 10px;text-align:center;'
                    f'border-bottom:1px solid {_t["border"]};font-style:italic;">'
                    f'{_cel_sla(_eq_kpi["n_sla_ok"], _eq_kpi["n_sla_total"], _eq_kpi["pct_sla"])}</td>'
                    f'</tr>'
                )

                # Fila 2: MP
                _html += (
                    f'<tr style="background:{_tr_bg(1)};">'
                    f'<td style="padding:8px 10px;font-weight:600;border-bottom:1px solid {_t["border"]};">'
                    f'Efectividad MP <span style="color:{_t["muted"]};font-size:0.76rem;">(30%)</span></td>'
                )
                for _tf in _miembros_full:
                    _k = _tec_kpis[_tf]
                    _html += (
                        f'<td style="padding:8px 10px;text-align:center;'
                        f'border-bottom:1px solid {_t["border"]};">'
                        f'{_cel_mp(_k["n_fallas"], _k["n_pm"])}</td>'
                    )
                _html += (
                    f'<td style="padding:8px 10px;text-align:center;'
                    f'border-bottom:1px solid {_t["border"]};font-style:italic;">'
                    f'{_cel_mp(_eq_kpi["n_fallas"], _eq_kpi["n_pm"])}</td>'
                    f'</tr>'
                )

                # Fila 3: Precisión
                _html += (
                    f'<tr style="background:{_tr_bg(2)};">'
                    f'<td style="padding:8px 10px;font-weight:600;border-bottom:1px solid {_t["border"]};">'
                    f'Precisión Fracttal <span style="color:{_t["muted"]};font-size:0.76rem;">(30%)</span></td>'
                )
                for _tf in _miembros_full:
                    _k = _tec_kpis[_tf]
                    _html += (
                        f'<td style="padding:8px 10px;text-align:center;'
                        f'border-bottom:1px solid {_t["border"]};">'
                        f'{_cel_prec(_k["pct_prec"], _k.get("n_correctas_prec"), _k.get("n_ots_prec"))}</td>'
                    )
                _html += (
                    f'<td style="padding:8px 10px;text-align:center;'
                    f'border-bottom:1px solid {_t["border"]};font-style:italic;">'
                    f'{_cel_prec(_eq_kpi.get("pct_prec"), _eq_kpi.get("n_correctas_prec"), _eq_kpi.get("n_ots_prec"))}</td>'
                    f'</tr>'
                )

                # Fila 4: Cumplimiento ponderado (highlight)
                _html += (
                    f'<tr style="background:{_t.get("info_bg", "#eff6ff")};">'
                    f'<td style="padding:8px 10px;font-weight:700;border-bottom:1px solid {_t["border"]};">'
                    f'Cumplimiento ponderado</td>'
                )

                def _pond_pct(kpis: dict) -> tuple:
                    _p = kpis.get("pct_sla")
                    _m_pct = ((1 - kpis["n_fallas"] / kpis["n_pm"]) * 100) if kpis["n_pm"] > 0 else None
                    _r = kpis.get("pct_prec")
                    _niv_sla  = _bono_sla(_p)[0]  if _p  is not None else 0
                    _niv_mp   = _bono_calidad(kpis["n_fallas"], kpis["n_pm"])[0] if kpis["n_pm"] > 0 else 0
                    _niv_prec = _bono_prec(_r)[0] if _r is not None else 0
                    _has_data = (_p is not None) or (kpis["n_pm"] > 0) or (_r is not None)
                    if not _has_data:
                        return None, "#888"
                    _w = 0.40 * _niv_sla + 0.30 * _niv_mp + 0.30 * _niv_prec
                    return _w, _nivel_color(int(_w))

                for _tf in _miembros_full:
                    _pond, _pcol = _pond_pct(_tec_kpis[_tf])
                    if _pond is None:
                        _pcell = f'<span style="color:{_t["muted"]};font-size:0.78rem;">Sin datos</span>'
                    else:
                        _pcell = _cel_pond(_pond, _pcol)
                    _html += (
                        f'<td style="padding:8px 10px;text-align:center;'
                        f'border-bottom:1px solid {_t["border"]};">{_pcell}</td>'
                    )
                _eq_pond, _eq_pcol = _pond_pct(_eq_kpi)
                if _eq_pond is None:
                    _eq_pcell = f'<span style="color:{_t["muted"]};font-size:0.78rem;">Sin datos</span>'
                else:
                    _eq_pcell = _cel_pond(_eq_pond, _eq_pcol)
                _html += (
                    f'<td style="padding:8px 10px;text-align:center;'
                    f'border-bottom:1px solid {_t["border"]};font-style:italic;">'
                    f'{_eq_pcell}</td></tr>'
                )

                # Fila 5: Bono individual estimado
                _html += (
                    f'<tr style="background:{_tr_bg(4)};">'
                    f'<td style="padding:8px 10px;font-weight:600;border-bottom:1px solid {_t["border"]};">'
                    f'Terreno individual <span style="color:{_t["muted"]};font-size:0.76rem;">'
                    f'({_clp_fmt(_pp_ind)} × cumpl. personal)</span></td>'
                )
                for _tf in _miembros_full:
                    _k = _tec_kpis[_tf]
                    _niv_sla_i  = _bono_sla(_k["pct_sla"])[0] if _k["pct_sla"] is not None else 0
                    _niv_mp_i   = _bono_calidad(_k["n_fallas"], _k["n_pm"])[0] if _k["n_pm"] > 0 else 0
                    _niv_prec_i = _bono_prec(_k["pct_prec"])[0] if _k["pct_prec"] is not None else 0
                    _bono_ind = int(
                        _MAX_IND_SLA  * _niv_sla_i  / 100 +
                        _MAX_IND_MP   * _niv_mp_i   / 100 +
                        _MAX_IND_PREC * _niv_prec_i / 100
                    )
                    _has_any = _k["pct_sla"] is not None or _k["n_pm"] > 0 or _k["pct_prec"] is not None
                    if not _has_any:
                        _bi_cell = f'<span style="color:{_t["muted"]};font-size:0.78rem;">Sin datos</span>'
                    else:
                        _bc = _nivel_color(int(0.40 * _niv_sla_i + 0.30 * _niv_mp_i + 0.30 * _niv_prec_i))
                        _bi_cell = f'<span style="color:{_bc};font-weight:700;">{_clp_fmt(_bono_ind)}</span>'
                    _html += (
                        f'<td style="padding:8px 10px;text-align:center;'
                        f'border-bottom:1px solid {_t["border"]};">{_bi_cell}</td>'
                    )
                _html += (
                    f'<td style="padding:8px 10px;text-align:center;'
                    f'border-bottom:1px solid {_t["border"]};color:{_t["muted"]};'
                    f'font-style:italic;font-size:0.80rem;">—</td></tr>'
                )

                # Fila 6: Bono equipo estimado (igual para todos)
                _eq_niv_sla  = _bono_sla(_eq_kpi["pct_sla"])[0] if _eq_kpi["pct_sla"] is not None else 0
                _eq_niv_mp   = _bono_calidad(_eq_kpi["n_fallas"], _eq_kpi["n_pm"])[0] if _eq_kpi["n_pm"] > 0 else 0
                _eq_niv_prec = _bono_prec(_eq_kpi.get("pct_prec"))[0] if _eq_kpi.get("pct_prec") is not None else 0
                _bono_eq_est = int(
                    _MAX_EQ_SLA  * _eq_niv_sla  / 100 +
                    _MAX_EQ_MP   * _eq_niv_mp   / 100 +
                    _MAX_EQ_PREC * _eq_niv_prec / 100
                )
                _eq_has_any = _eq_kpi["pct_sla"] is not None or _eq_kpi["n_pm"] > 0 or _eq_kpi.get("pct_prec") is not None
                if not _eq_has_any:
                    _be_val_cell = f'<span style="color:{_t["muted"]};font-size:0.78rem;">Sin datos</span>'
                else:
                    _eq_bc = _nivel_color(int(0.40 * _eq_niv_sla + 0.30 * _eq_niv_mp + 0.30 * _eq_niv_prec))
                    _be_val_cell = f'<span style="color:{_eq_bc};font-weight:700;">{_clp_fmt(_bono_eq_est)}</span>'

                _eq_pond_lbl = f'{_eq_pond:.0f}% eq.' if _eq_pond is not None else '% eq.'
                _html += (
                    f'<tr style="background:{_tr_bg(5)};">'
                    f'<td style="padding:8px 10px;font-weight:600;border-bottom:1px solid {_t["border"]};">'
                    f'Terreno colectivo <span style="color:{_t["muted"]};font-size:0.76rem;">'
                    f'({_clp_fmt(_pp_eq)} × {_eq_pond_lbl})</span></td>'
                )
                for _tf in _miembros_full:
                    _html += (
                        f'<td style="padding:8px 10px;text-align:center;'
                        f'border-bottom:1px solid {_t["border"]};">{_be_val_cell}</td>'
                    )
                _html += (
                    f'<td style="padding:8px 10px;text-align:center;'
                    f'border-bottom:1px solid {_t["border"]};font-style:italic;">'
                    f'{_be_val_cell}</td></tr>'
                )

                # Fila 6.5: Callcenter (fijo por trimestre)
                _cc_cell = f'<span style="font-weight:700;">{_clp_fmt(_BONO_CC)}</span>'
                _html += (
                    f'<tr style="background:{_tr_bg(6)};">'
                    f'<td style="padding:8px 10px;font-weight:600;border-bottom:1px solid {_t["border"]};">'
                    f'Callcenter <span style="color:{_t["muted"]};font-size:0.76rem;">'
                    f'(fijo trimestral)</span></td>'
                )
                for _tf in _miembros_full:
                    _html += (
                        f'<td style="padding:8px 10px;text-align:center;'
                        f'border-bottom:1px solid {_t["border"]};">{_cc_cell}</td>'
                    )
                _html += (
                    f'<td style="padding:8px 10px;text-align:center;'
                    f'border-bottom:1px solid {_t["border"]};font-style:italic;">'
                    f'{_cc_cell}</td></tr>'
                )

                # Fila 7: TOTAL trimestral (individual + equipo + callcenter)
                _totales_trim = {}
                _html += (
                    f'<tr style="background:{_t.get("info_bg", "#eff6ff")};">'
                    f'<td style="padding:9px 10px;font-weight:800;font-size:0.90rem;'
                    f'border-top:2px solid {_t["border"]};">TOTAL trimestral</td>'
                )
                for _tf in _miembros_full:
                    _k = _tec_kpis[_tf]
                    _niv_sla_i  = _bono_sla(_k["pct_sla"])[0] if _k["pct_sla"] is not None else 0
                    _niv_mp_i   = _bono_calidad(_k["n_fallas"], _k["n_pm"])[0] if _k["n_pm"] > 0 else 0
                    _niv_prec_i = _bono_prec(_k["pct_prec"])[0] if _k["pct_prec"] is not None else 0
                    _bono_ind_t = int(
                        _MAX_IND_SLA  * _niv_sla_i  / 100 +
                        _MAX_IND_MP   * _niv_mp_i   / 100 +
                        _MAX_IND_PREC * _niv_prec_i / 100
                    )
                    _total_t = _bono_ind_t + _bono_eq_est + _BONO_CC
                    _has_any_t = _k["pct_sla"] is not None or _k["n_pm"] > 0 or _k["pct_prec"] is not None
                    _totales_trim[_tf] = (_total_t, _has_any_t)
                    if not _has_any_t and not _eq_has_any:
                        _tot_cell = f'<span style="color:{_t["muted"]};font-size:0.78rem;">Sin datos</span>'
                    else:
                        _pond_tot = (
                            0.40 * _niv_sla_i + 0.30 * _niv_mp_i + 0.30 * _niv_prec_i
                            if _has_any_t else
                            0.40 * _eq_niv_sla + 0.30 * _eq_niv_mp + 0.30 * _eq_niv_prec
                        )
                        _tc = _nivel_color(int(_pond_tot))
                        _tot_cell = (
                            f'<span style="color:{_tc};font-weight:800;font-size:1.0rem;">'
                            f'{_clp_fmt(_total_t)}</span>'
                        )
                    _html += (
                        f'<td style="padding:9px 10px;text-align:center;'
                        f'border-top:2px solid {_t["border"]};">{_tot_cell}</td>'
                    )
                _html += (
                    f'<td style="padding:9px 10px;text-align:center;'
                    f'border-top:2px solid {_t["border"]};color:{_t["muted"]};'
                    f'font-style:italic;font-size:0.80rem;">—</td></tr>'
                )

                # Fila 8: Promedio mensual (trimestral ÷ 3)
                _html += (
                    f'<tr style="background:{_t.get("info_bg", "#eff6ff")};">'
                    f'<td style="padding:8px 10px;font-weight:700;font-size:0.87rem;">'
                    f'Promedio mensual <span style="color:{_t["muted"]};font-size:0.76rem;">(÷ 3)</span></td>'
                )
                for _tf in _miembros_full:
                    _tot_v, _has_v = _totales_trim[_tf]
                    if not _has_v and not _eq_has_any:
                        _mens_cell = f'<span style="color:{_t["muted"]};font-size:0.78rem;">—</span>'
                    else:
                        _mens_cell = (
                            f'<span style="font-weight:700;font-size:0.92rem;">'
                            f'{_clp_fmt(_tot_v // 3)}</span>'
                        )
                    _html += (
                        f'<td style="padding:8px 10px;text-align:center;">{_mens_cell}</td>'
                    )
                _html += (
                    f'<td style="padding:8px 10px;text-align:center;'
                    f'color:{_t["muted"]};font-style:italic;font-size:0.80rem;">—</td></tr>'
                )

                _html += '</tbody></table></div>'
                st.markdown(_html, unsafe_allow_html=True)

            # ── Disclaimer ────────────────────────────────────────────────────
            st.caption(
                "Estimación basada en los datos del período seleccionado. "
                "El bono equipo aplica igual para todos los miembros del equipo según el "
                "desempeño agregado. Los valores son orientativos y pueden diferir del "
                "cálculo oficial."
            )

            # ── Advertencia si faltan datos ───────────────────────────────────
            _missing = []
            if _df_sla_bono.empty:
                _missing.append("SLA (visita la pestaña 'Desempeño SLA')")
            if _df_reinc_bono.empty and _df_pm_bono.empty:
                _missing.append("Efectividad MP (visita la pestaña 'Efectividad MP')")
            if _df_ot_bono_filt.empty:
                _missing.append("Precisión Fracttal (visita la pestaña 'Precisión Fracttal')")
            if _missing:
                st.info(
                    "Datos no disponibles para: **" + "**, **".join(_missing) + "**. "
                    "Visita las pestañas correspondientes para que se carguen los datos."
                )

# ─────────────────────────────────────────────────────────────────────────────
# PÁGINA 5: MANTENCIONES PREVENTIVAS
# ─────────────────────────────────────────────────────────────────────────────
elif _page == _NAV_PAGES[2]:
    _hdr(_PAGE_TITLE[_NAV_PAGES[2]], "Órdenes de mantenimiento preventivo — Fracttal One · 2026")
    st.divider()

    # ── Carga ─────────────────────────────────────────────────────────────
    with st.spinner("Cargando mantenciones preventivas…"):
        _raw_prev = load_preventivas_supabase()
    if not _raw_prev:
        st.warning("Sin datos de mantenciones preventivas. Verifica la sincronización con Fracttal.")
        st.stop()

    df_prev = pd.DataFrame(_raw_prev)
    df_prev["fecha_creacion"]    = pd.to_datetime(df_prev["fecha_creacion"],    errors="coerce", utc=True)
    df_prev["fecha_finalizacion"]= pd.to_datetime(df_prev["fecha_finalizacion"], errors="coerce", utc=True)
    _fc_prev  = df_prev["fecha_creacion"].dt.tz_convert(None)
    df_prev["_mes"]   = _fc_prev.dt.to_period("M").astype(str)
    df_prev["_month"] = _fc_prev.dt.month.astype("Int64")

    # ── Normalizar estado_tarea (Fracttal devuelve valores en inglés) ─────────
    _ESTADO_TAREA_MAP = {"DONE": "Finalizada", "NO_STARTED": "No Iniciada"}
    df_prev["estado_tarea"] = df_prev["estado_tarea"].replace(_ESTADO_TAREA_MAP)

    # ── Formatear duración (segundos → "HH:MM") ───────────────────────────────
    def _seg_a_hhmm(seg) -> str:
        try:
            s = int(seg)
            if s <= 0: return "—"
            h, m = divmod(s, 3600)
            return f"{h:02d}:{m // 60:02d}"
        except Exception:
            return "—"

    df_prev["dur_est_fmt"]  = df_prev["duracion_estim_seg"].apply(_seg_a_hhmm) \
        if "duracion_estim_seg" in df_prev.columns else "—"
    df_prev["dur_real_fmt"] = df_prev["duracion_real_seg"].apply(_seg_a_hhmm) \
        if "duracion_real_seg" in df_prev.columns else "—"

    # ── Fecha de inicio (local, sin tz) ──────────────────────────────────────
    if "fecha_inicio" in df_prev.columns:
        df_prev["fecha_inicio_fmt"] = (
            pd.to_datetime(df_prev["fecha_inicio"], errors="coerce", utc=True)
            .dt.tz_convert(None)
            .dt.strftime("%d/%m/%Y %H:%M")
        )

    # ── Rango fijo: Ene 2026 → hoy ────────────────────────────────────────
    _rango_inicio = pd.Timestamp("2026-01-01")
    _rango_fin    = pd.Timestamp.today().normalize()
    df_prev = df_prev[(_fc_prev >= _rango_inicio) & (_fc_prev <= _rango_fin)].copy()

    # ── Filtros fila 1 ────────────────────────────────────────────────────
    _pTRIM = {"T1 (Ene–Mar)":[1,2,3],"T2 (Abr–Jun)":[4,5,6],
              "T3 (Jul–Sep)":[7,8,9],"T4 (Oct–Dic)":[10,11,12]}
    _pf1, _pf2, _pf3, _pf4, _pf5 = st.columns([1.1, 1.3, 1.5, 1.5, 1.5])
    with _pf1:
        sel_ptrim = st.selectbox("Trimestre", ["Todos"] + list(_pTRIM.keys()), key="prev_trim")
    with _pf2:
        import calendar as _cal
        _pmeses_raw = sorted(df_prev["_mes"].dropna().unique(), reverse=True)
        def _pym_lbl(ym):
            try: y,m = ym.split("-"); return f"{_cal.month_abbr[int(m)]} {y}"
            except: return ym
        _pmes_map = {_pym_lbl(m): m for m in _pmeses_raw}
        sel_pmes = st.multiselect("Mes", list(_pmes_map.keys()), key="prev_mes",
                                  placeholder="Todos los meses")
    with _pf3:
        _ptipo_opts = ["Todos"] + sorted(df_prev["tipo_tarea"].dropna().unique().tolist())
        sel_ptipo = st.selectbox("Tipo de tarea", _ptipo_opts, key="prev_tipo")
    with _pf4:
        _pactiv_opts = ["Todos"] + sorted(df_prev["activador"].dropna().unique().tolist())
        sel_pactiv = st.selectbox("Activador", _pactiv_opts, key="prev_activ")
    with _pf5:
        _presp_opts = ["Todos"] + sorted(df_prev["responsable"].dropna().unique().tolist())
        sel_presp = st.selectbox("Responsable", _presp_opts, key="prev_resp")

    # ── Filtros fila 2 ────────────────────────────────────────────────────
    _pf6, _pf7, _pf8, _pf9, _ = st.columns([1.1, 1.3, 1.5, 1.5, 1.5])
    with _pf6:
        sel_pestado = st.selectbox("Estado",
            ["Todos"] + sorted(df_prev["estado"].dropna().unique().tolist()), key="prev_estado")
    with _pf7:
        sel_pestarea = st.selectbox("Estado tarea",
            ["Todos"] + sorted(df_prev["estado_tarea"].dropna().unique().tolist()), key="prev_estarea")
    with _pf8:
        sel_pclasi = st.text_input("Clasificación 2", key="prev_clasi",
                                   placeholder="60198  ó  SH_736…")

    # ── Aplicar filtros ───────────────────────────────────────────────────
    dfp = df_prev.copy()
    if sel_ptrim != "Todos":
        dfp = dfp[dfp["_month"].isin(_pTRIM[sel_ptrim])]
    if sel_pmes:
        _ppers = [_pmes_map[l] for l in sel_pmes if l in _pmes_map]
        if _ppers: dfp = dfp[dfp["_mes"].isin(_ppers)]
    if sel_ptipo   != "Todos": dfp = dfp[dfp["tipo_tarea"]  == sel_ptipo]
    if sel_pactiv  != "Todos": dfp = dfp[dfp["activador"]   == sel_pactiv]
    if sel_presp   != "Todos": dfp = dfp[dfp["responsable"] == sel_presp]
    if sel_pestado != "Todos": dfp = dfp[dfp["estado"]      == sel_pestado]
    if sel_pestarea!= "Todos": dfp = dfp[dfp["estado_tarea"]== sel_pestarea]
    if sel_pclasi.strip():
        dfp = dfp[dfp["clasificacion_2"].str.contains(sel_pclasi.strip(), case=False, na=False)]

    # ── df sin filtros de período (usado en tabs Planificación y EDS) ─────
    # Aplica tipo/activador/responsable/estado/clasif pero NO trimestre ni mes,
    # para que esos tabs puedan mostrar el rango completo de fecha_programada.
    _dfp_full = df_prev.copy()
    if sel_ptipo   != "Todos": _dfp_full = _dfp_full[_dfp_full["tipo_tarea"]  == sel_ptipo]
    if sel_pactiv  != "Todos": _dfp_full = _dfp_full[_dfp_full["activador"]   == sel_pactiv]
    if sel_presp   != "Todos": _dfp_full = _dfp_full[_dfp_full["responsable"] == sel_presp]
    if sel_pestado != "Todos": _dfp_full = _dfp_full[_dfp_full["estado"]      == sel_pestado]
    if sel_pestarea!= "Todos": _dfp_full = _dfp_full[_dfp_full["estado_tarea"]== sel_pestarea]
    if sel_pclasi.strip():
        _dfp_full = _dfp_full[_dfp_full["clasificacion_2"].str.contains(
            sel_pclasi.strip(), case=False, na=False)]

    # ── KPIs ──────────────────────────────────────────────────────────────
    _ptot   = len(dfp)
    _fin_mask = dfp["estado_tarea"].isin(["Finalizada"]) | dfp["estado"].isin(["Finalizadas"])
    _noi_mask = dfp["estado_tarea"].isin(["No Iniciada"]) & ~dfp["estado"].isin(["Finalizadas"])
    _pfin   = int(_fin_mask.sum())
    _pproc  = int((~_fin_mask & ~_noi_mask & ~dfp["estado"].isin(["Cancelado"])).sum())
    _pnoi   = int(_noi_mask.sum())
    _ppct   = round(_pfin / _ptot * 100, 1) if _ptot > 0 else 0.0
    pk1,pk2,pk3,pk4,pk5 = st.columns(5)
    pk1.metric("Total OTs",              f"{_ptot:,}")
    pk2.metric("Finalizadas",            f"{_pfin:,}",  delta=f"{_ppct}%")
    pk3.metric("En Proceso / Revisión",  f"{_pproc:,}")
    pk4.metric("No Iniciadas",           f"{_pnoi:,}")
    pk5.metric("% Completadas",          f"{_ppct}%")
    st.divider()

    # ── Tabs ──────────────────────────────────────────────────────────────
    _ptab_plan, _ptab_lista, _ptab_tipo, _ptab_tec, _ptab_eds = st.tabs([
        "📅  Planificación", "📋  Listado", "🔧  Por Tipo", "👷  Por Técnico", "🏭  Por Activo/EDS"
    ])

    # ── Tab 1: Listado ────────────────────────────────────────────────────
    with _ptab_lista:
        if dfp.empty:
            st.info("Sin registros para el filtro seleccionado.")
        else:
            _pcols = {
                "id_ot":             "OT",
                "fecha_programada":  "F. Programada",
                "fecha_inicio_fmt":  "F. Inicio",
                "activador":         "Activador",
                "nombre_tarea":      "Tarea",
                "tipo_tarea":        "Tipo",
                "estado_tarea":      "Estado Tarea",
                "estado":            "Estado",
                "dur_est_fmt":       "Dur. Est.",
                "dur_real_fmt":      "T. Ejec.",
                "codigo_activo":     "Código",
                "nombre_activo":     "Activo",
                "clasificacion_2":   "Clasif. 2",
                "ubicacion":         "Ubicación",
                "responsable":       "Responsable",
                "fecha_creacion":    "Creación",
                "fecha_finalizacion":"Finalización",
            }
            _df_show = dfp[[c for c in _pcols if c in dfp.columns]].copy()
            _df_show.rename(columns=_pcols, inplace=True)
            for _dc in ["Creación", "Finalización", "F. Programada"]:
                if _dc in _df_show.columns:
                    _df_show[_dc] = pd.to_datetime(_df_show[_dc], errors="coerce").dt.strftime("%d/%m/%Y")
            _show_df(_df_show.reset_index(drop=True), hide_index=True, use_container_width=True,
                column_config={
                    "OT":            st.column_config.TextColumn(width=90),
                    "F. Programada": st.column_config.TextColumn(width=100),
                    "F. Inicio":     st.column_config.TextColumn(width=120),
                    "Activador":     st.column_config.TextColumn(width=100),
                    "Tarea":         st.column_config.TextColumn(width=200),
                    "Tipo":          st.column_config.TextColumn(width=200),
                    "Estado Tarea":  st.column_config.TextColumn(width=100),
                    "Estado":        st.column_config.TextColumn(width=100),
                    "Dur. Est.":     st.column_config.TextColumn(width=75),
                    "T. Ejec.":      st.column_config.TextColumn(width=75),
                    "Código":        st.column_config.TextColumn(width=80),
                    "Clasif. 2":     st.column_config.TextColumn(width=80),
                    "Responsable":   st.column_config.TextColumn(width=160),
                    "Creación":      st.column_config.TextColumn(width=90),
                    "Finalización":  st.column_config.TextColumn(width=90),
                })
            st.caption(f"{len(_df_show):,} registros mostrados")

    # ── Tab 2: Por Tipo ───────────────────────────────────────────────────
    with _ptab_tipo:
        if dfp.empty:
            st.info("Sin datos para mostrar.")
        else:
            # ── Por activador (frecuencia de mantención) ───────────────────
            st.markdown('<div class="section-header">🔁  Por frecuencia / activador</div>',
                        unsafe_allow_html=True)
            _pt_activ = dfp.groupby("activador").agg(
                Total    =("id_ot","count"),
                Finaliz  =("estado_tarea", lambda s: (s == "Finalizada").sum()),
                EnProceso=("estado_tarea", lambda s: s.isin(["En Proceso","En Revisión"]).sum()),
                NoInicia =("estado_tarea", lambda s: s.isin(["No Iniciada","En Espera"]).sum()),
            ).reset_index().sort_values("Total", ascending=False)
            _pt_activ["% Comp."] = _pt_activ.apply(
                lambda r: f"{round(r['Finaliz']/r['Total']*100,1)}%" if r["Total"]>0 else "—", axis=1)
            _pt_activ.rename(columns={
                "activador":"Activador","Finaliz":"Finalizadas",
                "EnProceso":"En Proceso","NoInicia":"No Iniciada"}, inplace=True)
            _show_df(_pt_activ.reset_index(drop=True), hide_index=True, use_container_width=True,
                column_config={
                    "Activador":   st.column_config.TextColumn(width=160),
                    "Total":       st.column_config.NumberColumn(format="%d", width=65),
                    "Finalizadas": st.column_config.NumberColumn(format="%d", width=90),
                    "En Proceso":  st.column_config.NumberColumn(format="%d", width=90),
                    "No Iniciada": st.column_config.NumberColumn(format="%d", width=90),
                    "% Comp.":     st.column_config.TextColumn(width=75),
                })
            st.divider()

            # ── Por tipo de tarea ─────────────────────────────────────────
            st.markdown('<div class="section-header">📋  Por tipo de tarea</div>',
                        unsafe_allow_html=True)
            _pt_tipo = dfp.groupby("tipo_tarea").agg(
                Total    =("id_ot","count"),
                Finaliz  =("estado_tarea", lambda s: (s == "Finalizada").sum()),
                EnProceso=("estado_tarea", lambda s: s.isin(["En Proceso","En Revisión"]).sum()),
                NoInicia =("estado_tarea", lambda s: s.isin(["No Iniciada","En Espera"]).sum()),
            ).reset_index().sort_values("Total", ascending=False)
            _pt_tipo["% Comp."] = _pt_tipo.apply(
                lambda r: f"{round(r['Finaliz']/r['Total']*100,1)}%" if r["Total"]>0 else "—", axis=1)
            _pt_tipo.rename(columns={
                "tipo_tarea":"Tipo de Tarea","Finaliz":"Finalizadas",
                "EnProceso":"En Proceso","NoInicia":"No Iniciada"}, inplace=True)
            _show_df(_pt_tipo.reset_index(drop=True), hide_index=True, use_container_width=True,
                column_config={
                    "Tipo de Tarea":st.column_config.TextColumn(width=280),
                    "Total":        st.column_config.NumberColumn(format="%d", width=65),
                    "Finalizadas":  st.column_config.NumberColumn(format="%d", width=90),
                    "En Proceso":   st.column_config.NumberColumn(format="%d", width=90),
                    "No Iniciada":  st.column_config.NumberColumn(format="%d", width=90),
                    "% Comp.":      st.column_config.TextColumn(width=75),
                })

    # ── Tab 3: Por Técnico ────────────────────────────────────────────────
    with _ptab_tec:
        if dfp.empty:
            st.info("Sin datos para mostrar.")
        else:
            st.markdown('<div class="section-header">👷  Desempeño por responsable</div>',
                        unsafe_allow_html=True)
            _pt_resp = dfp.copy()
            # duracion_estim_seg / duracion_real_seg están en segundos → convertir a minutos
            _pt_resp["_dur_est_min"]  = pd.to_numeric(_pt_resp["duracion_estim_seg"],  errors="coerce") / 60 \
                if "duracion_estim_seg"  in _pt_resp.columns else None
            _pt_resp["_dur_real_min"] = pd.to_numeric(_pt_resp["duracion_real_seg"], errors="coerce") / 60 \
                if "duracion_real_seg" in _pt_resp.columns else None
            _pt_resp_g = _pt_resp.groupby("responsable").agg(
                Total      =("id_ot","count"),
                Finaliz    =("estado_tarea", lambda s: (s == "Finalizada").sum()),
                EnProceso  =("estado_tarea", lambda s: s.isin(["En Proceso","En Revisión"]).sum()),
                AvgEst_min =("_dur_est_min",  lambda s: round(s.dropna().mean(), 1) if len(s.dropna())>0 else None),
                AvgReal_min=("_dur_real_min", lambda s: round(s.dropna().mean(), 1) if len(s.dropna())>0 else None),
            ).reset_index().sort_values("Total", ascending=False)
            def _min_to_hhmm(mins):
                if mins is None or (isinstance(mins, float) and pd.isna(mins)):
                    return "—"
                try:
                    m = int(round(float(mins)))
                    return f"{m//60:02d}:{m%60:02d}"
                except Exception:
                    return "—"
            def _calc_efic(row):
                est  = row["AvgEst_min"]
                real = row["AvgReal_min"]
                if est and real and not pd.isna(est) and not pd.isna(real) and float(real) > 0:
                    return f"{round(float(est)/float(real)*100, 1)}%"
                return "—"
            _pt_resp_g["% Comp."]    = _pt_resp_g.apply(
                lambda r: f"{round(r['Finaliz']/r['Total']*100,1)}%" if r["Total"]>0 else "—", axis=1)
            _pt_resp_g["T.Est."]     = _pt_resp_g["AvgEst_min"].apply(_min_to_hhmm)
            _pt_resp_g["T.Real."]    = _pt_resp_g["AvgReal_min"].apply(_min_to_hhmm)
            _pt_resp_g["Eficiencia"] = _pt_resp_g.apply(_calc_efic, axis=1)
            _pt_resp_g = _pt_resp_g[["responsable","Total","Finaliz","EnProceso",
                                     "% Comp.","T.Est.","T.Real.","Eficiencia"]]
            _pt_resp_g.rename(columns={
                "responsable":"Responsable","Finaliz":"Finalizadas",
                "EnProceso":"En Proceso"}, inplace=True)
            _show_df(_pt_resp_g.reset_index(drop=True), hide_index=True, use_container_width=True,
                column_config={
                    "Responsable":  st.column_config.TextColumn(width=200),
                    "Total":        st.column_config.NumberColumn(format="%d", width=60),
                    "Finalizadas":  st.column_config.NumberColumn(format="%d", width=90),
                    "En Proceso":   st.column_config.NumberColumn(format="%d", width=90),
                    "% Comp.":      st.column_config.TextColumn(width=75),
                    "T.Est.":       st.column_config.TextColumn(width=70),
                    "T.Real.":      st.column_config.TextColumn(width=70),
                    "Eficiencia":   st.column_config.TextColumn(width=90),
                })
            st.caption("Eficiencia = T.Estimado / T.Real × 100%  (>100% = más rápido que lo estimado)")
            st.divider()

            # ── OTs activas por técnico ────────────────────────────────────
            st.markdown('<div class="section-header">⚙️  OTs en curso por responsable</div>',
                        unsafe_allow_html=True)
            _pt_activas = dfp[dfp["estado_tarea"].isin(["En Proceso","En Revisión","En Espera"])].copy()
            if _pt_activas.empty:
                st.info("No hay OTs activas en el período seleccionado.")
            else:
                _pt_activas_show = _pt_activas[[c for c in [
                    "id_ot","responsable","nombre_tarea","tipo_tarea",
                    "estado_tarea","fecha_programada","activador"
                ] if c in _pt_activas.columns]].copy()
                _pt_activas_show["fecha_programada"] = pd.to_datetime(
                    _pt_activas_show["fecha_programada"], errors="coerce").dt.strftime("%d/%m/%Y")
                _pt_activas_show.rename(columns={
                    "id_ot":"OT","responsable":"Responsable","nombre_tarea":"Tarea",
                    "tipo_tarea":"Tipo","estado_tarea":"Estado","fecha_programada":"F.Prog.",
                    "activador":"Activador"
                }, inplace=True)
                _show_df(_pt_activas_show.reset_index(drop=True), hide_index=True, use_container_width=True,
                    column_config={
                        "OT":          st.column_config.TextColumn(width=90),
                        "Responsable": st.column_config.TextColumn(width=180),
                        "Tarea":       st.column_config.TextColumn(width=200),
                        "Tipo":        st.column_config.TextColumn(width=170),
                        "Estado":      st.column_config.TextColumn(width=110),
                        "F.Prog.":     st.column_config.TextColumn(width=90),
                        "Activador":   st.column_config.TextColumn(width=100),
                    })
                st.caption(f"{len(_pt_activas_show):,} OTs activas")

    # ── Tab 4: Planificación ──────────────────────────────────────────────
    with _ptab_plan:
        # Parte de _dfp_full: respeta responsable/tipo/estado pero no trimestre/mes
        _dfplan = _dfp_full.copy()
        _dfplan["_fp_dt"] = pd.to_datetime(
            _dfplan["fecha_programada"], errors="coerce", utc=True).dt.tz_convert(None)
        _dfplan = _dfplan[_dfplan["_fp_dt"].notna()].copy()

        _hoy = pd.Timestamp.today().normalize()
        _lun_actual = _hoy - pd.Timedelta(days=_hoy.weekday())
        _dom_actual = _lun_actual + pd.Timedelta(days=6)
        _lun_prox   = _lun_actual + pd.Timedelta(weeks=1)
        _dom_prox   = _lun_prox   + pd.Timedelta(days=6)

        _plan_sem_opts = {
            f"Semana actual  ({_lun_actual.strftime('%d/%m')} – {_dom_actual.strftime('%d/%m/%Y')})":
                (_lun_actual, _dom_actual),
            f"Próxima semana ({_lun_prox.strftime('%d/%m')} – {_dom_prox.strftime('%d/%m/%Y')})":
                (_lun_prox, _dom_prox),
            "Últimas 2 semanas":
                (_lun_actual - pd.Timedelta(weeks=2), _hoy),
            "Próximas 4 semanas":
                (_hoy, _hoy + pd.Timedelta(weeks=4)),
        }
        _plan_sel = st.radio("Período", list(_plan_sem_opts.keys()),
                             horizontal=True, key="prev_plan_sem")
        _plan_ini, _plan_fin = _plan_sem_opts[_plan_sel]

        _df_semana = _dfplan[
            (_dfplan["_fp_dt"] >= _plan_ini) & (_dfplan["_fp_dt"] <= _plan_fin)
        ].sort_values("_fp_dt")

        st.markdown('<div class="section-header">📅  OTs programadas en el período</div>',
                    unsafe_allow_html=True)
        if _df_semana.empty:
            st.info("No hay OTs programadas para este período.")
        else:
            _df_semana_show = _df_semana[[c for c in [
                "fecha_programada","id_ot","responsable","nombre_tarea",
                "tipo_tarea","activador","estado_tarea","estado",
                "fecha_inicio_fmt","dur_est_fmt","dur_real_fmt","clasificacion_2"
            ] if c in _df_semana.columns]].copy()
            _df_semana_show["fecha_programada"] = pd.to_datetime(
                _df_semana_show["fecha_programada"], errors="coerce").dt.strftime("%d/%m/%Y")
            _df_semana_show.rename(columns={
                "fecha_programada":"F.Prog.","id_ot":"OT","responsable":"Responsable",
                "nombre_tarea":"Tarea","tipo_tarea":"Tipo","activador":"Activador",
                "estado_tarea":"Estado Tarea","estado":"Estado",
                "fecha_inicio_fmt":"F. Inicio",
                "dur_est_fmt":"Dur. Est.","dur_real_fmt":"T. Ejec.",
                "clasificacion_2":"Clasif."
            }, inplace=True)
            _show_df(_df_semana_show.reset_index(drop=True), hide_index=True, use_container_width=True,
                column_config={
                    "F.Prog.":      st.column_config.TextColumn(width=90),
                    "OT":           st.column_config.TextColumn(width=90),
                    "Responsable":  st.column_config.TextColumn(width=180),
                    "Tarea":        st.column_config.TextColumn(width=200),
                    "Tipo":         st.column_config.TextColumn(width=160),
                    "Activador":    st.column_config.TextColumn(width=100),
                    "Estado Tarea": st.column_config.TextColumn(width=100),
                    "Estado":       st.column_config.TextColumn(width=100),
                    "F. Inicio":    st.column_config.TextColumn(width=130),
                    "Dur. Est.":    st.column_config.TextColumn(width=75),
                    "T. Ejec.":     st.column_config.TextColumn(width=75),
                    "Clasif.":      st.column_config.TextColumn(width=80),
                })
            _sfin_m  = _df_semana["estado_tarea"].isin(["Finalizada"]) | _df_semana["estado"].isin(["Finalizadas"])
            _snoi_m  = _df_semana["estado_tarea"].isin(["No Iniciada"]) & ~_df_semana["estado"].isin(["Finalizadas"])
            _sfin    = int(_sfin_m.sum())
            _sproc   = int((~_sfin_m & ~_snoi_m & ~_df_semana["estado"].isin(["Cancelado"])).sum())
            _snoi    = int(_snoi_m.sum())
            st.caption(f"{len(_df_semana_show):,} OTs en el período — "
                       f"{_sfin} finalizadas · {_sproc} en proceso · {_snoi} no iniciadas")
        st.divider()

        # ── OTs vencidas (programadas en pasado y no finalizadas) ─────────
        st.markdown('<div class="section-header">⚠️  OTs vencidas (no finalizadas)</div>',
                    unsafe_allow_html=True)
        _df_venc = _dfplan[
            (_dfplan["_fp_dt"] < _hoy) &
            (~_dfplan["estado"].isin(["Finalizadas","Cancelado"])) &
            (~_dfplan["estado_tarea"].isin(["Finalizada"]))   # excluir DONE normalizados
        ].copy()
        _df_venc["_atraso"] = (_hoy - _df_venc["_fp_dt"]).dt.days
        _df_venc = _df_venc.sort_values("_atraso", ascending=False)
        if _df_venc.empty:
            st.success("✅ No hay OTs preventivas vencidas.")
        else:
            _df_venc_show = _df_venc[[c for c in [
                "fecha_programada","_atraso","id_ot","responsable",
                "nombre_tarea","tipo_tarea","activador","estado","estado_tarea"
            ] if c in _df_venc.columns]].copy()
            _df_venc_show["fecha_programada"] = pd.to_datetime(
                _df_venc_show["fecha_programada"], errors="coerce").dt.strftime("%d/%m/%Y")
            _df_venc_show.rename(columns={
                "fecha_programada":"F.Prog.","_atraso":"Atraso (días)","id_ot":"OT",
                "responsable":"Responsable","nombre_tarea":"Tarea","tipo_tarea":"Tipo",
                "activador":"Activador","estado":"Estado","estado_tarea":"Estado Tarea"
            }, inplace=True)
            _show_df(_df_venc_show.reset_index(drop=True), hide_index=True, use_container_width=True,
                column_config={
                    "F.Prog.":       st.column_config.TextColumn(width=90),
                    "Atraso (días)": st.column_config.NumberColumn(format="%d días", width=105),
                    "OT":            st.column_config.TextColumn(width=90),
                    "Responsable":   st.column_config.TextColumn(width=180),
                    "Tarea":         st.column_config.TextColumn(width=200),
                    "Tipo":          st.column_config.TextColumn(width=170),
                    "Activador":     st.column_config.TextColumn(width=100),
                    "Estado":        st.column_config.TextColumn(width=100),
                    "Estado Tarea":  st.column_config.TextColumn(width=110),
                })
            st.caption(f"⚠️ {len(_df_venc_show):,} OTs preventivas vencidas sin finalizar")
        st.divider()

        # ── Próximas OTs futuras ───────────────────────────────────────────
        st.markdown('<div class="section-header">🔮  Próximas OTs programadas (hoy en adelante)</div>',
                    unsafe_allow_html=True)
        _df_fut = _dfplan[
            (_dfplan["_fp_dt"] >= _hoy) &
            (~_dfplan["estado"].isin(["Finalizadas","Cancelado"])) &
            (~_dfplan["estado_tarea"].isin(["Finalizada"]))
        ].sort_values("_fp_dt").head(50)
        if _df_fut.empty:
            st.info("No hay OTs preventivas futuras registradas.")
        else:
            _df_fut_show = _df_fut[[c for c in [
                "fecha_programada","id_ot","responsable","nombre_tarea",
                "tipo_tarea","activador","clasificacion_2"
            ] if c in _df_fut.columns]].copy()
            _df_fut_show["fecha_programada"] = pd.to_datetime(
                _df_fut_show["fecha_programada"], errors="coerce").dt.strftime("%d/%m/%Y")
            _df_fut_show.rename(columns={
                "fecha_programada":"F.Prog.","id_ot":"OT","responsable":"Responsable",
                "nombre_tarea":"Tarea","tipo_tarea":"Tipo","activador":"Activador",
                "clasificacion_2":"Clasif."
            }, inplace=True)
            _show_df(_df_fut_show.reset_index(drop=True), hide_index=True, use_container_width=True,
                column_config={
                    "F.Prog.":    st.column_config.TextColumn(width=90),
                    "OT":         st.column_config.TextColumn(width=90),
                    "Responsable":st.column_config.TextColumn(width=180),
                    "Tarea":      st.column_config.TextColumn(width=200),
                    "Tipo":       st.column_config.TextColumn(width=170),
                    "Activador":  st.column_config.TextColumn(width=100),
                    "Clasif.":    st.column_config.TextColumn(width=80),
                })
            st.caption(f"Mostrando las próximas {len(_df_fut_show):,} OTs programadas")

    # ── Tab 5: Por Activo/EDS ─────────────────────────────────────────────
    with _ptab_eds:
        if _dfp_full.empty:
            st.info("Sin datos de mantenciones preventivas.")
        else:
            _dfeds = _dfp_full.copy()
            _dfeds["_fp_dt"] = pd.to_datetime(
                _dfeds["fecha_programada"],   errors="coerce", utc=True).dt.tz_convert(None)
            _dfeds["_ff_dt"] = pd.to_datetime(
                _dfeds["fecha_finalizacion"], errors="coerce", utc=True).dt.tz_convert(None)
            _hoy_eds = pd.Timestamp.today().normalize()

            # Última PM finalizada por activo (fecha_finalizacion más reciente)
            _df_ult = (
                _dfeds[_dfeds["estado"] == "Finalizadas"]
                .sort_values("_ff_dt", ascending=False)
                .groupby(["codigo_activo","nombre_activo"], as_index=False)
                .first()
                [["codigo_activo","nombre_activo","ubicacion","clasificacion_2","activador","_ff_dt"]]
                .rename(columns={"_ff_dt":"_ultima_pm"})
            )

            # Próxima PM no finalizada por activo (fecha_programada futura más próxima)
            _df_prx = (
                _dfeds[
                    (_dfeds["_fp_dt"] >= _hoy_eds) &
                    (~_dfeds["estado"].isin(["Finalizadas","Cancelado"]))
                ]
                .sort_values("_fp_dt", ascending=True)
                .groupby(["codigo_activo","nombre_activo"], as_index=False)
                .first()
                [["codigo_activo","nombre_activo","_fp_dt","responsable"]]
                .rename(columns={"_fp_dt":"_prox_pm","responsable":"resp_prox"})
            )

            # Frecuencia histórica total por activo
            _df_freq_eds = (
                _dfeds.groupby(["codigo_activo","nombre_activo"], as_index=False)
                .agg(Total_PM=("id_ot","count"))
            )

            # Merge: outer para incluir activos con sólo última o sólo próxima
            _df_eds = _df_ult.merge(_df_prx,      on=["codigo_activo","nombre_activo"], how="outer")
            _df_eds = _df_eds.merge(_df_freq_eds, on=["codigo_activo","nombre_activo"], how="left")

            # Días calculados
            _df_eds["_dias_ult"]  = (_hoy_eds - _df_eds["_ultima_pm"]).dt.days
            _df_eds["_dias_prox"] = (_df_eds["_prox_pm"] - _hoy_eds).dt.days

            # Ordenar por proximidad de próxima PM (activos sin próxima van al final)
            _df_eds = _df_eds.sort_values("_dias_prox", na_position="last")

            # Formatear
            _df_eds_show = _df_eds.copy()
            _df_eds_show["Última PM"]        = _df_eds["_ultima_pm"].dt.strftime("%d/%m/%Y").fillna("—")
            _df_eds_show["Próxima PM"]       = _df_eds["_prox_pm"].dt.strftime("%d/%m/%Y").fillna("—")
            _df_eds_show["Días desde últ."]  = _df_eds["_dias_ult"].apply(
                lambda x: int(x) if pd.notna(x) else None)
            _df_eds_show["Días hasta prox."] = _df_eds["_dias_prox"].apply(
                lambda x: int(x) if pd.notna(x) else None)

            _df_eds_show = _df_eds_show[[
                "codigo_activo","nombre_activo","clasificacion_2","ubicacion",
                "Total_PM","activador","Última PM","Días desde últ.",
                "Próxima PM","Días hasta prox.","resp_prox"
            ]].rename(columns={
                "codigo_activo":"Código","nombre_activo":"Activo",
                "clasificacion_2":"Clasif.","ubicacion":"Ubicación",
                "Total_PM":"Total PM","activador":"Activador",
                "resp_prox":"Próx. Resp."
            })

            st.markdown('<div class="section-header">🏭  Estado de mantenciones por activo / EDS</div>',
                        unsafe_allow_html=True)
            _show_df(_df_eds_show.reset_index(drop=True), hide_index=True, use_container_width=True,
                column_config={
                    "Código":           st.column_config.TextColumn(width=90),
                    "Activo":           st.column_config.TextColumn(width=220),
                    "Clasif.":          st.column_config.TextColumn(width=80),
                    "Ubicación":        st.column_config.TextColumn(width=200),
                    "Total PM":         st.column_config.NumberColumn(format="%d", width=70),
                    "Activador":        st.column_config.TextColumn(width=110),
                    "Última PM":        st.column_config.TextColumn(width=90),
                    "Días desde últ.":  st.column_config.NumberColumn(format="%d", width=110),
                    "Próxima PM":       st.column_config.TextColumn(width=90),
                    "Días hasta prox.": st.column_config.NumberColumn(format="%d", width=115),
                    "Próx. Resp.":      st.column_config.TextColumn(width=160),
                })
            st.caption(f"{len(_df_eds_show):,} activos únicos · ordenados por proximidad de próxima PM")

# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()

# Timestamps de actualización de datos — todo en hora Chile (America/Santiago)
from zoneinfo import ZoneInfo as _ZoneInfo
_tz_stgo = _ZoneInfo("America/Santiago")

def _to_stgo(ts) -> "pd.Timestamp":
    """Convierte un Timestamp (con o sin tz) a hora Santiago."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")   # Supabase almacena en UTC
    return t.tz_convert("America/Santiago")

if "_session_start" not in st.session_state:
    st.session_state["_session_start"] = datetime.now(_tz_stgo)

_ultima_ot_str = "—"
_ultima_ll_str = "—"
try:
    # OT más reciente en Supabase (caché ya cargado, sin request adicional)
    _raw_check = load_work_orders_supabase()
    _cd_vals = [r.get("creation_date") for r in _raw_check if r.get("creation_date")]
    if _cd_vals:
        _ultima_ot_str = _to_stgo(max(_cd_vals)).strftime("%d/%m/%Y %H:%M")
except Exception:
    pass
try:
    # Llamado más reciente en Supabase (v_llamados_sla, caché vía _sc)
    _ll_check = st.session_state.get("_sc_df_llamados_supa")
    if _ll_check is None:
        _ll_check = load_all_llamados_supabase()
    if isinstance(_ll_check, pd.DataFrame) and not _ll_check.empty and "fecha_llamado" in _ll_check.columns:
        _max_ll = pd.to_datetime(_ll_check["fecha_llamado"], errors="coerce").max()
        if pd.notna(_max_ll):
            _ultima_ll_str = _to_stgo(_max_ll).strftime("%d/%m/%Y")
except Exception:
    pass

_session_ts = st.session_state["_session_start"]
if hasattr(_session_ts, "strftime"):
    _session_str = _session_ts.strftime("%d/%m/%Y %H:%M")
else:
    _session_str = "—"
_footer_col1, _footer_col2 = st.columns([5, 5])
with _footer_col1:
    st.caption("Occimiano — Indicadores de Gestión Operacional v1.2 | Fracttal One API + Supabase")
with _footer_col2:
    st.markdown(
        f'<div style="text-align:right;color:#94a3b8;font-size:0.72rem;line-height:1.6;">'
        f'📦 Última OT en Supabase: <b>{_ultima_ot_str}</b> &nbsp;·&nbsp; '
        f'📋 Último llamado: <b>{_ultima_ll_str}</b><br>'
        f'🔄 Dashboard cargado: <b>{_session_str}</b>'
        f'</div>',
        unsafe_allow_html=True,
    )

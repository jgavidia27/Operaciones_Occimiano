"""
Panel de validación STO - Occim — Reincidencias EDS por ventana móvil.

Regla de reincidencia: 3+ correctivos en una ventana móvil de 20 días.
Cada disparo (fecha del 3er llamado) es una validación independiente.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

# ── Path: importar módulos del dashboard padre ─────────────────────────────
_HERE   = Path(__file__).resolve().parent
_PARENT = _HERE.parent
for _p in (_PARENT, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from auth import (  # noqa: E402
    init_cookie_manager, is_authenticated, try_login, logout,
)
from data import SENIORS  # noqa: E402
from mobile_auth import USERS, ADMINS  # noqa: E402

from reincidencias import (  # noqa: E402
    eds_con_reincidencia, correctivos_del_caso, clientes_en_rango,
    top_eds_ultimos_dias, DEFAULT_VENTANA_DIAS, DEFAULT_RANGO_DIAS,
)
from ui_kanban import render_kanban  # noqa: E402
import db  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# Config general
# ═══════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Panel de validación STO - Occim",
    page_icon="☑️",
    layout="wide",
    initial_sidebar_state="expanded",
)

OCCIM_BLUE       = "#1a4a8f"
OCCIM_BLUE_DARK  = "#123566"
OCCIM_BLUE_SOFT  = "#e6edf7"

st.markdown(f"""
<style>
    h1, h2, h3 {{ color: {OCCIM_BLUE_DARK} !important; }}
    h1 {{ border-bottom: 3px solid {OCCIM_BLUE}; padding-bottom: 6px; }}

    [data-testid="stSidebar"] {{
        background: linear-gradient(180deg, {OCCIM_BLUE_SOFT} 0%, #ffffff 220px);
    }}
    [data-testid="stSidebar"] .stMarkdown h3 {{ color: {OCCIM_BLUE_DARK} !important; }}

    .stMetric {{ background: {OCCIM_BLUE_SOFT}; padding: 10px; border-radius: 8px;
                 border-left: 4px solid {OCCIM_BLUE}; }}
    .badge-pend {{ background: #ffb020; color: #000; padding: 2px 8px;
                  border-radius: 12px; font-size: 0.78em; font-weight: 600; }}
    .badge-ok   {{ background: {OCCIM_BLUE}; color: #fff; padding: 2px 8px;
                  border-radius: 12px; font-size: 0.78em; font-weight: 600; }}
    .badge-crit {{ background: #dc2626; color: #fff; padding: 2px 10px;
                  border-radius: 12px; font-size: 0.78em; font-weight: 700;
                  box-shadow: 0 0 0 2px rgba(220,38,38,0.15); }}
    .banner-crit {{
        background: linear-gradient(90deg, #fee2e2 0%, #fef2f2 100%);
        border-left: 5px solid #dc2626;
        padding: 14px 18px;
        border-radius: 8px;
        margin-bottom: 14px;
    }}
    .banner-crit .title {{ color: #991b1b; font-weight: 700; font-size: 1.05em; }}
    .banner-crit .sub   {{ color: #7f1d1d; font-size: 0.9em; }}

    button[kind="primary"],
    button[kind="primaryFormSubmit"],
    button[data-testid="stBaseButton-primary"],
    button[data-testid="stBaseButton-primaryFormSubmit"],
    button[data-testid="stFormSubmitButton-primary"] {{
        background-color: {OCCIM_BLUE} !important;
        border-color: {OCCIM_BLUE} !important;
        color: #ffffff !important;
    }}
    button[kind="primary"]:hover,
    button[kind="primaryFormSubmit"]:hover,
    button[data-testid="stBaseButton-primary"]:hover,
    button[data-testid="stBaseButton-primaryFormSubmit"]:hover,
    button[data-testid="stFormSubmitButton-primary"]:hover {{
        background-color: {OCCIM_BLUE_DARK} !important;
        border-color: {OCCIM_BLUE_DARK} !important;
    }}

    div[data-testid="stHorizontalBlock"] div[data-testid="column"] .stButton > button {{
        border-radius: 999px;
        padding: 6px 14px;
        font-size: 0.82em;
        font-weight: 600;
        line-height: 1.2;
        white-space: normal;
        min-height: 34px;
        transition: all 0.15s ease-out;
    }}
    div[data-testid="stHorizontalBlock"] div[data-testid="column"] .stButton > button[kind="secondary"] {{
        background-color: #ffffff;
        color: {OCCIM_BLUE};
        border: 1.5px solid {OCCIM_BLUE};
    }}
    div[data-testid="stHorizontalBlock"] div[data-testid="column"] .stButton > button[kind="secondary"]:hover {{
        background-color: {OCCIM_BLUE_SOFT};
        border-color: {OCCIM_BLUE_DARK};
        color: {OCCIM_BLUE_DARK};
    }}
    div[data-testid="stHorizontalBlock"] div[data-testid="column"] .stButton > button[kind="primary"] {{
        background-color: {OCCIM_BLUE};
        color: #ffffff;
        border: 1.5px solid {OCCIM_BLUE};
    }}
    div[data-testid="stHorizontalBlock"] div[data-testid="column"] .stButton > button[kind="primary"]:hover {{
        background-color: {OCCIM_BLUE_DARK};
        border-color: {OCCIM_BLUE_DARK};
    }}

    hr {{ border-top: 1px solid {OCCIM_BLUE_SOFT}; }}
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# Auth + gate senior/admin
# ═══════════════════════════════════════════════════════════════════════════

init_cookie_manager()

ASSETS_DIR = _HERE / "assets"
LOGO_LOGIN = str(ASSETS_DIR / "logo_login.png")
LOGO_OCCIM = str(ASSETS_DIR / "logo_occim.png")


def _cliente_bonito(cli) -> str:
    if not cli:
        return "—"
    return str(cli).title()


def _login_view():
    st.markdown(
        "<h1 style='margin-bottom:0;border:none;'>☑️ Panel de validación STO - Occim</h1>"
        "<p style='color:#556;margin-top:4px;'>Panel de validación de reincidencias "
        "— acceso restringido a técnicos senior</p>",
        unsafe_allow_html=True,
    )
    st.markdown("<hr style='margin-top:4px;'>", unsafe_allow_html=True)

    _form_col, _logo_col = st.columns([2, 1])
    with _form_col:
        with st.form("login_form"):
            email = st.text_input("Correo", placeholder="tuemail@occimiano.cl")
            pwd   = st.text_input("Contraseña", type="password")
            ok = st.form_submit_button("Ingresar", type="primary", use_container_width=True)
            if ok:
                if try_login(email, pwd):
                    st.rerun()
                else:
                    st.error("Correo o contraseña incorrectos.")
    with _logo_col:
        try:
            st.image(LOGO_LOGIN, width=170)
        except Exception:
            pass


# Acceso explícito SOLO a este panel (supervisores/seniors que no son técnicos
# de terreno). No les da admin en la app móvil ni gestión de PINs.
PANEL_ACCESO_EXTRA: dict[str, str] = {
    "bballadares@occimiano.cl": "Braulio Balladares",
}


def _senior_info() -> dict | None:
    email = (st.session_state.get("_auth_email") or "").strip().lower()
    if not email:
        return None
    nombre_sess = st.session_state.get("_auth_nombre") or ""
    if email in ADMINS:
        return {"email": email, "short": "Admin", "nombre": nombre_sess or "Administrador"}
    if email in PANEL_ACCESO_EXTRA:
        return {"email": email, "short": "Senior",
                "nombre": nombre_sess or PANEL_ACCESO_EXTRA[email]}
    u = USERS.get(email)
    if u and u.get("short") in SENIORS:
        return {"email": email, "short": u["short"], "nombre": nombre_sess or u.get("full", u["short"])}
    return None


if not is_authenticated():
    _login_view()
    st.stop()

_me = _senior_info()
if _me is None:
    st.error("🚫 Este panel está restringido a técnicos senior. "
             "Si crees que deberías tener acceso, contacta a operaciones@occimiano.cl.")
    if st.button("Cerrar sesión"):
        logout()
        st.rerun()
    st.stop()


# ═══════════════════════════════════════════════════════════════════════════
# Catálogo de causas
# ═══════════════════════════════════════════════════════════════════════════

CAUSAS: list[dict] = [
    {"nombre": "Daño de Cliente",                    "atrib": "FNAO"},
    {"nombre": "Atribuible a la estación",           "atrib": "FNAO"},
    {"nombre": "Vandalismo",                         "atrib": "FNAO"},
    {"nombre": "Corte externo (luz/agua de la red)", "atrib": "FNAO"},
    {"nombre": "Falla en Componentes (Internos)",    "atrib": "FAO"},
    {"nombre": "Falla en Componentes (Externos)",    "atrib": "FAO"},
    {"nombre": "Falla eléctrica (001)",              "atrib": "FAO"},
    {"nombre": "Fuga de agua (003)",                 "atrib": "FAO"},
    {"nombre": "MP anterior ineficiente",            "atrib": "FAO"},
]
CAUSAS_NOMBRES = [c["nombre"] for c in CAUSAS]
CAUSA_ATRIB: dict[str, str] = {c["nombre"]: c["atrib"] for c in CAUSAS}


def _letra(i: int) -> str:
    s = ""
    n = i
    while True:
        s = chr(65 + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def _next_grupo_id(grupos: list[dict]) -> str:
    usados = {g["id"] for g in grupos}
    i = 1
    while f"g{i}" in usados:
        i += 1
    return f"g{i}"


# ═══════════════════════════════════════════════════════════════════════════
# Sidebar
# ═══════════════════════════════════════════════════════════════════════════

RANGOS_LABEL = {30: "Últimos 30 días", 60: "Últimos 60 días", 90: "Últimos 90 días"}

with st.sidebar:
    try:
        st.image(LOGO_OCCIM, use_container_width=True)
    except Exception:
        pass
    st.markdown(f"### 👋 {_me['nombre']}")
    st.caption(_me["email"])
    st.divider()

    rango_sel: int = st.selectbox(
        "Rango",
        options=[30, 60, 90],
        index=1,
        format_func=lambda d: RANGOS_LABEL[d],
    )

    _clientes = ["Todos"] + clientes_en_rango(rango_sel)
    cliente_sel = st.selectbox("Cliente", options=_clientes, index=0)

    filtro_eds = st.text_input(
        "Buscar EDS",
        value="",
        placeholder="Ej. 40051, SH_216…",
    ).strip()

    st.divider()
    _nav = st.radio(
        "Vista",
        options=["Reincidencias", "Historial", "Últimos 30 días"],
        index=0,
        label_visibility="collapsed",
    )

    st.divider()
    if st.button("Cerrar sesión", use_container_width=True):
        logout()
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# Estado & serialización
# ═══════════════════════════════════════════════════════════════════════════

def _state_key(codigo_eds: str, fecha_disparo: date) -> str:
    return f"edit_{codigo_eds}_{fecha_disparo.isoformat()}"


def _cargar_estado_edicion(codigo_eds: str, fecha_disparo: date, fila: dict):
    key = _state_key(codigo_eds, fecha_disparo)
    if key in st.session_state:
        return st.session_state[key]

    clasif = fila.get("clasificaciones") or {}
    grupos: list[dict] = []
    asignaciones: dict = {}
    seguimiento: dict = {}

    if isinstance(clasif, dict):
        grupos       = list(clasif.get("grupos") or [])
        asignaciones = dict(clasif.get("asignaciones") or {})
        seguimiento  = dict(clasif.get("seguimiento") or {})

    state = {
        "grupos":       grupos,
        "asignaciones": asignaciones,
        "seguimiento":  seguimiento,
        "resumen":      fila.get("resumen_falla") or "",
        "solucion":     fila.get("solucion_propuesta") or "",
    }
    st.session_state[key] = state
    return state


def _serializar(state: dict) -> dict:
    return {
        "grupos":       [dict(g) for g in state["grupos"]],
        "asignaciones": dict(state["asignaciones"]),
        "seguimiento":  dict(state.get("seguimiento") or {}),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Navegación
# ═══════════════════════════════════════════════════════════════════════════

def _volver_a_home():
    st.session_state.pop("eds_detalle", None)
    st.session_state.pop("fecha_disparo", None)


def render_historial():
    st.title("📚 Historial de validaciones")
    df = db.historial(limit=1000)
    if df.empty:
        st.info("Aún no hay validaciones firmadas.")
        return
    df = df.copy()
    df["fecha_disparo"] = pd.to_datetime(df["fecha_disparo"]).dt.strftime("%d-%m-%Y")
    df["validado_at"]   = pd.to_datetime(df["validado_at"]).dt.strftime("%d/%m/%Y %H:%M")

    c1, c2 = st.columns(2)
    _seniors_hist = ["Todos"] + sorted(df["validado_por_nombre"].dropna().unique().tolist())
    _f_sen = c1.selectbox("Senior", options=_seniors_hist, index=0)
    _f_eds = c2.text_input("Filtrar EDS (opcional)", value="", placeholder="EDS...")

    v = df
    if _f_sen != "Todos":
        v = v[v["validado_por_nombre"] == _f_sen]
    if _f_eds.strip():
        v = v[v["codigo_eds"].str.contains(_f_eds.strip(), case=False, na=False)]

    st.dataframe(
        v[["fecha_disparo", "codigo_eds", "validado_por_nombre", "validado_at",
           "resumen_falla", "solucion_propuesta"]]
        .rename(columns={
            "fecha_disparo": "Disparo",
            "codigo_eds": "EDS",
            "validado_por_nombre": "Firmado por",
            "validado_at": "Firmado el",
            "resumen_falla": "Resumen",
            "solucion_propuesta": "Solución",
        }),
        use_container_width=True,
        hide_index=True,
    )


def render_ultimos_30():
    st.title("📈 Últimos 30 días")
    st.caption("Top 10 EDS con más correctivos en los últimos 30 días (actualizado en tiempo real).")

    df = top_eds_ultimos_dias(top_n=10, dias=30)
    if df.empty:
        st.info("Aún no hay correctivos en los últimos 30 días.")
        return

    total = int(df["n_llamados"].sum())
    umbral_3 = int((df["n_llamados"] >= 3).sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("Top 10 EDS", len(df))
    c2.metric("Total correctivos (Top 10)", total)
    c3.metric("EDS con ≥3 llamados", umbral_3)

    st.markdown("### Ranking")
    _view = df.copy()
    _view["Estación"] = _view["estacion"].fillna("").astype(str).str.title()
    _view["Ciudad"]   = _view["comuna"].fillna("").astype(str).str.title()
    _view["Cliente"]  = _view["cliente"].apply(_cliente_bonito)
    _view = _view.rename(columns={"codigo_eds": "EDS", "n_llamados": "N° llamados"})
    st.dataframe(
        _view[["EDS", "Estación", "Ciudad", "Cliente", "N° llamados"]],
        use_container_width=True, hide_index=True,
    )

    st.markdown("### Gráfico")
    _chart = df.copy()
    _chart["label"] = (
        _chart["codigo_eds"].astype(str)
        + " — " + _chart["estacion"].fillna("").astype(str).str.title()
    )
    _chart = _chart.set_index("label")[["n_llamados"]].rename(
        columns={"n_llamados": "N° llamados"}
    )
    st.bar_chart(_chart, horizontal=True, height=380, color=OCCIM_BLUE)


if _nav == "Historial":
    _volver_a_home()
    render_historial()
    st.stop()

if _nav == "Últimos 30 días":
    _volver_a_home()
    render_ultimos_30()
    st.stop()


# ═══════════════════════════════════════════════════════════════════════════
# HOME — Reincidencias detectadas por ventana móvil
# ═══════════════════════════════════════════════════════════════════════════

def render_home(rango_dias: int, cliente: str, filtro_eds: str = ""):
    st.title("☑️ Panel de validación STO - Occim")
    st.caption(f"{RANGOS_LABEL[rango_dias]} · Ventana de {DEFAULT_VENTANA_DIAS} días para detectar reincidencia"
               + (f" · {cliente}" if cliente != "Todos" else "")
               + (f" · filtro: {filtro_eds}" if filtro_eds else ""))

    # Primera pasada: obtener candidatos de EDS para pedir sus firmas
    _preview = eds_con_reincidencia(rango_dias=rango_dias, cliente=cliente,
                                     firmas_por_eds={})
    eds_candidatas = list(_preview["codigo_eds"].unique()) if not _preview.empty else []
    firmas = db.firmas_por_eds(eds_candidatas)

    # Segunda pasada: ahora con firmas reales, detección correcta
    df = eds_con_reincidencia(rango_dias=rango_dias, cliente=cliente,
                              firmas_por_eds=firmas)

    pares = [(r["codigo_eds"], r["fecha_disparo"]) for _, r in df.iterrows()]
    estados = db.estados_por_disparos(pares) if pares else {}

    df = df.copy()
    df["_ya_validada"] = df["cerrado"]
    df["_critica"] = df["critico"] & ~df["_ya_validada"]

    # Filtro por código EDS (case-insensitive, substring)
    if filtro_eds:
        df = df[df["codigo_eds"].str.contains(filtro_eds, case=False, na=False)]

    # Orden: críticas arriba, luego por N° llamados desc, luego fecha_disparo desc
    df = df.sort_values(
        by=["_critica", "n_llamados", "fecha_disparo"],
        ascending=[False, False, False],
    ).reset_index(drop=True)

    total = len(df)
    n_criticas   = int(df["_critica"].sum())
    n_validadas  = int(df["_ya_validada"].sum())
    n_pendientes = total - n_validadas

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Disparos detectados", total)
    c2.metric("Pendientes", n_pendientes)
    c3.metric("Reincidentes 🔴", n_criticas)
    c4.metric("Validadas", n_validadas)

    if df.empty:
        st.info(f"No hay EDS con reincidencia en los últimos {rango_dias} días.")
        return

    st.markdown("### EDS con reincidencia")
    st.caption("Ordenadas: 🔴 reincidentes primero, luego por N° de llamados sin cerrar. Click para revisar y clasificar.")

    hdr = st.columns([1, 3, 2, 2, 1, 2, 2, 1])
    hdr[0].markdown("**EDS**")
    hdr[1].markdown("**Estación**")
    hdr[2].markdown("**Ciudad**")
    hdr[3].markdown("**Cliente**")
    hdr[4].markdown("**N°**")
    hdr[5].markdown("**Disparo**")
    hdr[6].markdown("**Estado**")
    hdr[7].markdown("")

    for _, row in df.iterrows():
        cod = row["codigo_eds"]
        fd  = row["fecha_disparo"]
        est = estados.get((cod, fd.isoformat()), {})
        cols = st.columns([1, 3, 2, 2, 1, 2, 2, 1])
        cols[0].write(cod or "—")
        cols[1].write((row.get("estacion") or "").title() or "—")
        cols[2].write((row.get("comuna") or "").title() or "—")
        cols[3].write(_cliente_bonito(row.get("cliente")))
        cols[4].write(int(row["n_llamados"]))
        cols[5].write(fd.strftime("%d-%m-%Y"))

        if est.get("validado"):
            firmante = est.get("validado_por_nombre") or "—"
            _at = est.get("validado_at")
            fecha = ""
            if _at:
                try:
                    fecha = pd.to_datetime(_at).strftime("%d/%m")
                except Exception:
                    fecha = ""
            cols[6].markdown(
                f'<span class="badge-ok">✅ {firmante}'
                + (f" · {fecha}" if fecha else "") + "</span>",
                unsafe_allow_html=True,
            )
        elif row["_critica"]:
            cols[6].markdown(
                '<span class="badge-crit">🔴 Reincidente</span>',
                unsafe_allow_html=True,
            )
        else:
            cols[6].markdown(
                '<span class="badge-pend">🔔 Pendiente</span>',
                unsafe_allow_html=True,
            )

        if cols[7].button("Abrir", key=f"open_{cod}_{fd.isoformat()}"):
            st.session_state["eds_detalle"]  = cod
            st.session_state["fecha_disparo"] = fd.isoformat()
            st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# DETALLE — chips, kanban, seguimiento, firma
# ═══════════════════════════════════════════════════════════════════════════

def _tarjeta_label(idx: int, row: pd.Series) -> str:
    try:
        _fecha = pd.to_datetime(row["fecha_creacion"]).strftime("%d-%m-%Y")
    except Exception:
        _fecha = "—"
    _tec = str(row.get("responsable") or "").strip() or "sin técnico"
    _parts = _tec.split()
    if len(_parts) >= 2:
        _tec = f"{_parts[0]} {_parts[-1]}"
    _tf  = str(row.get("tipo_falla") or "").strip()
    _cr  = str(row.get("causa_raiz") or "").strip()
    _clas = f"{_tf} — {_cr}" if _tf and _cr else (_tf or _cr or "Sin clasificación")
    _com = str(row.get("comentario_tecnico") or "").strip() or "_Sin comentario del técnico_"
    if len(_com) > 240:
        _com = _com[:240].rstrip() + "…"
    return f"{idx}. {row['id_ot']}  ·  {_fecha}\n{_clas}\nTécnico {_tec}: {_com}"


def render_detalle(codigo_eds: str, fecha_disparo: date):
    top = st.columns([1, 6])
    if top[0].button("← Volver"):
        _volver_a_home()
        st.rerun()

    with top[1]:
        st.title(f"EDS {codigo_eds}")
        st.caption(f"Disparo de reincidencia: {fecha_disparo.strftime('%d-%m-%Y')}")

    firmas_eds = db.firmas_por_eds([codigo_eds]).get(codigo_eds, set())
    df = correctivos_del_caso(codigo_eds, fecha_disparo, firmas=firmas_eds)
    fila = db.get(codigo_eds, fecha_disparo) or {}
    ya_validado = bool(fila.get("validado"))

    if df.empty:
        st.warning("No se encontraron correctivos para esta ventana.")
        return

    cliente  = df["cliente"].dropna().iloc[0] if not df.empty else ""
    estacion = df["estacion"].dropna().iloc[0] if not df.empty else ""
    st.markdown(
        f"**{(estacion or '—').title()}** · Cliente: **{_cliente_bonito(cliente)}** · "
        f"Correctivos del caso: **{len(df)}** · "
        f"Del {pd.to_datetime(df['fecha_creacion'].min()).strftime('%d-%m-%Y')} "
        f"al {pd.to_datetime(df['fecha_creacion'].max()).strftime('%d-%m-%Y')}"
    )

    if ya_validado:
        _at = fila.get("validado_at")
        _at_txt = pd.to_datetime(_at).strftime("%d/%m/%Y %H:%M") if _at else "—"
        st.success(f"✅ Validado por **{fila.get('validado_por_nombre') or '—'}** el {_at_txt}")

    # Banner de reincidente crítica
    if not ya_validado:
        _prev = db.validaciones_anteriores_eds(codigo_eds, antes_de=fecha_disparo)
        if _prev:
            p = _prev[0]
            _p_fecha = pd.to_datetime(p["fecha_disparo"]).strftime("%d-%m-%Y")
            _p_firmante = p.get("validado_por_nombre") or "—"
            _p_clasif = p.get("clasificaciones") or {}
            _p_grupos = _p_clasif.get("grupos") or []
            _motivos_previos = ", ".join((g.get("nombre") or "—") for g in _p_grupos) or "—"
            st.markdown(
                f"""
                <div class="banner-crit">
                    <div class="title">🔴 Reincidencia — esta EDS ya fue validada antes</div>
                    <div class="sub">
                        Validación anterior firmada por <b>{_p_firmante}</b>
                        con disparo el <b>{_p_fecha}</b>.<br>
                        Motivos previos: <b>{_motivos_previos}</b>.<br>
                        Considera si los llamados actuales repiten esos motivos.
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    state = _cargar_estado_edicion(codigo_eds, fecha_disparo, fila)
    grupos: list[dict] = state["grupos"]
    asignaciones: dict = state["asignaciones"]

    # ── Chips de motivos ────────────────────────────────────────────────
    st.markdown("### Motivos disponibles")
    st.caption("Haz click en un motivo para añadir su columna al análisis. "
               "Click de nuevo para quitarlo (sus OTs vuelven a **Por clasificar**).")

    nombre_a_gid = {g["nombre"]: g["id"] for g in grupos if g.get("nombre")}
    if not ya_validado:
        chip_cols = st.columns(4)
        for idx, causa in enumerate(CAUSAS_NOMBRES):
            activo = causa in nombre_a_gid
            with chip_cols[idx % 4]:
                if st.button(
                    causa,
                    key=f"chip_{codigo_eds}_{idx}",
                    type=("primary" if activo else "secondary"),
                    use_container_width=True,
                ):
                    if activo:
                        gid = nombre_a_gid[causa]
                        for ot, dest in list(asignaciones.items()):
                            if dest == gid:
                                asignaciones.pop(ot)
                        grupos[:] = [g for g in grupos if g["id"] != gid]
                    else:
                        nuevo_id = _next_grupo_id(grupos)
                        grupos.append({
                            "id":       nuevo_id,
                            "nombre":   causa,
                            "atrib":    CAUSA_ATRIB[causa],
                            "resumen":  "",
                            "solucion": "",
                        })
                    state["grupos"] = grupos
                    state["asignaciones"] = asignaciones
                    st.rerun()
    else:
        activos = [g.get("nombre") for g in grupos if g.get("nombre")]
        if activos:
            st.markdown(" ".join(f"`{n}`" for n in activos))

    for i, g in enumerate(grupos):
        g["letra"] = _letra(i)

    if not grupos and not ya_validado:
        st.info("Selecciona uno o más motivos arriba para empezar el análisis.")

    # ── Kanban ───────────────────────────────────────────────────────────
    st.markdown("### Clasificación por correctivo")
    if grupos:
        st.caption("Arrastra cada tarjeta al grupo que corresponde, o a **Sin coincidencia** "
                   "si no coincide con ninguna.")

    items = [
        {"id_ot": r["id_ot"], "label": _tarjeta_label(i + 1, r)}
        for i, (_, r) in enumerate(df.iterrows())
    ]
    resultado = render_kanban(
        items, asignaciones, grupos,
        read_only=ya_validado,
        key_prefix=f"k_{codigo_eds}_{fecha_disparo.isoformat()}",
    )
    if not ya_validado and not resultado.get("unchanged"):
        nuevas_asig = resultado["asignaciones"]
        if nuevas_asig != state["asignaciones"]:
            state["asignaciones"] = nuevas_asig
            asignaciones = state["asignaciones"]

    # ── Resumen + solución generales de la EDS ───────────────────────────
    if grupos:
        st.markdown("### Resumen y solución")
        st.caption("Un resumen general de lo que ves y una propuesta de solución para toda la EDS.")
        c1, c2 = st.columns(2)
        with c1:
            state["resumen"] = st.text_area(
                "Resumen de la falla",
                value=state.get("resumen") or "",
                height=110,
                disabled=ya_validado,
                placeholder="¿Qué patrón ves en los llamados de esta EDS?",
                key=f"eds_resumen_{codigo_eds}_{fecha_disparo}",
            )
        with c2:
            state["solucion"] = st.text_area(
                "Solución propuesta",
                value=state.get("solucion") or "",
                height=110,
                disabled=ya_validado,
                placeholder="Acción de fondo para reducir la reincidencia.",
                key=f"eds_solucion_{codigo_eds}_{fecha_disparo}",
            )

    # ── Seguimiento de la EDS ────────────────────────────────────────────
    if grupos:
        st.markdown("### Seguimiento de la EDS")
        _seg = state.get("seguimiento") or {}
        _OPC_SOL = ["Sí", "Aún no"]
        _OPC_CTC = ["Sí", "No"]
        _OPC_MED = ["Llamada", "Presencial"]
        _MAP_SOL = {"Sí": "si", "Aún no": "aun_no"}
        _MAP_CTC = {"Sí": "si", "No": "no"}
        _MAP_MED = {"Llamada": "llamada", "Presencial": "presencial"}
        _INV_SOL = {v: k for k, v in _MAP_SOL.items()}
        _INV_CTC = {v: k for k, v in _MAP_CTC.items()}
        _INV_MED = {v: k for k, v in _MAP_MED.items()}

        s1, s2 = st.columns(2)
        with s1:
            _sol_ini = _INV_SOL.get(_seg.get("soluciono"))
            _sol_idx = _OPC_SOL.index(_sol_ini) if _sol_ini in _OPC_SOL else None
            sol_sel = st.radio(
                "¿Se solucionó el problema?",
                options=_OPC_SOL, index=_sol_idx, horizontal=True,
                disabled=ya_validado, key=f"seg_sol_{codigo_eds}_{fecha_disparo}",
            )
            _seg["soluciono"] = _MAP_SOL.get(sol_sel)

        with s2:
            _ctc_ini = _INV_CTC.get(_seg.get("contacto_estacion"))
            _ctc_idx = _OPC_CTC.index(_ctc_ini) if _ctc_ini in _OPC_CTC else None
            ctc_sel = st.radio(
                "¿Se contactó a la estación?",
                options=_OPC_CTC, index=_ctc_idx, horizontal=True,
                disabled=ya_validado, key=f"seg_ctc_{codigo_eds}_{fecha_disparo}",
            )
            _seg["contacto_estacion"] = _MAP_CTC.get(ctc_sel)
            if _seg.get("contacto_estacion") == "si":
                _med_ini = _INV_MED.get(_seg.get("medio_contacto"))
                _med_idx = _OPC_MED.index(_med_ini) if _med_ini in _OPC_MED else None
                med_sel = st.radio(
                    "¿Por qué medio?",
                    options=_OPC_MED, index=_med_idx, horizontal=True,
                    disabled=ya_validado, key=f"seg_med_{codigo_eds}_{fecha_disparo}",
                )
                _seg["medio_contacto"] = _MAP_MED.get(med_sel)
            else:
                _seg["medio_contacto"] = None

        state["seguimiento"] = _seg

    # ── Acciones ─────────────────────────────────────────────────────────
    if ya_validado:
        st.info("Esta validación ya fue firmada. Para editarla, contacta a operaciones.")
        return

    a1, a2 = st.columns([1, 1])
    if a1.button("💾 Guardar avance", use_container_width=True):
        ok1 = db.upsert_clasificaciones(codigo_eds, fecha_disparo, _serializar(state))
        ok2 = db.upsert_textos(
            codigo_eds, fecha_disparo,
            resumen=state.get("resumen") or "",
            solucion=state.get("solucion") or "",
        )
        if ok1 and ok2:
            st.success("Avance guardado.")
        else:
            st.error("No se pudo guardar. Reintenta.")

    if a2.button("✍️ Validar y firmar", use_container_width=True, type="primary"):
        errores = []
        pendientes = [it["id_ot"] for it in items if it["id_ot"] not in asignaciones]
        if pendientes:
            errores.append(f"Aún quedan {len(pendientes)} OT(s) en **Por clasificar**.")

        nombres_g = [g.get("nombre") for g in grupos if g.get("nombre")]
        dup = {n for n in nombres_g if nombres_g.count(n) > 1}
        if dup:
            errores.append(f"Causas repetidas entre grupos: {', '.join(dup)}.")

        for g in grupos:
            n_ots = sum(1 for _, v in asignaciones.items() if v == g["id"])
            _titulo = g.get("nombre") or "(sin motivo)"
            if n_ots == 0:
                errores.append(f"**{_titulo}** está vacío — quítalo o asígnale OTs.")

        if not (state.get("resumen") or "").strip():
            errores.append("Completa el **Resumen de la falla** general.")
        if not (state.get("solucion") or "").strip():
            errores.append("Completa la **Solución propuesta** general.")

        _seg = state.get("seguimiento") or {}
        if not _seg.get("soluciono"):
            errores.append("Responde *¿Se solucionó el problema?*")
        if not _seg.get("contacto_estacion"):
            errores.append("Responde *¿Se contactó a la estación?*")
        elif _seg.get("contacto_estacion") == "si" and not _seg.get("medio_contacto"):
            errores.append("Indica el *medio de contacto*.")

        if errores:
            for e in errores:
                st.error(e)
        else:
            # Último llamado del caso = fecha máxima entre los llamados mostrados
            _ult = pd.to_datetime(df["fecha_creacion"].max()).date()
            ok = db.firmar(
                codigo_eds, fecha_disparo,
                email=_me["email"], nombre=_me["nombre"],
                clasificaciones=_serializar(state),
                resumen=state.get("resumen") or "",
                solucion=state.get("solucion") or "",
                ultimo_llamado=_ult,
            )
            if ok:
                st.success("✅ Validación firmada. Gracias.")
                st.balloons()
                st.session_state.pop(_state_key(codigo_eds, fecha_disparo), None)
                st.rerun()
            else:
                st.error("No se pudo firmar. Reintenta.")


# ═══════════════════════════════════════════════════════════════════════════
# Dispatcher
# ═══════════════════════════════════════════════════════════════════════════

if "eds_detalle" in st.session_state and "fecha_disparo" in st.session_state:
    try:
        _fd = date.fromisoformat(st.session_state["fecha_disparo"])
    except Exception:
        _fd = None
    if _fd:
        render_detalle(st.session_state["eds_detalle"], _fd)
    else:
        _volver_a_home()
        render_home(rango_sel, cliente_sel, filtro_eds)
else:
    render_home(rango_sel, cliente_sel, filtro_eds)

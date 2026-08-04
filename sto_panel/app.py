"""
Análisis STO Occim — Panel de validación de reincidencias EDS.

Entry point Streamlit. En Streamlit Cloud, apunta este archivo como
Main file path del segundo deploy.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
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

from periodo import (  # noqa: E402
    ultimo_mes_cerrado, periodo_label, periodos_disponibles,
)
from reincidencias import (  # noqa: E402
    eds_reincidentes, correctivos_de_eds, clientes_del_periodo,
)
from ui_kanban import render_kanban  # noqa: E402
import db  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# Config general
# ═══════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Análisis STO Occim",
    page_icon="🔧",
    layout="wide",
    initial_sidebar_state="expanded",
)

OCCIM_BLUE       = "#1a4a8f"   # azul principal del logo OCCIM
OCCIM_BLUE_DARK  = "#123566"
OCCIM_BLUE_SOFT  = "#e6edf7"

st.markdown(f"""
<style>
    /* Títulos con toque azul OCCIM */
    h1, h2, h3 {{ color: {OCCIM_BLUE_DARK} !important; }}
    h1 {{ border-bottom: 3px solid {OCCIM_BLUE}; padding-bottom: 6px; }}

    /* Sidebar */
    [data-testid="stSidebar"] {{
        background: linear-gradient(180deg, {OCCIM_BLUE_SOFT} 0%, #ffffff 220px);
    }}
    [data-testid="stSidebar"] .stMarkdown h3 {{
        color: {OCCIM_BLUE_DARK} !important;
    }}

    .stMetric {{ background: {OCCIM_BLUE_SOFT}; padding: 10px; border-radius: 8px;
                 border-left: 4px solid {OCCIM_BLUE}; }}
    .eds-card {{ padding: 10px 12px; border: 1px solid rgba(200,200,200,0.2);
                border-radius: 8px; margin-bottom: 6px; }}
    .badge-pend {{ background: #ffb020; color: #000; padding: 2px 8px;
                  border-radius: 12px; font-size: 0.78em; font-weight: 600; }}
    .badge-ok   {{ background: {OCCIM_BLUE}; color: #fff; padding: 2px 8px;
                  border-radius: 12px; font-size: 0.78em; font-weight: 600; }}

    /* Botones primarios (Ingresar, Validar y firmar…) — cubre todos los kinds */
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
        color: #ffffff !important;
    }}

    /* Chips de motivos (burbujas clickeables) */
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
    /* Chip inactivo — contorno azul OCCIM */
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
    /* Chip activo — relleno azul OCCIM */
    div[data-testid="stHorizontalBlock"] div[data-testid="column"] .stButton > button[kind="primary"] {{
        background-color: {OCCIM_BLUE};
        color: #ffffff;
        border: 1.5px solid {OCCIM_BLUE};
    }}
    div[data-testid="stHorizontalBlock"] div[data-testid="column"] .stButton > button[kind="primary"]:hover {{
        background-color: {OCCIM_BLUE_DARK};
        border-color: {OCCIM_BLUE_DARK};
    }}

    /* Divisor sutil */
    hr {{ border-top: 1px solid {OCCIM_BLUE_SOFT}; }}
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# Auth + gate senior
# ═══════════════════════════════════════════════════════════════════════════

init_cookie_manager()

ASSETS_DIR = _HERE / "assets"
LOGO_LOGIN = str(ASSETS_DIR / "logo_login.png")
LOGO_OCCIM = str(ASSETS_DIR / "logo_occim.png")


def _login_view():
    st.markdown(
        "<h1 style='margin-bottom:0;border:none;'>Análisis STO Occim</h1>"
        "<p style='color:#556;margin-top:4px;'>Panel de validación de reincidencias "
        "— acceso restringido a técnicos senior</p>",
        unsafe_allow_html=True,
    )
    st.markdown("<hr style='margin-top:4px;'>", unsafe_allow_html=True)

    # Formulario a la izquierda + logo 25 años a la derecha
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


def _senior_info() -> dict | None:
    """Devuelve {email, short, nombre} si el usuario es senior o admin.
    None si no tiene acceso al panel."""
    email = (st.session_state.get("_auth_email") or "").strip().lower()
    if not email:
        return None
    nombre_sess = st.session_state.get("_auth_nombre") or ""

    if email in ADMINS:
        return {"email": email, "short": "Admin", "nombre": nombre_sess or "Administrador"}

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
# Sidebar
# ═══════════════════════════════════════════════════════════════════════════

with st.sidebar:
    try:
        st.image(LOGO_OCCIM, use_container_width=True)
    except Exception:
        pass
    st.markdown(f"### 👋 {_me['nombre']}")
    st.caption(_me["email"])
    st.divider()

    _periodos = periodos_disponibles()
    if not _periodos:
        _periodos = [ultimo_mes_cerrado()]

    _default_idx = 0
    periodo_sel: date = st.selectbox(
        "Periodo",
        options=_periodos,
        index=_default_idx,
        format_func=periodo_label,
    )

    _clientes_disp = ["Todos"] + clientes_del_periodo(periodo_sel)
    cliente_sel = st.selectbox("Cliente", options=_clientes_disp, index=0)

    st.divider()
    _nav = st.radio(
        "Vista",
        options=["Reincidencias", "Historial"],
        index=0,
        label_visibility="collapsed",
    )

    st.divider()
    if st.button("Cerrar sesión", use_container_width=True):
        logout()
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# Router
# ═══════════════════════════════════════════════════════════════════════════

def _volver_a_home():
    st.session_state.pop("eds_detalle", None)


def render_historial():
    st.title("📚 Historial de validaciones")
    df = db.historial(limit=1000)
    if df.empty:
        st.info("Aún no hay validaciones firmadas.")
        return
    df = df.copy()
    df["periodo"]     = pd.to_datetime(df["periodo"]).dt.strftime("%Y-%m")
    df["validado_at"] = pd.to_datetime(df["validado_at"]).dt.strftime("%d/%m/%Y %H:%M")

    c1, c2 = st.columns(2)
    _seniors_hist = ["Todos"] + sorted(df["validado_por_nombre"].dropna().unique().tolist())
    _f_sen = c1.selectbox("Senior", options=_seniors_hist, index=0)
    _meses = ["Todos"] + sorted(df["periodo"].dropna().unique().tolist(), reverse=True)
    _f_mes = c2.selectbox("Mes", options=_meses, index=0)

    v = df
    if _f_sen != "Todos":
        v = v[v["validado_por_nombre"] == _f_sen]
    if _f_mes != "Todos":
        v = v[v["periodo"] == _f_mes]

    st.dataframe(
        v[["periodo", "codigo_eds", "validado_por_nombre", "validado_at",
           "resumen_falla", "solucion_propuesta"]]
        .rename(columns={
            "periodo": "Mes",
            "codigo_eds": "EDS",
            "validado_por_nombre": "Firmado por",
            "validado_at": "Firmado el",
            "resumen_falla": "Resumen",
            "solucion_propuesta": "Solución",
        }),
        use_container_width=True,
        hide_index=True,
    )


if _nav == "Historial":
    _volver_a_home()
    render_historial()
    st.stop()


# ── Home / Detalle ────────────────────────────────────────────────────────
if "eds_detalle" in st.session_state:
    _view = "detalle"
else:
    _view = "home"


# ═══════════════════════════════════════════════════════════════════════════
# HOME — lista de EDS reincidentes
# ═══════════════════════════════════════════════════════════════════════════

def render_home(periodo: date, cliente: str):
    st.title("🔧 Análisis STO Occim")
    st.caption(f"Reincidencias · {periodo_label(periodo)}"
               + (f" · {cliente}" if cliente != "Todos" else ""))

    df = eds_reincidentes(periodo, cliente=cliente)
    estados = db.estados_del_periodo(periodo)

    total = len(df)
    validadas = sum(1 for e in estados.values() if e.get("validado"))
    pendientes = total - sum(
        1 for _, r in df.iterrows()
        if estados.get(r["codigo_eds"], {}).get("validado")
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("EDS con ≥3 correctivos", total)
    c2.metric("Pendientes", pendientes)
    c3.metric("Validadas", min(validadas, total))

    if df.empty:
        st.info("No hay EDS con 3 o más correctivos en este periodo.")
        return

    st.markdown("### EDS reincidentes")
    st.caption("Ordenadas por N° de llamados descendente. Click para revisar y clasificar.")

    hdr = st.columns([1, 3, 2, 1, 2, 1])
    hdr[0].markdown("**EDS**")
    hdr[1].markdown("**Estación**")
    hdr[2].markdown("**Cliente**")
    hdr[3].markdown("**N°**")
    hdr[4].markdown("**Estado**")
    hdr[5].markdown("")

    for _, row in df.iterrows():
        cod = row["codigo_eds"]
        est = estados.get(cod, {})
        cols = st.columns([1, 3, 2, 1, 2, 1])
        cols[0].write(cod or "—")
        cols[1].write(row.get("estacion") or "—")
        cols[2].write(row.get("cliente") or "—")
        cols[3].write(int(row["n_llamados"]))
        if est.get("validado"):
            firmante = est.get("validado_por_nombre") or "—"
            _at = est.get("validado_at")
            fecha = ""
            if _at:
                try:
                    fecha = pd.to_datetime(_at).strftime("%d/%m")
                except Exception:
                    fecha = ""
            cols[4].markdown(
                f'<span class="badge-ok">✅ Validado por {firmante}'
                + (f" · {fecha}" if fecha else "") + "</span>",
                unsafe_allow_html=True,
            )
        else:
            cols[4].markdown(
                '<span class="badge-pend">🔔 Pendiente</span>',
                unsafe_allow_html=True,
            )
        if cols[5].button("Abrir", key=f"open_{cod}"):
            st.session_state["eds_detalle"] = cod
            st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# DETALLE EDS — kanban + firma
# ═══════════════════════════════════════════════════════════════════════════

def _tarjeta_label(idx: int, row: pd.Series) -> str:
    """Etiqueta rica multilínea para la tarjeta del kanban.
    Formato:
        1 · 27-07-2026 · OS-38570
        F.N.A.O — Desgastes de repuestos
        Técnico Juan Toro: Manguera rota por manipulación del cliente
    """
    try:
        _fecha = pd.to_datetime(row["fecha_creacion"]).strftime("%d-%m-%Y")
    except Exception:
        _fecha = "—"
    _tec = str(row.get("responsable") or "").strip() or "sin técnico"
    # Nombre corto del técnico (primer + apellido)
    _parts = _tec.split()
    if len(_parts) >= 2:
        _tec = f"{_parts[0]} {_parts[-1]}"

    _tf  = str(row.get("tipo_falla") or "").strip()
    _cr  = str(row.get("causa_raiz") or "").strip()
    if _tf and _cr:
        _clas = f"{_tf} — {_cr}"
    else:
        _clas = _tf or _cr or "Sin clasificación"

    _com = str(row.get("comentario_tecnico") or "").strip()
    if not _com:
        _com = "_Sin comentario del técnico_"
    if len(_com) > 240:
        _com = _com[:240].rstrip() + "…"

    return (
        f"{idx}. {row['id_ot']}  ·  {_fecha}\n"
        f"{_clas}\n"
        f"Técnico {_tec}: {_com}"
    )


# ── Catálogo de causas ────────────────────────────────────────────────────
# El senior elige una causa por grupo. El campo "atrib" (F.A.O / F.N.A.O / Otro)
# se guarda en el JSONB para reportes internos pero no se muestra en la UI.
CAUSAS: list[dict] = [
    {"nombre": "Daño de Cliente",                    "atrib": "FNAO"},
    {"nombre": "Atribuible a la estación",           "atrib": "FNAO"},
    {"nombre": "Vandalismo",                         "atrib": "FNAO"},
    {"nombre": "Corte externo (luz/agua de la red)", "atrib": "FNAO"},
    {"nombre": "Falla en Componentes (Internos)",    "atrib": "FAO"},
    {"nombre": "Falla en Componentes (Externos)",    "atrib": "FAO"},
    {"nombre": "Falla eléctrica (001)",              "atrib": "FAO"},
    {"nombre": "Fuga de agua (003)",                 "atrib": "FAO"},
]
CAUSAS_NOMBRES = [c["nombre"] for c in CAUSAS]
CAUSA_ATRIB: dict[str, str] = {c["nombre"]: c["atrib"] for c in CAUSAS}


def _letra(i: int) -> str:
    """0→A, 1→B, ..., 25→Z, 26→AA…"""
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


def _cargar_estado_edicion(codigo_eds: str, periodo: date, fila: dict):
    """Carga clasificaciones desde Supabase a session_state en la primera
    apertura de la EDS. Estructura nueva del JSONB clasificaciones:
        {"grupos": [{id, nombre, resumen, solucion}], "asignaciones": {ot: gid|"sin_coincidencia"}}
    Retrocompat con formato antiguo {ot: "coincidente"|"sin_coincidencia"}.
    """
    key = f"edit_{codigo_eds}_{periodo.isoformat()}"
    if key in st.session_state:
        return st.session_state[key]

    clasif = fila.get("clasificaciones") or {}
    grupos: list[dict] = []
    asignaciones: dict = {}

    if isinstance(clasif, dict) and "grupos" in clasif and "asignaciones" in clasif:
        grupos = list(clasif.get("grupos") or [])
        asignaciones = dict(clasif.get("asignaciones") or {})
    else:
        # Formato viejo: todo lo "coincidente" cae en un solo grupo default
        for ot, v in (clasif or {}).items():
            if v == "coincidente":
                asignaciones[ot] = "g1"
            elif v == "sin_coincidencia":
                asignaciones[ot] = "sin_coincidencia"
        if any(v == "g1" for v in asignaciones.values()):
            grupos = [{"id": "g1", "nombre": "", "resumen": "", "solucion": ""}]

    state = {"grupos": grupos, "asignaciones": asignaciones}
    st.session_state[key] = state
    return state


def _serializar(state: dict) -> dict:
    """Convierte session_state → JSONB para persistir."""
    return {
        "grupos":       [dict(g) for g in state["grupos"]],
        "asignaciones": dict(state["asignaciones"]),
    }


def render_detalle(codigo_eds: str, periodo: date):
    top = st.columns([1, 6])
    if top[0].button("← Volver"):
        _volver_a_home()
        st.rerun()

    with top[1]:
        st.title(f"EDS {codigo_eds}")
        st.caption(f"Periodo: {periodo_label(periodo)}")

    df = correctivos_de_eds(codigo_eds, periodo)
    fila = db.get(codigo_eds, periodo) or {}
    ya_validado = bool(fila.get("validado"))

    if df.empty:
        st.warning("No se encontraron correctivos para esta EDS en el periodo.")
        return

    cliente  = df["cliente"].dropna().iloc[0] if not df.empty else ""
    estacion = df["estacion"].dropna().iloc[0] if not df.empty else ""
    st.markdown(f"**{estacion or '—'}** · Cliente: **{cliente or '—'}** · "
                f"Correctivos en el mes: **{len(df)}**")

    if ya_validado:
        _at = fila.get("validado_at")
        _at_txt = pd.to_datetime(_at).strftime("%d/%m/%Y %H:%M") if _at else "—"
        st.success(f"✅ Validado por **{fila.get('validado_por_nombre') or '—'}** el {_at_txt}")

    state = _cargar_estado_edicion(codigo_eds, periodo, fila)
    grupos: list[dict] = state["grupos"]
    asignaciones: dict = state["asignaciones"]

    # Añadir la letra para header display
    for i, g in enumerate(grupos):
        g["letra"] = _letra(i)

    # ── Chips de motivos ────────────────────────────────────────────────
    st.markdown("### Motivos disponibles")
    st.caption("Haz click en un motivo para añadir su columna al análisis. "
               "Click de nuevo para quitarlo (sus OTs vuelven a **Por clasificar**).")

    # Motivo activo = ya tiene un grupo creado
    nombre_a_gid: dict[str, str] = {g["nombre"]: g["id"] for g in grupos if g.get("nombre")}

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
                        # Quitar: eliminar grupo y liberar sus OTs
                        gid = nombre_a_gid[causa]
                        for ot, dest in list(asignaciones.items()):
                            if dest == gid:
                                asignaciones.pop(ot)
                        grupos[:] = [g for g in grupos if g["id"] != gid]
                    else:
                        # Añadir grupo con esa causa
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
        # Vista firmada: mostrar solo los chips activos como referencia
        activos = [g.get("nombre") for g in grupos if g.get("nombre")]
        if activos:
            st.markdown(" ".join(f"`{n}`" for n in activos))

    # Refresco letra por si hubo cambios
    for i, g in enumerate(grupos):
        g["letra"] = _letra(i)

    if not grupos and not ya_validado:
        st.info("Selecciona uno o más motivos arriba para empezar el análisis.")

    # ── Kanban ───────────────────────────────────────────────────────────
    st.markdown("### Clasificación por correctivo")
    if grupos:
        st.caption("Arrastra cada tarjeta al grupo que corresponde, o a **Sin coincidencia** "
                   "si no coincide con ninguna. El detalle del técnico va dentro de la tarjeta.")
    else:
        st.caption("Aún no hay grupos. Crea uno arriba antes de clasificar.")

    items = [
        {"id_ot": r["id_ot"], "label": _tarjeta_label(i + 1, r)}
        for i, (_, r) in enumerate(df.iterrows())
    ]

    resultado = render_kanban(
        items,
        asignaciones,
        grupos,
        read_only=ya_validado,
        key_prefix=f"k_{codigo_eds}_{periodo.isoformat()}",
    )
    if not ya_validado and not resultado.get("unchanged"):
        # Solo persistimos si el sortable devolvió algo distinto — evita
        # loop de re-render con react error #185 cuando default cambia
        # entre reruns con la misma key.
        nuevas_asig = resultado["asignaciones"]
        if nuevas_asig != state["asignaciones"]:
            state["asignaciones"] = nuevas_asig
            asignaciones = state["asignaciones"]

    # ── Resumen + solución por grupo ────────────────────────────────────
    if grupos:
        st.markdown("### Resumen y solución por motivo")
        for i, g in enumerate(grupos):
            g["letra"] = _letra(i)
            titulo = g.get("nombre") or "(sin motivo)"
            n_ots = sum(1 for _, v in asignaciones.items() if v == g["id"])
            st.markdown(f"**{titulo}** — {n_ots} OT(s)")

            c1, c2 = st.columns(2)
            with c1:
                g["resumen"] = st.text_area(
                    "Resumen de la falla",
                    value=g.get("resumen") or "",
                    height=100,
                    disabled=ya_validado,
                    placeholder="¿Qué falla comparten estas OTs?",
                    key=f"gr_{codigo_eds}_{g['id']}",
                )
            with c2:
                g["solucion"] = st.text_area(
                    "Solución propuesta",
                    value=g.get("solucion") or "",
                    height=100,
                    disabled=ya_validado,
                    placeholder="Acción de fondo para este grupo.",
                    key=f"gs_{codigo_eds}_{g['id']}",
                )

            # ── Preguntas de seguimiento ─────────────────────────────────
            _OPC_SOL = ["Sí", "Aún no"]
            _OPC_CTC = ["Sí", "No"]
            _OPC_MED = ["Llamada", "Presencial"]
            _MAP_SOL = {"Sí": "si", "Aún no": "aun_no"}
            _MAP_CTC = {"Sí": "si", "No": "no"}
            _MAP_MED = {"Llamada": "llamada", "Presencial": "presencial"}
            _INV_SOL = {v: k for k, v in _MAP_SOL.items()}
            _INV_CTC = {v: k for k, v in _MAP_CTC.items()}
            _INV_MED = {v: k for k, v in _MAP_MED.items()}

            q1, q2 = st.columns(2)
            with q1:
                _sol_ini = _INV_SOL.get(g.get("soluciono"))
                _sol_idx = _OPC_SOL.index(_sol_ini) if _sol_ini in _OPC_SOL else None
                sol_sel = st.radio(
                    "¿Se solucionó el problema?",
                    options=_OPC_SOL, index=_sol_idx, horizontal=True,
                    disabled=ya_validado,
                    key=f"gsol_{codigo_eds}_{g['id']}",
                )
                g["soluciono"] = _MAP_SOL.get(sol_sel)

            with q2:
                _ctc_ini = _INV_CTC.get(g.get("contacto_estacion"))
                _ctc_idx = _OPC_CTC.index(_ctc_ini) if _ctc_ini in _OPC_CTC else None
                ctc_sel = st.radio(
                    "¿Se contactó a la estación?",
                    options=_OPC_CTC, index=_ctc_idx, horizontal=True,
                    disabled=ya_validado,
                    key=f"gctc_{codigo_eds}_{g['id']}",
                )
                g["contacto_estacion"] = _MAP_CTC.get(ctc_sel)

                # Solo mostrar "¿Por qué medio?" si contacto_estacion == Sí
                if g.get("contacto_estacion") == "si":
                    _med_ini = _INV_MED.get(g.get("medio_contacto"))
                    _med_idx = _OPC_MED.index(_med_ini) if _med_ini in _OPC_MED else None
                    med_sel = st.radio(
                        "¿Por qué medio?",
                        options=_OPC_MED, index=_med_idx, horizontal=True,
                        disabled=ya_validado,
                        key=f"gmed_{codigo_eds}_{g['id']}",
                    )
                    g["medio_contacto"] = _MAP_MED.get(med_sel)
                else:
                    # Si cambió a "No", limpiar el medio
                    g["medio_contacto"] = None

            st.divider()

    # ── Acciones ─────────────────────────────────────────────────────────
    if ya_validado:
        st.info("Esta validación ya fue firmada. Para editarla, contacta a operaciones.")
        return

    a1, a2 = st.columns([1, 1])
    if a1.button("💾 Guardar avance", use_container_width=True):
        ok = db.upsert_clasificaciones(codigo_eds, periodo, _serializar(state))
        if ok:
            st.success("Avance guardado.")
        else:
            st.error("No se pudo guardar. Reintenta.")

    if a2.button("✍️ Validar y firmar", use_container_width=True, type="primary"):
        # Validaciones al firmar
        errores = []
        pendientes = [it["id_ot"] for it in items if it["id_ot"] not in asignaciones]
        if pendientes:
            errores.append(f"Aún quedan {len(pendientes)} OT(s) en **Por clasificar**.")

        # No duplicar causas entre grupos
        nombres_g = [g.get("nombre") for g in grupos if g.get("nombre")]
        dup = {n for n in nombres_g if nombres_g.count(n) > 1}
        if dup:
            errores.append(f"Hay causas repetidas entre grupos: {', '.join(dup)}. "
                           "Cada causa puede usarse una sola vez por EDS.")

        # Grupos: cada uno debe tener ≥1 OT, textos completos y seguimiento respondido
        for g in grupos:
            n_ots = sum(1 for _, v in asignaciones.items() if v == g["id"])
            _titulo = g.get("nombre") or "(sin motivo)"
            if n_ots == 0:
                errores.append(f"**{_titulo}** está vacío — quítalo o asígnale OTs.")
            if not (g.get("resumen") or "").strip() or not (g.get("solucion") or "").strip():
                errores.append(f"**{_titulo}**: completa resumen y solución.")
            if not g.get("soluciono"):
                errores.append(f"**{_titulo}**: responde *¿Se solucionó el problema?*")
            if not g.get("contacto_estacion"):
                errores.append(f"**{_titulo}**: responde *¿Se contactó a la estación?*")
            elif g.get("contacto_estacion") == "si" and not g.get("medio_contacto"):
                errores.append(f"**{_titulo}**: indica el *medio de contacto* (llamada / presencial).")

        if errores:
            for e in errores:
                st.error(e)
        else:
            ok = db.firmar(
                codigo_eds, periodo,
                email=_me["email"], nombre=_me["nombre"],
                clasificaciones=_serializar(state),
                resumen="",   # ahora la info detallada vive por grupo
                solucion="",
            )
            if ok:
                st.success("✅ Validación firmada. Gracias.")
                st.balloons()
                # Limpiar estado de edición para forzar recarga desde DB
                st.session_state.pop(f"edit_{codigo_eds}_{periodo.isoformat()}", None)
                st.rerun()
            else:
                st.error("No se pudo firmar. Reintenta.")


# ═══════════════════════════════════════════════════════════════════════════
# Dispatcher
# ═══════════════════════════════════════════════════════════════════════════

if _view == "detalle":
    render_detalle(st.session_state["eds_detalle"], periodo_sel)
else:
    render_home(periodo_sel, cliente_sel)

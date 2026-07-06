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
from datetime import datetime, timedelta

import pandas as pd
import requests
import streamlit as st

# ══════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="Correctivas · Espejo",
    page_icon="📞",
    layout="wide",
    initial_sidebar_state="collapsed",
)

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

# Estado derivado del cumplimiento + excepción
def estado_ot(row):
    if pd.notna(row.get("excepcion_motivo")) and str(row.get("excepcion_motivo") or "").strip():
        return ("EXCEPCIÓN", "⚪", "#0284c7")
    c = str(row.get("cumplimiento") or "").upper()
    if c == "CUMPLE":     return ("CUMPLE",     "✅", "#16a34a")
    if c == "NO CUMPLE":  return ("NO CUMPLE",  "❌", "#dc2626")
    if c == "PENDIENTE":  return ("EN CURSO",   "🕒", "#f59e0b")
    return ("SIN DATOS", "⏳", "#64748b")


# ══════════════════════════════════════════════════════════════════════
# Supabase client
# ══════════════════════════════════════════════════════════════════════
def _sb_config():
    url = st.secrets.get("SUPABASE_URL", os.getenv("SUPABASE_URL", ""))
    key = st.secrets.get("SUPABASE_KEY", os.getenv("SUPABASE_KEY", ""))
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


@st.cache_data(ttl=300, show_spinner="Cargando llamados correctivos...")
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
                       "fecha_atencion,hora_fin,tecnico,tecnico_corto,"
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

    # 2) Fuentes por OT
    fuente_map = {}
    lc = _sb_get("llamados_correctivos", {
        "select": "os_fracttal,fuente",
        "fecha_llamado": f"gte.{fecha_desde}",
        "limit": 10000,
    })
    for r in lc:
        if r.get("os_fracttal"):
            fuente_map[r["os_fracttal"]] = r.get("fuente")
    df["fuente"] = df["os_fracttal"].map(fuente_map)

    # Fallback: si no hay match directo por os_fracttal (típico en Copec
    # porque su robot no linkea os_fracttal al momento del correo), se
    # infiere la fuente por cliente. Correlación real observada en la
    # tabla llamados_correctivos:
    #   robot_email → 100% Copec
    #   robot_shell → 100% Shell (Enex)
    #   robot_esmax → 100% Esmax (Aramco)
    # Si el cliente no es uno de los 3 que tienen robot, asumimos
    # ot_directa (OT manual/directa en Fracttal sin canal automatizado).
    _cli_to_robot = {
        "COPEC":          "robot_email",
        "SHELL (Enex)":   "robot_shell",
        "ESMAX":          "robot_esmax",
        "Aramco (Esmax)": "robot_esmax",
        "ESMAX (Aramco)": "robot_esmax",
    }
    _fallback = df["cliente"].map(_cli_to_robot).fillna("ot_directa")
    df["fuente"] = df["fuente"].fillna(_fallback)
    # Marca si fue inferida (para explicar en UI si hace falta)
    df["fuente_inferida"] = df["os_fracttal"].map(fuente_map).isna()

    # 3) Normalización
    df["cliente"] = df["cliente"].replace({"ESMAX (Aramco)": "Aramco (Esmax)"})

    # Fechas
    def _ts(x):
        if not x or str(x).strip() in ("", "None", "null"):
            return pd.NaT
        try:
            t = pd.Timestamp(str(x))
            return t.tz_convert(None) if t.tzinfo is not None else t
        except Exception:
            return pd.NaT
    df["fecha_llamado"]  = df["fecha_llamado"].apply(_ts)
    df["fecha_atencion"] = df["fecha_atencion"].apply(_ts)

    # Numéricos seguros
    df["tiempo_resp_horas"] = pd.to_numeric(df["tiempo_resp_horas"], errors="coerce")
    df["tiempo_resp_esp"]   = pd.to_numeric(df["tiempo_resp_esp"],   errors="coerce")

    # Técnico "amigable"
    df["tecnico_disp"] = df["tecnico_corto"].fillna(df["tecnico"])

    # Estado derivado
    df[["estado_lbl","estado_ico","estado_fg"]] = df.apply(
        lambda r: pd.Series(estado_ot(r)), axis=1)

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
st.markdown("# 📞 Correctivas · Espejo")
st.markdown(
    f'<div class="hdr-sub">Fuente: <code>v_llamados_sla</code> (misma vista que el dashboard principal) · '
    f'Enriquecida con <code>fuente</code> desde <code>llamados_correctivos</code> · '
    f'Datos desde <b>{FECHA_CORTE}</b> · Cache 5 min.</div>',
    unsafe_allow_html=True,
)

_c1, _c2 = st.columns([6, 1])
with _c2:
    if st.button("🔄 Recargar", use_container_width=True):
        cargar_llamados.clear()
        st.rerun()

df = cargar_llamados(FECHA_CORTE)
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
        "Fuente", _fuentes, default=_fuentes,
        format_func=lambda f: f"{FUENTE_META.get(f, ('❓','?','',''))[0]} {FUENTE_META.get(f, ('','?','',''))[1]}"
                              if f in FUENTE_META else f,
    )
with _f2:
    _clientes = sorted(df["cliente"].dropna().unique())
    cliente_sel = st.multiselect("Cliente", _clientes, default=_clientes)
with _f3:
    _prios = sorted(df["prioridad"].dropna().unique())
    pri_sel = st.multiselect("Prioridad", _prios, default=_prios)
with _f4:
    _est_opts = ["CUMPLE", "NO CUMPLE", "EN CURSO", "EXCEPCIÓN", "SIN DATOS"]
    est_sel = st.multiselect("Estado / SLA", _est_opts, default=_est_opts)
with _f5:
    buscar = st.text_input(
        "Buscar",
        placeholder="OS-XXXXX · N° aviso · código EDS · nombre · falla · técnico · comuna",
        key="q",
    )

_r1, _r2 = st.columns([1.6, 5])
with _r1:
    _fmax = df["fecha_llamado"].max().date() if pd.notna(df["fecha_llamado"].max()) else datetime.today().date()
    _fmin = df["fecha_llamado"].min().date() if pd.notna(df["fecha_llamado"].min()) else _fmax
    fecha_rng = st.date_input("Rango de fechas", (_fmin, _fmax),
                              min_value=_fmin, max_value=_fmax)

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
if len(fecha_rng) == 2:
    d0, d1 = fecha_rng
    _df = _df[(_df["fecha_llamado"].dt.date >= d0) & (_df["fecha_llamado"].dt.date <= d1)]


# ══════════════════════════════════════════════════════════════════════
# KPIs
# ══════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-hdr">Panorama</div>', unsafe_allow_html=True)

_hoy = pd.Timestamp.now(tz="America/Santiago").tz_localize(None).date()
_semana = _hoy - timedelta(days=7)

_n_tot = len(_df)
_n_hoy = int((_df["fecha_llamado"].dt.date == _hoy).sum())
_n_semana = int((_df["fecha_llamado"].dt.date >= _semana).sum())
_n_cumple = int((_df["estado_lbl"] == "CUMPLE").sum())
_n_nocump = int((_df["estado_lbl"] == "NO CUMPLE").sum())
_n_encur  = int((_df["estado_lbl"] == "EN CURSO").sum())
_evaluadas = _n_cumple + _n_nocump
_pct_cumpl = (_n_cumple / _evaluadas * 100) if _evaluadas else 0

_k1, _k2, _k3, _k4, _k5 = st.columns(5)
_k1.metric("Total (filtrado)", f"{_n_tot:,}")
_k2.metric("Hoy", f"{_n_hoy:,}")
_k3.metric("Últimos 7 días", f"{_n_semana:,}")
_k4.metric("Cumplimiento SLA", f"{_pct_cumpl:.1f}%",
           delta=f"{_n_cumple:,} de {_evaluadas:,}", delta_color="off")
_k5.metric("🕒 En curso", f"{_n_encur:,}",
           delta=f"{_n_nocump} no cumplen" if _n_nocump else "",
           delta_color="inverse" if _n_nocump else "off")

# Distribución por fuente
if _n_tot:
    _dist = _df["fuente"].fillna("(sin fuente)").value_counts()

    # Barra visual con count + % inline
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

    # Resumen numérico agrupado (Robots vs Directa)
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
    if _n_inf:
        st.caption(
            f"Distribución por canal en el filtro actual · "
            f"⚠️ {_n_inf:,} OTs con fuente **inferida por cliente** "
            f"(el robot Copec no linkea os_fracttal al recibir el correo, "
            f"así que se asume: Copec→robot email, Shell→robot Shell, "
            f"Aramco→robot Aramco, resto→directa Fracttal)."
        )
    else:
        st.caption("Distribución por canal de entrada en el filtro actual.")


# ══════════════════════════════════════════════════════════════════════
# Vistas
# ══════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-hdr">Vista</div>', unsafe_allow_html=True)

vista = st.radio("vista", ["📰 Feed cronológico", "📋 Tabla enriquecida"],
                 horizontal=True, label_visibility="collapsed")


# ────────── Feed ──────────
if vista == "📰 Feed cronológico":
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

        _fl = r["fecha_llamado"]
        _fl_s = _fl.strftime("%d/%m %H:%M") if pd.notna(_fl) else "—"
        _fc = r["fecha_atencion"]
        _fc_s = ("cerrada " + _fc.strftime("%d/%m %H:%M")) if pd.notna(_fc) else "abierta"

        _hr = r.get("tiempo_resp_horas")
        _um = r.get("tiempo_resp_esp")
        _hr_s = ""
        if pd.notna(_hr):
            _u = f"{int(_um)}h" if pd.notna(_um) else "?h"
            _hr_s = f" · <b>{_hr:.1f}h</b> resp. / SLA {_u}"

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
            f'<span class="badge est" style="--e-fg:{_e_fg}">{_e_ico} {_e_lbl}</span>'
            f'</div>'
            f'</div>'
            f'<div class="eds">{_eds} · {_nom}</div>'
            f'<div class="cli">{_cli} · {_cm} ({_zn}) · Equipo: {_eq} · Téc: {_tec}</div>'
            f'{_exc_html}'
            f'<div class="meta">📅 {_fl_s} · {_fc_s}{_hr_s}</div>'
            f'</div>'
        )

    st.markdown("".join(_card(r) for _, r in _dff.iterrows()), unsafe_allow_html=True)


# ────────── Tabla ──────────
else:
    _dft = _df.copy()
    _dft["Fuente"] = _dft["fuente"].map(
        lambda f: (f"{FUENTE_META.get(f, ('❓','?','',''))[0]} "
                   f"{FUENTE_META.get(f, ('','?','',''))[1]}")
                  if f in FUENTE_META else "❓ (sin fuente)")
    _dft["Estado"] = _dft["estado_ico"] + " " + _dft["estado_lbl"]
    _dft["F. Llamado"] = _dft["fecha_llamado"].dt.strftime("%d/%m/%Y %H:%M")
    _dft["F. Cierre"]  = _dft["fecha_atencion"].dt.strftime("%d/%m/%Y %H:%M").fillna("—")
    _dft["Horas resp."]= _dft["tiempo_resp_horas"].round(2)
    _dft["SLA (h)"]    = _dft["tiempo_resp_esp"]
    _dft["Excepción"]  = _dft["excepcion_motivo"].fillna("")

    _cols = ["os_fracttal","n_llamado","cliente","eds_occim","eds_nombre",
             "comuna","zona","prioridad","Fuente","Estado",
             "F. Llamado","F. Cierre","Horas resp.","SLA (h)",
             "equipo","tecnico_disp","Excepción","facturacion"]
    _ren = {
        "os_fracttal":"OS Fracttal", "n_llamado":"N° Aviso",
        "cliente":"Cliente", "eds_occim":"Cód. EDS", "eds_nombre":"EDS",
        "comuna":"Comuna", "zona":"Zona", "prioridad":"Prioridad",
        "equipo":"Equipo", "tecnico_disp":"Técnico", "facturacion":"Facturación",
    }
    _show = _dft[_cols].rename(columns=_ren).sort_values("F. Llamado", ascending=False)

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
            "Estado":      st.column_config.TextColumn(width=115),
            "F. Llamado":  st.column_config.TextColumn(width=125),
            "F. Cierre":   st.column_config.TextColumn(width=125),
            "Horas resp.": st.column_config.NumberColumn(width=90, format="%.2f"),
            "SLA (h)":     st.column_config.NumberColumn(width=70),
            "Equipo":      st.column_config.TextColumn(width=85),
            "Técnico":     st.column_config.TextColumn(width=140),
            "Excepción":   st.column_config.TextColumn(width=200),
            "Facturación": st.column_config.TextColumn(width=115),
        },
    )

    _csv = _show.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ Descargar CSV (filtro actual)", _csv,
        file_name=f"correctivas_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )


# Footer
st.divider()
st.caption(
    f"Fuente: Supabase · vista `v_llamados_sla` desde {FECHA_CORTE} · "
    f"Fuente (robot/directa) desde `llamados_correctivos` · Cache 5 min · "
    f"Última consulta: {datetime.now().strftime('%H:%M:%S')}"
)

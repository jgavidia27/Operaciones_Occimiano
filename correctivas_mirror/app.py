"""
Correctivas Mirror — Espejo amigable de OTs correctivas de Supabase.
====================================================================

Sitio web ligero, complementario al dashboard principal, para MONITOREAR
en vivo lo que los 3 robots (Copec / Aramco-Esmax / Shell) están
enviando a Fracttal + Supabase, además de las OTs directas.

NO reemplaza al dashboard: es un espejo de lectura, más ameno.

Datos: tabla `llamados_correctivos` de Supabase, desde 2026-05-01.
Vistas: Feed cronológico (tarjetas) + Tabla enriquecida (filtros/export).

Deploy: Streamlit Cloud → apuntar a correctivas_mirror/app.py.
Secrets requeridos: SUPABASE_URL, SUPABASE_KEY.
"""

import os
from datetime import date, datetime, timedelta

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
    "P1":  ("#dc2626", "#fee2e2", "P1 · Crítico"),
    "P2":  ("#ea580c", "#ffedd5", "P2 · Alto"),
    "P3":  ("#ca8a04", "#fef9c3", "P3 · Medio"),
    "P4":  ("#0284c7", "#e0f2fe", "P4 · Bajo"),
    "P5":  ("#64748b", "#f1f5f9", "P5 · Info"),
    None:  ("#64748b", "#f1f5f9", "Sin prioridad"),
}

# Fuente → icono + label + color
FUENTE_META = {
    "robot_esmax":  ("🤖", "Robot Aramco",  "#7c3aed", "#ede9fe"),
    "robot_shell":  ("🤖", "Robot Shell",   "#c026d3", "#fae8ff"),
    "robot_email":  ("🤖", "Robot Copec",   "#2563eb", "#dbeafe"),
    "ot_directa":   ("📞", "Directa Fracttal", "#475569", "#f1f5f9"),
}

CUMPL_STYLE = {
    "CUMPLE":       ("✅", "#16a34a"),
    "NO CUMPLE":    ("❌", "#dc2626"),
    "EXCEPCIÓN":    ("⚪", "#0284c7"),
    None:           ("⏳", "#64748b"),
}


# ══════════════════════════════════════════════════════════════════════
# Supabase client
# ══════════════════════════════════════════════════════════════════════
def _sb_config():
    """Lee credenciales de secrets (deploy) o env vars (local)."""
    url = st.secrets.get("SUPABASE_URL", os.getenv("SUPABASE_URL", ""))
    key = st.secrets.get("SUPABASE_KEY", os.getenv("SUPABASE_KEY", ""))
    if not url or not key:
        st.error(
            "Faltan credenciales de Supabase. Configura los secrets "
            "**SUPABASE_URL** y **SUPABASE_KEY** en Streamlit Cloud "
            "(Settings → Secrets)."
        )
        st.stop()
    return url, key


@st.cache_data(ttl=300, show_spinner="Cargando llamados correctivos...")
def cargar_llamados(fecha_desde: str) -> pd.DataFrame:
    """Trae todos los llamados desde `fecha_desde` (paginado)."""
    url, key = _sb_config()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    rows = []
    page = 0
    while True:
        r = requests.get(
            f"{url}/rest/v1/llamados_correctivos",
            params={
                "select": ("id,os_fracttal,n_aviso,cliente,eds_codigo,"
                           "eds_nombre,fecha_llamado,prioridad,equipo,"
                           "falla,tecnico,fecha_cierre,cumplimiento,"
                           "horas_respuesta,fuente,umbral_horas,created_at"),
                "fecha_llamado": f"gte.{fecha_desde}",
                "order": "fecha_llamado.desc",
                "limit": 1000,
                "offset": page * 1000,
            },
            headers=headers,
            timeout=25,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        page += 1
        if page > 30:   # tope de seguridad (30k filas)
            break
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["fecha_llamado"] = pd.to_datetime(df["fecha_llamado"], errors="coerce", utc=True)
    df["fecha_cierre"] = pd.to_datetime(df["fecha_cierre"], errors="coerce", utc=True)
    df["fecha_llamado_local"] = df["fecha_llamado"].dt.tz_convert("America/Santiago")
    df["fecha_cierre_local"] = df["fecha_cierre"].dt.tz_convert("America/Santiago")
    return df


# ══════════════════════════════════════════════════════════════════════
# Estilos
# ══════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
.block-container {padding-top: 1.5rem; padding-bottom: 3rem; max-width: 1400px;}
h1 {font-size: 1.7rem !important; margin-bottom: .3rem !important;}
.hdr-sub {color: #64748b; font-size: .92rem; margin-bottom: 1rem;}

.card {
    background: #fff; border: 1px solid #e2e8f0; border-radius: 10px;
    padding: 14px 16px; margin-bottom: 10px;
    border-left: 4px solid var(--pri, #64748b);
    transition: transform .1s ease, box-shadow .1s ease;
}
.card:hover {transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,.06);}
.card .top {display:flex; justify-content:space-between; align-items:flex-start; gap:12px;}
.card .os {font-weight:700; font-size:1.02rem; color:#0f172a;}
.card .aviso {color:#64748b; font-weight:500; font-size:.78rem; margin-left:6px;}
.card .eds {color:#334155; font-size:.88rem; margin-top:2px;}
.card .cli {color:#64748b; font-size:.78rem; margin-top:2px;}
.card .meta {color:#94a3b8; font-size:.72rem; margin-top:6px;}
.card .falla {color:#334155; font-size:.82rem; margin-top:6px; font-style:italic;}

.badge {
    display:inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: .68rem; font-weight: 700; margin-right: 4px;
    letter-spacing: .02em;
}
.badge.fuente {background: var(--f-bg); color: var(--f-fg);}
.badge.pri {background: var(--p-bg); color: var(--p-fg); border: 1px solid var(--p-fg);}
.badge.cumpl {background:#fff; color: var(--c-fg); border:1px solid var(--c-fg);}

.section-hdr {
    font-weight: 700; color: #475569; font-size: .78rem;
    text-transform: uppercase; letter-spacing: .05em;
    margin: 18px 0 8px 0; padding-bottom: 4px;
    border-bottom: 1px solid #e2e8f0;
}
[data-testid="stMetricValue"] {font-size: 1.6rem;}
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════
# Header
# ══════════════════════════════════════════════════════════════════════
st.markdown("# 📞 Correctivas · Espejo")
st.markdown(
    '<div class="hdr-sub">Vista en vivo de llamados correctivos desde Supabase · '
    'Alimentado por robots Copec / Aramco / Shell + OTs directas · '
    f'Datos desde <b>{FECHA_CORTE}</b> · Actualiza cada 5 min.</div>',
    unsafe_allow_html=True,
)

# Botón recargar
_c1, _c2 = st.columns([6, 1])
with _c2:
    if st.button("🔄 Recargar", use_container_width=True):
        cargar_llamados.clear()
        st.rerun()

# Datos
df = cargar_llamados(FECHA_CORTE)
if df.empty:
    st.warning("No hay datos en Supabase para el período configurado.")
    st.stop()


# ══════════════════════════════════════════════════════════════════════
# Filtros
# ══════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-hdr">Filtros</div>', unsafe_allow_html=True)

_f1, _f2, _f3, _f4, _f5 = st.columns([1.3, 1.3, 1.1, 1.1, 2])

with _f1:
    _fuentes = sorted(df["fuente"].dropna().unique())
    fuente_sel = st.multiselect(
        "Fuente",
        _fuentes,
        default=_fuentes,
        format_func=lambda f: f"{FUENTE_META.get(f, ('❓','?','',''))[0]} {FUENTE_META.get(f, ('','?','',''))[1]}",
    )
with _f2:
    _clientes = sorted(df["cliente"].dropna().unique())
    cliente_sel = st.multiselect("Cliente", _clientes, default=_clientes)
with _f3:
    _prios = sorted(df["prioridad"].dropna().unique())
    pri_sel = st.multiselect("Prioridad", _prios, default=_prios)
with _f4:
    _cumpl_opts = ["CUMPLE", "NO CUMPLE", "EXCEPCIÓN", "Sin evaluar"]
    cumpl_sel = st.multiselect("Cumplimiento", _cumpl_opts, default=_cumpl_opts)
with _f5:
    buscar = st.text_input(
        "Buscar",
        placeholder="OS-XXXXX · Nº aviso · código EDS · nombre EDS · falla · técnico",
        key="q",
    )

_r1, _r2 = st.columns([1.5, 5])
with _r1:
    _fecha_max = df["fecha_llamado_local"].max().date()
    _fecha_min = df["fecha_llamado_local"].min().date()
    fecha_rng = st.date_input(
        "Rango de fechas",
        (_fecha_min, _fecha_max),
        min_value=_fecha_min,
        max_value=_fecha_max,
    )

# Aplicar filtros
_df = df.copy()
if fuente_sel:
    _df = _df[_df["fuente"].isin(fuente_sel)]
if cliente_sel:
    _df = _df[_df["cliente"].isin(cliente_sel)]
if pri_sel:
    _df = _df[_df["prioridad"].isin(pri_sel)]

def _cumpl_bucket(v):
    if v in ("CUMPLE", "NO CUMPLE", "EXCEPCIÓN"):
        return v
    return "Sin evaluar"
_df["_cumpl_b"] = _df["cumplimiento"].apply(_cumpl_bucket)
if cumpl_sel:
    _df = _df[_df["_cumpl_b"].isin(cumpl_sel)]

if buscar and buscar.strip():
    q = buscar.strip().upper()
    _df = _df[
        _df["os_fracttal"].astype(str).str.upper().str.contains(q, na=False)
        | _df["n_aviso"].astype(str).str.upper().str.contains(q, na=False)
        | _df["eds_codigo"].astype(str).str.upper().str.contains(q, na=False)
        | _df["eds_nombre"].astype(str).str.upper().str.contains(q, na=False)
        | _df["falla"].astype(str).str.upper().str.contains(q, na=False)
        | _df["tecnico"].astype(str).str.upper().str.contains(q, na=False)
    ]

if len(fecha_rng) == 2:
    d0, d1 = fecha_rng
    _df = _df[
        (_df["fecha_llamado_local"].dt.date >= d0)
        & (_df["fecha_llamado_local"].dt.date <= d1)
    ]


# ══════════════════════════════════════════════════════════════════════
# KPIs
# ══════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-hdr">Panorama</div>', unsafe_allow_html=True)

_hoy = pd.Timestamp.now(tz="America/Santiago").date()
_ayer = _hoy - timedelta(days=1)
_semana = _hoy - timedelta(days=7)

_n_tot = len(_df)
_n_hoy = int((_df["fecha_llamado_local"].dt.date == _hoy).sum())
_n_semana = int((_df["fecha_llamado_local"].dt.date >= _semana).sum())
_n_cumple = int((_df["cumplimiento"] == "CUMPLE").sum())
_n_nocump = int((_df["cumplimiento"] == "NO CUMPLE").sum())
_evaluadas = _n_cumple + _n_nocump
_pct_cumpl = (_n_cumple / _evaluadas * 100) if _evaluadas else 0

_k1, _k2, _k3, _k4, _k5 = st.columns(5)
_k1.metric("Total (filtrado)", f"{_n_tot:,}")
_k2.metric("Hoy", f"{_n_hoy:,}")
_k3.metric("Últimos 7 días", f"{_n_semana:,}")
_k4.metric("Cumplimiento SLA", f"{_pct_cumpl:.1f}%",
           delta=f"{_n_cumple:,} de {_evaluadas:,} evaluadas",
           delta_color="off")
_k5.metric("No cumplen",
           f"{_n_nocump:,}",
           delta="requieren revisión" if _n_nocump else "",
           delta_color="inverse" if _n_nocump else "off")

# Distribución por fuente (mini-barra HTML)
if _n_tot:
    _dist = _df["fuente"].value_counts()
    _bar_html = '<div style="display:flex;gap:4px;margin-top:14px;height:34px;overflow:hidden;border-radius:6px;">'
    for _f, _n in _dist.items():
        _meta = FUENTE_META.get(_f, ("❓", "?", "#64748b", "#f1f5f9"))
        _pct = _n / _n_tot * 100
        _bar_html += (
            f'<div style="flex:{_n};background:{_meta[2]};color:#fff;'
            f'display:flex;align-items:center;justify-content:center;'
            f'font-size:.75rem;font-weight:600;min-width:60px" '
            f'title="{_meta[1]}: {_n:,} ({_pct:.0f}%)">{_meta[0]} {_n:,}</div>'
        )
    _bar_html += "</div>"
    st.markdown(_bar_html, unsafe_allow_html=True)
    st.caption("Distribución por fuente en el filtro actual — hover para detalle.")


# ══════════════════════════════════════════════════════════════════════
# Vistas: Feed / Tabla
# ══════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-hdr">Vista</div>', unsafe_allow_html=True)

vista = st.radio(
    "vista",
    ["📰 Feed cronológico", "📋 Tabla enriquecida"],
    horizontal=True,
    label_visibility="collapsed",
)

# ────────── Feed ──────────
if vista == "📰 Feed cronológico":
    _lim_col1, _lim_col2 = st.columns([1, 5])
    with _lim_col1:
        _lim = st.selectbox("Mostrar", [50, 100, 250, 500, "Todo"], index=1, key="feed_lim")
    _dff = _df.sort_values("fecha_llamado_local", ascending=False)
    if _lim != "Todo":
        _dff = _dff.head(int(_lim))

    st.caption(
        f"Mostrando **{len(_dff):,}** de {_n_tot:,} llamados "
        f"(orden: más recientes primero)."
    )

    def _card(r):
        _p = (r["prioridad"] or "").upper() or None
        _p_fg, _p_bg, _p_lbl = PRI_STYLE.get(_p, PRI_STYLE[None])
        _f_ico, _f_lbl, _f_fg, _f_bg = FUENTE_META.get(
            r["fuente"], ("❓", r["fuente"] or "?", "#64748b", "#f1f5f9")
        )
        _c_ico, _c_fg = CUMPL_STYLE.get(r["cumplimiento"], CUMPL_STYLE[None])
        _c_lbl = r["cumplimiento"] or "Pendiente"

        _fl = r["fecha_llamado_local"]
        _fl_s = _fl.strftime("%d/%m %H:%M") if pd.notna(_fl) else "—"

        _fc = r["fecha_cierre_local"]
        _fc_s = ("cerrada " + _fc.strftime("%d/%m %H:%M")) if pd.notna(_fc) else "abierta"

        _hr = r.get("horas_respuesta")
        _hr_s = ""
        if pd.notna(_hr):
            _hr_s = f" · <b>{_hr:.1f}h</b> resp. / SLA {r.get('umbral_horas','?')}h"

        return (
            f'<div class="card" style="--pri:{_p_fg}">'
            f'<div class="top">'
            f'<div>'
            f'<span class="os">{r["os_fracttal"] or "—"}</span>'
            f'<span class="aviso">· Aviso {r["n_aviso"] or "—"}</span>'
            f'</div>'
            f'<div>'
            f'<span class="badge fuente" style="--f-bg:{_f_bg};--f-fg:{_f_fg}">{_f_ico} {_f_lbl}</span>'
            f'<span class="badge pri" style="--p-bg:{_p_bg};--p-fg:{_p_fg}">{_p_lbl}</span>'
            f'<span class="badge cumpl" style="--c-fg:{_c_fg}">{_c_ico} {_c_lbl}</span>'
            f'</div>'
            f'</div>'
            f'<div class="eds">{r["eds_codigo"] or "—"} · {r["eds_nombre"] or "—"}</div>'
            f'<div class="cli">{r["cliente"] or "—"} · {r["equipo"] or "—"} · Téc: {r["tecnico"] or "—"}</div>'
            f'<div class="falla">"{r["falla"] or "sin descripción"}"</div>'
            f'<div class="meta">📅 {_fl_s} · {_fc_s}{_hr_s}</div>'
            f'</div>'
        )

    st.markdown(
        "".join(_card(r) for _, r in _dff.iterrows()),
        unsafe_allow_html=True,
    )


# ────────── Tabla ──────────
else:
    _dft = _df.copy()
    _dft["Fuente"] = _dft["fuente"].map(
        lambda f: f"{FUENTE_META.get(f, ('❓','?','',''))[0]} "
                  f"{FUENTE_META.get(f, ('','?','',''))[1]}"
    )
    _dft["F. Llamado"] = _dft["fecha_llamado_local"].dt.strftime("%d/%m/%Y %H:%M")
    _dft["F. Cierre"] = _dft["fecha_cierre_local"].dt.strftime("%d/%m/%Y %H:%M")
    _dft["Horas resp."] = _dft["horas_respuesta"].round(2)
    _dft["SLA (h)"] = _dft["umbral_horas"]
    _dft["Cumplimiento"] = _dft["cumplimiento"].fillna("⏳ Pendiente")

    _cols = [
        "os_fracttal", "n_aviso", "cliente", "eds_codigo", "eds_nombre",
        "prioridad", "Fuente", "F. Llamado", "F. Cierre",
        "Horas resp.", "SLA (h)", "Cumplimiento",
        "equipo", "tecnico", "falla",
    ]
    _renames = {
        "os_fracttal": "OS Fracttal",
        "n_aviso": "N° Aviso",
        "cliente": "Cliente",
        "eds_codigo": "Cód. EDS",
        "eds_nombre": "EDS",
        "prioridad": "Prioridad",
        "equipo": "Equipo",
        "tecnico": "Técnico",
        "falla": "Falla",
    }
    _show = _dft[_cols].rename(columns=_renames).sort_values(
        "F. Llamado", ascending=False
    )

    st.dataframe(
        _show,
        hide_index=True,
        use_container_width=True,
        height=680,
        column_config={
            "OS Fracttal": st.column_config.TextColumn(width=110),
            "N° Aviso":    st.column_config.TextColumn(width=90),
            "Cliente":     st.column_config.TextColumn(width=140),
            "Cód. EDS":    st.column_config.TextColumn(width=90),
            "EDS":         st.column_config.TextColumn(width=200),
            "Prioridad":   st.column_config.TextColumn(width=75),
            "Fuente":      st.column_config.TextColumn(width=150),
            "F. Llamado":  st.column_config.TextColumn(width=130),
            "F. Cierre":   st.column_config.TextColumn(width=130),
            "Horas resp.": st.column_config.NumberColumn(width=90, format="%.2f"),
            "SLA (h)":     st.column_config.NumberColumn(width=70),
            "Cumplimiento":st.column_config.TextColumn(width=115),
            "Equipo":      st.column_config.TextColumn(width=90),
            "Técnico":     st.column_config.TextColumn(width=140),
            "Falla":       st.column_config.TextColumn(width=280),
        },
    )

    # Export CSV
    _csv = _show.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ Descargar CSV (filtro actual)",
        _csv,
        file_name=f"correctivas_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )

# ══════════════════════════════════════════════════════════════════════
# Footer
# ══════════════════════════════════════════════════════════════════════
st.divider()
st.caption(
    f"Fuente: Supabase · tabla `llamados_correctivos` desde {FECHA_CORTE} · "
    f"Cache 5 min · Última consulta: {datetime.now().strftime('%H:%M:%S')}"
)

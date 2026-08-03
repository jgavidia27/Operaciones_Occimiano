"""
reincidencias.py — Query de correctivos y agrupación por EDS reincidente.

Se apoya en supabase_client._query del dashboard principal. El sys.path se
extiende hacia la carpeta padre para permitir el import cuando el entry
point de Streamlit Cloud es sto_panel/app.py.
"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from supabase_client import _query  # noqa: E402

from periodo import periodo_rango  # noqa: E402


@st.cache_data(ttl=600, show_spinner=False)
def _correctivas_mes(periodo_iso: str) -> pd.DataFrame:
    """Todos los correctivos creados en el mes. Cache 10 min por periodo."""
    p = date.fromisoformat(periodo_iso)
    ini, fin = periodo_rango(p)
    rows = _query(
        "ordenes_trabajo",
        f"select=id_ot,codigo_eds,cliente,estacion,responsable,fecha_creacion,"
        f"causa_raiz,tipo_falla,comentario_tecnico,prioridad,prioridad_calc"
        f"&tipo_tarea=ilike.*CORRECTIV*"
        f"&fecha_creacion=gte.{ini.isoformat()}"
        f"&fecha_creacion=lt.{fin.isoformat()}"
        f"&codigo_eds=not.is.null"
        f"&order=fecha_creacion.asc",
        limit=20_000,
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["fecha_creacion"] = pd.to_datetime(
        df["fecha_creacion"], errors="coerce", utc=True
    ).dt.tz_convert(None)
    # Deduplicar por id_ot: ordenes_trabajo puede repetir el id_ot si tiene
    # múltiples activos (codigo_activo="EQ-001, EQ-002"). Para conteo de
    # llamados a la EDS, cuenta 1 vez.
    df = df.drop_duplicates(subset=["id_ot"]).reset_index(drop=True)
    return df


def eds_reincidentes(periodo: date, cliente: str | None = None) -> pd.DataFrame:
    """EDS con ≥3 correctivos en el mes. Ordena desc por N° de llamados."""
    df = _correctivas_mes(periodo.isoformat())
    if df.empty:
        return pd.DataFrame(columns=["codigo_eds", "cliente", "estacion", "n_llamados"])
    if cliente and cliente != "Todos":
        df = df[df["cliente"] == cliente]
    g = (df.groupby(["codigo_eds", "cliente", "estacion"], dropna=False)
           .size().reset_index(name="n_llamados"))
    g = g[g["n_llamados"] >= 3].sort_values(
        "n_llamados", ascending=False
    ).reset_index(drop=True)
    return g


def correctivos_de_eds(codigo_eds: str, periodo: date) -> pd.DataFrame:
    df = _correctivas_mes(periodo.isoformat())
    if df.empty:
        return df
    return (df[df["codigo_eds"] == codigo_eds]
            .sort_values("fecha_creacion")
            .reset_index(drop=True))


def clientes_del_periodo(periodo: date) -> list[str]:
    df = _correctivas_mes(periodo.isoformat())
    if df.empty:
        return []
    return sorted(df["cliente"].dropna().unique().tolist())

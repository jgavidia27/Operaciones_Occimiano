"""
reincidencias.py — Detección de reincidencias por ventana móvil.

Regla: una EDS es reincidente si tiene 3+ llamados correctivos dentro de una
ventana móvil de N días (default 20). Se detectan disparos disjuntos: una vez
que se dispara y se cierra la ventana, el próximo disparo requiere otros 3
llamados nuevos en su propia ventana.

El "periodo mensual" ya no existe como concepto — se reemplaza por
`fecha_disparo` (la fecha del 3er llamado que dispara la reincidencia).
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from supabase_client import _query  # noqa: E402


DEFAULT_VENTANA_DIAS = 20
DEFAULT_RANGO_DIAS   = 60


@st.cache_data(ttl=3600, show_spinner=False)
def _mapa_comunas() -> dict[str, str]:
    rows = _query("estaciones_servicio", "select=eds_occim,comuna", limit=5000)
    return {r["eds_occim"]: (r.get("comuna") or "") for r in rows if r.get("eds_occim")}


@st.cache_data(ttl=600, show_spinner=False)
def _correctivos_rango(desde_iso: str, hasta_iso: str) -> pd.DataFrame:
    """Todos los correctivos entre [desde, hasta). Cache 10 min por rango."""
    rows = _query(
        "ordenes_trabajo",
        f"select=id_ot,codigo_eds,cliente,estacion,responsable,fecha_creacion,"
        f"causa_raiz,tipo_falla,comentario_tecnico,prioridad,prioridad_calc"
        f"&tipo_tarea=ilike.*CORRECTIV*"
        f"&fecha_creacion=gte.{desde_iso}"
        f"&fecha_creacion=lt.{hasta_iso}"
        f"&codigo_eds=not.is.null"
        f"&order=fecha_creacion.asc",
        limit=30_000,
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["fecha_creacion"] = pd.to_datetime(
        df["fecha_creacion"], errors="coerce", utc=True
    ).dt.tz_convert(None)
    df = df.drop_duplicates(subset=["id_ot"]).reset_index(drop=True)
    return df


# ── Detección de disparos ─────────────────────────────────────────────────

def detectar_disparos(fechas_asc: list[pd.Timestamp],
                      ventana_dias: int = DEFAULT_VENTANA_DIAS
                      ) -> list[tuple[pd.Timestamp, tuple[int, int]]]:
    """Detecta todos los disparos DISJUNTOS de reincidencia.

    Un disparo = 3+ llamados dentro de una ventana móvil de `ventana_dias`.
    Se define por el 3er llamado (fecha_disparo). Una vez disparado, la
    ventana se extiende mientras siga cabiendo un 4°, 5°, etc. dentro de
    ventana_dias desde el 1er llamado del grupo. Al cerrar, el próximo
    disparo empieza desde el siguiente llamado.

    Retorna lista de (fecha_disparo, (i_inicio, i_fin)) donde los índices
    apuntan a la lista original.
    """
    disparos = []
    n = len(fechas_asc)
    i = 0
    while i <= n - 3:
        span = (fechas_asc[i + 2] - fechas_asc[i]).days
        if span <= ventana_dias:
            j = i + 2
            while j + 1 < n and (fechas_asc[j + 1] - fechas_asc[i]).days <= ventana_dias:
                j += 1
            disparos.append((fechas_asc[i + 2], (i, j)))
            i = j + 1
        else:
            i += 1
    return disparos


# ── API pública ───────────────────────────────────────────────────────────

def eds_con_reincidencia(rango_dias: int = DEFAULT_RANGO_DIAS,
                         cliente: str | None = None,
                         ventana_dias: int = DEFAULT_VENTANA_DIAS,
                         hoy: date | None = None) -> pd.DataFrame:
    """Retorna una fila por cada disparo de reincidencia dentro del rango.

    Columnas: codigo_eds, cliente, estacion, comuna, fecha_disparo,
              n_llamados_ventana, primer_llamado, ultimo_llamado.
    Orden: fecha_disparo desc.
    """
    hoy = hoy or date.today()
    hasta = hoy + timedelta(days=1)                # exclusivo (incluye hoy)
    # Amortiguar `ventana_dias` extra hacia atrás para captar ventanas que
    # empiezan antes del rango pero cuyo 3er llamado cae dentro del rango.
    desde = hoy - timedelta(days=rango_dias + ventana_dias)

    df = _correctivos_rango(desde.isoformat(), hasta.isoformat())
    if df.empty:
        return pd.DataFrame(columns=[
            "codigo_eds", "cliente", "estacion", "comuna",
            "fecha_disparo", "n_llamados_ventana",
            "primer_llamado", "ultimo_llamado",
        ])
    if cliente and cliente != "Todos":
        df = df[df["cliente"] == cliente]

    comunas = _mapa_comunas()
    filas: list[dict] = []
    fecha_min_disparo = pd.Timestamp(hoy - timedelta(days=rango_dias))

    for cod_eds, g in df.groupby("codigo_eds", dropna=True):
        g = g.sort_values("fecha_creacion").reset_index(drop=True)
        fechas = list(g["fecha_creacion"])
        for f_disp, (i0, i1) in detectar_disparos(fechas, ventana_dias):
            if f_disp < fecha_min_disparo:
                continue  # el disparo cayó fuera del rango pedido
            filas.append({
                "codigo_eds":         cod_eds,
                "cliente":            g["cliente"].iloc[0],
                "estacion":           g["estacion"].iloc[0],
                "comuna":             comunas.get(cod_eds, ""),
                "fecha_disparo":      f_disp.date(),
                "n_llamados_ventana": i1 - i0 + 1,
                "primer_llamado":     fechas[i0].date(),
                "ultimo_llamado":     fechas[i1].date(),
            })

    if not filas:
        return pd.DataFrame(columns=[
            "codigo_eds", "cliente", "estacion", "comuna",
            "fecha_disparo", "n_llamados_ventana",
            "primer_llamado", "ultimo_llamado",
        ])

    out = pd.DataFrame(filas).sort_values(
        "fecha_disparo", ascending=False
    ).reset_index(drop=True)
    return out


def correctivos_de_ventana(codigo_eds: str, fecha_disparo: date,
                           ventana_dias: int = DEFAULT_VENTANA_DIAS) -> pd.DataFrame:
    """Devuelve los correctivos que integraron la ventana asociada al disparo.

    La ventana se define como [fecha_disparo - ventana_dias, fecha_disparo + ventana_dias]
    y se recorta al bloque de llamados consecutivos que caen dentro de esa franja
    en torno al disparo (mismo criterio de detección).
    """
    # Traer un rango amplio (2×ventana) alrededor del disparo para cubrir el bloque
    desde = fecha_disparo - timedelta(days=ventana_dias)
    hasta = fecha_disparo + timedelta(days=ventana_dias + 1)
    rows = _query(
        "ordenes_trabajo",
        f"select=id_ot,codigo_eds,cliente,estacion,responsable,fecha_creacion,"
        f"causa_raiz,tipo_falla,comentario_tecnico,prioridad,prioridad_calc"
        f"&tipo_tarea=ilike.*CORRECTIV*"
        f"&codigo_eds=eq.{codigo_eds}"
        f"&fecha_creacion=gte.{desde.isoformat()}"
        f"&fecha_creacion=lt.{hasta.isoformat()}"
        f"&order=fecha_creacion.asc",
        limit=1000,
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates(subset=["id_ot"]).reset_index(drop=True)
    df["fecha_creacion"] = pd.to_datetime(
        df["fecha_creacion"], errors="coerce", utc=True
    ).dt.tz_convert(None)

    # Reconstruir la ventana exacta: reproducir el algoritmo de detección y
    # quedarnos con el bloque cuyo 3er llamado coincide con fecha_disparo.
    fechas = list(df["fecha_creacion"])
    for f_disp, (i0, i1) in detectar_disparos(fechas, ventana_dias):
        if f_disp.date() == fecha_disparo:
            return df.iloc[i0:i1 + 1].reset_index(drop=True)
    # Fallback: si por alguna razón no matchea, devolver el rango completo
    return df


def clientes_en_rango(rango_dias: int = DEFAULT_RANGO_DIAS,
                      hoy: date | None = None) -> list[str]:
    hoy = hoy or date.today()
    hasta = hoy + timedelta(days=1)
    desde = hoy - timedelta(days=rango_dias + DEFAULT_VENTANA_DIAS)
    df = _correctivos_rango(desde.isoformat(), hasta.isoformat())
    if df.empty:
        return []
    return sorted(df["cliente"].dropna().unique().tolist())


@st.cache_data(ttl=300, show_spinner=False)
def top_eds_ultimos_dias(top_n: int = 10, dias: int = 30) -> pd.DataFrame:
    """Top N EDS con más correctivos en los últimos `dias` días (cache 5 min)."""
    hoy = date.today()
    ini = hoy - timedelta(days=dias)
    hasta = hoy + timedelta(days=1)
    df = _correctivos_rango(ini.isoformat(), hasta.isoformat())
    if df.empty:
        return pd.DataFrame(columns=["codigo_eds", "cliente", "estacion", "comuna", "n_llamados"])
    g = (df.groupby(["codigo_eds", "cliente", "estacion"], dropna=False)
           .size().reset_index(name="n_llamados"))
    comunas = _mapa_comunas()
    g["comuna"] = g["codigo_eds"].map(comunas).fillna("")
    g = g[["codigo_eds", "cliente", "estacion", "comuna", "n_llamados"]]
    return g.sort_values("n_llamados", ascending=False).head(top_n).reset_index(drop=True)

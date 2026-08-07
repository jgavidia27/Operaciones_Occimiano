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


# ── Detección de casos ────────────────────────────────────────────────────

def detectar_casos(fechas_asc: list[pd.Timestamp],
                   firmas_iso: dict[str, str] | set[str],
                   ventana_dias: int = DEFAULT_VENTANA_DIAS) -> list[dict]:
    """Detecta los casos de reincidencia de una EDS con la regla real:

    - **Sin caso activo:** los llamados se acumulan; cuando llegan 3 dentro
      de una ventana de 20 días desde el 1º, se **ABRE UN CASO** con
      fecha_disparo = fecha del 3er llamado.
    - **Caso abierto (no firmado):** TODOS los llamados posteriores suman
      al caso, sin importar los días.
    - **Caso firmado (cerrado):** se cierra con los llamados que caían
      dentro de la ventana de 20 días desde su 1er llamado. El próximo
      llamado, si está dentro de 20 días desde el último del caso cerrado,
      abre un **caso crítico** (🔴 reincidente); si excede 20 días, la EDS
      queda "limpia" y el ciclo empieza de cero.

    Retorna lista de casos:
      {fecha_disparo (date), primer_llamado (date), ultimo_llamado (date),
       n_llamados (int), cerrado (bool), critico (bool),
       indices (tuple[int,int])}
    """
    # Normalizar: acepta set{iso} o dict{iso: ultimo_iso}
    if isinstance(firmas_iso, set):
        firmas_dict = {d: d for d in firmas_iso}
    else:
        firmas_dict = dict(firmas_iso or {})

    casos: list[dict] = []
    n = len(fechas_asc)
    i = 0
    prev_cerrado_ultimo: pd.Timestamp | None = None

    while i < n:
        if i + 2 >= n or (fechas_asc[i + 2] - fechas_asc[i]).days > ventana_dias:
            if prev_cerrado_ultimo is not None and \
               (fechas_asc[i] - prev_cerrado_ultimo).days > ventana_dias:
                prev_cerrado_ultimo = None
            i += 1
            continue

        primer_idx   = i
        disparo_idx  = i + 2
        fecha_disp   = fechas_asc[disparo_idx]
        disp_iso     = fecha_disp.date().isoformat()
        ultimo_iso   = firmas_dict.get(disp_iso)
        cerrado      = ultimo_iso is not None

        critico = False
        if prev_cerrado_ultimo is not None:
            gap = (fechas_asc[primer_idx] - prev_cerrado_ultimo).days
            if gap <= ventana_dias:
                critico = True

        if not cerrado:
            # Caso abierto: absorbe TODOS los llamados posteriores
            ultimo_idx = n - 1
            casos.append({
                "fecha_disparo":   fecha_disp.date(),
                "primer_llamado":  fechas_asc[primer_idx].date(),
                "ultimo_llamado":  fechas_asc[ultimo_idx].date(),
                "n_llamados":      ultimo_idx - primer_idx + 1,
                "cerrado":         False,
                "critico":         critico,
                "indices":         (primer_idx, ultimo_idx),
            })
            break
        else:
            # Caso cerrado: corta en el `ultimo_llamado_iso` que se guardó al firmar
            # Si no hay (firma antigua), usa la ventana de 20 días desde el 1º
            ultimo_ts = pd.Timestamp(ultimo_iso)
            j = disparo_idx
            while j + 1 < n and fechas_asc[j + 1] <= ultimo_ts:
                j += 1
            ultimo_idx = j
            casos.append({
                "fecha_disparo":   fecha_disp.date(),
                "primer_llamado":  fechas_asc[primer_idx].date(),
                "ultimo_llamado":  fechas_asc[ultimo_idx].date(),
                "n_llamados":      ultimo_idx - primer_idx + 1,
                "cerrado":         True,
                "critico":         critico,
                "indices":         (primer_idx, ultimo_idx),
            })
            prev_cerrado_ultimo = fechas_asc[ultimo_idx]
            i = ultimo_idx + 1

    return casos


# ── API pública ───────────────────────────────────────────────────────────

def eds_con_reincidencia(rango_dias: int = DEFAULT_RANGO_DIAS,
                         cliente: str | None = None,
                         ventana_dias: int = DEFAULT_VENTANA_DIAS,
                         hoy: date | None = None,
                         firmas_por_eds: dict[str, set[str]] | None = None
                         ) -> pd.DataFrame:
    """Retorna una fila por cada caso de reincidencia con disparo en el rango.

    `firmas_por_eds`: {codigo_eds: {fecha_disparo_iso, ...}} — casos firmados
                       en Supabase. Se usa para saber cuáles casos están cerrados.

    Columnas: codigo_eds, cliente, estacion, comuna, fecha_disparo,
              n_llamados, primer_llamado, ultimo_llamado, cerrado, critico.
    Orden: fecha_disparo desc.
    """
    hoy = hoy or date.today()
    hasta = hoy + timedelta(days=1)
    # Miramos bastante hacia atrás para reconstruir correctamente casos abiertos
    # que empezaron antes del rango pero siguen activos.
    desde = hoy - timedelta(days=rango_dias + 180)

    df = _correctivos_rango(desde.isoformat(), hasta.isoformat())
    empty_cols = ["codigo_eds", "cliente", "estacion", "comuna",
                  "fecha_disparo", "n_llamados", "primer_llamado",
                  "ultimo_llamado", "cerrado", "critico"]
    if df.empty:
        return pd.DataFrame(columns=empty_cols)
    if cliente and cliente != "Todos":
        df = df[df["cliente"] == cliente]

    firmas_por_eds = firmas_por_eds or {}
    comunas = _mapa_comunas()
    filas: list[dict] = []
    fecha_min_disparo = pd.Timestamp(hoy - timedelta(days=rango_dias)).date()

    for cod_eds, g in df.groupby("codigo_eds", dropna=True):
        g = g.sort_values("fecha_creacion").reset_index(drop=True)
        fechas = list(g["fecha_creacion"])
        firmas = firmas_por_eds.get(cod_eds, set())
        casos = detectar_casos(fechas, firmas, ventana_dias)
        for c in casos:
            if c["fecha_disparo"] < fecha_min_disparo:
                continue
            filas.append({
                "codigo_eds":     cod_eds,
                "cliente":        g["cliente"].iloc[0],
                "estacion":       g["estacion"].iloc[0],
                "comuna":         comunas.get(cod_eds, ""),
                "fecha_disparo":  c["fecha_disparo"],
                "n_llamados":     c["n_llamados"],
                "primer_llamado": c["primer_llamado"],
                "ultimo_llamado": c["ultimo_llamado"],
                "cerrado":        c["cerrado"],
                "critico":        c["critico"],
            })

    if not filas:
        return pd.DataFrame(columns=empty_cols)
    return pd.DataFrame(filas).sort_values(
        "fecha_disparo", ascending=False
    ).reset_index(drop=True)


def correctivos_del_caso(codigo_eds: str, fecha_disparo: date,
                         firmas: set[str] | None = None,
                         ventana_dias: int = DEFAULT_VENTANA_DIAS) -> pd.DataFrame:
    """Devuelve todos los correctivos que integran el caso identificado por
    (codigo_eds, fecha_disparo). Aplica la misma lógica de detección para
    determinar si el caso está abierto (absorbe todo) o cerrado (recorta a
    la ventana de 20 días)."""
    # Rango amplio hacia atrás y hacia adelante para reconstruir el caso
    desde = fecha_disparo - timedelta(days=180)
    hasta = date.today() + timedelta(days=1)
    rows = _query(
        "ordenes_trabajo",
        f"select=id_ot,codigo_eds,cliente,estacion,responsable,fecha_creacion,"
        f"causa_raiz,tipo_falla,comentario_tecnico,prioridad,prioridad_calc"
        f"&tipo_tarea=ilike.*CORRECTIV*"
        f"&codigo_eds=eq.{codigo_eds}"
        f"&fecha_creacion=gte.{desde.isoformat()}"
        f"&fecha_creacion=lt.{hasta.isoformat()}"
        f"&order=fecha_creacion.asc",
        limit=2000,
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates(subset=["id_ot"]).reset_index(drop=True)
    df["fecha_creacion"] = pd.to_datetime(
        df["fecha_creacion"], errors="coerce", utc=True
    ).dt.tz_convert(None)

    fechas = list(df["fecha_creacion"])
    casos = detectar_casos(fechas, firmas or set(), ventana_dias)
    for c in casos:
        if c["fecha_disparo"] == fecha_disparo:
            i0, i1 = c["indices"]
            return df.iloc[i0:i1 + 1].reset_index(drop=True)
    return df  # fallback


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

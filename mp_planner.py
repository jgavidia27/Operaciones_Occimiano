"""
Planificador de Mantenciones Preventivas — motor de cartera, ruteo y calendario.

Reemplaza la vista "Planificación" (kanban por semana) que sólo reflejaba lo que
Fracttal ya tenía programado. Aquí el orden se invierte: primero se calcula QUÉ
toca (a partir de la última MP real de cada EDS), después CUÁNDO (ventana de
ciclo ± tolerancia) y recién entonces CÓMO agruparlo en rutas por cercanía.

Criterios operativos (definidos por operaciones, sep-2026):
  • Ciclo objetivo: 30 días desde la última MP realizada.
  • Tolerancia: ±5 días  →  se incumple recién pasados los 35 días.
  • Capacidad estándar: 3 MP por técnico por día.

Fuentes:
  • ordenes_trabajo  — MPs finalizadas → última MP real por EDS.
  • eds_geo          — lat/lon/comuna por EDS (poblada desde ubicaciones Fracttal).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd

CICLO_DIAS = 30
TOLERANCIA = 5
CAP_DIA = 3
RADIO_KM = 8.0

# Puesta en marcha del sistema. Hasta esta fecha la programacion de MP se lleva
# en el Excel de operaciones; desde aqui, toda visita de MP se planifica y se
# mide con este modulo. Antes de la fecha la vista es una PREVISUALIZACION: el
# mes en curso todavia se esta ejecutando por el proceso antiguo, asi que las
# MP que aparecen "atrasadas" no son deuda de este sistema.
INICIO_SISTEMA = date(2026, 10, 1)

# Comunas del Gran Santiago + provincias de la RM. Se usa para separar la
# cartera en zonas operativas: un técnico de la RM no cruza a regiones.
COMUNAS_RM = frozenset({
    "Cerrillos", "Cerro Navia", "Conchalí", "El Bosque", "Estación Central",
    "Huechuraba", "Independencia", "La Cisterna", "La Florida", "La Granja",
    "La Pintana", "La Reina", "Las Condes", "Lo Barnechea", "Lo Espejo",
    "Lo Prado", "Macul", "Maipú", "Ñuñoa", "Pedro Aguirre Cerda", "Peñalolén",
    "Providencia", "Pudahuel", "Quilicura", "Quinta Normal", "Recoleta",
    "Renca", "San Joaquín", "San Miguel", "San Ramón", "Santiago", "Vitacura",
    "Puente Alto", "San Bernardo", "Padre Hurtado", "Colina", "Lampa", "Buin",
    "Paine", "Melipilla", "Talagante", "Peñaflor", "Calera de Tango",
    "San José de Maipo", "Pirque", "Isla de Maipo", "El Monte", "Curacaví",
    "Til Til", "Malloco",
})

ESTADOS = {
    "vencida":  ("🔴 Vencida",   [220,  38,  38]),
    "ventana":  ("🟡 En ventana", [234, 179,   8]),
    "proxima":  ("🟠 Próxima",   [249, 115,  22]),
    "al_dia":   ("🟢 Al día",    [ 34, 197,  94]),
    "sin_hist": ("⚪ Sin historial", [148, 163, 184]),
}


# ── Geometría ────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2) -> float:
    """Distancia en km entre dos puntos. Suficiente para agrupar por cercanía;
    no pretende ser distancia de manejo."""
    r, p = 6371.0, math.pi / 180
    dla, dlo = (lat2 - lat1) * p, (lon2 - lon1) * p
    a = (math.sin(dla / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlo / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


# ── Cartera ──────────────────────────────────────────────────────────────────

def cargar_geo() -> pd.DataFrame:
    """Coordenadas y comuna por EDS desde Supabase (tabla eds_geo).

    Cacheada: se vuelve a leer en cada rerun de Streamlit —y un clic en el mapa
    ES un rerun—, pero cambia solo cuando corre sync_eds_geo.py.
    """
    try:
        import streamlit as _st
        return _cargar_geo_cache()
    except Exception:
        return _cargar_geo_raw()


def _cargar_geo_raw() -> pd.DataFrame:
    try:
        from supabase_client import _query
        rows = _query("eds_geo", "select=eds_occim,loc_fracttal,latitud,longitud,comuna", 5000)
    except Exception:
        return pd.DataFrame(columns=["eds_occim", "latitud", "longitud", "comuna"])
    g = pd.DataFrame(rows)
    if g.empty:
        return g
    for c in ("latitud", "longitud"):
        g[c] = pd.to_numeric(g[c], errors="coerce")
    return g.dropna(subset=["latitud", "longitud"])


# Localidades y grafías que Fracttal usa pero que NO son comunas, o que no
# coinciden con el nombre oficial. Sin esto la EDS queda fuera de todo polígono
# y desaparece del mapa por comuna.
ALIAS_COMUNA = {
    "Batuco": "Lampa",              # localidad de Lampa
    "Llolleo": "San Antonio",       # localidad de San Antonio
    "Reñaca": "Viña del Mar",       # sector de Viña del Mar
    "Malloco": "Peñaflor",          # localidad de Peñaflor
    "La Calera": "Calera",          # nombre oficial: Calera
    "Valpapaíso": "Valparaíso",     # error de tipeo en el maestro de Fracttal
    "Til Til": "Tiltil",            # nombre oficial: Tiltil, en una palabra
}


def norm_comuna(nombre) -> str:
    """Nombre de comuna comparable: sin tildes, mayúsculas, sin puntuación."""
    import re as _re
    import unicodedata as _u
    s = _u.normalize("NFKD", str(nombre or "").upper())
    s = s.encode("ascii", "ignore").decode()
    return " ".join(_re.sub(r"[^A-Z0-9 ]", " ", s).split())


def _centroide(geom) -> list | None:
    """Punto donde va el rótulo: centroide del anillo exterior más grande.

    No es el centroide del polígono completo — con comunas de forma irregular
    ese punto puede caer fuera del área; el del anillo mayor siempre queda
    dentro del cuerpo principal.
    """
    gt, co = geom.get("type"), geom.get("coordinates")
    anillos = co if gt == "Polygon" else [p[0] for p in co if p]
    if not anillos:
        return None
    r = max(anillos, key=len)
    if not r:
        return None
    return [round(sum(p[0] for p in r) / len(r), 5),
            round(sum(p[1] for p in r) / len(r), 5)]


def cargar_comunas() -> dict | None:
    """Polígonos de las comunas de Chile (comunas_chile.geojson, en el repo).

    Origen: github.com/fcortes/Chile-GeoJSON, con la geometría simplificada
    (Douglas-Peucker) — la RM en alta resolución y el resto del país a ~650 m
    de tolerancia, que a zoom nacional es invisible y baja el archivo de
    1,7 MB a 531 KB.
    """
    try:
        import streamlit as _st
        return _cargar_comunas_cache()
    except Exception:
        return _cargar_comunas_raw()


def _cargar_comunas_raw() -> dict | None:
    import json as _j
    import os as _o
    ruta = _o.path.join(_o.path.dirname(_o.path.abspath(__file__)),
                        "comunas_chile.geojson")
    if not _o.path.exists(ruta):
        return None
    try:
        return _j.loads(open(ruta, encoding="utf-8").read())
    except Exception:
        return None


# ── Cachés de Streamlit ──────────────────────────────────────────────────────
# Cada interacción con el mapa dispara un rerun completo del script. Sin caché,
# un clic en una comuna volvía a pedir eds_geo a Supabase (1,3 s), releer el
# GeoJSON y reconstruir toda la cartera (1,8 s): ~3 s de espera por clic para
# recalcular cosas que no cambiaron.
try:
    import streamlit as _stc

    @_stc.cache_data(ttl=1800, show_spinner=False)
    def _cargar_geo_cache() -> pd.DataFrame:
        return _cargar_geo_raw()

    @_stc.cache_resource(show_spinner=False)
    def _cargar_comunas_cache() -> dict | None:
        return _cargar_comunas_raw()

    @_stc.cache_data(ttl=900, show_spinner=False)
    def _cartera_cache(_raw, _geo, hoy_iso: str, excl: tuple,
                       ciclo: int, tol: int) -> pd.DataFrame:
        """`_raw` y `_geo` van con guion bajo: Streamlit no los hashea. La clave
        real es (fecha, exclusiones, ciclo, tolerancia), que es lo que el usuario
        puede cambiar en pantalla."""
        return construir_cartera(_raw, _geo, date.fromisoformat(hoy_iso),
                                 frozenset(excl), ciclo, tol)
except Exception:                      # fuera de Streamlit (tests, scripts)
    _cargar_geo_cache = None
    _cargar_comunas_cache = None
    _cartera_cache = None


def construir_cartera(raw_prev, geo: pd.DataFrame, hoy: date,
                      eds_excluidas=frozenset(),
                      ciclo: int = CICLO_DIAS, tol: int = TOLERANCIA) -> pd.DataFrame:
    """Una fila por EDS operativa: última MP real, fecha objetivo, límite y estado.

    "Operativa" = tiene al menos una MP finalizada en el historial. Una EDS sin
    ninguna MP no entra a la cartera de ruteo (no hay ciclo que proyectar), pero
    se cuenta aparte para que no desaparezca silenciosamente.
    """
    dfp = pd.DataFrame(raw_prev)
    if dfp.empty:
        return pd.DataFrame()

    fin = pd.to_datetime(dfp.get("fecha_finalizacion"), errors="coerce", utc=True)
    fin = fin.dt.tz_convert("America/Santiago").dt.tz_localize(None)
    est = dfp.get("estado", pd.Series(dtype=str)).astype(str)
    ok = fin.notna() & est.str.contains("Finaliz", case=False, na=False)
    h = pd.DataFrame({
        "eds":       dfp.get("codigo_eds").astype(str),
        "estacion":  dfp.get("estacion", pd.Series(dtype=str)).astype(str),
        "cliente":   dfp.get("cliente", pd.Series(dtype=str)).astype(str),
        "resp":      dfp.get("responsable", pd.Series(dtype=str)).astype(str),
        "plan":      dfp.get("plan_tareas", pd.Series(dtype=str)).astype(str),
        "tipo":      dfp.get("tipo_tarea", pd.Series(dtype=str)).astype(str),
        "fin":       fin,
    })[ok]
    h = h[h["eds"].notna() & (h["eds"].str.strip() != "") & (h["eds"] != "nan")]
    if h.empty:
        return pd.DataFrame()

    h = h.sort_values("fin")
    agg = h.groupby("eds").agg(
        ultima_mp=("fin", "max"),
        primera_mp=("fin", "min"),
        n_mp=("fin", "count"),
        estacion=("estacion", "last"),
        cliente=("cliente", "last"),
        ultimo_resp=("resp", "last"),
        # El plan y el tipo de la ULTIMA MP: es el que se va a repetir en la
        # proxima visita, asi que es el dato util para programar.
        plan=("plan", "last"),
        tipo=("tipo", "last"),
    ).reset_index()

    # Intervalo real entre MPs consecutivas — el cumplimiento histórico honesto.
    gaps = h.groupby("eds")["fin"].apply(
        lambda s: s.dt.normalize().drop_duplicates().diff().dt.days.dropna()
    )
    gm = gaps[(gaps >= 5) & (gaps <= 200)].groupby(level=0).median()
    agg["intervalo_real"] = agg["eds"].map(gm)

    agg = agg[~agg["eds"].isin(eds_excluidas)]
    agg["ultima_mp"] = agg["ultima_mp"].dt.normalize()
    agg["objetivo"] = agg["ultima_mp"] + pd.Timedelta(days=ciclo)
    agg["limite"] = agg["ultima_mp"] + pd.Timedelta(days=ciclo + tol)
    hoy_ts = pd.Timestamp(hoy)
    agg["dias_sin_mp"] = (hoy_ts - agg["ultima_mp"]).dt.days
    agg["dias_a_limite"] = (agg["limite"] - hoy_ts).dt.days

    def _estado(r):
        if r["limite"] < hoy_ts:      return "vencida"
        if r["objetivo"] <= hoy_ts:   return "ventana"
        if (r["objetivo"] - hoy_ts).days <= 10: return "proxima"
        return "al_dia"
    agg["estado"] = agg.apply(_estado, axis=1)

    if not geo.empty:
        agg = agg.merge(
            geo.rename(columns={"eds_occim": "eds", "latitud": "lat", "longitud": "lon"}),
            on="eds", how="left")
    for c in ("lat", "lon", "comuna"):
        if c not in agg.columns:
            agg[c] = None
    agg["zona"] = agg["comuna"].apply(
        lambda c: "Santiago (RM)" if c in COMUNAS_RM else ("Regiones" if c else "Sin comuna"))
    return agg.sort_values("limite").reset_index(drop=True)


# ── Motor de rutas ───────────────────────────────────────────────────────────

def dias_habiles(desde: date, hasta: date, sabados: bool = False) -> list[date]:
    tope = 5 if not sabados else 6
    out, d = [], desde
    while d <= hasta:
        if d.weekday() < tope:
            out.append(d)
        d += timedelta(days=1)
    return out


def planificar(cartera: pd.DataFrame, dias: list[date], tecnicos: list[str],
               cap: int = CAP_DIA, radio: float = RADIO_KM,
               fijos: dict | None = None) -> pd.DataFrame:
    """Asigna cada MP a un (día, técnico) equilibrando urgencia y cercanía.

    Algoritmo — greedy por día, "semilla + vecinos":
      1. Para cada día/técnico se toma como SEMILLA la MP viable más urgente
         (menor fecha límite). Esto garantiza que lo vencido salga primero.
      2. La ruta se completa con las MPs más cercanas a la semilla, priorizando
         entre ellas las de límite más próximo. Así la ruta queda geográficamente
         compacta sin postergar lo urgente.
      3. Si dentro del radio no hay vecinos, el radio se expande (16, 32 km…)
         antes de dejar al técnico con media jornada vacía.

    `fijos` son asignaciones manuales del administrador: {eds: (fecha, tecnico)}.
    Se respetan tal cual y el resto se recalcula alrededor de ellas.
    """
    fijos = fijos or {}
    # `tecnicos` puede ser una lista fija o un dict {fecha: [técnicos]}. Lo
    # segundo es lo normal: quien está de turno atiende correctivas y no está
    # disponible para MP ese día, así que la dotación cambia semana a semana.
    por_dia = isinstance(tecnicos, dict)
    pend = {r["eds"]: r for r in cartera.to_dict("records")
            if pd.notna(r.get("lat")) and pd.notna(r.get("lon"))}
    filas = []

    for eds, (f, tec) in fijos.items():
        if eds in pend:
            r = pend.pop(eds)
            filas.append({**r, "fecha": f, "tecnico": tec, "origen": "manual"})

    ocupado = {(f["fecha"], f["tecnico"]): 1 for f in filas}
    for f in filas:
        k = (f["fecha"], f["tecnico"])
        ocupado[k] = sum(1 for x in filas if (x["fecha"], x["tecnico"]) == k)

    for d in dias:
        for tec in (tecnicos.get(d, []) if por_dia else tecnicos):
            if not pend:
                break
            cupo = cap - ocupado.get((d, tec), 0)
            if cupo <= 0:
                continue
            viables = [r for r in pend.values()
                       if pd.Timestamp(r["limite"]).date() >= d] or list(pend.values())
            seed = min(viables, key=lambda r: (r["limite"], r["eds"]))
            ruta = [seed]
            pend.pop(seed["eds"])
            while len(ruta) < cupo and pend:
                rad, cand = radio, []
                while rad <= radio * 8 and not cand:
                    cand = [r for r in pend.values()
                            if haversine(seed["lat"], seed["lon"], r["lat"], r["lon"]) <= rad]
                    rad *= 2
                if not cand:
                    break
                nxt = min(cand, key=lambda r: (
                    haversine(ruta[-1]["lat"], ruta[-1]["lon"], r["lat"], r["lon"]) * 0.4
                    + max(0, (pd.Timestamp(r["limite"]).date() - d).days) * 0.6))
                ruta.append(nxt)
                pend.pop(nxt["eds"])
            for r in ruta:
                filas.append({**r, "fecha": d, "tecnico": tec, "origen": "auto"})
            ocupado[(d, tec)] = ocupado.get((d, tec), 0) + len(ruta)

    if not filas:
        return pd.DataFrame()
    p = pd.DataFrame(filas)
    p["fecha"] = pd.to_datetime(p["fecha"])
    p["atraso_dias"] = (p["fecha"] - pd.to_datetime(p["limite"])).dt.days.clip(lower=0)
    p["en_ventana"] = p["atraso_dias"] == 0
    # Dispersión real de cada ruta (km entre los dos puntos más lejanos del día).
    disp = {}
    for k, g in p.groupby(["fecha", "tecnico"]):
        pts = g[["lat", "lon"]].dropna().values
        disp[k] = max((haversine(a[0], a[1], b[0], b[1]) for a in pts for b in pts),
                      default=0.0)
    p["km_ruta"] = [disp.get((f, t), 0.0) for f, t in zip(p["fecha"], p["tecnico"])]
    return p.sort_values(["fecha", "tecnico", "limite"]).reset_index(drop=True)


def detectar_ejecutadas(plan: pd.DataFrame, cartera: pd.DataFrame) -> pd.DataFrame:
    """Requisito de 'recalcular': cruza lo programado contra la última MP real.

    Una MP programada se considera EJECUTADA si la última MP de esa EDS es igual
    o posterior a la fecha en que se programó. Las demás siguen pendientes y son
    las que el recálculo vuelve a poner en cola.
    """
    if plan.empty:
        return plan
    ult = cartera.set_index("eds")["ultima_mp"]
    p = plan.copy()
    p["ultima_real"] = p["eds"].map(ult)
    p["ejecutada"] = pd.to_datetime(p["ultima_real"]) >= pd.to_datetime(p["fecha"])
    return p


# ── Interfaz ─────────────────────────────────────────────────────────────────

_DIAS_ES = {"Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miércoles",
            "Thursday": "Jueves", "Friday": "Viernes", "Saturday": "Sábado"}


def _cli_corto(c) -> str:
    """Cliente en una palabra para que quepa en la tarjeta de ruta.
    Las variantes 'PARTICULAR XXX' se consolidan en 'Particular'."""
    s = str(c or "").strip()
    if not s or s.lower() == "nan":
        return "—"
    if s.upper().startswith("PARTICULAR"):
        return "Particular"
    return s.title()


def _equipos(hasta: date | None = None):
    """Equipos de terreno y sus miembros, sin los que están de baja a esa fecha.

    El roster maestro conserva a los técnicos dados de baja para que la data
    histórica siga mapeando a su equipo. Para planificar hacia adelante hay que
    sacarlos, o el motor les asigna rutas a gente que ya no está.
    """
    try:
        from data import GRUPOS_TERRENO, TECNICOS_BAJA
    except Exception:
        return {}
    corte = (hasta or date.today()).isoformat()
    # Las bajas se registran con nombre completo y el roster usa nombre corto:
    # el cruce es por tokens, igual que con los turnos.
    fuera = set()
    for full, info in (TECNICOS_BAJA or {}).items():
        if str(info.get("hasta", "")) < corte:
            fuera.add(frozenset(_tok(full)))

    def de_baja(corto: str) -> bool:
        t = _tok(corto)
        return bool(t) and any(t <= f for f in fuera)

    return {k: [m for m in v["miembros"] if not de_baja(m)]
            for k, v in GRUPOS_TERRENO.items()}


def _tok(nombre: str) -> set:
    import unicodedata as _u
    s = _u.normalize("NFKD", str(nombre or "").lower()).encode("ascii", "ignore").decode()
    return {p for p in s.split() if len(p) > 2}


def render(raw_prev, hoy: date, theme: dict | None = None):
    """Vista completa del planificador.

    `raw_prev` = filas crudas de las OTs preventivas SIN filtrar por período:
    el ciclo se calcula sobre todo el historial, no sobre el rango que el
    usuario tenga elegido arriba.
    """
    import streamlit as st

    muted = (theme or {}).get("muted", "#94a3b8")
    dark = bool((theme or {}).get("dark", False))

    st.caption(
        "Planificador de MP por **ciclo real**, no por lo que Fracttal tenga "
        "cargado. Para cada EDS se toma su **última MP finalizada**, se proyecta "
        f"el objetivo a **{CICLO_DIAS} días** con tolerancia **±{TOLERANCIA}**, y "
        "las MPs que caen en el horizonte se agrupan en rutas por **cercanía "
        "geográfica** respetando la capacidad de cada técnico. Cuenta como "
        f"visita de este sistema toda MP desde el {INICIO_SISTEMA:%d-%m-%Y}."
    )

    try:
        from data import EDS_NO_APLICA
    except Exception:
        EDS_NO_APLICA = frozenset()

    if hoy < INICIO_SISTEMA:
        _faltan = (INICIO_SISTEMA - hoy).days
        st.info(
            f"🗓️ **Previsualización.** Este planificador rige las visitas de MP "
            f"**desde el {INICIO_SISTEMA:%d-%m-%Y}** (faltan {_faltan} días). "
            "Hasta entonces la programación vigente es la del **Excel de "
            "operaciones**, y lo que ves aquí se recalcula solo a medida que se "
            "cierran las MP del mes en curso: cada MP que se realiza corre su "
            "próxima fecha objetivo.")

    geo = cargar_geo()
    if geo.empty:
        st.error("Tabla `eds_geo` vacía o inaccesible. Ejecuta `sync_eds_geo.py`.")
        return

    # ── Criterios ────────────────────────────────────────────────────────────
    with st.expander("⚙️ Criterios de planificación", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        ciclo = int(c1.number_input("Ciclo objetivo (días)", 15, 90, CICLO_DIAS, 1,
                                    help="Días entre una MP y la siguiente."))
        tol = int(c2.number_input("Tolerancia (± días)", 0, 20, TOLERANCIA, 1,
                                  help="Se incumple recién pasado ciclo + tolerancia."))
        cap = int(c3.number_input("MP por técnico/día", 1, 8, CAP_DIA, 1))
        radio = float(c4.number_input("Radio de agrupación (km)", 2.0, 40.0,
                                      RADIO_KM, 1.0,
                                      help="Distancia máxima entre EDS de una misma "
                                           "ruta. Se expande sola si no hay vecinos."))

    if _cartera_cache is not None:
        cartera = _cartera_cache(raw_prev, geo, hoy.isoformat(),
                                 tuple(sorted(EDS_NO_APLICA)), ciclo, tol)
    else:
        cartera = construir_cartera(raw_prev, geo, hoy, EDS_NO_APLICA, ciclo, tol)
    if cartera.empty:
        st.warning("Sin historial de MP finalizadas para construir la cartera.")
        return

    # ── Filtros ──────────────────────────────────────────────────────────────
    f1, f2, f3 = st.columns([1.1, 1.1, 2.2])
    zona = f1.selectbox("Zona", ["Santiago (RM)", "Regiones", "Todas"],
                        key="mpp_zona",
                        help="Etapa 1 del sistema: Santiago. Regiones usa el mismo "
                             "motor, pero ahí las rutas son interurbanas.")
    eq_map = _equipos()
    equipo = f2.selectbox("Equipo", ["Todos"] + sorted(eq_map), key="mpp_eq")

    base = cartera if zona == "Todas" else cartera[cartera["zona"] == zona]
    comunas = sorted(c for c in base["comuna"].dropna().unique())
    sel_com = f3.multiselect("Comuna", comunas, key="mpp_com",
                             placeholder="Todas las comunas")
    if sel_com:
        base = base[base["comuna"].isin(sel_com)]

    # ── KPIs de cartera ──────────────────────────────────────────────────────
    nv = int((base["estado"] == "vencida").sum())
    nw = int((base["estado"] == "ventana").sum())
    npx = int((base["estado"] == "proxima").sum())
    ints = cartera["intervalo_real"].dropna()
    cumpl = float((ints <= ciclo + tol).mean() * 100) if len(ints) else float("nan")

    k = st.columns(5)
    k[0].metric("EDS en cartera", f"{len(base):,}")
    k[1].metric("🔴 Vencidas", f"{nv:,}",
                help=f"Pasaron {ciclo + tol} días desde su última MP.")
    k[2].metric("🟡 En ventana hoy", f"{nw:,}")
    k[3].metric("🟠 Vencen ≤10 días", f"{npx:,}")
    k[4].metric("Cumplimiento real", f"{cumpl:.0f}%" if cumpl == cumpl else "—",
                help="Porcentaje de los intervalos históricos entre MPs "
                     f"consecutivas que se cerraron dentro de {ciclo + tol} días. "
                     "Es desempeño medido, no una meta.")

    # ── Horizonte y dotación ─────────────────────────────────────────────────
    # Viven fuera de las pestañas porque el plan lo consumen todas: el mapa
    # muestra la fecha programada de cada EDS, Rutas el detalle por jornada y
    # Ajuste manual el recálculo de lo pendiente.
    with st.expander("📆 Horizonte y dotación", expanded=False):
        r1, r2, r3 = st.columns([1.1, 1.1, 1.8])
        _ini_def = max(hoy, INICIO_SISTEMA)
        _fin_def = (_ini_def.replace(day=1) + timedelta(days=62)).replace(day=1)             - timedelta(days=1)
        ini = r1.date_input("Inicio", _ini_def, key="mpp_ini",
                            help=f"El sistema entra en marcha el "
                                 f"{INICIO_SISTEMA:%d-%m-%Y}.")
        fin_ = r2.date_input("Término", _fin_def, key="mpp_fin")
        todos_tec = sorted({m for v in eq_map.values() for m in v})
        miembros = eq_map.get(equipo, todos_tec) if equipo != "Todos" else todos_tec
        tecs = r3.multiselect("Técnicos del equipo", miembros,
                              default=miembros, key="mpp_tec")
        s1, s2 = st.columns([1, 2])
        sab = s1.checkbox("Incluir sábados", value=False, key="mpp_sab")
        desc_turno = s2.checkbox(
            "Descontar técnicos de turno (hacen correctivas)", value=True,
            key="mpp_turno",
            help="Estar de turno significa atender correctivas: ese día el "
                 "técnico no está disponible para MP. La dotación se toma de "
                 "Planificación Turnos STO, semana a semana.")

    plan, sin_turno, aviso_plan = pd.DataFrame(), None, None
    if not tecs:
        aviso_plan = "Selecciona al menos un técnico en «Horizonte y dotación»."
    elif fin_ < ini:
        aviso_plan = "La fecha de término es anterior al inicio."
    else:
        dias = dias_habiles(ini, fin_, sab)
        fijos = st.session_state.get("mpp_fijos", {})
        disp = tecs
        if desc_turno:
            try:
                import turnos as _tn
                mapa = _tn.de_turno_por_dia(fin_)
                disp = {d: _tn.disponibles_para_mp(tecs, d, mapa) for d in dias}
                sin_turno = sum(len(v) for v in disp.values()) / max(1, len(dias))
            except Exception as exc:
                st.warning(f"No se pudo leer la programación de turnos ({exc}). "
                           "Se usa la dotación completa.")
        plan = planificar(base, dias, disp, cap, radio, fijos)
        st.session_state["mpp_plan"] = plan

    t_map, t_rut, t_atr, t_man = st.tabs(
        ["🗺️  Mapa y cartera", "📅  Rutas", "🔴  Arrastre", "🔧  Ajuste manual"])

    # ── Mapa ─────────────────────────────────────────────────────────────────
    with t_map:
        pts = base.dropna(subset=["lat", "lon"]).copy()
        if pts.empty:
            st.info("Sin coordenadas para la selección actual.")
        else:
            # Resumen por comuna: es lo que pinta el polígono y lo que se
            # muestra al hacer clic.
            res = (base.groupby("comuna")
                       .agg(eds=("eds", "count"),
                            vencidas=("estado", lambda s: int((s == "vencida").sum())),
                            ventana=("estado", lambda s: int((s == "ventana").sum())),
                            dias=("dias_sin_mp", "median"))
                       .reset_index())
            res_ix = res.set_index("comuna")

            geo_poly = cargar_comunas()
            sel_click = None

            if geo_poly:
                # Una comuna se pinta por su carga y su urgencia: rojo si tiene
                # MP vencidas, ámbar si hay alguna en ventana, teal si está al
                # día. La opacidad sube con la cantidad de EDS, para que a
                # simple vista se vea dónde se concentra el trabajo.
                # El cruce es por nombre normalizado + alias: "Los Ángeles"
                # vs "Los Angeles", "La Calera" vs "Calera", y localidades
                # como Batuco o Reñaca que se resuelven a su comuna real.
                por_norm = {}
                for c in res["comuna"].dropna():
                    por_norm[norm_comuna(ALIAS_COMUNA.get(c, c))] = c
                feats = []
                for f in geo_poly["features"]:
                    cn = por_norm.get(norm_comuna(f["properties"].get("comuna")))
                    if cn is None:
                        continue
                    r = res_ix.loc[cn]
                    n_eds = int(r["eds"])
                    if int(r["vencidas"]):
                        col = [220, 38, 38]
                    elif int(r["ventana"]):
                        col = [234, 179, 8]
                    else:
                        col = [1, 121, 138]
                    alpha = 45 + min(90, n_eds * 9)
                    feats.append({
                        "type": "Feature",
                        "geometry": f["geometry"],
                        "properties": {
                            "comuna": cn,
                            "centro": _centroide(f["geometry"]),
                            "fill": col + [alpha],
                            "linea": col,
                            # Cada capa trae su tooltip ya armado en `tip`: el
                            # de Deck es uno solo para todas, y un template
                            # compartido deja placeholders sin resolver (se ven
                            # literales: "{estacion}") sobre la capa que no
                            # tiene ese campo.
                            "tip": (f"<b>{cn}</b><br/>{n_eds} EDS en cartera<br/>"
                                    f"🔴 {int(r['vencidas'])} vencidas · "
                                    f"🟡 {int(r['ventana'])} en ventana<br/>"
                                    f"Mediana {int(r['dias']) if pd.notna(r['dias']) else 0}"
                                    " días sin MP"),
                        },
                    })
                st.caption(
                    "**Haz clic en una comuna** para ver su detalle abajo. "
                    "El color es la urgencia (rojo = tiene MP vencidas, ámbar = "
                    "alguna en ventana, teal = al día) y la intensidad, cuántas "
                    "EDS concentra.")
            else:
                feats = []
                st.warning(
                    "No se encontró `comunas_chile.geojson`; se muestran sólo los "
                    "puntos. Sin polígonos no se puede seleccionar por comuna.")

            pts["color"] = pts["estado"].map(
                lambda e: ESTADOS.get(e, ESTADOS["sin_hist"])[1])
            pts["radio_m"] = pts["estado"].map(
                {"vencida": 900, "ventana": 700, "proxima": 550}).fillna(400)
            pts["lbl"] = pts["estado"].map(
                lambda e: ESTADOS.get(e, ESTADOS["sin_hist"])[0])
            pts["ult"] = pd.to_datetime(pts["ultima_mp"]).dt.strftime("%d-%m-%Y")
            pts["lim"] = pd.to_datetime(pts["limite"]).dt.strftime("%d-%m-%Y")
            pts["cli"] = pts["cliente"].map(_cli_corto)
            # Fecha programada por EDS, desde el plan ya calculado arriba.
            prog = ({} if plan.empty
                    else dict(zip(plan["eds"],
                                  pd.to_datetime(plan["fecha"]).dt.strftime("%d-%m-%Y"))))
            pts["prog"] = pts["eds"].map(prog).fillna("sin programar")
            pts["tip"] = [
                f"<b>{e}</b> — {str(es)[:40]}<br/>{c} · {cm}<br/>"
                f"{l} · {d} días sin MP<br/>Programada: {pr}<br/>"
                f"Última: {u} · Límite: {li}"
                for e, es, c, cm, l, d, pr, u, li in zip(
                    pts["eds"], pts["estacion"], pts["cli"], pts["comuna"],
                    pts["lbl"], pts["dias_sin_mp"], pts["prog"],
                    pts["ult"], pts["lim"])]

            try:
                import pydeck as pdk
                zoom = 11.5 if sel_com else (9.2 if zona == "Santiago (RM)" else 4.2)
                estilo_mapa = (pdk.map_styles.CARTO_DARK if dark
                               else pdk.map_styles.CARTO_LIGHT)
                capas = []
                if feats:
                    capas.append(pdk.Layer(
                        "GeoJsonLayer",
                        data={"type": "FeatureCollection", "features": feats},
                        get_fill_color="properties.fill",
                        get_line_color="properties.linea",
                        get_line_width=90, line_width_min_pixels=1.2,
                        stroked=True, filled=True, pickable=True,
                        auto_highlight=True, id="comunas"))
                    # Nombre de la comuna sobre su centroide: sin rótulo hay
                    # que adivinar qué polígono es cuál.
                    # Sin SDF: activarlo hacía que deck.gl dibujara los glifos
                    # enormes y difuminados, tapando el mapa entero. El fondo
                    # semitransparente reemplaza al contorno para dar contraste.
                    capas.append(pdk.Layer(
                        "TextLayer",
                        data=[{"comuna": f["properties"]["comuna"],
                               "pos": f["properties"]["centro"]}
                              for f in feats if f["properties"]["centro"]],
                        get_position="pos", get_text="comuna",
                        get_size=11, size_units="pixels",
                        size_min_pixels=9, size_max_pixels=14,
                        get_color=[226, 232, 240] if dark else [15, 23, 42],
                        get_alignment_baseline="'center'",
                        get_text_anchor="'middle'",
                        background=True,
                        get_background_color=[12, 37, 64, 190] if dark
                                             else [255, 255, 255, 205],
                        background_padding=[3, 1, 3, 1],
                        pickable=False, id="rotulos"))
                capas.append(pdk.Layer(
                    "ScatterplotLayer", data=pts, id="eds",
                    get_position="[lon, lat]", get_fill_color="color",
                    get_radius="radio_m", radius_min_pixels=4,
                    radius_max_pixels=18, pickable=True, opacity=0.9,
                    stroked=True, get_line_color=[255, 255, 255], line_width_min_pixels=1))
                ev = st.pydeck_chart(
                    pdk.Deck(
                        map_style=estilo_mapa,
                        initial_view_state=pdk.ViewState(
                            latitude=float(pts["lat"].mean()),
                            longitude=float(pts["lon"].mean()),
                            zoom=zoom, pitch=0),
                        layers=capas,
                        tooltip={
                            "html": "{tip}",
                            "style": {
                                "backgroundColor": "#0C2540" if dark else "#ffffff",
                                "color": "#e2e8f0" if dark else "#1e293b",
                                "border": "1px solid " + ("#1e3356" if dark else "#e2e8f0"),
                                "borderRadius": "8px",
                                "padding": "8px 10px",
                                "fontSize": "12px",
                                "lineHeight": "1.45",
                                "boxShadow": "0 4px 14px rgba(0,0,0,.18)",
                                "maxWidth": "300px",
                            },
                        }),
                    use_container_width=True,
                    selection_mode="single-object", on_select="rerun",
                    key="mpp_mapa")
                # Lo que devuelve el clic: la comuna del polígono, o la comuna
                # de la EDS si se pinchó un punto.
                try:
                    objs = (ev.selection or {}).get("objects", {}) or {}
                    for capa, filas in objs.items():
                        if not filas:
                            continue
                        o = filas[0]
                        sel_click = o.get("comuna") or o.get("properties", {}).get("comuna")
                        if sel_click:
                            break
                except Exception:
                    sel_click = None
            except Exception as exc:
                st.map(pts[["lat", "lon"]], size=120)
                st.caption(f"Mapa simple (pydeck no disponible: {exc}).")

            st.markdown(
                f"<span style='font-size:.8rem;color:{muted};'>"
                "Puntos — 🔴 vencida · 🟡 en ventana hoy · 🟠 vence ≤10 días · "
                "🟢 al día</span>", unsafe_allow_html=True)

            # ── Detalle de la comuna seleccionada ────────────────────────────
            foco = sel_click or (sel_com[0] if len(sel_com) == 1 else None)
            if foco:
                det = base[base["comuna"] == foco].copy()
                st.markdown(f"### 📍 {foco}")
                m = st.columns(4)
                m[0].metric("EDS", f"{len(det):,}")
                m[1].metric("🔴 Vencidas", int((det["estado"] == "vencida").sum()))
                m[2].metric("🟡 En ventana", int((det["estado"] == "ventana").sum()))
                m[3].metric("Días sin MP (mediana)",
                            f"{det['dias_sin_mp'].median():.0f}")

                det["Programada"] = det["eds"].map(prog).fillna("—")
                if not plan.empty:
                    det["Técnico"] = det["eds"].map(
                        dict(zip(plan["eds"], plan["tecnico"]))).fillna("—")
                else:
                    det["Técnico"] = "—"
                det["Estado"] = det["estado"].map(
                    lambda e: ESTADOS.get(e, ESTADOS["sin_hist"])[0])
                det["Cliente"] = det["cliente"].map(_cli_corto)
                tbl = (det[["eds", "estacion", "Cliente", "tipo", "plan",
                            "Programada", "Técnico", "ultima_mp", "limite",
                            "dias_sin_mp", "Estado"]]
                       .rename(columns={"eds": "Código EDS", "estacion": "Estación",
                                        "tipo": "Tipo de mantención",
                                        "plan": "Plan de tareas",
                                        "ultima_mp": "Última MP", "limite": "Límite",
                                        "dias_sin_mp": "Días sin MP"}))
                for c in ("Última MP", "Límite"):
                    tbl[c] = pd.to_datetime(tbl[c]).dt.strftime("%d-%m-%Y")
                tbl = tbl.sort_values("Límite", key=lambda s: pd.to_datetime(
                    s, format="%d-%m-%Y"))
                st.dataframe(tbl, use_container_width=True, hide_index=True,
                             height=min(460, 38 * len(tbl) + 40))
                st.download_button(
                    f"⬇️ Descargar {foco} (CSV)",
                    tbl.to_csv(index=False).encode("utf-8-sig"),
                    f"mp_{foco.lower().replace(' ', '_')}_{hoy:%Y%m%d}.csv",
                    "text/csv", key="mpp_dl_comuna")
            else:
                st.caption("Selecciona una comuna en el mapa para ver su detalle.")

            st.markdown("**Cartera por comuna**")
            pc = (res.rename(columns={"comuna": "Comuna", "eds": "EDS",
                                      "vencidas": "Vencidas",
                                      "ventana": "En ventana",
                                      "dias": "Días sin MP (mediana)"})
                     .sort_values(["Vencidas", "EDS"], ascending=False))
            st.dataframe(pc, use_container_width=True, hide_index=True,
                         height=min(420, 38 * len(pc) + 40))

    # ── Rutas ────────────────────────────────────────────────────────────────
    with t_rut:
        if aviso_plan:
            st.info(aviso_plan)
        elif plan.empty:
            st.info("Nada que programar en el horizonte elegido.")
        else:
            if sin_turno is not None:
                st.caption(
                    f"Dotación disponible para MP: **{sin_turno:.1f} de {len(tecs)} "
                    f"técnicos** por día hábil, una vez descontados los de turno.")
            arr = int((pd.to_datetime(base["limite"]).dt.date < ini).sum())
            if arr and ini <= INICIO_SISTEMA:
                # Antes de la puesta en marcha, lo "atrasado" no es deuda de
                # este sistema: son las MP que el proceso en Excel todavia
                # esta cerrando este mes. Recien el dia de arranque, con
                # septiembre ya cerrado, el atraso que quede es real.
                st.info(
                    f"**{arr} MP** aparecen fuera de plazo al {ini:%d-%m-%Y}, "
                    "pero su ventana cae **antes** de la puesta en marcha: "
                    "siguen a cargo de la planificacion en Excel. Este numero "
                    "baja solo a medida que se cierren, y el arrastre real se "
                    f"conoce el {INICIO_SISTEMA:%d-%m-%Y}.")
            elif arr:
                st.warning(
                    f"**{arr} MP** de la seleccion ya superan su limite antes "
                    f"del {ini:%d-%m-%Y}. Entran igual a la ruta, pero nacen "
                    "incumplidas: son arrastre, no planificacion.")
            total_geo = len(base.dropna(subset=["lat", "lon"]))
            m = st.columns(4)
            m[0].metric("MP programadas", f"{len(plan):,}")
            m[1].metric("Dentro de ventana", f"{int(plan['en_ventana'].sum()):,}",
                        f"{plan['en_ventana'].mean() * 100:.0f}%")
            m[2].metric("Jornadas usadas",
                        f"{plan.groupby(['fecha', 'tecnico']).ngroups:,}",
                        help=f"A {cap} MP por técnico/día.")
            m[3].metric("Km por ruta (mediana)", f"{plan['km_ruta'].median():.1f}",
                        help="Distancia entre las dos EDS más lejanas de la "
                             "jornada. Baja = ruta compacta.")
            sin_cupo = total_geo - len(plan)
            if sin_cupo > 0:
                need = math.ceil(total_geo / max(1, len(dias)) / cap)
                st.info(f"**{sin_cupo} MP** no alcanzan cupo con {len(tecs)} "
                        f"técnico(s) en el período. Cubrir todo requiere "
                        f"**{need} técnicos** dedicados a MP.")

            # Cobertura por cliente. El ruteo NO discrimina cliente — si la
            # EDS más cercana a una Copec es una Shell, esa es la siguiente
            # parada. Esto es para saber a quién se está atendiendo, no para
            # condicionar la ruta.
            with st.expander("🏷️ Clientes cubiertos en el período", expanded=False):
                st.caption(
                    "Las rutas se optimizan **sólo por cercanía y fecha "
                    "límite**: una jornada puede mezclar clientes si las "
                    "estaciones están al lado. Este cuadro es para saber a "
                    "quién se atiende, no para separar rutas por marca.")
                plan["_cli"] = plan["cliente"].map(_cli_corto)
                pend_cli = base.copy()
                pend_cli["_cli"] = pend_cli["cliente"].map(_cli_corto)
                rc = (plan.groupby("_cli")
                          .agg(MP=("eds", "count"),
                               EnVentana=("en_ventana", "sum"))
                          .reset_index())
                rc["Cobertura"] = rc["_cli"].map(
                    pend_cli.groupby("_cli")["eds"].count())
                rc["% en ventana"] = (rc["EnVentana"] / rc["MP"] * 100).round(0)
                rc = (rc.rename(columns={"_cli": "Cliente",
                                         "MP": "MP programadas",
                                         "EnVentana": "Dentro de ventana",
                                         "Cobertura": "EDS en cartera"})
                        [["Cliente", "EDS en cartera", "MP programadas",
                          "Dentro de ventana", "% en ventana"]]
                        .sort_values("MP programadas", ascending=False))
                st.dataframe(rc, use_container_width=True, hide_index=True)
                mixtos = int(sum(
                    1 for _, g in plan.groupby(["fecha", "tecnico"])
                    if g["cliente"].map(_cli_corto).nunique() > 1))
                tot_j = plan.groupby(["fecha", "tecnico"]).ngroups
                st.caption(
                    f"**{mixtos} de {tot_j} jornadas** combinan más de un "
                    "cliente — señal de que la ruta se está armando por "
                    "cercanía real y no por marca.")

            # Paginado por semana. Streamlit re-renderiza TODAS las pestañas
            # en cada rerun —y un clic en el mapa es un rerun—, así que dibujar
            # dos meses de tarjetas de ruta encarecía cada interacción aunque
            # el usuario estuviera mirando el mapa.
            _sem = plan["fecha"].dt.to_period("W")
            _ops = sorted(_sem.unique())
            _lbl = {w: f"{w.start_time:%d-%m} al {w.end_time:%d-%m}" for w in _ops}
            _pick = st.radio("Semana", _ops, horizontal=True, key="mpp_semrut",
                             format_func=lambda w: _lbl[w],
                             help="Se dibuja una semana a la vez para que la "
                                  "vista responda rápido.")
            _vis = plan[_sem == _pick]
            for d, gd in _vis.groupby("fecha"):
                dia_lbl = f"{d:%A %d-%m-%Y}"
                for en, es in _DIAS_ES.items():
                    dia_lbl = dia_lbl.replace(en, es)
                st.markdown(f"##### {dia_lbl}")
                # Sólo los técnicos con ruta ese día: con la dotación
                # completa, una columna por persona deja tarjetas ilegibles.
                del_dia = sorted(gd["tecnico"].unique())
                cols = st.columns(min(4, len(del_dia)) or 1)
                for i, tec in enumerate(del_dia):
                    gt = gd[gd["tecnico"] == tec]
                    with cols[i % len(cols)]:
                        km = float(gt["km_ruta"].iloc[0])
                        coms = ", ".join(sorted(gt["comuna"].dropna().unique()))
                        # Los clientes de la jornada son informativos: la ruta
                        # se arma sólo por cercanía y fecha límite. Que un día
                        # mezcle Copec y Shell es lo correcto si quedan al lado.
                        clis = ", ".join(sorted(
                            {_cli_corto(c) for c in gt["cliente"].dropna()
                             if str(c).strip()}))
                        with st.container(border=True):
                            st.markdown(
                                f"**{tec}** · {len(gt)} MP · {km:.1f} km  \n"
                                f"<span style='font-size:.75rem;color:{muted};'>"
                                f"{coms}</span>  \n"
                                f"<span style='font-size:.75rem;'>🏷️ {clis}</span>",
                                unsafe_allow_html=True)
                            for _, r in gt.iterrows():
                                fl = "  ⚠️" if not r["en_ventana"] else ""
                                st.markdown(
                                    f"<span style='font-size:.78rem;'>"
                                    f"<b>{r['eds']}</b> {str(r['estacion'])[:28]}"
                                    f"<br/><span style='color:{muted};'>"
                                    f"{_cli_corto(r['cliente'])} · lím "
                                    f"{pd.Timestamp(r['limite']):%d-%m}{fl}</span>"
                                    f"</span>", unsafe_allow_html=True)
            st.session_state["mpp_plan"] = plan

    # ── Arrastre ─────────────────────────────────────────────────────────────
    with t_atr:
        v = base[base["estado"] == "vencida"].copy()
        if v.empty:
            st.success("Sin EDS fuera de plazo en la selección. 🎉")
        else:
            st.caption(
                "EDS que **ya superaron** ciclo + tolerancia, ordenadas por "
                "gravedad. Esto es lo que la vista anterior no mostraba: una EDS "
                "que dejaba de aparecer en la programación de Fracttal "
                "desaparecía también del radar.")
            v["Atraso"] = v["dias_sin_mp"] - ciclo - tol
            vs = (v[["eds", "estacion", "cliente", "comuna", "ultima_mp",
                     "dias_sin_mp", "Atraso", "n_mp"]]
                  .rename(columns={"eds": "EDS", "estacion": "Estación",
                                   "cliente": "Cliente", "comuna": "Comuna",
                                   "ultima_mp": "Última MP",
                                   "dias_sin_mp": "Días sin MP",
                                   "n_mp": "MP históricas"})
                  .sort_values("Atraso", ascending=False))
            vs["Última MP"] = pd.to_datetime(vs["Última MP"]).dt.strftime("%d-%m-%Y")
            st.dataframe(vs, use_container_width=True, hide_index=True,
                         height=min(560, 38 * len(vs) + 40))
            st.download_button("⬇️ Descargar arrastre (CSV)",
                               vs.to_csv(index=False).encode("utf-8-sig"),
                               f"arrastre_mp_{hoy:%Y%m%d}.csv", "text/csv")

    # ── Ajuste manual ────────────────────────────────────────────────────────
    with t_man:
        st.caption(
            "El administrador fija aquí visitas puntuales (una EDS, una fecha, un "
            "técnico). El motor **respeta lo fijado** y recalcula el resto de las "
            "rutas alrededor. Las fijaciones viven durante la sesión.")
        a1, a2, a3 = st.columns([2, 1.2, 1.2])
        opts = base["eds"].tolist()
        if not opts:
            st.info("Sin EDS en la selección actual.")
        else:
            eds_sel = a1.selectbox(
                "EDS", opts, key="mpp_fix_eds",
                format_func=lambda e: f"{e} — "
                f"{str(base.loc[base['eds'] == e, 'estacion'].iloc[0])[:36]}")
            f_sel = a2.date_input("Fecha", max(hoy, INICIO_SISTEMA), key="mpp_fix_f")
            todos_tec = sorted({m for v in eq_map.values() for m in v})
            t_sel = a3.selectbox("Técnico", todos_tec, key="mpp_fix_t")
            b1, b2 = st.columns(2)
            if b1.button("📌 Fijar visita", use_container_width=True):
                fx = dict(st.session_state.get("mpp_fijos", {}))
                fx[eds_sel] = (f_sel, t_sel)
                st.session_state["mpp_fijos"] = fx
                st.rerun()
            if b2.button("🔄 Limpiar fijaciones", use_container_width=True):
                st.session_state["mpp_fijos"] = {}
                st.rerun()

        fx = st.session_state.get("mpp_fijos", {})
        if fx:
            st.markdown("**Visitas fijadas**")
            st.dataframe(pd.DataFrame(
                [{"EDS": e, "Fecha": f"{f:%d-%m-%Y}", "Técnico": t}
                 for e, (f, t) in fx.items()]),
                use_container_width=True, hide_index=True)

        st.divider()
        st.markdown("**Recálculo — qué se hizo y qué quedó pendiente**")
        st.caption(
            "Cruza lo programado contra la última MP real de cada EDS. Lo que se "
            "ejecutó sale de la cola; lo pendiente vuelve a entrar en la próxima "
            "generación de rutas.")
        plan = st.session_state.get("mpp_plan", pd.DataFrame())
        if plan.empty:
            st.info("Genera rutas primero en la pestaña **Rutas**.")
        else:
            ch = detectar_ejecutadas(plan, cartera)
            done = int(ch["ejecutada"].sum())
            c = st.columns(3)
            c[0].metric("Programadas", f"{len(ch):,}")
            c[1].metric("Ya ejecutadas", f"{done:,}")
            c[2].metric("Pendientes de reprogramar", f"{len(ch) - done:,}")
            pend = (ch[~ch["ejecutada"]][["eds", "estacion", "comuna", "fecha",
                                          "tecnico", "limite"]]
                    .rename(columns={"eds": "EDS", "estacion": "Estación",
                                     "comuna": "Comuna", "fecha": "Programada",
                                     "tecnico": "Técnico", "limite": "Límite"}))
            for col in ("Programada", "Límite"):
                pend[col] = pd.to_datetime(pend[col]).dt.strftime("%d-%m-%Y")
            st.dataframe(pend, use_container_width=True, hide_index=True,
                         height=min(420, 38 * len(pend) + 40))

from __future__ import annotations

import re
import unicodedata
import pandas as pd
from datetime import datetime


# ══════════════════════════════════════════════════════════════════════════════
# CLASIFICACIÓN DE CAUSA RAÍZ (Fracttal One)
# ══════════════════════════════════════════════════════════════════════════════
# Basado en el catálogo real de Occimiano en Fracttal One.
# "Cliente/Concesionario" = no responsabilidad del técnico.
# "Técnico" = responsabilidad directa del técnico.
# "Sin clasificar" = técnico no fue específico → afecta KPI Llenado Fracttal.

_PREFIJOS_CLIENTE = {
    "01.1",   # DAÑO CAUSADO POR CLIENTE
    "01.2",   # MAL USO U OMISION EDS (sin sal, llave cerrada, etc.)
    "01.3",   # FICHERO (MOJADO/DAÑADO)
    "02.3",   # FICHERO / FALLA PROGRAMACION
    "02.4",   # REPUESTOS /OTROS (solo si la falla proviene del concesionario)
    "03.1",   # DAÑOS EN ESTRUCTURAS/GASFITERÍA/OOCC
}
_PREFIJOS_TECNICO = {
    "01.4",   # REPUESTOS (DESGASTE)/OTROS  — desgaste que el técnico debe detectar en PM
    "01.5",   # ERROR 01 ELECTRICO — falla eléctrica atribuible a Occimiano
    "01.6",   # ERROR 03 AGUA — falla de agua atribuible a Occimiano
    "01.7",   # OTROS (F.A.O) — otro tipo de falla atribuible a Occimiano
    "02.1",   # MANIOBRA NO REALIZADA EN LA MP — procedimiento no ejecutado
    "02.2",   # BY PASS / MOTORES — atribuible a Occimiano
    "02.5",   # ERROR 01 ELECTRICO (variante 02)
    "02.6",   # ERROR 03 AGUA (variante 02)
    "02.7",   # OTROS (F.A.O, variante 02)
}
_KEYWORDS_CLIENTE = (
    "CLIENTE", "MOJADO", "DAÑADO", "DANADO",
    "MAL USO", "CONCESIONARIO", "SAL", "PROGRAMACION",
)
_KEYWORDS_TECNICO = ("MANIOBRA", "REPUESTO", "DESGASTE", "NO REALIZ", "BYPASS", "BY PASS")

# Categoría "ANULADO POR EDS": el concesionario pidió NO hacer la mantención.
# La OT queda resuelta sin trabajo → no penaliza al técnico ni al KPI Llenado.
# Puede aparecer en:
#   - causes_description:       "05.1 ANULADO POR CORREO"
#   - types_description:        "5.- ANULADO POR EDS"
#   - detection_method_desc.:   "5.- ANULADO POR EDS"
_PREFIJOS_ANULADO = {"05.1", "05.2"}   # 05.x = ANULADO POR EDS
_KEYWORDS_ANULADO = ("ANULADO POR EDS", "ANULADO POR CORREO", "ANULADO EDS")


def es_anulado_por_eds(*textos: str) -> bool:
    """True si CUALQUIERA de los textos contiene 'ANULADO POR EDS/CORREO'."""
    for t in textos:
        if not t:
            continue
        s = str(t).strip().upper()
        if not s:
            continue
        # Prefijo 05.x
        import re as _re_anul
        m = _re_anul.match(r"(\d{2}\.\d)", s)
        if m and m.group(1) in _PREFIJOS_ANULADO:
            return True
        # Prefijo tipo "5.-" (sin punto decimal)
        if _re_anul.match(r"^5[\.\-]", s):
            if any(kw in s for kw in _KEYWORDS_ANULADO) or "ANULADO" in s:
                return True
        # Keywords directos
        if any(kw in s for kw in _KEYWORDS_ANULADO):
            return True
    return False


# ── Clasificación del tipo de falla (columna "Falla" en Fracttal) ─────────────
# Catálogo real de Fracttal Occimiano (types_description):
#   "01.- F. N. A. O."       → No Atribuible a Occimiano  (cliente/externo)
#   "02.- F. A.O."           → Atribuible a Occimiano     (responsabilidad técnico)
#   "03.- TRABAJOS ESPECIALES"
#   "04.- SIN INFORMACION"
#
# ESTRATEGIA: usar el prefijo numérico "NN.-" como clave primaria — es robusto
# frente a variaciones de espaciado o acentos en el texto ("F. N. A. O." vs "F.N.A.O").
# Fallback: búsqueda de texto normalizado (sin espacios) para formatos desconocidos.

_FALLA_PREFIX_MAP = {
    "01": "fnao",
    "02": "fao",
    "03": "especial",
    "04": "sin_info",
}


def classify_falla_type(falla: str) -> str:
    """
    Clasifica el campo 'Falla' (types_description) de una OT correctiva.

    Returns:
        "fnao"     → Falla No Atribuible a Occimiano (no responsabilidad técnico)
        "fao"      → Falla Atribuible a Occimiano (responsabilidad confirmada)
        "sin_info" → SIN INFORMACION u otro valor sin clasificar
        "especial" → TRABAJOS ESPECIALES (no es falla propiamente)
        "sin_dato" → campo vacío / None
    """
    falla = (falla or "").strip().upper()
    if not falla:
        return "sin_dato"

    # Primario: prefijo numérico "NN.-" — más robusto que match de texto
    m = re.match(r"^(\d{2})\.-", falla)
    if m:
        return _FALLA_PREFIX_MAP.get(m.group(1), "sin_info")

    # Fallback: texto normalizado sin espacios (cubre formatos sin prefijo)
    falla_norm = falla.replace(" ", "").replace(".", "")
    if "FNAO" in falla_norm or "NOATRIBUIBLE" in falla_norm:
        return "fnao"
    if "FAO" in falla_norm or "ATRIBUIBLEAOCCIM" in falla_norm:
        return "fao"
    if "TRABAJOESPECIAL" in falla_norm:
        return "especial"
    return "sin_info"


def classify_causa_raiz(causa: str) -> str:
    """
    Clasifica la causa raíz registrada en una OT correctiva.

    Returns:
        "anulado"        → OT anulada por EDS (concesionario pidió NO hacer)
                           → causa_ok automático, no penaliza al técnico
        "cliente"        → falla imputable al cliente/concesionario (no afecta técnico)
        "tecnico"        → falla imputable al técnico
        "sin_clasificar" → campo vacío, SIN CLASIFICAR, 01.7 OTROS u otro código
                           vago → doble penalización: KPI Llenado + tratado como técnico
    """
    causa = (causa or "").strip().upper()
    if not causa or causa in ("SIN CLASIFICAR", "NONE", "-", ""):
        return "sin_clasificar"

    # Extraer prefijo numérico (ej: "01.4" de "01.4.- REPUESTOS...")
    m = re.match(r"(\d{2}\.\d)", causa)
    prefix = m.group(1) if m else ""

    if prefix in _PREFIJOS_ANULADO or es_anulado_por_eds(causa):
        return "anulado"
    if prefix in _PREFIJOS_CLIENTE or any(kw in causa for kw in _KEYWORDS_CLIENTE):
        return "cliente"
    if prefix in _PREFIJOS_TECNICO or any(kw in causa for kw in _KEYWORDS_TECNICO):
        return "tecnico"

    # 01.7 OTROS, códigos desconocidos o texto demasiado vago
    return "sin_clasificar"


# ══════════════════════════════════════════════════════════════════════════════
# GRUPOS DE TRABAJO — BONO TERRENO (período junio–agosto 2026)
# ══════════════════════════════════════════════════════════════════════════════
GRUPOS_TERRENO = {
    # Orden de aparición en el dashboard: Gallardo → Pinto → Bahamonde → Avila Norte → Avila Sur
    "Juan Gallardo": {
        "senior":   "Juan Gallardo",
        # Juan Gallardo = Juan Antonio Gallardo Romero
        # Edison Carrasco = Edison Jhon Carrasco Navarro
        "miembros": ["Juan Gallardo", "Javier Hein", "Edison Carrasco"],
    },
    # Región Metropolitana — equipos nombrados por su jefe
    "Luis Pinto": {
        "senior":   "Luis Pinto",
        # Luis Pinto = Luis Alberto Pinto Jofre
        # Juan Francisco = Juan Francisco Toro Jimenez
        # Jorge Rodriguez = Jorge Raúl Rodríguez Fuentes
        # Breyans Toledo = Breyans Andrés Toledo Quintana
        "miembros": ["Luis Pinto", "Juan Francisco", "Jorge Rodriguez", "Breyans Toledo"],
    },
    "Victor Bahamonde": {
        "senior":   "Victor Bahamonde",
        # Victor Bahamonde = Victor Hugo Bahamonde Bustamante
        # Martin Flores = Martín Ignacio Flores Galaz
        # Ignacio Ferrari = Iván Ignacio Vergara Ferrari (transfer desde Juan Gallardo, 2026-06-22)
        "miembros": ["Victor Bahamonde", "Martin Flores", "Eduardo Toro", "Ignacio Ferrari"],
    },
    # Carlos Avila Norte — Coquimbo (equipo directo de Carlos Avila)
    "Carlos Avila Norte": {
        "senior":   "Carlos Avila",
        # Carlos Avila = Carlos Alberto Avila Palacios
        # Edson Perez  = Edson José Pérez Henríquez
        # Erwin Rivera = Erwin Maximiliano Rivera Talamilla
        "miembros": ["Carlos Avila", "Edson Perez", "Erwin Rivera"],
    },
    # Carlos Avila Sur — Concepción (supervisado por Carlos Avila)
    "Carlos Avila Sur": {
        "senior":   "Carlos Avila",
        # Luis Lopez   = Luis Joel Lopez Isla
        # Gaston Fuller= Gastón Eduardo Fuller Quilodrán
        "miembros": ["Carlos Avila", "Luis Lopez", "Gaston Fuller"],
    },
}

# Técnicos que NO aplican al dashboard / bono (según Libro4.xlsx actualizado)
# Nota: Luis Lopez, Edson Perez y Erwin Rivera YA NO son "no aplica" — pasaron a Equipo Norte/Sur
TECNICOS_NO_APLICA: frozenset[str] = frozenset({
    # Personas marcadas "No aplica" en Libro4.xlsx
    "Juan Valle",    # Juan Pablo Valle Guerrero
    "Jaime Ocampo",  # Jaime Humberto Ocampo Romero / Jaime Ocampo
    "Walter Soto",   # Walter Mauricio Soto Curilen
    "Ana Guzman",    # Ana María Guzman Doddis
    # AUTEC es empresa subcontratista, no técnico individual
    "AUTEC",
    "Autec",
    "AUTEC LTDA",
    "AUTEC IQUIQUE",
    # Personal Occimiano (no técnicos de terreno) — aparecen en OTs pero
    # no aplican a KPIs ni bono
    "Alexis Ricardo Rojas Sanchez",
    "Alexis Ricardo Rojas Sánchez",
    "Roberto Carlos Muñoz Ordenes",
    "Eric Esteban Dayller Mesa",
    "Jorge Cáceres Hormaechea",
})

# Lookup rápido: nombre corto → grupo
_TECNICO_A_GRUPO: dict[str, str] = {
    tec: grupo
    for grupo, info in GRUPOS_TERRENO.items()
    for tec in info["miembros"]
}
_TECNICO_A_GRUPO["Carlos Avila"] = "Carlos Avila Norte"


def get_grupo_tecnico(nombre_corto: str) -> str | None:
    """Retorna el nombre del equipo ('Luis Pinto', 'Victor Bahamonde', etc.) o None."""
    return _TECNICO_A_GRUPO.get(nombre_corto)


# ── Transferencias de equipo con fecha de corte ──────────────────────────────
# Antes de la fecha: el técnico pertenece a "desde". Desde la fecha: a "hacia".
# GRUPOS_TERRENO refleja el estado ACTUAL (post-transferencia).
TRANSFERENCIAS_EQUIPO: list[dict] = [
    {
        "tecnico_patterns": ["ignacio ferrari", "vergara ferrari"],
        "desde": "Juan Gallardo",
        "hacia": "Victor Bahamonde",
        "fecha": "2026-06-22",
    },
]


def aplicar_transferencias(df, col_fecha, col_equipo="equipo", col_tecnico=None):
    """Reasigna equipo/grupo para filas anteriores a una transferencia de técnico.

    GRUPOS_TERRENO refleja el roster actual → la asignación base mapea al equipo
    nuevo.  Esta función corrige las filas cuya fecha es anterior al corte,
    devolviéndolas al equipo original.
    """
    if df.empty or col_tecnico is None or not TRANSFERENCIAS_EQUIPO:
        return df
    _dates = pd.to_datetime(df[col_fecha], errors="coerce")
    if _dates.dt.tz is not None:
        _dates = _dates.dt.tz_convert(None)
    _names = df[col_tecnico].fillna("").str.lower().str.strip()
    for t in TRANSFERENCIAS_EQUIPO:
        corte = pd.Timestamp(t["fecha"])
        mask_tec = pd.Series(False, index=df.index)
        for pat in t["tecnico_patterns"]:
            mask_tec |= _names.str.contains(pat, na=False, regex=False)
        mask = mask_tec & (_dates < corte) & (df[col_equipo] == t["hacia"])
        if mask.any():
            df.loc[mask, col_equipo] = t["desde"]
    return df


# Técnicos senior cuyo KPI individual = promedio del equipo completo (no solo sus propios casos).
# Para estos 3, los indicadores SLA, Efectividad MP y Precisión Fracttal se calculan
# como el agregado de todos los miembros de su equipo (incluido el propio senior).
SENIORS: frozenset[str] = frozenset({"Juan Gallardo", "Victor Bahamonde", "Luis Pinto", "Carlos Avila"})

_snr_teams: dict[str, list[str]] = {}
for _gk, _gv in GRUPOS_TERRENO.items():
    _snr_teams.setdefault(_gv["senior"], []).append(_gk)
SENIOR_MULTI_TEAMS: dict[str, list[str]] = {k: v for k, v in _snr_teams.items() if len(v) > 1}


def get_senior_team_members(senior_short: str) -> list[str]:
    """Retorna todos los miembros del equipo del senior (incluido él mismo).
    Para multi-team seniors (Carlos Avila), retorna miembros de todos sus equipos."""
    mt = SENIOR_MULTI_TEAMS.get(senior_short)
    if mt:
        seen = set()
        result = []
        for gk in mt:
            for m in GRUPOS_TERRENO[gk].get("miembros", []):
                if m not in seen:
                    seen.add(m)
                    result.append(m)
        return result
    grp = GRUPOS_TERRENO.get(senior_short)
    return list(grp["miembros"]) if grp else [senior_short]


# ── Clientes reconocidos ─────────────────────────────────────────────────────
CLIENT_MAP = {
    "COPEC": "COPEC",
    "ESMAX": "Aramco (Esmax)",
    "SHELL": "SHELL (Enex)",
    "ABAST": "ABASTIBLE",
    "ENEX": "SHELL (Enex)",
    "ARAMCO": "Aramco (Esmax)",
    "PARTICULAR": "PARTICULAR",
}

CLIENT_COLORS = {
    "COPEC": "#E31837",
    "Aramco (Esmax)": "#00A650",
    "ESMAX (Aramco)": "#00A650",  # alias legacy por si queda algún dato antiguo
    "SHELL (Enex)": "#FFC72C",
    "ABASTIBLE": "#0055A5",
    "PARTICULAR": "#7C3AED",
    "OCCIMIANO": "#6B7280",
    "OTROS": "#9CA3AF",
}


def _eds_occim_str(raw) -> str:
    """Normaliza el código EDS Occimiano: '60079.0' → '60079', deja strings
    no numéricos tal cual (ej. 'EE_S045')."""
    if raw in (None, "", "None"):
        return ""
    try:
        return str(int(float(raw)))
    except (ValueError, TypeError):
        return str(raw).strip()


def _parse_hierarchy(parent_description: str) -> tuple[str, str]:
    """
    Extract (client, station) from strings like:
      '// COPEC/ COPEC COYHAIQUE/ '
      '// ESMAX/ ESMAX CARRASCAL/ '
    """
    if not parent_description:
        return "OTROS", "SIN ESTACION"
    parts = [p.strip() for p in parent_description.split("/") if p.strip()]
    client_raw = parts[0].upper() if parts else ""
    station = parts[1] if len(parts) > 1 else client_raw

    for key, label in CLIENT_MAP.items():
        if key in client_raw:
            return label, station

    return client_raw or "OTROS", station


# ══════════════════════════════════════════════════════════════════════════════
# NUMERALES — parseo y detección de anomalías
# ══════════════════════════════════════════════════════════════════════════════
# El numeral es un CONTADOR acumulativo de fichas. Reglas (definidas con operaciones):
#   • Entre OTs distintas: un salto grande es NORMAL (la máquina vende fichas a
#     clientes entre visitas). NO se evalúa aquí.
#   • Dentro de la misma OT (inicial → final): el técnico solo gasta fichas
#     PROBANDO la máquina tras configurarla. 1–30 normal, 50–100 raro,
#     >100 anómalo, y final < inicial es imposible.
# El valor con ≥8 dígitos se considera tecleo basura (ej. 99999999999999).

_VALOR_GARBAGE     = 10_000_000   # ≥8 díg: un solo valor de numeral sospechoso
_VALOR_SIEMPRE_BASURA = 1_000_000_000  # ≥1B: siempre basura (ningún contador real llega)
_DIFF_INCONGRUENTE = 400_000      # diff dentro de OT > esto = registro imposible

# Mapa categoría → severidad (para colorear / contar en paneles)
_CAT_SEVERIDAD = {
    "normal":       "ok",
    "raro":         "warn",
    "anomalo":      "alert",
    "incongruente": "alert",
    "sin_dato":     "na",
}
# Etiquetas cortas para filtros y leyendas
CAT_LABEL = {
    "normal":       "✅ Normal",
    "raro":         "🟡 Raro (revisar)",
    "anomalo":      "🔴 Anómalo",
    "incongruente": "🟣 Registro incongruente",
    "sin_dato":     "— Sin dato",
}


def _numeral_raw_int(s) -> "int | None":
    """Extrae el entero de un valor de numeral (sin filtrar magnitud)."""
    m = re.search(r"\d+", str(s or ""))
    return int(m.group(0)) if m else None


def clasificar_numeral(inicial, final) -> tuple:
    """
    Clasifica una lectura de numeral según la diferencia final − inicial
    DENTRO de una misma OT (fichas que el técnico usó probando la máquina).

    Categorías:
      'normal'        1–20 fichas (prueba normal)
      'raro'          21–50 fichas (a revisar)
      'anomalo'       >50 fichas  o  Final < Inicial
      'incongruente'  diferencia imposible (>400.000), salto de orden de
                      magnitud (ej. 5.000→50.000) o valor basura (≥8 díg.)
      'sin_dato'      sin valores suficientes para evaluar

    Returns (categoria, etiqueta, fichas).
    """
    vi, vf = _numeral_raw_int(inicial), _numeral_raw_int(final)

    # Valor basura: >= 1B siempre; >= 10M solo si diff no es razonable
    vi_huge = vi is not None and vi >= _VALOR_SIEMPRE_BASURA
    vf_huge = vf is not None and vf >= _VALOR_SIEMPRE_BASURA
    if vi_huge or vf_huge:
        return ("incongruente", "🟣 Valor inválido (basura)", None)
    vi_big = vi is not None and vi >= _VALOR_GARBAGE
    vf_big = vf is not None and vf >= _VALOR_GARBAGE
    if vi_big or vf_big:
        if vi_big and vf_big and vf is not None and vi is not None:
            dif = vf - vi
            if not (0 <= dif <= 20):
                return ("incongruente", "🟣 Valor inválido (basura)", None)
        else:
            return ("incongruente", "🟣 Valor inválido (basura)", None)

    if vi is None or vf is None:
        return ("sin_dato", "—", None)

    fichas = vf - vi

    # ── Incongruente (lo más roto: precede al resto) ──────────────────────────
    if fichas > _DIFF_INCONGRUENTE:
        return ("incongruente", f"🟣 {fichas:,} fichas (registro imposible)", fichas)
    if vi >= 100 and vf >= vi * 9:          # salto ~10× = cero de más
        return ("incongruente", f"🟣 Salto de magnitud ({vi:,}→{vf:,})", fichas)

    # ── Anómalo ───────────────────────────────────────────────────────────────
    # Criterio Occimiano: un técnico rara vez gasta >20 fichas probando la máquina.
    if fichas < 0:
        return ("anomalo", f"🔴 Final < Inicial ({fichas:,})", fichas)
    if fichas > 50:
        return ("anomalo", f"🔴 {fichas:,} fichas (>50)", fichas)

    # ── Raro / Normal ─────────────────────────────────────────────────────────
    if fichas > 20:
        return ("raro", f"🟡 {fichas} fichas (21–50)", fichas)
    return ("normal", f"✅ {fichas} fichas", fichas)


# ── Calidad del numeral para el KPI Precisión Fracttal ───────────────────────
# A diferencia de clasificar_numeral (que da granularidad para el historial), esto
# devuelve un veredicto BINARIO + motivo, usado para puntuar el bono.
# Criterio Occimiano: un técnico rara vez gasta >20 fichas probando una máquina.
# Un valor basura (≥8 díg, ej. 99.999.999) o un salto imposible = dato malo.
_NUMERAL_FICHAS_MAX = 25          # > esto dentro de una misma OT = sospechoso
# (_VALOR_GARBAGE ya definido arriba = 10_000_000)

# Motivos de numeral malo (para etiquetar y mostrar el comentario del técnico)
NUMERAL_MOTIVO_LABEL = {
    "ok":               "✅ Numeral válido",
    "no_aplica":        "🔵 No aplica",
    "no_aplica_mc":     "🔵 No aplica (correctiva)",
    "no_aplica_remota": "🔵 No aplica (atención remota)",
    "sin_numeral":      "❌ Sin numeral",
    "basura":           "🟣 Valor basura (≥8 díg.)",
    "negativo":         "🔴 Final < Inicial",
    "salto_magnitud":   "🟣 Salto de magnitud (cero de más)",
    "exceso_fichas":    "🔴 Exceso de fichas (>20 en una OT)",
}


def eval_numeral_kpi(es_lavadora: bool, inicial, final,
                     es_correctiva: bool = False,
                     form_tiene_numeral=None,
                     es_remota: bool = False) -> tuple:
    """
    Veredicto BINARIO de calidad del numeral para el KPI Precisión.
    Returns (numeral_ok: bool, motivo: str).

    Aplica SOLO a lavadoras/aspiradoras en PREVENTIVAS (por decisión operativa
    2026-07: las correctivas se excluyen del cálculo del numeral porque el
    técnico atiende una falla, no siempre puede leer el contador). Reglas con
    valor registrado (de peor a mejor):
      • basura         → algún valor ≥8 díg (tecleo imposible, ej. 99.999.999)
      • negativo       → final < inicial (imposible, el contador no retrocede)
      • salto_magnitud → final ≥ 9× inicial (un cero de más al teclear)
      • exceso_fichas  → final − inicial > 20 (un técnico no gasta tantas fichas)
      • ok             → numeral coherente

    Sin valor registrado:
      • Preventiva → sin_numeral (el form MP siempre lo pide)
      • Correctiva → no_aplica_mc (excluida del KPI, no penaliza)
    """
    if not es_lavadora:
        return True, "no_aplica"

    # DECISIÓN OPERACIONES (2026-07): las correctivas quedan FUERA del cálculo
    # del KPI de numeral. Siempre devuelven no_aplica (sin penalizar) sin
    # importar si tienen numeral cargado o no.
    if es_correctiva:
        return True, "no_aplica_mc"

    _ni = str(inicial or "").strip()
    _nf = str(final or "").strip()
    _ni = "" if _ni.lower() in ("none", "null") else _ni
    _nf = "" if _nf.lower() in ("none", "null") else _nf
    if not _ni and not _nf:
        return False, "sin_numeral"          # MP: el formulario siempre lo pide

    vi, vf = _numeral_raw_int(_ni), _numeral_raw_int(_nf)

    # Basura: >= 1B siempre; >= 10M solo si la diferencia no es razonable
    # (equipos viejos tienen contadores legítimos de hasta ~300M).
    vi_huge = vi is not None and vi >= _VALOR_SIEMPRE_BASURA
    vf_huge = vf is not None and vf >= _VALOR_SIEMPRE_BASURA
    if vi_huge or vf_huge:
        return False, "basura"
    vi_big = vi is not None and vi >= _VALOR_GARBAGE
    vf_big = vf is not None and vf >= _VALOR_GARBAGE
    if vi_big or vf_big:
        if vi_big and vf_big and vi is not None and vf is not None:
            dif = vf - vi
            if not (0 <= dif <= _NUMERAL_FICHAS_MAX):
                return False, "basura"
        else:
            return False, "basura"

    # Con ambos valores se valida la diferencia dentro de la OT
    if vi is not None and vf is not None:
        dif = vf - vi
        if dif < 0:
            return False, "negativo"
        if vi >= 100 and vf >= vi * 9:
            return False, "salto_magnitud"
        if dif > _NUMERAL_FICHAS_MAX:
            return False, "exceso_fichas"

    # Un solo valor presente (y no basura) → registrado, no se puede validar la diff
    return True, "ok"


# Severidad: peor a mejor. Cuando varias subtareas conviven en una OT, el motivo
# de la OT entera es el peor de las subtareas (la prueba más fuerte de descuido).
_MOTIVO_SEVERIDAD = {
    "basura":          5,
    "negativo":        4,
    "salto_magnitud":  3,
    "exceso_fichas":   2,
    "sin_numeral":     1,
    "ok":              0,
    "no_aplica_mc":    0,
    "no_aplica":       0,
}


def aplicar_numerales_subtarea(ot: pd.DataFrame, df_sub: pd.DataFrame) -> pd.DataFrame:
    """Combina el scoring de numeral por OT con el desglose por subtarea.

    Si una OT está en `df_sub` (al menos una subtarea con numeral o un activo
    con campo numeral en el formulario), su `numeral_ok` se RECALCULA con la
    regla: pasa solo si TODAS sus subtareas pasan. Y `numeral_motivo` toma el
    motivo más severo presente en sus subtareas.

    Para OTs no presentes en `df_sub`, se conserva el numeral_ok original
    calculado por eval_numeral_kpi a nivel OT (compatibilidad pre-backfill).
    """
    if df_sub is None or df_sub.empty or "folio" not in ot.columns:
        return ot
    sub = df_sub.copy()
    # Solo subtareas en activos donde aplica numeral (lavadora/aspira/lavainterior)
    sub = sub[sub["tipo_activo"].isin(("lavadora","aspiradora","lavainterior"))]
    if sub.empty:
        return ot
    # Agregado por OT: la OT pasa solo si TODAS las subtareas-numeral pasan;
    # tomamos el motivo más severo presente.
    sub["_sev"] = sub["motivo"].map(_MOTIVO_SEVERIDAD).fillna(0)
    agg = (sub.groupby("id_ot")
              .agg(_n_ok=("numeral_ok", "min"),       # min(True,True,..)=True; con un False → False
                   _n_sev=("_sev", "max"),
                   _n_motivo=("motivo", lambda s: s.iloc[s.map(_MOTIVO_SEVERIDAD).fillna(0).argmax()]),
                   _n_subs=("numeral_ok", "count"))
              .reset_index()
              .rename(columns={"id_ot": "folio"}))
    out = ot.merge(agg, on="folio", how="left")
    # Solo sobrescribimos para OTs presentes en df_sub
    _mask = out["_n_subs"].notna() & (out["_n_subs"] > 0)
    out.loc[_mask, "numeral_ok"]     = out.loc[_mask, "_n_ok"].astype(bool)
    out.loc[_mask, "numeral_motivo"] = out.loc[_mask, "_n_motivo"]
    out = out.drop(columns=["_n_ok","_n_sev","_n_motivo","_n_subs"], errors="ignore")

    # DECISIÓN OPERACIONES (2026-07): TODAS las correctivas quedan fuera del
    # cálculo del numeral. Antes solo se eximían las correctivas remotas; ahora
    # se eximen todas (presenciales y remotas). El sync de subtareas puede
    # haber marcado numeral_ok=False para correctivas — lo revertimos aquí.
    if "es_correctiva" in out.columns:
        _mc = out["es_correctiva"].fillna(False).astype(bool)
        out.loc[_mc, "numeral_ok"]     = True
        out.loc[_mc, "numeral_motivo"] = "no_aplica_mc"

    return out


def build_numeral_historial(df_wo: pd.DataFrame, eds_code: str = None,
                            n: "int | None" = 10) -> pd.DataFrame:
    """
    Historial de lecturas de numeral por equipo (lavadoras/aspiradoras).

    Toma un df de build_work_orders_df (que ya incluye numeral_inicial/final),
    opcionalmente filtra por EDS (eds_occim == eds_code), y devuelve los últimos
    `n` registros de CADA equipo con su clasificación. Si n=None, no limita
    (útil para la pestaña global con filtros propios).

    Columnas: client, equipment, equipment_code, station, fecha, technician,
              folio, numeral_inicial, numeral_final, fichas, categoria,
              severidad, estado.
    Ordenado por equipo y fecha descendente.
    """
    if df_wo.empty or "numeral_final" not in df_wo.columns:
        return pd.DataFrame()

    df = df_wo.copy()
    if eds_code and "eds_occim" in df.columns:
        df = df[df["eds_occim"] == eds_code]

    # Solo lavadoras/aspiradoras/lavainteriores
    _nombre = df["equipment"].fillna("").str.upper()
    df = df[_nombre.str.contains("LAVAD|ASPIRA|LAVAINT", regex=True, na=False)]
    if df.empty:
        return pd.DataFrame()

    # Solo OTs con al menos un valor de numeral registrado
    _ni = df["numeral_inicial"].fillna("").astype(str).str.strip()
    _nf = df["numeral_final"].fillna("").astype(str).str.strip()
    _tiene = (_ni.str.contains(r"\d", na=False)) | (_nf.str.contains(r"\d", na=False))
    df = df[_tiene]
    if df.empty:
        return pd.DataFrame()

    # Fecha de referencia: finalización; fallback a creación
    df["_fecha"] = df["final_date"].fillna(df["creation_date"])

    # Clasificación por fila (categoría, etiqueta, fichas)
    _clasif = df.apply(
        lambda r: clasificar_numeral(r["numeral_inicial"], r["numeral_final"]),
        axis=1,
    )
    df["categoria"] = _clasif.apply(lambda t: t[0])
    df["estado"]    = _clasif.apply(lambda t: t[1])
    df["fichas"]    = _clasif.apply(lambda t: t[2])
    df["severidad"] = df["categoria"].map(_CAT_SEVERIDAD).fillna("na")

    # Orden por equipo y fecha descendente; limitar últimos n por equipo si aplica
    df = df.sort_values(["equipment_code", "_fecha"], ascending=[True, False])
    if n is not None:
        df = df.groupby("equipment_code", group_keys=False).head(n)

    cols = ["client", "equipment", "equipment_code", "station", "eds_occim", "_fecha",
            "technician", "folio", "numeral_inicial", "numeral_final", "fichas",
            "categoria", "severidad", "estado", "comentario_tecnico"]
    cols = [c for c in cols if c in df.columns]
    return df[cols].rename(columns={"_fecha": "fecha"}).reset_index(drop=True)


_SEQ_SALTO_FACTOR = 5     # inicial ≥ 5× el final previo = salto de magnitud
_SEQ_PREV_MIN     = 200   # piso para evaluar saltos (evita ruido en contadores chicos)


def analizar_secuencias(df_hist: pd.DataFrame, n: "int | None" = 10) -> pd.DataFrame:
    """
    Validación de secuencia ENTRE OTs (no solo dentro de la OT).

    Un numeral es un contador acumulativo: entre dos visitas SOLO puede subir
    (ventas de fichas a clientes). Por eso, comparando el numeral inicial de cada
    visita contra el numeral final de la visita ANTERIOR del mismo equipo, se
    detectan errores que se arrastran y que la validación intra-OT no ve:

      • Retroceso: inicial < final previo  → el contador "bajó" (imposible:
        error inflado arrastrado, o una corrección tardía).
      • Salto de magnitud: inicial ≥ 5× el final previo → dígito de más
        introducido justo en esa visita.

    Recibe la salida de build_numeral_historial(n=None) y devuelve el historial
    ordenado cronológicamente por equipo, con columnas extra:
      salto_seq      (str: etiqueta del salto o "")
      seq_severidad  ('alert' | 'ok')
      prev_final     (int | None: final de la visita previa, para contexto)
    Limita a los últimos `n` registros por equipo (None = sin límite).
    """
    if df_hist.empty:
        return df_hist

    df = df_hist.copy()
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce", utc=True)
    df = df.sort_values(["equipment_code", "fecha"])

    df["_vi"] = df["numeral_inicial"].apply(_numeral_raw_int)
    df["_vf"] = df["numeral_final"].apply(_numeral_raw_int)
    # Final de la visita previa del mismo equipo.
    # ffill() primero: si una OT no tiene numeral final, no rompe la cadena
    # para la siguiente visita (usamos el último final conocido del equipo).
    df["prev_final"] = df.groupby("equipment_code")["_vf"].transform(
        lambda s: s.ffill().shift(1)
    )

    def _seq(row):
        pf, ci = row["prev_final"], row["_vi"]
        if pd.isna(pf) or ci is None:
            return ("", "ok")
        pf = int(pf)
        if pf <= 0:
            return ("", "ok")
        if ci < pf:
            return (f"🔴 Retroceso (previo {pf:,} → inicial {ci:,})", "alert")
        if pf >= _SEQ_PREV_MIN and ci >= pf * _SEQ_SALTO_FACTOR:
            return (f"🟣 Salto ×{ci/pf:.0f} (previo {pf:,} → inicial {ci:,})", "alert")
        return ("", "ok")

    _res = df.apply(_seq, axis=1)
    df["salto_seq"]     = _res.apply(lambda t: t[0])
    df["seq_severidad"] = _res.apply(lambda t: t[1])

    # Últimos n por equipo (mantener orden cronológico ascendente dentro del grupo)
    if n is not None:
        df = df.groupby("equipment_code", group_keys=False).tail(n)

    return df.drop(columns=["_vi", "_vf"], errors="ignore")


def build_work_orders_df(raw: list) -> pd.DataFrame:
    """
    Convierte la lista cruda de work_orders de Fracttal en un DataFrame limpio.

    Optimización: extrae todos los campos en listas paralelas en un solo recorrido
    y llama pd.to_datetime UNA SOLA VEZ por columna de fecha (10-50× más rápido
    que convertir fila por fila en el bucle). El resultado es idéntico al anterior.
    """
    if not raw:
        return pd.DataFrame()

    # ── Un solo recorrido para extraer listas paralelas ───────────────────────
    clients, stations       = [], []
    creation_dates          = []
    final_dates             = []
    ids, folios             = [], []
    equipments, eq_codes    = [], []
    maint_type_raws         = []
    status_ids              = []
    technicians             = []
    failure_types           = []
    failure_causes          = []
    failure_detections      = []
    failure_severities      = []
    priorities              = []
    ratings                 = []
    costs                   = []
    stop_minutes_list       = []
    numerales_ini           = []
    numerales_fin           = []
    eds_occims              = []
    comentarios_tec         = []

    for wo in raw:
        client, station = _parse_hierarchy(wo.get("parent_description") or "")
        clients.append(client)
        stations.append(station)
        creation_dates.append(wo.get("creation_date") or wo.get("date_maintenance"))
        final_dates.append(wo.get("final_date") or wo.get("wo_final_date"))
        ids.append(wo.get("id_work_order"))
        folios.append(wo.get("wo_folio"))
        equipments.append((wo.get("items_log_description") or "").strip())
        eq_codes.append(wo.get("code") or "")
        maint_type_raws.append(wo.get("tasks_log_task_type_main") or "")
        status_ids.append(wo.get("id_status_work_order"))
        technicians.append((wo.get("personnel_description") or "").strip())
        failure_types.append((wo.get("types_description") or "").strip())
        failure_causes.append((wo.get("causes_description") or "").strip())
        failure_detections.append((wo.get("detection_method_description") or "").strip())
        failure_severities.append((wo.get("severiry_description") or "").strip())
        priorities.append(wo.get("priorities_description") or "")
        ratings.append(wo.get("rating"))
        costs.append(wo.get("total_cost_task") or 0)
        stop_minutes_list.append((wo.get("stop_assets_sec") or 0) / 60)
        numerales_ini.append(wo.get("numeral_inicial"))
        numerales_fin.append(wo.get("numeral_final"))
        comentarios_tec.append((wo.get("comentario_tecnico") or "").strip())
        _eo_raw = wo.get("groups_2_description")
        try:
            eds_occims.append(str(int(float(_eo_raw))) if _eo_raw not in (None, "", "None") else "")
        except (ValueError, TypeError):
            eds_occims.append(str(_eo_raw or "").strip())

    # ── Construir DataFrame de una vez (sin append iterativo) ─────────────────
    df = pd.DataFrame({
        "id_wo":             ids,
        "folio":             folios,
        "client":            clients,
        "station":           stations,
        "equipment":         equipments,
        "equipment_code":    eq_codes,
        "maint_type_raw":    maint_type_raws,
        "status_id":         status_ids,
        "technician":        technicians,
        "failure_type":      failure_types,
        "failure_cause":     failure_causes,
        "failure_detection": failure_detections,
        "failure_severity":  failure_severities,
        "priority":          priorities,
        "rating":            ratings,
        "cost":              costs,
        "stop_minutes":      stop_minutes_list,
        "numeral_inicial":   numerales_ini,
        "numeral_final":     numerales_fin,
        "comentario_tecnico": comentarios_tec,
        "eds_occim":         eds_occims,
    })

    # ── pd.to_datetime vectorizado: UNA llamada por columna (no 20k) ──────────
    # format='ISO8601' acepta con y sin milisegundos (ej: T16:50:00+00:00 Y T16:49:23.263482+00:00)
    df["creation_date"] = pd.to_datetime(creation_dates, format="ISO8601", utc=True, errors="coerce")
    df["final_date"]    = pd.to_datetime(final_dates,    format="ISO8601", utc=True, errors="coerce")

    # ── Clasificación de tipo de mantenimiento (vectorizada) ──────────────────
    # Misma lógica que antes: CORRECTIVA tiene prioridad sobre PREVENTIVA/INSPECC
    _tipo = pd.Series(maint_type_raws, dtype=str).str.upper()
    df["maint_type"] = "Otra"
    df.loc[_tipo.str.contains("PREVENTIVA|INSPECC", na=False, regex=True), "maint_type"] = "Preventiva"
    df.loc[_tipo.str.contains("CORRECTIVA",         na=False),             "maint_type"] = "Correctiva"

    return df


def build_third_parties_df(raw: list) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame()
    rows = []
    for tp in raw:
        name = (tp.get("name") or "").strip()
        client = "OTROS"
        for key, label in CLIENT_MAP.items():
            if key in name.upper():
                client = label
                break
        rows.append({
            "code": tp.get("code"),
            "name": name,
            "client": client,
            "address": (tp.get("address") or "").strip(),
            "city": (tp.get("city") or "").strip().rstrip("\t"),
            "state": (tp.get("state") or "").strip().rstrip("\t"),
            "country": (tp.get("country") or "").strip(),
            "latitude": tp.get("latitude"),
            "longitude": tp.get("longitud"),
            "active": tp.get("active", True),
        })
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════════════════════
# KPI CORRECTO LLENADO DE FRACTTAL
# ══════════════════════════════════════════════════════════════════════════════

def build_kpi_llenado_df(raw: list) -> pd.DataFrame:
    """
    Extrae los campos de calidad de llenado de cada tarea/OT del raw de work_orders.
    Cada fila = UNA tarea dentro de un OT (un OT puede tener N tareas).
    Campos clave:
      - wo_folio      → llave del OT
      - note          → narrativa escrita por el técnico
      - tasks_duration → tiempo de ejecución en segundos
      - resources_*   → recursos imputados (materiales, RRHH, servicios)
      - done          → si la tarea fue marcada como finalizada
    """
    if not raw:
        return pd.DataFrame()

    # ── OPTIMIZACIÓN: pre-computar fechas vectorizadas ────────────────────────
    # ANTES: pd.to_datetime(single_value) dentro del loop llamaba 3× por wo →
    # 52k llamadas × infer format = 38s de los 40s totales.
    # AHORA: una llamada vectorizada por columna → ~200ms total.
    # Los timestamps por índice se leen del pre-cache dentro del loop.
    _n_raw = len(raw)
    _init_series  = pd.to_datetime([w.get("initial_date")  for w in raw], utc=True, errors="coerce", format="ISO8601")
    _final_series = pd.to_datetime([w.get("final_date")    for w in raw], utc=True, errors="coerce", format="ISO8601")
    _creat_series = pd.to_datetime([w.get("creation_date") for w in raw], utc=True, errors="coerce", format="ISO8601")

    rows = []
    for _i, wo in enumerate(raw):
        # Saltar subtareas que nunca se ejecutaron (NO_STARTED).
        # No se puede evaluar calidad de algo que no se hizo.
        _ts = (wo.get("task_status") or "").strip().upper()
        if _ts == "NO_STARTED":
            continue

        # Limpieza de nota (puede ser None o "None" string)
        raw_note = wo.get("note") or ""
        note = "" if str(raw_note).strip() in ("", "None", "none") else str(raw_note).strip()

        raw_task_note = wo.get("task_note") or ""
        task_note = "" if str(raw_task_note).strip() in ("", "None", "none") else str(raw_task_note).strip()

        # Mejor nota disponible (OT-level o task-level)
        best_note = note if note else task_note

        client, station = _parse_hierarchy(wo.get("parent_description") or "")

        # Recursos registrados (cualquier campo distinto de None)
        def _has_value(v) -> bool:
            return v is not None and str(v).strip() not in ("", "None", "none", "0")

        has_resources = any(_has_value(wo.get(f)) for f in [
            "resources_inventory", "resources_human_resources",
            "resources_hours", "resources_services",
        ])

        # Tipo de mantenimiento (necesario para el bloque de duración).
        # Se calcula aquí temprano; abajo se reusa en Causa raíz.
        maint_type_raw = (wo.get("tasks_log_task_type_main") or "").strip().upper()
        es_correctiva = "CORRECTIVA" in maint_type_raw

        # Duración real y estimada.
        # ══════════════════════════════════════════════════════════════
        # REGLA CRÍTICA (blindaje contra bug OS-38480/38481):
        # Para PREVENTIVAS solo aceptamos duracion_*_neta_seg (calculado
        # por sync_estim_neta.py). NUNCA caemos al fallback `duration`
        # (total de la OT) porque eso incluye bomba+ablandador+
        # lavatapices+inspecciones y distorsiona el KPI de tiempo.
        # Si la neta no está poblada → 0 (no evaluable, no penaliza).
        # Un warning en el dashboard lista las OTs sin sincronizar.
        #
        # Para CORRECTIVAS sí es seguro caer a tasks_duration: en Fracttal
        # una MC siempre tiene 1 sola subtarea (la falla puntual).
        # ══════════════════════════════════════════════════════════════
        _real_neto  = wo.get("duracion_real_neta_seg")
        _estim_neto = wo.get("duracion_estim_neta_seg")
        _has_neto_est  = _estim_neto not in (None, "", "None")
        _has_neto_real = _real_neto  not in (None, "", "None")
        try:
            if _has_neto_real:
                duration_sec = int(_real_neto)
            elif es_correctiva:
                duration_sec = int(wo.get("tasks_duration") or 0)
            else:
                duration_sec = 0   # preventiva sin sync → no evaluable
        except (ValueError, TypeError):
            duration_sec = 0
        try:
            if _has_neto_est:
                estimated_sec = int(_estim_neto)
            elif es_correctiva:
                estimated_sec = int(wo.get("duration") or 0)
            else:
                estimated_sec = 0   # preventiva sin sync → no evaluable
        except (ValueError, TypeError):
            estimated_sec = 0
        # Tiempo OK: ejecución >= piso% de la duración estimada
        # Shell (Enex) → 50% (equipos con mantención más corta)
        # Resto         → 70%
        _piso_tiempo = 0.50 if client == "SHELL (Enex)" else 0.70
        tiempo_ok_estim = (
            (duration_sec >= estimated_sec * _piso_tiempo)
            if estimated_sec > 0 else None
        )

        # Estado de la tarea
        done = str(wo.get("done", "False")).lower() in ("true", "1", "yes")

        # Tiempo real que el técnico tuvo Fracttal abierto (initial_date → final_date)
        # Timestamps pre-computados vectorizados (optim 2026-07-10).
        initial_ts = _init_series[_i]
        final_ts   = _final_series[_i]
        if pd.notna(initial_ts) and pd.notna(final_ts):
            elapsed_sec = max(0.0, (final_ts - initial_ts).total_seconds())
        else:
            elapsed_sec = 0.0

        # ── Causa raíz ────────────────────────────────────────────────────────
        raw_causa = (wo.get("causes_description") or "").strip()
        raw_tipo_falla = (wo.get("types_description") or "").strip()
        raw_deteccion  = (wo.get("detection_method_description") or "").strip()
        causa_clasif = classify_causa_raiz(raw_causa)

        # EXCEPCIÓN ANULADO POR EDS: el concesionario pidió no hacer la
        # mantención. Aparece en cualquiera de estos 3 campos:
        #   causes_description:            "05.1 ANULADO POR CORREO"
        #   types_description:             "5.- ANULADO POR EDS"
        #   detection_method_description:  "5.- ANULADO POR EDS"
        # → causa_ok automático (no penaliza al técnico), sobreescribe causa_clasif.
        _anulado = es_anulado_por_eds(raw_causa, raw_tipo_falla, raw_deteccion)
        if _anulado:
            causa_clasif = "anulado"

        # Causa raíz aplica a AMBOS tipos (MC y MP).
        # MC: debe tener código Fracttal válido (01.x – 04.x) o keyword reconocida.
        # MP: cualquier texto no vacío es aceptado (el técnico documenta lo observado).
        # Malo para MC: vacío, "SIN CLASIFICAR", números sueltos (150, 1, 12345, etc.).
        # Bueno para MC: código con prefijo Fracttal real (01.x, 02.x, 03.x, 04.x).
        _causa_tiene_codigo = bool(re.match(r"0[1-4]\.\d", raw_causa))
        if _anulado:
            causa_ok = True   # ANULADO POR EDS: OT resuelta sin trabajo, no penaliza
        elif es_correctiva:
            causa_ok = (
                _causa_tiene_codigo                         # MC: código Fracttal (0X.X.-)
                or causa_clasif in ("tecnico", "cliente")   # MC: también reconocido por keywords
            )
        else:
            causa_ok = bool(raw_causa and raw_causa.upper() not in ("SIN CLASIFICAR", "N/A", "NA"))

        # ── Numeral (lectura del contador de fichas) ───────────────────────────
        # REGLA: aplica a LAVADORAS, ASPIRADORAS y LAVAINTERIORES.
        # El resto (ablandadores, compresores, etc.) → numeral_ok = True automático.
        #
        # FUENTE PRIMARIA (confiable): valor REAL del formulario de la tarea,
        #   extraído de /api/work_orders_subtasks/ y persistido en Supabase:
        #     numeral_inicial → ítem "TOMA DE NUMERAL INICIAL" (type=3)
        #     numeral_final   → ítem "TOMA DE NUMERAL FINAL"   (type=5)
        # FALLBACK (heurístico): si no hay valor real (ruta Fracttal directa o
        #   caché previo), se busca un número ≥4 dígitos en la nota. Menos fiable.
        _equipo_nombre = (wo.get("items_log_description") or "").strip().upper()
        _es_lavadora_por_nombre = bool(re.search(r"LAVAD|ASPIRA|LAVAINT", _equipo_nombre))
        # Señal más confiable: el sync detectó que el formulario tenía campo
        # numeral (form_tiene_numeral). Cubre OTs compuestas tipo OS-37930 donde
        # el activo principal es Bomba pero hay subtareas de Lavadora/Aspiradora
        # con su numeral. Si el sync aún no corrió (form_tiene_numeral=None),
        # caemos al heurístico por nombre del activo principal.
        _form_num_raw = wo.get("form_tiene_numeral")
        if _form_num_raw is True:
            _es_lavadora = True
        elif _form_num_raw is False:
            _es_lavadora = False
        else:
            _es_lavadora = _es_lavadora_por_nombre

        _num_inicial = str(wo.get("numeral_inicial") or "").strip()
        _num_final   = str(wo.get("numeral_final")   or "").strip()
        _num_inicial = "" if _num_inicial.lower() in ("none", "null") else _num_inicial
        _num_final   = "" if _num_final.lower()   in ("none", "null") else _num_final

        if _num_inicial or _num_final:
            # Valor real disponible → fuente de verdad
            _numeral_valor = _num_final or _num_inicial   # final = lectura vigente
        elif _es_lavadora:
            # Fallback regex sobre la nota (legacy / Fracttal directo)
            _texto_numeral = (best_note + " " + task_note).upper()
            _numeral_match = re.search(r"\b\d{4,}\b", _texto_numeral)
            _numeral_valor = _numeral_match.group(0) if _numeral_match else ""
        else:
            _numeral_valor = ""

        # Fichas del período = final − inicial (cuando ambos son numéricos).
        # Guard de sanidad: valores con >7 dígitos son casi siempre tecleo erróneo
        # (ej. 99999999999999). Diferencia negativa o absurda → no se reporta.
        def _to_int(s):
            m = re.search(r"\d+", s or "")
            if not m:
                return None
            v = int(m.group(0))
            return v if v < 10_000_000 else None   # descartar basura (>7 díg.)
        _vi, _vf = _to_int(_num_inicial), _to_int(_num_final)
        if _vi is not None and _vf is not None and 0 <= (_vf - _vi) <= 1_000_000:
            _fichas_periodo = _vf - _vi
        else:
            _fichas_periodo = None   # negativo, reseteo de contador o dato corrupto

        # ── Método de detección de falla ──────────────────────────────────────
        # Campo detection_method_description de Fracttal (Análisis de Fallas).
        # Valores válidos: "1.- ATENDIDO PRESENCIAL", "2.- ATENDIDO VÍA REMOTA",
        #                  "3.- ATENDIDO CON SU MP",  "4.- LLAMADO DUPLICADO"
        # Inválido:        "" o "SIN CLASIFICAR" → el técnico no actualizó el campo.
        # REGLA: las PMs siempre son presenciales → siempre OK, no penalizar.
        raw_deteccion = (wo.get("detection_method_description") or "").strip().upper()
        deteccion_ok = (
            not es_correctiva                                   # PM → siempre OK
            or (bool(raw_deteccion) and "SIN CLASIFICAR" not in raw_deteccion)
        )
        # "Es remota" debe leerse de modalidad_atencion (ej. "2.- ATENDIDO
        # VÍA REMOTA"), NO de detection_method_description. Fallback al
        # antiguo por compatibilidad con OTs viejas que solo llenaban
        # detection_method.
        raw_modalidad = (wo.get("modalidad_atencion") or "").strip().upper()
        _es_remota = (
            "REMOTA" in raw_modalidad or "REMOTO" in raw_modalidad
            or "REMOTA" in raw_deteccion or "REMOTO" in raw_deteccion
        )

        # CALIDAD del numeral (no solo presencia): un 99.999.999 o un salto de
        # >20 fichas dentro de la OT ahora cuenta como dato MALO (numeral_ok=False).
        # Aplica a MC y MP; en MC sin valor, solo penaliza si el form tenía el campo.
        # Si la MC fue REMOTA, el numeral no aplica (no hay forma de leerlo).
        _form_num = wo.get("form_tiene_numeral")
        numeral_ok, numeral_motivo = eval_numeral_kpi(
            _es_lavadora, _num_inicial, _num_final,
            es_correctiva=es_correctiva, form_tiene_numeral=_form_num,
            es_remota=_es_remota,
        )

        rows.append({
            "folio":            wo.get("wo_folio") or "",
            "id_task":          wo.get("id_work_orders_tasks"),
            "tecnico":          (wo.get("personnel_description") or "").strip(),
            "client":           client,
            "station":          station,
            "equipment_code":   (wo.get("code") or "").strip(),
            "eds_occim":        _eds_occim_str(wo.get("groups_2_description")),
            "creation_date":    _creat_series[_i],
            "initial_date":     initial_ts,
            "final_date":       final_ts,
            "maint_type":       maint_type_raw,
            "es_correctiva":    es_correctiva,
            "status_id":        wo.get("id_status_work_order"),
            "wo_status":        wo.get("wo_status") or {1:"Por Iniciar",2:"En Progreso",3:"Finalizadas",4:"Por Validar",5:"Canceladas"}.get(wo.get("id_status_work_order"), ""),
            "task_status":      (wo.get("task_status") or "").upper(),
            "note":             best_note,
            "note_words":       len(best_note.split()) if best_note else 0,
            "task_done":        done,
            "duration_sec":     duration_sec,
            "elapsed_sec":      elapsed_sec,
            "has_resources":    has_resources,
            "rating":           int(wo.get("rating") or 0),
            "completed_pct":    int(wo.get("completed_percentage") or 0),
            # ── Campos KPI Llenado ─────────────────────────────────────────────
            "causa_raiz_raw":    raw_causa,
            "causa_clasif":      causa_clasif,
            "causa_ok":          causa_ok,
            "comentario_tecnico": (wo.get("comentario_tecnico") or "").strip(),
            "numeral_ok":        numeral_ok,
            "numeral_motivo":    numeral_motivo,
            "form_tiene_numeral": wo.get("form_tiene_numeral"),
            "es_lavadora":       _es_lavadora,
            "equipo_nombre":     _equipo_nombre,   # UPPERCASE, para mostrar tipo
            "numeral_valor":     _numeral_valor,
            "numeral_inicial":   _num_inicial,
            "numeral_final":     _num_final,
            "fichas_periodo":    _fichas_periodo,
            "deteccion_raw":     raw_deteccion,
            "modalidad_atencion": raw_modalidad,   # propagar para _es_remota_row
            "deteccion_ok":      deteccion_ok,
            "duration_sec":      duration_sec,
            "estimated_sec":     estimated_sec,
            "tiempo_ok_estim":   tiempo_ok_estim,
        })

    return pd.DataFrame(rows)


def score_llenado_por_ot(df_kpi: pd.DataFrame) -> pd.DataFrame:
    """
    Calcula puntaje de calidad de llenado por OT (0–100).

    KPI Llenado Fracttal — 3 componentes (25 pts c/u = 75 total):
    ┌──────────────────────────────────┬──────┬────────────────────────────────────────────────────┐
    │ Componente                       │ Pts  │ Criterio                                           │
    ├──────────────────────────────────┼──────┼────────────────────────────────────────────────────┤
    │ 1. Tiempo de ejecución           │ 0–25 │ MC: >15min→25 | 5-15→12 | <5→0                    │
    │                                  │      │ MP (con estim.): ≥70%→25 | 35-69%→12 | <35%→0     │
    │                                  │      │ MP (sin estim.): >30min→25 | 15-30→12 | <15→0      │
    │ 2. Causa raíz llenada            │ 0–25 │ MC: código Fracttal válido → 25 | vacía/vaga → 0   │
    │                                  │      │ MP: no aplica (sin falla que documentar) → 25 auto │
    │ 3. Numeral registrado            │ 0–25 │ lavadora/aspiradora (MC+MP): numeral válido → 25  │
    │                                  │      │ basura/exceso>20/salto/negativo/sin dato → 0      │
    │                                  │      │ equipo sin numeral (no lavadora) → 25 (no aplica) │
    └──────────────────────────────────┴──────┴────────────────────────────────────────────────────┘
    Nota: Modalidad de atención (ex componente 4) se muestra como dato informativo
    pero no entra al KPI (no está en contrato).

    Una OT es "mala" si score_total < 75 (al menos 1 componente falló).
    El KPI mide la CANTIDAD DE OTs MALAS, no la suma de errores individuales.
    """
    if df_kpi.empty:
        return pd.DataFrame()

    _agg = dict(
        tecnico=        ("tecnico",        "first"),
        client=         ("client",         "first"),
        station=        ("station",        "first"),
        creation_date=  ("creation_date",  "first"),
        final_date=     ("final_date",     "max"),
        maint_type=     ("maint_type",     "first"),
        es_correctiva=  ("es_correctiva",  "first"),
        status_id=      ("status_id",      "first"),
        note=           ("note",           "first"),
        note_words=     ("note_words",     "first"),
        total_tasks=    ("task_done",      "count"),
        done_tasks=     ("task_done",      "sum"),
        max_elapsed=    ("elapsed_sec",    "max"),
        has_resources=  ("has_resources",  "any"),
        rating=         ("rating",         "first"),
        completed_pct=  ("completed_pct",  "first"),
        causa_raiz_raw= ("causa_raiz_raw", "first"),
        causa_clasif=   ("causa_clasif",   "first"),
        causa_ok=       ("causa_ok",       "first"),
        numeral_ok=     ("numeral_ok",     "any"),   # True si CUALQUIER tarea tiene numeral
    )
    # Deteccion puede faltar en caches pre-migración
    if "deteccion_ok" in df_kpi.columns:
        _agg["deteccion_ok"]  = ("deteccion_ok",  "first")
        _agg["deteccion_raw"] = ("deteccion_raw", "first")
    if "modalidad_atencion" in df_kpi.columns:
        _agg["modalidad_atencion"] = ("modalidad_atencion", "first")
    # Campos de numeral (pueden faltar en caches pre-migración)
    if "es_lavadora" in df_kpi.columns:
        # any() = True si CUALQUIER subtarea es de lavadora/aspiradora.
        # (antes era 'first', pero una OT con 4 equipos donde solo uno es
        # lavadora puede tomar first=False del ablandador y marcar
        # 'no aplica', mientras que numeral_ok=any() reporta el numeral
        # faltante de la lavadora → contradicción visual en la tabla).
        _agg["es_lavadora"]   = ("es_lavadora",   "any")
    if "numeral_valor" in df_kpi.columns:
        # Tomar el primer valor no-vacío entre las tareas de la OT
        _agg["numeral_valor"] = ("numeral_valor", lambda x: next((v for v in x if v), ""))
    if "numeral_inicial" in df_kpi.columns:
        _agg["numeral_inicial"] = ("numeral_inicial", lambda x: next((v for v in x if v), ""))
    if "numeral_final" in df_kpi.columns:
        _agg["numeral_final"]   = ("numeral_final",   lambda x: next((v for v in x if v), ""))
    if "comentario_tecnico" in df_kpi.columns:
        _agg["comentario_tecnico"] = ("comentario_tecnico", lambda x: next((v for v in x if v), ""))
    if "form_tiene_numeral" in df_kpi.columns:
        # True si CUALQUIER tarea de la OT tenía el campo numeral en el formulario
        _agg["form_tiene_numeral"] = ("form_tiene_numeral", lambda x: bool(any(bool(v) for v in x)))
    if "equipment_code" in df_kpi.columns:
        _agg["equipment_code"] = ("equipment_code", "first")
    if "equipo_nombre" in df_kpi.columns:
        # Concatenar nombres únicos de todos los equipos de la OT (para
        # detectar en el dashboard cuando una OT toca varios tipos: p.ej.
        # LAVADORA + ABLANDADOR + BOMBA).
        _agg["equipo_nombre"] = ("equipo_nombre",
            lambda x: " · ".join(sorted({str(v).strip() for v in x if v})))
    if "eds_occim" in df_kpi.columns:
        _agg["eds_occim"] = ("eds_occim", "first")
    if "fichas_periodo" in df_kpi.columns:
        _agg["fichas_periodo"]  = ("fichas_periodo",  lambda x: next((v for v in x if v is not None), None))
    if "wo_status" in df_kpi.columns:
        _agg["wo_status"] = ("wo_status", "first")

    ot = df_kpi.groupby("folio").agg(**_agg).reset_index()

    if "wo_status" not in ot.columns:
        ot["wo_status"] = ""

    # Guard: asegurar columnas nuevas aunque vengan de caché viejo
    if "deteccion_ok"  not in ot.columns: ot["deteccion_ok"]  = False
    if "deteccion_raw" not in ot.columns: ot["deteccion_raw"] = ""
    if "es_lavadora"   not in ot.columns: ot["es_lavadora"]   = True   # conservador: asumir lavadora
    if "numeral_valor" not in ot.columns: ot["numeral_valor"]  = ""
    if "numeral_inicial" not in ot.columns: ot["numeral_inicial"] = ""
    if "numeral_final"   not in ot.columns: ot["numeral_final"]   = ""
    if "fichas_periodo"  not in ot.columns: ot["fichas_periodo"]  = None
    if "comentario_tecnico" not in ot.columns: ot["comentario_tecnico"] = ""
    if "form_tiene_numeral" not in ot.columns: ot["form_tiene_numeral"] = None
    if "equipment_code" not in ot.columns: ot["equipment_code"] = ""
    if "eds_occim" not in ot.columns: ot["eds_occim"] = ""

    # Re-evaluar la CALIDAD del numeral a nivel OT desde los valores agregados
    # (inicial/final = primer no-vacío). Garantiza que el veredicto coincida con
    # lo que se muestra en la tabla, y aplica la regla de calidad aunque el caché
    # venga de antes del cambio (donde numeral_ok era solo presencia).
    def _es_remota_row(r) -> bool:
        # Fuente primaria: modalidad_atencion (ej. "2.- ATENDIDO VÍA REMOTA").
        # Fallback: detection_method_description (deteccion_raw).
        mod = str(r.get("modalidad_atencion", "") or "").upper()
        if "REMOTA" in mod or "REMOTO" in mod:
            return True
        det = str(r.get("deteccion_raw", "") or "").upper()
        return "REMOTA" in det or "REMOTO" in det
    _num_eval = ot.apply(
        lambda r: eval_numeral_kpi(
            r.get("es_lavadora", True), r.get("numeral_inicial", ""), r.get("numeral_final", ""),
            es_correctiva=r.get("es_correctiva", False),
            form_tiene_numeral=r.get("form_tiene_numeral"),
            es_remota=_es_remota_row(r),
        ),
        axis=1,
    )
    ot["numeral_ok"]     = _num_eval.apply(lambda t: t[0])
    ot["numeral_motivo"] = _num_eval.apply(lambda t: t[1])

    # Agregar campos de tiempo condicionalmente (pueden faltar en caché viejo)
    # Se usa .first() en lugar de .sum() porque los valores neta (de sync)
    # son a nivel de OT y están replicados en cada fila de equipo; sumarlos
    # multiplicaría el valor por la cantidad de subtareas.
    if "duration_sec" in df_kpi.columns:
        _exec_g = df_kpi.groupby("folio")["duration_sec"].first().rename("exec_sec_sum")
        ot = ot.merge(_exec_g, on="folio", how="left")
    else:
        ot["exec_sec_sum"] = 0

    if "estimated_sec" in df_kpi.columns:
        _estim_g = df_kpi.groupby("folio")["estimated_sec"].first().rename("estim_sec_sum")
        ot = ot.merge(_estim_g, on="folio", how="left")
        ot["estim_sec_sum"] = ot["estim_sec_sum"].fillna(0)
        ot["tiempo_ok_estim"] = ot.apply(
            lambda r: (r["exec_sec_sum"] >= r["estim_sec_sum"]
                       * (0.50 if r.get("client") == "SHELL (Enex)" else 0.70))
                      if r["estim_sec_sum"] > 0 else None,
            axis=1,
        )
    else:
        ot["estim_sec_sum"]   = 0
        ot["tiempo_ok_estim"] = None

    ot["elapsed_min"] = (ot["max_elapsed"] / 60).round(1)
    ot["pct_tareas"]  = (
        ot["done_tasks"] / ot["total_tasks"].clip(lower=1) * 100
    ).round(1)

    # ── Componente 1: Tiempo de ejecución (0–25 pts) ──────────────────────────
    # MC: < 5 min → 0 | 5-15 min → 12 | > 15 min → 25
    # MP (con estim.): usa max(tasks_duration, elapsed) / estimado
    #   Regla operativa (validada con Ops 06-jul-2026):
    #     ≥75% → 25 (cumple)
    #     35-74% → 12 (cumple parcial)
    #     <35% → 0 (no cumple; posible ficticio o técnico no realizó bien)
    #     >150% → 25 (CUMPLE + marca sobretiempo; informativo, no penaliza).
    #             Un técnico que tarda más NO es un problema de calidad; solo
    #             informa exceso. Se marca en 'sobretiempo' para reportes.
    # MP (sin estim.): <15 min → 0 | 15-30 min → 12 | >30 min → 25
    # NOTA: para PMs se usa max(exec_sec_sum, max_elapsed) porque si el técnico
    #       no llenó tasks_duration (= 0) pero tuvo el OT abierto 100 min, ese
    #       tiempo real debe contar. La función max() toma el mayor valor disponible.
    def _score_tiempo(row) -> tuple:
        """Retorna (puntaje, sobretiempo_bool) — sobretiempo NO penaliza."""
        if row["es_correctiva"]:
            return 25, False  # Tiempo no se evalúa en correctivas → 25 pts auto
        elapsed = row["max_elapsed"]
        estim = row.get("estim_sec_sum", 0) or 0
        _piso = 0.50 if row.get("client") == "SHELL (Enex)" else 0.70
        if estim > 60:
            exec_r = row.get("exec_sec_sum", 0) or 0
            effective = max(exec_r, elapsed)
            ratio = effective / estim
            if ratio > 1.50:
                return 25, True
            if ratio >= _piso: return 25, False
            if ratio >= 0.35: return 12, False
            return 0, False
        else:
            if elapsed > 1800: return 25, False
            if elapsed > 900:  return 12, False
            return 0, False

    _scores_time = ot.apply(_score_tiempo, axis=1)
    ot["score_tiempo"]  = _scores_time.apply(lambda t: t[0])
    ot["sobretiempo"]   = _scores_time.apply(lambda t: t[1])

    # ── Componente 2: Causa raíz llenada (0–25 pts) ───────────────────────────
    # SOLO aplica a CORRECTIVAS. Una preventiva no responde a una falla — no
    # hay causa raíz que documentar — así que MP siempre da 25 pts auto (mismo
    # criterio que Tiempo en MC: no se penaliza lo que no aplica).
    ot["score_causa"] = ot.apply(
        lambda r: 25 if not r["es_correctiva"] else (25 if r["causa_ok"] else 0),
        axis=1,
    )

    # ── Componente 3: Numeral registrado (0–25 pts) ───────────────────────────
    # Aplica a TODA lavadora/aspiradora (MC y MP): el formulario exige el numeral
    # en ambos tipos, así que un dato basura en una correctiva también penaliza.
    # numeral_ok ya devuelve True para equipos que no son lavadora (no_aplica).
    # NOTA: si hay datos en numerales_subtarea (1 fila/activo), se reemplaza este
    # numeral_ok al inicio (en aplicar_numerales_subtarea) por la regla:
    # "una OT pasa SOLO si TODAS sus subtareas-numeral pasan". Aquí solo
    # mapeamos el booleano a 25/0.
    ot["score_numeral"] = ot["numeral_ok"].apply(lambda ok: 25 if ok else 0)

    # ── Componente 4: Método de detección de falla (0–25 pts) ────────────────
    # OK = cualquier valor que no sea vacío ni "SIN CLASIFICAR"
    ot["score_deteccion"] = ot["deteccion_ok"].apply(lambda ok: 25 if ok else 0)

    # ── Etiqueta quick-tick (informativa) ─────────────────────────────────────
    def _quick_tick_label(row) -> str:
        elapsed = row["max_elapsed"]
        is_prev = not row["es_correctiva"]
        if elapsed <= 60:
            return "🔴 Quick-tick (<1 min)"
        if elapsed <= 300 and not is_prev:
            return "⚠️ Muy rápido (<5 min)"
        if elapsed <= 900 and is_prev:
            return "🔴 MP en <15 min (imposible)"
        if elapsed <= 900 and not is_prev:
            return "🟡 MC rápido (posible fix simple)"
        if elapsed <= 1800 and is_prev:
            return "🟡 MP en 15-30 min"
        return "✅ Tiempo normal"

    ot["quick_tick_label"] = ot.apply(_quick_tick_label, axis=1)

    # ── Etiqueta causa raíz (informativa) ─────────────────────────────────────
    # Caso especial: el técnico DEJÓ "SIN CLASIFICAR" pero SÍ describió la falla
    # en el texto libre → sabía qué pasaba y no clasificó = descuido de llenado.
    # Es un error atribuible más grave que simplemente no documentar nada.
    def _desglosa_falla(coment: str) -> bool:
        c = (coment or "").upper()
        return any(k in c for k in ("FALLA", "TRABAJO REALIZADO", "OBSERVACI"))

    def _causa_label(row) -> str:
        if not row["es_correctiva"]:
            return "✅ Preventiva (no aplica)"
        c = row["causa_clasif"]
        if c == "tecnico":   return "✅ Causa: Técnico"
        if c == "cliente":   return "✅ Causa: Cliente"
        # sin clasificar: distinguir descuido (describió pero no clasificó) de vacío total
        if _desglosa_falla(row.get("comentario_tecnico", "")):
            return "🔴 Sin clasificar (describió la falla)"
        return "❌ Sin causa / Vaga"

    ot["causa_label"] = ot.apply(_causa_label, axis=1)
    # Flag booleano para filtrar/contar este descuido específico
    ot["causa_sin_clasif_con_desglose"] = ot.apply(
        lambda r: bool(
            r["es_correctiva"]
            and r["causa_clasif"] not in ("tecnico", "cliente", "anulado")
            and _desglosa_falla(r.get("comentario_tecnico", ""))
        ),
        axis=1,
    )

    # ── Etiqueta método de detección (informativa) ────────────────────────────
    ot["deteccion_label"] = ot["deteccion_ok"].apply(
        lambda ok: "✅ Método registrado" if ok else "❌ Sin clasificar"
    )

    # ── Score total 0–75 (3 componentes × 25 pts; Modalidad excluida) ───────
    ot["score_total"] = (
        ot["score_tiempo"] + ot["score_causa"] + ot["score_numeral"]
    ).clip(upper=75).round(1)

    # Mantener columnas legacy para compatibilidad con código existente
    ot["score_nota"]       = ot["note_words"].apply(
        lambda w: 30 if w >= 15 else (15 if w >= 5 else (5 if w >= 1 else 0))
    )
    ot["score_checklist"]  = (ot["pct_tareas"] / 100 * 30).round(1)
    ot["score_recursos"]   = ot["has_resources"].apply(lambda x: 20 if x else 0)

    return ot


def build_teorico_vs_real(
    df_util: pd.DataFrame,
    df_kpi: pd.DataFrame,
    excel_to_full: dict,
) -> pd.DataFrame:
    """
    Cruce entre el cronograma (Planificación del Tiempo) y Fracttal.

    Reglas:
    ─ Mant. Preventivo / Llamado Correctivo → validado contra OTs de Fracttal.
      Se busca por AMBAS fechas del OT (final_date Y creation_date) para no
      perder OTs cuya fecha de apertura difiere de la de cierre.
      Estado: ✅ Realizó el día / 🔄 Realizó al día siguiente / ❌ Sin registro

    ─ Todo lo demás (Inventario, Reunión, Instalación, etc.) → justificado por
      estar en la planificación; no se cruza con Fracttal.
      Estado: ✅ Justificado (en plan)

    Columna "_seccion": "ot" | "otras"

    Nota nombres: la comparación es case-insensitive y sin tildes para tolerar
    diferencias de codificación entre base_tecnicos.xlsx y la API de Fracttal.
    """
    if df_util.empty:
        return pd.DataFrame()

    # ── Helpers de nombre ─────────────────────────────────────────────────────
    full_to_excel = {v: k for k, v in excel_to_full.items()}

    def _norm_str(s: str) -> str:
        """Minúsculas + sin tildes para comparación flexible."""
        return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().casefold()

    # Índice normalizado: versión sin tildes → nombre corto Excel
    _full_norm_idx: dict = {_norm_str(k): v for k, v in full_to_excel.items()}

    def _to_short(name: str) -> str:
        """
        Convierte nombre completo Fracttal → clave corta Excel.
        1) Búsqueda exacta en full_to_excel
        2) Búsqueda sin tildes (Fracttal puede no incluir acentos)
        3) Fallback estructural: para nombre típico chileno de 4 palabras
           "Nombre SegundoNombre Apellido1 Apellido2" → "Nombre Apellido1"
           Para 2-3 palabras → "Nombre Apellido"
        """
        name = (name or "").strip()
        if not name:
            return name
        # 1. Exacto
        if name in full_to_excel:
            return full_to_excel[name]
        # 2. Sin tildes
        nn = _norm_str(name)
        if nn in _full_norm_idx:
            return _full_norm_idx[nn]
        # 3. Estructural
        parts = name.split()
        if len(parts) == 4:
            # "Breyans Andrés Toledo Quintana" → "Breyans Toledo"
            return f"{parts[0]} {parts[2]}"
        if len(parts) >= 2:
            return f"{parts[0]} {parts[1]}"
        return name

    # ── Agregar df_kpi a nivel de OT (folio) ─────────────────────────────────
    df_f = df_kpi.groupby("folio").agg(
        tecnico=       ("tecnico",       "first"),
        creation_date= ("creation_date", "min"),
        final_date=    ("final_date",    "max"),
        maint_type=    ("maint_type",    "first"),
        station=       ("station",       "first"),
    ).reset_index()

    def _to_local_date(col: pd.Series) -> pd.Series:
        """Convierte columna datetime UTC → fecha local normalizada."""
        return col.dropna().dt.tz_convert(None).dt.normalize()

    df_f["fecha_final"]    = _to_local_date(df_f["final_date"])
    df_f["fecha_creation"] = _to_local_date(df_f["creation_date"])
    df_f["tecnico_short"]  = df_f["tecnico"].apply(_to_short)

    def _ot_cat(mt: str) -> str:
        t = str(mt).upper()
        if "PREVENTIVA" in t: return "Mant. Preventivo"
        if "CORRECTIVA" in t: return "Llamado Correctivo"
        return "Otra"

    df_f["cat_ot"] = df_f["maint_type"].apply(_ot_cat)

    # Expandir a pares (fecha, tecnico) usando AMBAS fechas
    # Así un OT abierto el lunes y cerrado el martes aparece en ambos días.
    # Vectorizado: concat de las dos columnas de fecha + drop_duplicates
    # (equivalente al set{} original para deduplicar cuando final == creation).
    _cols_keep = ["folio", "tecnico_short", "cat_ot", "station"]
    _df_a = df_f[_cols_keep + ["fecha_final"]].rename(columns={"fecha_final": "fecha"})
    _df_b = df_f[_cols_keep + ["fecha_creation"]].rename(columns={"fecha_creation": "fecha"})
    df_dates = (
        pd.concat([_df_a, _df_b], ignore_index=True)
        .dropna(subset=["fecha"])
        .drop_duplicates()
        .reset_index(drop=True)
    )

    if not df_dates.empty:

        def _agg_dates(grp):
            return pd.Series({
                "n_ots":      grp["folio"].nunique(),
                "tipos_ot":   ", ".join(sorted(grp["cat_ot"].unique())),
                "estaciones": ", ".join(sorted(grp["station"].dropna().unique())[:3]),
            })

        df_grp = (
            df_dates.groupby(["fecha", "tecnico_short"])
            .apply(_agg_dates, include_groups=False)
            .reset_index()
        )
    else:
        df_grp = pd.DataFrame(columns=["fecha", "tecnico_short",
                                        "n_ots", "tipos_ot", "estaciones"])

    # ── Normalizar cronograma ─────────────────────────────────────────────────
    _CATS_OT = {"Mant. Preventivo", "Llamado Correctivo"}

    df_u = df_util.copy()
    df_u["fecha_norm"]  = pd.to_datetime(df_u["fecha"]).dt.normalize()
    df_u["tecnico_key"] = df_u["tecnico"].apply(
        lambda n: _to_short(excel_to_full.get(n, n))
    )

    # ── Cruce fila a fila ─────────────────────────────────────────────────────
    records = []
    for _, row in df_u.iterrows():
        fecha    = row["fecha_norm"]
        tec_key  = row["tecnico_key"]
        cat_plan = row["categoria"]
        tareas   = row.get("tareas", "")
        fuera    = row.get("fuera_santiago", False)
        fecha_str = fecha.strftime("%a %d/%m") if pd.notna(fecha) else ""

        # ── Sin OT: justificada por el plan ───────────────────────────────────
        if cat_plan not in _CATS_OT:
            records.append({
                "Fecha":           fecha_str,
                "_fecha":          fecha,
                "Técnico":         row["tecnico"],
                "_tecnico_key":    tec_key,
                "Categoría":       cat_plan,
                "Tareas del plan": tareas,
                "Fuera Stgo":      "🟢" if fuera else "",
                "OTs en Fracttal": 0,
                "Tipos OT":        "",
                "Estaciones":      "",
                "Estado":          "✅ Justificado (en plan)",
                "Observación":     f"{cat_plan} — sin OT requerida",
                "_seccion":        "otras",
            })
            continue

        # ── Con OT: buscar en Fracttal (mismo día o siguiente) ────────────────
        def _buscar(fecha_buscar):
            return df_grp[
                (df_grp["fecha"] == fecha_buscar) &
                (df_grp["tecnico_short"] == tec_key)
            ]

        q_same = _buscar(fecha)
        fecha_sig = fecha + pd.Timedelta(days=1)
        q_next = _buscar(fecha_sig)

        if not q_same.empty:
            m         = q_same.iloc[0]
            n_ots     = int(m["n_ots"])
            tipos     = m["tipos_ot"]
            ests      = m["estaciones"]
            tipos_set = {t.strip() for t in tipos.split(",")}
            if cat_plan in tipos_set:
                estado = "✅ Realizó el día planificado"
                obs    = f"{n_ots} OT(s) del tipo correcto"
            else:
                estado = "⚠️ Tipo distinto en Fracttal"
                obs    = f"Plan: {cat_plan} → Fracttal: {tipos}"
        elif not q_next.empty:
            m      = q_next.iloc[0]
            n_ots  = int(m["n_ots"])
            tipos  = m["tipos_ot"]
            ests   = m["estaciones"]
            estado = "🔄 Realizó al día siguiente"
            obs    = f"OT el {fecha_sig.strftime('%d/%m')} — {n_ots} OT(s): {tipos}"
        else:
            n_ots, tipos, ests = 0, "", ""
            estado = "❌ Sin registro en Fracttal"
            obs    = f"Plan: '{cat_plan}' — sin OT ese día ni el siguiente"

        records.append({
            "Fecha":           fecha_str,
            "_fecha":          fecha,
            "Técnico":         row["tecnico"],
            "_tecnico_key":    tec_key,
            "Categoría":       cat_plan,
            "Tareas del plan": tareas,
            "Fuera Stgo":      "🟢" if fuera else "",
            "OTs en Fracttal": n_ots,
            "Tipos OT":        tipos,
            "Estaciones":      ests,
            "Estado":          estado,
            "Observación":     obs,
            "_seccion":        "ot",
        })

    return pd.DataFrame(records)


def score_llenado_por_tecnico(
    df_ot: pd.DataFrame,
    mes: str = None,
    meses_lista: list = None,
) -> pd.DataFrame:
    """
    Agrega puntajes por técnico. Filtra por mes (formato 'YYYY-MM') o por
    lista de meses. Si ambos son None, usa todos los datos.

    Retorna DataFrame con: tecnico, client, ots_evaluadas, score_promedio,
    score_nota_prom, score_checklist_prom, score_recursos_prom,
    score_tiempo_prom, pct_tareas_prom, pct_con_nota, pct_con_recursos,
    umbral_bono (bool: score ≥ 80)
    """
    if df_ot.empty:
        return pd.DataFrame()

    df = df_ot.copy()
    df["mes"] = df["creation_date"].dt.tz_convert(None).dt.to_period("M").astype(str)

    if mes:
        df = df[df["mes"] == mes]
    elif meses_lista:
        df = df[df["mes"].isin(meses_lista)]

    if df.empty:
        return pd.DataFrame()

    # ── Columnas auxiliares para % coherentes por dimensión ─────────────────
    # Cada dimensión aplica solo a un subconjunto de OTs:
    #   Tiempo    → solo MP (correctivas no evalúan tiempo)
    #   Causa raíz→ solo MC (preventivas no responden a falla)
    #   Numeral   → solo lavadora/aspiradora (no aplica al resto)
    # Se cuentan aparte "aplica" (denominador real) y "ok" (numerador)
    # para que % Cumplimiento = ok / aplica × 100 sin distorsión.
    df["_es_correctiva"] = df["es_correctiva"].fillna(False).astype(bool) \
        if "es_correctiva" in df.columns else False
    df["_es_lavadora"]   = df["es_lavadora"].fillna(False).astype(bool) \
        if "es_lavadora" in df.columns else False

    df["_tiempo_aplica"]   = ~df["_es_correctiva"]                          # MP
    df["_tiempo_ok"]       = df["_tiempo_aplica"] & (df["score_tiempo"].fillna(0) >= 25)
    df["_causa_aplica"]    = df["_es_correctiva"]                            # MC
    df["_causa_ok_effective"] = df["_causa_aplica"] & df["causa_ok"].fillna(False)
    df["_numeral_aplica"]  = df["_es_lavadora"]                             # lavadora/aspiradora
    df["_numeral_ok_effective"] = df["_numeral_aplica"] & df["numeral_ok"].fillna(False)

    grp = df.groupby("tecnico").agg(
        cliente_principal=    ("client",          lambda x: x.mode().iloc[0] if len(x) > 0 else ""),
        ots_evaluadas=        ("folio",           "count"),
        score_promedio=       ("score_total",     "mean"),
        # ── 3 componentes KPI Llenado (Tiempo, Causa raíz, Numeral) ──
        score_tiempo_prom=    ("score_tiempo",    "mean"),
        score_causa_prom=     ("score_causa",     "mean"),
        score_numeral_prom=   ("score_numeral",   "mean"),
        score_deteccion_prom= ("score_deteccion", "mean"),
        # ── Denominadores por dimensión (OTs donde SÍ aplica) ──
        tiempo_aplica_count=  ("_tiempo_aplica",  "sum"),
        causa_aplica_count=   ("_causa_aplica",   "sum"),
        numeral_aplica_count= ("_numeral_aplica", "sum"),
        # ── Numeradores: OTs que aplican Y cumplen ──
        tiempo_ok_count=      ("_tiempo_ok",           "sum"),
        causa_ok_count=       ("_causa_ok_effective",  "sum"),
        numeral_ok_count=     ("_numeral_ok_effective","sum"),
        pct_deteccion_ok=     ("deteccion_ok",    lambda x: x.mean() * 100),
        # ── Detalle causa raíz (solo correctivas) ──
        correctivas=          ("es_correctiva",   "sum"),
        sin_causa=            ("causa_clasif",    lambda x: (x == "sin_clasificar").sum()),
        causa_tecnico=        ("causa_clasif",    lambda x: (x == "tecnico").sum()),
        causa_cliente=        ("causa_clasif",    lambda x: (x == "cliente").sum()),
        # ── OTs con error global (score < 75) — métrica X (3 componentes) ──────
        n_errores=            ("score_total",     lambda x: int((x < 75).sum())),
        # ── Errores por dimensión — para métrica Y (suma de fallos individuales)
        err_tiempo=           ("score_tiempo",    lambda x: int((x < 25).sum())),
        err_causa=            ("score_causa",     lambda x: int((x < 25).sum())),
        err_numeral=          ("score_numeral",   lambda x: int((x < 25).sum())),
        err_deteccion=        ("score_deteccion", lambda x: int((x < 25).sum())),  # informativo
    ).reset_index()

    # ── % Cumplimiento coherente por dimensión (solo sobre OTs que aplican) ──
    # pct = ok / aplica × 100. Si no hay OTs que apliquen, dejar en NaN
    # para que la UI muestre '—' y no un 100% falso.
    import numpy as _np
    for _dim in ("tiempo", "causa", "numeral"):
        _num = grp[f"{_dim}_ok_count"].astype(float)
        _den = grp[f"{_dim}_aplica_count"].astype(float)
        # División segura: donde _den==0, resultado NaN
        _pct = _np.where(_den > 0, _num / _den * 100, _np.nan)
        grp[f"pct_{_dim}_ok"] = _np.round(_pct, 1)

    for c in ["score_promedio", "score_tiempo_prom", "score_causa_prom",
              "score_numeral_prom", "score_deteccion_prom",
              "pct_tiempo_ok", "pct_causa_ok", "pct_numeral_ok", "pct_deteccion_ok"]:
        if c in grp.columns:
            grp[c] = grp[c].round(1)

    # ── Métricas derivadas ─────────────────────────────────────────────────────
    # Y = suma de errores individuales por dimensión KPI (una OT puede contribuir hasta 3)
    grp["err_total_dim"]      = (grp["err_tiempo"] + grp["err_causa"] + grp["err_numeral"])
    # OTs sin ningún error
    grp["ots_correctas"]      = grp["ots_evaluadas"] - grp["n_errores"]
    # Conteo deteccion (informativo) mantiene la fórmula antigua
    grp["deteccion_ok_count"] = grp["ots_evaluadas"] - grp["err_deteccion"]

    # ── Bono Precisión — escala porcentual (no penaliza por volumen) ─────────────
    # Exactitud = % OTs sin error. Escala:
    #   ≥ 97%  exactitud (≤ 4% error)  → 100% → $75.000/sem
    #   93-96.9%          (4.1-7%)      →  70% → $52.500/sem
    #   90-92.9%          (7.1-10%)     →  35% → $26.250/sem
    #   < 90%             (>10%)        →   0% → $0/sem
    grp["exactitud_pct"] = (
        (1 - grp["n_errores"] / grp["ots_evaluadas"].clip(lower=1)) * 100
    ).round(1)

    _MAX_PREC_TRM = 105_000  # 30% × $350.000 trimestral
    def _bono_prec(exactitud: float) -> tuple:
        m = _MAX_PREC_TRM
        _lbl = f"{exactitud:.1f}%"
        if exactitud >= 95: return (100, m,            _lbl)
        if exactitud >= 90: return ( 90, int(m*.90),   _lbl)
        if exactitud >= 85: return ( 80, int(m*.80),   _lbl)
        if exactitud >= 80: return ( 70, int(m*.70),   _lbl)
        if exactitud >= 75: return ( 60, int(m*.60),   _lbl)
        if exactitud >= 70: return ( 50, int(m*.50),   _lbl)
        return (0, 0, _lbl)

    _bono_vals = grp["exactitud_pct"].apply(_bono_prec)
    grp["bono_pct"]     = _bono_vals.apply(lambda x: x[0])
    grp["bono_semanal"] = _bono_vals.apply(lambda x: x[1])
    grp["bono_label"]   = _bono_vals.apply(lambda x: x[2])
    grp["umbral_bono"]  = grp["bono_pct"] > 0

    return grp.sort_values("score_promedio", ascending=False).reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════════════
# KPI 1.2 CALIDAD — DETECCIÓN DE REINCIDENCIAS
# ══════════════════════════════════════════════════════════════════════════════

def build_reincidencias(df_wo: pd.DataFrame, excel_to_full: dict = None) -> pd.DataFrame:
    """
    Detecta fallas post-preventiva: llamado correctivo generado dentro de los
    5 días siguientes a un mantenimiento preventivo en el mismo equipo.

    Reglas:
    - Para cada correctivo se busca el preventivo MÁS RECIENTE anterior en el
      mismo equipo.
    - Si ese preventivo ocurrió hace 1–5 días (inclusive), se considera falla
      post-preventiva.
    - El error se imputa al técnico que realizó el PREVENTIVO (no quien atendió
      el correctivo), porque debió detectar/resolver el problema en la mantención.
    - Excepción: si la causa raíz del correctivo es "cliente" → no se imputa
      al técnico (no era responsabilidad de la mantención).
    - Si la causa es "sin_clasificar" → sí afecta (técnico no registró bien).

    Returns DataFrame con columnas:
      equipment_code, equipment, client, station,
      folio_pm, fecha_pm, tecnico_responsable (del preventivo),
      tecnico_resp_short, grupo_responsable,
      folio_cm, fecha_cm, tecnico_cm (quien atendió el correctivo),
      causa_raiz, causa_clasif, es_reincidencia_tecnico, dias_entre
    """
    if df_wo.empty:
        return pd.DataFrame()

    # Helper nombre corto
    full_to_excel = {v: k for k, v in excel_to_full.items()} if excel_to_full else {}

    def _norm_str(s: str) -> str:
        return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().casefold()

    _full_norm_idx = {_norm_str(k): v for k, v in full_to_excel.items()}

    def _to_short(name: str) -> str:
        name = (name or "").strip()
        if not name:
            return name
        if name in full_to_excel:
            return full_to_excel[name]
        nn = _norm_str(name)
        if nn in _full_norm_idx:
            return _full_norm_idx[nn]
        parts = name.split()
        if len(parts) == 4:
            return f"{parts[0]} {parts[2]}"
        if len(parts) >= 2:
            return f"{parts[0]} {parts[1]}"
        return name

    # Solo OTs con fecha y tipo conocido
    # PMs: fecha finalización (cuando se realizó el PM)
    # CMs: fecha creación    (cuando ocurrió la falla, no cuando se cerró)
    prev_all = df_wo[
        (df_wo["maint_type"] == "Preventiva") &
        df_wo["final_date"].notna()
    ].copy()

    corr_raw = df_wo[
        (df_wo["maint_type"] == "Correctiva") &
        (df_wo["creation_date"].notna() | df_wo["final_date"].notna())
    ].copy()

    if prev_all.empty or corr_raw.empty:
        return pd.DataFrame()

    # ── Excluir PMs de técnicos no aplica (AUTEC, Jaime Ocampo, etc.) ──────────
    _no_aplica_norm_loc: set = set()
    for _na in TECNICOS_NO_APLICA:
        _nn = unicodedata.normalize("NFD", _na).encode("ascii", "ignore").decode().casefold()
        _no_aplica_norm_loc.add(_nn)
        _pts = _na.split()
        for _i in range(len(_pts)):
            for _j in range(_i + 1, len(_pts)):
                _no_aplica_norm_loc.add(
                    unicodedata.normalize("NFD", _pts[_i] + " " + _pts[_j])
                    .encode("ascii", "ignore").decode().casefold()
                )

    def _es_excl_local(t: str) -> bool:
        if not isinstance(t, str) or not t.strip():
            return False
        tn = unicodedata.normalize("NFD", t.strip()).encode("ascii", "ignore").decode().casefold()
        if tn in _no_aplica_norm_loc:
            return True
        pts = t.strip().split()
        for ii in range(len(pts)):
            for jj in range(ii + 1, len(pts)):
                pn = (
                    unicodedata.normalize("NFD", pts[ii] + " " + pts[jj])
                    .encode("ascii", "ignore").decode().casefold()
                )
                if pn in _no_aplica_norm_loc:
                    return True
        return False

    # REGLA DE AUDITORÍA:
    # ┌─ PM por técnico externo (AUTEC, ELECONS/Jaime Ocampo, etc.) ──────────────
    # │  → EXCLUIDO de prev.  Un correctivo posterior NO se imputa a nadie:
    # │    no evaluamos a terceros, y no es justo culpar al técnico Occimiano
    # │    si fue el externo quien hizo el PM más reciente.
    # └─ CM por técnico externo ──────────────────────────────────────────────────
    #    → INCLUIDO en corr.  Actúa como AUDITOR: si AUTEC/ELECONS realizaron
    #      un correctivo dentro de 5 días de un PM hecho por un técnico Occimiano,
    #      eso sí se imputa al técnico Occimiano que hizo el PM (su mantención
    #      no fue suficiente para evitar la falla).
    prev = prev_all[~prev_all["technician"].apply(_es_excl_local)].copy()
    corr = corr_raw.copy()   # Sin filtro de técnico: externos son auditores

    if prev.empty or corr.empty:
        return pd.DataFrame()

    # ── Vectorizado con merge_asof (10-50× más rápido que loop Python) ───────
    # PMs → final_date; CMs → creation_date (con fallback a final_date)
    prev["fecha_dt"] = pd.to_datetime(prev["final_date"].dt.tz_convert(None).dt.date)
    _corr_date = corr["creation_date"].where(corr["creation_date"].notna(), corr["final_date"])
    corr["fecha_dt"] = pd.to_datetime(_corr_date.dt.tz_convert(None).dt.date)
    prev["tecnico_short"] = prev["technician"].apply(_to_short)
    corr["tecnico_short"] = corr["technician"].apply(_to_short)

    # Columnas necesarias de cada lado
    _pm_cols = ["equipment_code","folio","fecha_dt","technician","tecnico_short",
                "equipment","client","station"]
    _cm_cols = ["equipment_code","folio","fecha_dt","technician","failure_type","failure_cause",
                "comentario_tecnico"]
    _pm_cols = [c for c in _pm_cols if c in prev.columns]
    _cm_cols = [c for c in _cm_cols if c in corr.columns]

    # merge_asof requiere orden global por la columna "on" (fecha_dt), no por grupo.
    # IMPORTANTE: la columna "on" NO recibe sufijo tras el merge — queda como "fecha_dt"
    # (valor del correctivo, lado izquierdo). Para recuperar la fecha del preventivo
    # usamos una columna auxiliar "fecha_val_pm" en prev_s que sí recibirá sufijo.
    prev_tmp = prev[_pm_cols].sort_values("fecha_dt").reset_index(drop=True).copy()
    corr_tmp = corr[_cm_cols].sort_values("fecha_dt").reset_index(drop=True).copy()
    # Añadir fecha_val en AMBOS lados — los sufijos solo aplican a columnas
    # que existen en los dos DataFrames, así fecha_val→fecha_val_cm (MC) y fecha_val_pm (PM)
    corr_tmp["fecha_val"] = corr_tmp["fecha_dt"]
    prev_tmp["fecha_val"] = prev_tmp["fecha_dt"]

    merged = pd.merge_asof(
        corr_tmp, prev_tmp,
        by="equipment_code",
        on="fecha_dt",
        direction="backward",
        suffixes=("_cm","_pm"),
    )

    # Eliminar filas sin PM encontrado
    merged = merged[merged["folio_pm"].notna()].copy()
    if merged.empty:
        return pd.DataFrame()

    # "fecha_dt"     = fecha del correctivo (columna "on", izquierda)
    # "fecha_val_pm" = fecha del preventivo (columna auxiliar del lado derecho)
    merged["fecha_dt_cm"] = merged["fecha_dt"]
    merged["fecha_dt_pm"] = merged["fecha_val_pm"]

    # Calcular días entre PM y MC
    merged["dias_entre"] = (merged["fecha_dt_cm"] - merged["fecha_dt_pm"]).dt.days
    merged = merged[(merged["dias_entre"] >= 1) & (merged["dias_entre"] <= 5)].copy()
    if merged.empty:
        return pd.DataFrame()

    # ── Notas sobre sufijos en merge_asof ────────────────────────────────────
    # Sufijo _cm/_pm SOLO aplica a columnas en AMBOS DataFrames.
    # Columnas solo en corr_tmp:  failure_type, failure_cause → sin sufijo
    # Columnas solo en prev_tmp:  tecnico_short, equipment, client, station → sin sufijo
    # Columnas en ambos:  folio→folio_cm/folio_pm, technician→technician_cm/pm, fecha_val→fecha_val_cm/pm
    # Columna "on" (fecha_dt): queda sin sufijo = valor del correctivo (lado izquierdo)

    # ── Clasificación vectorizada ─────────────────────────────────────────────
    merged["falla_raw"]   = merged.get("failure_type",  pd.Series("", index=merged.index)).fillna("")
    merged["causa_raiz"]  = merged.get("failure_cause", pd.Series("", index=merged.index)).fillna("")
    merged["falla_tipo"]  = merged["falla_raw"].apply(classify_falla_type)
    merged["causa_clasif"] = merged["causa_raiz"].apply(classify_causa_raiz)

    def _es_tecnico(row) -> bool:
        # Política actualizada: F.A.O + F.N.A.O ambas imputan KPI.
        # Un correctivo dentro de los 5 días de un PM en la misma estación
        # es error del técnico del preventivo, independiente de la clasificación
        # que ponga el técnico del correctivo en el campo "Falla".
        # Solo se excluye si la causa raíz confirma daño externo del cliente
        # (ej: equipo roto por cliente, falta de sal en ablandador, etc.)
        # o si es un Trabajo Especial (categoría que no es falla post-PM).
        ft = row["falla_tipo"]
        cc = row["causa_clasif"]
        if ft == "especial":  return False   # Trabajo Especial → no es reincidencia
        if cc == "cliente":   return False   # Causa confirmada del cliente → no imputa
        if cc == "anulado":   return False   # ANULADO POR EDS → concesionario, no imputa
        return True   # FAO + FNAO + sin_info + sin_dato → todos imputan

    merged["es_reincidencia_tecnico"] = merged.apply(_es_tecnico, axis=1)
    # tecnico_short solo está en prev_tmp → no tiene sufijo
    merged["tecnico_resp_short"] = merged.get("tecnico_short", pd.Series("", index=merged.index)).fillna("")
    merged["grupo_responsable"]  = merged["tecnico_resp_short"].apply(get_grupo_tecnico)
    aplicar_transferencias(merged, "fecha_dt_pm", "grupo_responsable", "tecnico_resp_short")

    # Construir DataFrame final
    _s = pd.Series("", index=merged.index)
    df_r = pd.DataFrame({
        "equipment_code":          merged["equipment_code"],
        "equipment":               merged.get("equipment",    _s).fillna(""),   # solo en prev
        "client":                  merged.get("client",       _s).fillna(""),   # solo en prev
        "station":                 merged.get("station",      _s).fillna(""),   # solo en prev
        "folio_pm":                merged["folio_pm"],                           # ambos→sufijado
        "fecha_pm":                merged["fecha_dt_pm"],
        "tecnico_responsable":     merged.get("technician_pm", _s).fillna(""),  # ambos→sufijado
        "tecnico_resp_short":      merged["tecnico_resp_short"],
        "grupo_responsable":       merged["grupo_responsable"],
        "folio_cm":                merged["folio_cm"],                           # ambos→sufijado
        "fecha_cm":                merged["fecha_dt_cm"],
        "tecnico_cm":              merged.get("technician_cm", _s).fillna(""),  # ambos→sufijado
        "falla_raw":               merged["falla_raw"].where(merged["falla_raw"] != "", "SIN TIPO"),
        "falla_tipo":              merged["falla_tipo"],
        "causa_raiz":              merged["causa_raiz"].where(merged["causa_raiz"] != "", "SIN CLASIFICAR"),
        "causa_clasif":            merged["causa_clasif"],
        "dias_entre":              merged["dias_entre"],
        "es_reincidencia_tecnico": merged["es_reincidencia_tecnico"],
        "comentario_tecnico_cm":   merged.get("comentario_tecnico", _s).fillna(""),
    }).reset_index(drop=True)

    df_r["fecha_pm"] = pd.to_datetime(df_r["fecha_pm"])
    df_r["fecha_cm"] = pd.to_datetime(df_r["fecha_cm"])
    return df_r


# ══════════════════════════════════════════════════════════════════════════════
# MEDIDORES — CONTADORES DE FICHAS (dispensadores)
# ══════════════════════════════════════════════════════════════════════════════

def build_meters_fichas_df(raw_meters: list) -> pd.DataFrame:
    """
    Filtra el catálogo de medidores y devuelve solo los contadores de fichas:
      - units_description = 'UNIDAD'
      - is_counter = True
      - description = 'NUMERAL' (nombre estándar en Occimiano)

    Una fila = un dispensador / contador.

    Columnas de salida:
      code              → código del activo (ej. EQ-6843)
      client            → COPEC / SHELL (Enex) / ESMAX (Aramco) / etc.
      station           → nombre de la EDS (ej. 'SHELL FEDERICO ERRÁZURIZ')
      equipment         → descripción completa del equipo (dispenser)
      numeral_acum      → total fichas acumuladas (accumulated_value)
      numeral_ultimo    → fichas en la última lectura (value de last_data)
      ultima_lectura    → fecha de la última lectura (UTC)
      dias_sin_lectura  → días desde la última lectura (hoy - ultima_lectura)
      promedio_mensual  → promedio mensual estimado por Fracttal
      from_subtask      → True si la última lectura vino de una OT
      id_meter          → ID interno del medidor
    """
    fichas = [
        m for m in (raw_meters or [])
        if (m.get("units_description") or "").upper() == "UNIDAD"
        and m.get("is_counter", False)
        and m.get("active", True)
    ]
    if not fichas:
        return pd.DataFrame()

    hoy = pd.Timestamp.utcnow()
    rows = []
    for m in fichas:
        client, station = _parse_hierarchy(m.get("parent_description") or "")
        last = m.get("last_data") or {}
        fecha_lect = pd.to_datetime(last.get("date"), utc=True, errors="coerce")
        dias_sin = int((hoy - fecha_lect).days) if pd.notna(fecha_lect) else None

        rows.append({
            "code":             m.get("code") or "",
            "client":           client,
            "station":          station,
            "equipment":        (m.get("items_description") or "").strip(),
            "numeral_acum":     last.get("accumulated_value") or 0,
            "numeral_ultimo":   last.get("value") or 0,
            "ultima_lectura":   fecha_lect,
            "dias_sin_lectura": dias_sin,
            "promedio_mensual": round(m.get("monthly_average_data") or 0, 0),
            "from_subtask":     bool(last.get("from_subtask", False)),
            "id_meter":         m.get("id"),
        })

    df = pd.DataFrame(rows)
    df = df.sort_values(["client", "station", "equipment"]).reset_index(drop=True)
    return df


def enrich_fichas_with_readings(df_fichas: pd.DataFrame, raw_readings: list) -> pd.DataFrame:
    """
    Añade las columnas de penúltima lectura a df_fichas usando el historial completo.

    Columnas añadidas:
      penultima_lectura  → fecha (Timestamp UTC) de la penúltima lectura registrada
      numeral_penultimo  → cantidad de fichas en esa penúltima lectura
    """
    from collections import defaultdict

    df_fichas = df_fichas.copy()
    df_fichas["penultima_lectura"] = pd.NaT
    df_fichas["numeral_penultimo"] = pd.NA

    if df_fichas.empty or not raw_readings:
        return df_fichas

    # Agrupar lecturas por items_code
    hist: dict[str, list] = defaultdict(list)
    for r in raw_readings:
        code = r.get("items_code") or r.get("code") or ""
        if not code:
            continue
        # date_reading puede estar en el nivel raíz o dentro de "data"
        date_raw = r.get("date_reading") or (r.get("data") or {}).get("date")
        dt = pd.to_datetime(date_raw, utc=True, errors="coerce")
        if pd.isna(dt):
            continue
        data = r.get("data") or {}
        value = data.get("value") if isinstance(data, dict) else r.get("value")
        hist[code].append((dt, value))

    # Ordenar por fecha DESC y extraer la segunda entrada (penúltima)
    penult_date: dict[str, pd.Timestamp] = {}
    penult_val:  dict[str, object]       = {}
    for code, entries in hist.items():
        entries.sort(key=lambda x: x[0], reverse=True)
        if len(entries) >= 2:
            penult_date[code] = entries[1][0]
            penult_val[code]  = entries[1][1]

    df_fichas["penultima_lectura"] = df_fichas["code"].map(penult_date)
    df_fichas["numeral_penultimo"] = df_fichas["code"].map(penult_val)

    return df_fichas


def station_summary(df_wo: pd.DataFrame, station: str) -> dict:
    """Compute KPIs for a single station."""
    s = df_wo[df_wo["station"] == station]
    if s.empty:
        return {}

    last_maint = s["final_date"].dropna().max()
    preventivas = s[s["maint_type"] == "Preventiva"]
    correctivas = s[s["maint_type"] == "Correctiva"]

    causes = (
        correctivas["failure_cause"]
        .value_counts()
        .rename_axis("causa")
        .reset_index(name="cantidad")
    )
    causes = causes[causes["causa"].str.strip() != ""]

    equipos = (
        s.groupby("equipment")
        .agg(
            total_ots=("folio", "count"),
            preventivas=("maint_type", lambda x: (x == "Preventiva").sum()),
            correctivas=("maint_type", lambda x: (x == "Correctiva").sum()),
            ultima_mant=("final_date", "max"),
        )
        .reset_index()
        .sort_values("total_ots", ascending=False)
    )

    return {
        "total_ots": len(s),
        "preventivas": len(preventivas),
        "correctivas": len(correctivas),
        "equipos_count": s["equipment"].nunique(),
        "last_maintenance": last_maint,
        "top_causes": causes,
        "equipos": equipos,
    }

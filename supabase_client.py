"""
supabase_client.py
==================
Cliente Supabase para el dashboard Occimiano.
Reemplaza las llamadas directas a Fracttal API y Excel.

Todas las funciones mantienen el mismo nombre y tipo de retorno
que las originales en api.py y gdrive.py para compatibilidad.
"""

import os
import re
import requests
import streamlit as st
import pandas as pd
from datetime import datetime, timezone

# ── Cargar .env en desarrollo local ──────────────────────────────────────────
def _load_env_file():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())

_load_env_file()

# ── Credenciales — se leen en tiempo de ejecución (no al importar) ───────────
def _get_creds() -> tuple[str, str]:
    """
    Lee SUPABASE_URL y SUPABASE_KEY en este orden:
    1. st.secrets  (Streamlit Cloud)
    2. os.environ  (local via .env ya cargado por _load_env_file)
    Se llama dentro de _query() para que st.secrets esté disponible.
    """
    url = key = ""
    try:
        url = str(st.secrets["SUPABASE_URL"])
        key = str(st.secrets["SUPABASE_KEY"])
    except Exception:
        pass
    if not url:
        url = os.getenv("SUPABASE_URL", "")
    if not key:
        key = os.getenv("SUPABASE_KEY", "")
    return url, key

import uuid as _uuid_mod

# ─────────────────────────────────────────────────────────────────────────────
# Helpers base (GET / POST / PATCH)
# ─────────────────────────────────────────────────────────────────────────────

def _query(tabla: str, params: str = "", limit: int = 10_000) -> list:
    """Paginación automática hasta limit registros."""
    supabase_url, supabase_key = _get_creds()
    headers = {
        "apikey":        supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept":        "application/json",
    }
    results = []
    offset  = 0
    page    = 1000
    while offset < limit:
        url = f"{supabase_url}/rest/v1/{tabla}?{params}&limit={page}&offset={offset}"
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        results.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return results[:limit]


def _post(tabla: str, data: dict) -> bool:
    """INSERT / upsert un registro."""
    supabase_url, supabase_key = _get_creds()
    r = requests.post(
        f"{supabase_url}/rest/v1/{tabla}",
        headers={
            "apikey":        supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type":  "application/json",
            "Prefer":        "resolution=merge-duplicates,return=minimal",
        },
        json=data,
        timeout=30,
    )
    return r.status_code in (200, 201, 204)


def _patch(tabla: str, filtro: str, data: dict) -> bool:
    """PATCH (UPDATE parcial) con filtro tipo 'col=eq.valor'."""
    supabase_url, supabase_key = _get_creds()
    r = requests.patch(
        f"{supabase_url}/rest/v1/{tabla}?{filtro}",
        headers={
            "apikey":        supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        },
        json=data,
        timeout=10,
    )
    return r.status_code in (200, 204)


# ═════════════════════════════════════════════════════════════════════════════
# AUTH — Whitelist de usuarios y sesiones
# ═════════════════════════════════════════════════════════════════════════════

def get_usuario_dashboard(email: str) -> "dict | None":
    """Retorna el usuario si está en la whitelist y activo, None si no.
    Incluye password_hash para que auth.py pueda validar contraseñas individuales.
    Fallback transparente: si las columnas nuevas no existen aún, reintenta sin ellas."""
    base = f"select=email,nombre,rol,activo,password_hash,password_set_at"
    rows = _query("usuarios_dashboard",
                  f"{base}&email=eq.{email.lower()}", limit=1)
    if not rows:
        # Posible 400 por columnas inexistentes (migración no corrida)
        rows = _query("usuarios_dashboard",
                      f"select=email,nombre,rol,activo&email=eq.{email.lower()}", limit=1)
    if rows and rows[0].get("activo"):
        return rows[0]
    return None


# ── Passwords individuales (migrate_auth_passwords.sql) ──────────────────────

def update_user_password_hash(email: str, password_hash: str) -> bool:
    """Setea password_hash + password_changed_at (y password_set_at si está null)."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        ok = _patch(
            "usuarios_dashboard",
            f"email=eq.{email.lower()}",
            {
                "password_hash":       password_hash,
                "password_changed_at": now,
                # password_set_at lo seteamos siempre; si ya existía, sobreescribir es inocuo
                "password_set_at":     now,
            },
        )
        return bool(ok)
    except Exception:
        return False


def create_password_reset_token(email: str, token_hash: str,
                                proposito: str,
                                expires_at_utc: datetime) -> bool:
    """Inserta un registro en password_resets. Devuelve True si se creó."""
    try:
        _post("password_resets", {
            "email":      email.lower(),
            "token_hash": token_hash,
            "proposito":  proposito or "reset",
            "expires_at": expires_at_utc.isoformat(),
        })
        return True
    except Exception:
        return False


def find_password_reset(token_hash: str) -> "dict | None":
    """Busca un token por su hash. Devuelve dict con email/expires_at/used_at/id."""
    rows = _query(
        "password_resets",
        f"select=id,email,proposito,expires_at,used_at"
        f"&token_hash=eq.{token_hash}",
        limit=1,
    )
    return rows[0] if rows else None


def mark_reset_used(reset_id) -> bool:
    """Marca un token como utilizado (used_at = now). Idempotente."""
    if reset_id is None:
        return False
    try:
        return bool(_patch(
            "password_resets",
            f"id=eq.{reset_id}",
            {"used_at": datetime.now(timezone.utc).isoformat()},
        ))
    except Exception:
        return False


def log_session_start(email: str, session_id: str) -> None:
    """Registra inicio de sesión y actualiza ultimo_acceso del usuario."""
    try:
        _post("sesiones_dashboard", {
            "id":            session_id,
            "email":         email.lower(),
            "pagina_actual": "Inicio",
        })
        _patch(
            "usuarios_dashboard",
            f"email=eq.{email.lower()}",
            {"ultimo_acceso": datetime.now(timezone.utc).isoformat()},
        )
    except Exception:
        pass


def update_session_activity(session_id: str, pagina: str) -> None:
    """Actualiza última actividad (llamar con debounce desde app.py)."""
    try:
        _patch(
            "sesiones_dashboard",
            f"id=eq.{session_id}",
            {
                "ultima_actividad": datetime.now(timezone.utc).isoformat(),
                "pagina_actual":    pagina,
            },
        )
    except Exception:
        pass


@st.cache_data(ttl=30, show_spinner=False)
def get_sesiones_admin() -> pd.DataFrame:
    """Sesiones para el panel admin (TTL 30 s = casi tiempo real)."""
    rows = _query(
        "sesiones_dashboard",
        "select=*&order=fecha_inicio.desc",
        limit=2000,
    )
    return pd.DataFrame(rows) if rows else pd.DataFrame()


@st.cache_data(ttl=15, show_spinner=False)
def get_usuarios_admin() -> pd.DataFrame:
    """Usuarios para el panel admin."""
    rows = _query(
        "usuarios_dashboard",
        "select=*&order=creado_en.desc",
        limit=500,
    )
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def upsert_usuario_dashboard(
    email: str, nombre: str, rol: str, activo: bool
) -> bool:
    """Crea o actualiza un usuario en la whitelist."""
    return _post("usuarios_dashboard", {
        "email":  email.strip().lower(),
        "nombre": nombre.strip(),
        "rol":    rol,
        "activo": activo,
    })


# ═════════════════════════════════════════════════════════════════════════════
# 1. ÓRDENES DE TRABAJO  (reemplaza load_work_orders + build_work_orders_df)
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def load_numerales_subtarea_supabase() -> pd.DataFrame:
    """
    Carga numerales_subtarea (1 fila por (id_ot, codigo_activo)).
    Es resiliente: si la tabla todavía no se creó, devuelve DataFrame vacío.
    """
    try:
        rows = _query(
            "numerales_subtarea",
            "select=id_ot,id_work_order_task,codigo_activo,nombre_activo,"
            "tipo_activo,numeral_inicial,numeral_final,fichas_periodo,"
            "numeral_ok,motivo,bomba_dosificadora,consumo_insumos,"
            "tiempo_fichas_seg,lts_hr_produccion_final,"
            "form_tiene_bomba,form_tiene_consumo,form_tiene_tiempo,"
            "form_tiene_produccion,"
            "fecha_inicio_subtarea,fecha_fin_subtarea"
            "&order=id_ot.desc",
            limit=20_000,
        )
        # Fallback si la migración lts_hr o form_tiene_produccion aún no
        # está aplicada: reintentar sin esas columnas.
        if not rows:
            rows = _query(
                "numerales_subtarea",
                "select=id_ot,id_work_order_task,codigo_activo,nombre_activo,"
                "tipo_activo,numeral_inicial,numeral_final,fichas_periodo,"
                "numeral_ok,motivo,bomba_dosificadora,consumo_insumos,"
                "tiempo_fichas_seg,"
                "form_tiene_bomba,form_tiene_consumo,form_tiene_tiempo,"
                "fecha_inicio_subtarea,fecha_fin_subtarea"
                "&order=id_ot.desc",
                limit=20_000,
            )
    except Exception:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


# Versión del cache: bumpear para invalidar el disk-cache tras cambios en
# duracion_estim_neta_seg / duracion_real_neta_seg del backend.
_WO_CACHE_VERSION = "v4-es-correctiva-fix-08jul"

@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def load_work_orders_supabase(cache_v: str = _WO_CACHE_VERSION) -> list:
    """
    Retorna lista de dicts compatible con el formato raw de Fracttal.
    El dashboard puede llamar build_work_orders_df() sobre este resultado.
    cache_v: bumpear para forzar recarga (invalida disk cache).
    """
    # Columnas base + numerales reales. Si la migración migrate_numerales.sql
    # aún no se corrió, numeral_inicial/final no existen → reintentar sin ellas
    # para no romper el dashboard (resiliencia ante orden de despliegue).
    _base_cols = (
        "select=id_ot,estado,estado_tarea,codigo_activo,nombre_activo,"
        "ubicacion,cliente,estacion,codigo_eds,responsable,tipo_tarea,"
        "prioridad,prioridad_calc,fecha_creacion,fecha_inicio,"
        "fecha_finalizacion,causa_raiz,tipo_falla,modalidad_atencion,"
        "nota,nota_tarea,tiene_numeral,"
        "duracion_real_seg,duracion_estim_seg,"
        "tiene_recursos,completada"
    )
    # Fallback escalonado: si comentario_tecnico aún no existe (migración nueva
    # sin correr), reintentar solo con numerales; y si esos tampoco, base sola.
    # Así un despliegue previo a la migración no rompe ni pierde el numeral.
    # Fallback escalonado: cada migración nueva añade columnas. Si una no se
    # aplicó aún, la consulta da 400 y caemos al fallback siguiente sin perder
    # las columnas que sí existen.
    rows = _query(
        "ordenes_trabajo",
        _base_cols + ",numeral_inicial,numeral_final,comentario_tecnico,"
                     "form_tiene_numeral,duracion_estim_neta_seg,"
                     "duracion_real_neta_seg&order=fecha_creacion.desc",
        limit=20_000
    )
    if not rows:
        rows = _query(
            "ordenes_trabajo",
            _base_cols + ",numeral_inicial,numeral_final,comentario_tecnico,"
                         "form_tiene_numeral&order=fecha_creacion.desc",
            limit=20_000
        )
    if not rows:
        rows = _query(
            "ordenes_trabajo",
            _base_cols + ",numeral_inicial,numeral_final&order=fecha_creacion.desc",
            limit=20_000
        )
    if not rows:
        rows = _query(
            "ordenes_trabajo",
            _base_cols + "&order=fecha_creacion.desc",
            limit=20_000
        )
    # Mapear al formato que espera build_work_orders_df.
    # EXPANSIÓN POR EQUIPO: el ETL combina varias sub-tareas en una sola fila
    # (codigo_activo="EQ-001, EQ-002, EQ-003"). Para KPIs que matchean por
    # equipment_code (build_reincidencias, etc.) hay que volver a expandir
    # a una fila por equipo. La pantalla MP usa load_preventivas_supabase
    # que mantiene la concat para mostrar todos los equipos juntos.
    mapped = []
    for r in rows:
        _cod_raw = r.get("codigo_activo") or ""
        _nom_raw = r.get("nombre_activo") or ""
        if "," in _cod_raw:
            _codes = [c.strip() for c in _cod_raw.split(",") if c.strip()]
            _names = [n.strip() for n in _nom_raw.split(" · ")] if " · " in _nom_raw else []
            # Pad/truncate nombres para que matchee la longitud
            while len(_names) < len(_codes):
                _names.append(_nom_raw)
        else:
            _codes = [_cod_raw] if _cod_raw else [None]
            _names = [_nom_raw] if _nom_raw else [None]
        for _i, _cod in enumerate(_codes):
            _nom = _names[_i] if _i < len(_names) else _nom_raw
            mapped.append({
                "wo_folio":                   r.get("id_ot"),
                "parent_description":         r.get("ubicacion") or f"// {r.get('cliente','')}/{r.get('estacion','')}/",
                "personnel_description":      r.get("responsable"),
                "tasks_log_task_type_main":   r.get("tipo_tarea"),
                "priorities_description":     r.get("prioridad_calc") or r.get("prioridad"),
                "creation_date":              r.get("fecha_creacion"),
                "final_date":                 r.get("fecha_finalizacion"),
                "initial_date":               r.get("fecha_inicio"),
                "code":                       _cod,
                "items_log_description":      _nom,
                "groups_2_description":       r.get("codigo_eds"),
                "id_status_work_order":       None,
                "wo_status":                  r.get("estado"),
                "task_status":                r.get("estado_tarea"),
                "done":                       r.get("completada", False),
                "tasks_duration":             r.get("duracion_real_seg"),
                "duration":                   r.get("duracion_estim_seg"),
                "causes_description":         r.get("causa_raiz"),
                "types_description":          r.get("tipo_falla"),
                "detection_method_description": r.get("modalidad_atencion"),
                "note":                       r.get("nota"),
                "task_note":                  r.get("nota_tarea"),
                "numeral_inicial":            r.get("numeral_inicial"),
                "numeral_final":              r.get("numeral_final"),
                "comentario_tecnico":         r.get("comentario_tecnico"),
                "form_tiene_numeral":         r.get("form_tiene_numeral"),
                "duracion_estim_neta_seg":    r.get("duracion_estim_neta_seg"),
                "duracion_real_neta_seg":     r.get("duracion_real_neta_seg"),
                "stop_assets_sec":            0,
                "total_cost_task":            None,
                "resources_inventory":        "1" if r.get("tiene_recursos") else None,
            })
    return mapped


# ═════════════════════════════════════════════════════════════════════════════
# 2. LISTADO DE EDS  (reemplaza load_listado_eds)
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False, persist="disk")
def load_listado_eds_supabase() -> pd.DataFrame:
    """
    Retorna DataFrame compatible con el formato de load_listado_eds().
    """
    rows = _query(
        "estaciones_servicio",
        "select=eds_occim,cliente,nombre,direccion,comuna,region,zona,activa,"
        "loc_fracttal,barcode_cliente,cod_occim_fracttal",
        limit=2000
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "zona":              "zona_occim",
        "loc_fracttal":      "_loc_code",
        "barcode_cliente":   "_cód_cliente_f",
        # eds_occim_raw = código Fracttal original (EE_S###) — necesario para
        # que resolve_llamados_eds_codes traduzca EE_S → PBR en llamados ESMAX
        "cod_occim_fracttal":"eds_occim_raw",
    })
    # Alias de compatibilidad para tablas que todavía usen el nombre antiguo
    if "eds_occim_raw" in df.columns:
        df["_cod_occim_frac"] = df["eds_occim_raw"]
    # Compatibilidad: campos que el dashboard espera
    if "nombre" in df.columns:
        df["direccion"] = df["nombre"]
    # Normalizar etiqueta del cliente: "ESMAX (Aramco)" → "Aramco (Esmax)"
    if "cliente" in df.columns:
        df["cliente"] = df["cliente"].replace({"ESMAX (Aramco)": "Aramco (Esmax)"})
    return df


# ═════════════════════════════════════════════════════════════════════════════
# 3. TÉCNICOS Y EQUIPOS  (reemplaza load_base_tecnicos + GRUPOS_TERRENO)
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False, persist="disk")
def load_tecnicos_supabase() -> pd.DataFrame:
    """Retorna DataFrame de técnicos con su equipo."""
    rows = _query(
        "tecnicos",
        "select=nombre_corto,nombre_completo,aplica_bono,equipo_id,"
        "equipos(nombre_equipo,senior,zona)",
        limit=200
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["equipo"]  = df["equipos"].apply(lambda x: (x or {}).get("nombre_equipo") if isinstance(x, dict) else None)
    df["senior"]  = df["equipos"].apply(lambda x: (x or {}).get("senior") if isinstance(x, dict) else None)
    df["zona"]    = df["equipos"].apply(lambda x: (x or {}).get("zona") if isinstance(x, dict) else None)
    return df.drop(columns=["equipos"], errors="ignore")


@st.cache_data(ttl=3600, show_spinner=False, persist="disk")
def load_equipos_supabase() -> dict:
    """
    Retorna estructura compatible con GRUPOS_TERRENO de data.py.
    {nombre_equipo: {senior, miembros, zona}}
    """
    rows = _query("equipos", "select=id,nombre_equipo,senior,zona&activo=eq.true", limit=100)
    tecs = _query("tecnicos", "select=nombre_corto,nombre_completo,equipo_id,aplica_bono", limit=200)

    tecs_por_equipo: dict = {}
    for t in tecs:
        eid = t.get("equipo_id")
        if eid:
            tecs_por_equipo.setdefault(eid, []).append(t.get("nombre_corto",""))

    grupos = {}
    for eq in rows:
        miembros = tecs_por_equipo.get(eq["id"], [])
        grupos[eq["nombre_equipo"]] = {
            "senior":   eq.get("senior", eq["nombre_equipo"]),
            "miembros": miembros,
            "zona":     eq.get("zona", ""),
        }
    return grupos


# ═════════════════════════════════════════════════════════════════════════════
# 4. LLAMADOS SLA  (reemplaza load_all_llamados / Excel COPEC+Shell+ESMAX)
# ═════════════════════════════════════════════════════════════════════════════

_PAT_SLA_COPEC = re.compile(r"Tiempo\s+de\s+respuesta\s*:\s*(\d+)", re.IGNORECASE)

# Umbral en horas: {prioridad: (horas_RM, horas_Regiones)}
_COPEC_UMBRALES: dict[str, tuple[int, int]] = {
    "P1": (18, 24),
    "P2": (24, 48),
    "P3": (36, 72),
    "P4": (96, 96),
}

# ── Aramco / Cotalker: SLA desde Metabase ────────────────────────────────────
_METABASE_COTALKER_URL = (
    "https://bi.cotalker.com/api/public/card"
    "/56662edd-715d-4dbe-af9a-21891f4dbb97/query/json"
)
_PAT_COTALKER_N = re.compile(r"^(\d{5,8})(?:\s*-|\s*$)")
_ARAMCO_SLA_TO_PRIO = {24: "P1", 48: "P2", 72: "P3"}

@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_cotalker_sla_index() -> dict:
    """
    Descarga el panel Metabase de Cotalker y retorna {n_cotalker(int): sla_horas(int)}.
    Cache 1h para no sobrecargar Metabase en cada recarga del dashboard.
    """
    try:
        r = requests.get(_METABASE_COTALKER_URL,
                         headers={"Accept": "application/json"}, timeout=60)
        r.raise_for_status()
        idx = {}
        for row in r.json():
            n = row.get("N° Cotalker")
            sla = row.get("SLA esperado")
            if n and sla:
                try:
                    idx[int(n)] = int(float(sla))
                except (ValueError, TypeError):
                    pass
        return idx
    except Exception:
        return {}

def _aramco_prio_from_nota(nota_tarea: str, cotalker_idx: dict) -> "tuple[str, int] | None":
    """
    Extrae N° Cotalker de nota_tarea, busca SLA en el índice Metabase,
    retorna (prioridad, umbral_horas) o None.
    Aramco SLA: 24h→P1, 48h→P2, 72h→P3, otro→P4 (umbral = mismo valor).
    """
    m = _PAT_COTALKER_N.match(str(nota_tarea or "").strip())
    if not m:
        return None
    n_cot = int(m.group(1))
    sla_h = cotalker_idx.get(n_cot)
    if sla_h is None:
        return None
    prio = _ARAMCO_SLA_TO_PRIO.get(sla_h, "P4")
    return prio, sla_h

def _copec_prio_from_nota(nota: str, zona: str) -> "tuple[str, int] | None":
    """
    Deriva (prioridad, umbral_horas) para un OT COPEC leyendo nota_tarea.
    Retorna None si no hay 'Tiempo de respuesta' en la nota.

    Tabla SLA COPEC:
      P1: Santiago=18h, Regiones=24h
      P2: Santiago=24h, Regiones=48h
      P3: Santiago=36h, Regiones=72h
      P4: cualquier zona=96h

    Mapeo inequívoco:   18h→P1  36h→P3  48h→P2  72h→P3  96h→P4
    Ambiguo:            24h → P2 si Santiago, P1 si Regiones
    """
    m = _PAT_SLA_COPEC.search(nota or "")
    if not m:
        return None
    sla_h = int(m.group(1))
    es_reg = "santiago" not in (zona or "").lower()

    # Derivar prioridad desde SLA declarado en el correo
    _SLA_TO_PRIO: dict[int, str] = {18: "P1", 36: "P3", 48: "P2", 72: "P3", 96: "P4"}
    if sla_h in _SLA_TO_PRIO:
        prio = _SLA_TO_PRIO[sla_h]
    elif sla_h == 24:
        prio = "P1" if es_reg else "P2"   # ambiguo: necesita zona
    else:
        return None  # SLA desconocido → no modificar

    umbral = _COPEC_UMBRALES[prio][1 if es_reg else 0]
    return prio, umbral


def load_all_llamados_supabase(desde: str = "2026-01-01") -> pd.DataFrame:
    """No usa @st.cache_data — el dashboard lo cachea via _sc() para control total."""
    """
    Retorna DataFrame compatible con load_all_llamados().
    Lee desde v_llamados_sla (vista que replica estructura del Excel).
    La prioridad COPEC se recalcula desde nota_tarea para que el sync
    de Fracttal no distorsione los resultados.
    """
    rows = _query(
        "v_llamados_sla",
        f"select=os_fracttal,n_llamado,cliente,eds_occim,eds_nombre,comuna,region,"
        f"fecha_llamado,hora_llamado,fecha_atencion,hora_fin,tecnico,tecnico_corto,"
        f"equipo,equipo_senior,prioridad,zona,tiempo_resp_horas,tiempo_resp_esp,"
        f"cumplimiento,excepcion_motivo,estado_atencion,facturacion,fecha_creacion"
        f"&fecha_llamado=gte.{desde}",
        limit=10_000
    )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Compatibilidad con campos que el dashboard espera
    df = df.rename(columns={
        "os_fracttal":      "os_fracttal",
        "n_llamado":        "n_llamado",
        "tiempo_resp_horas":"horas_resolucion",
        "tiempo_resp_esp":  "tiempo_resp_esp",
    })
    # Convertir fechas: pd.to_datetime falla con timestamps sin microsegundos en pandas 3.x
    # pd.Timestamp() maneja ambos formatos correctamente
    def _safe_ts(x):
        if not x or str(x).strip() in ("", "None", "null"):
            return pd.NaT
        try:
            t = pd.Timestamp(str(x))
            return t.tz_convert(None) if t.tzinfo is not None else t
        except Exception:
            return pd.NaT

    df["fecha_llamado"]  = df["fecha_llamado"].apply(_safe_ts)
    df["fecha_atencion"] = df["fecha_atencion"].apply(_safe_ts)
    df["fecha_llamado_dt"] = df["fecha_llamado"]

    # Normalizar etiqueta del cliente: "ESMAX (Aramco)" → "Aramco (Esmax)"
    if "cliente" in df.columns:
        df["cliente"] = df["cliente"].replace({"ESMAX (Aramco)": "Aramco (Esmax)"})

    # Mapear cumplimiento al formato original
    df["cumplimiento"] = df["cumplimiento"].replace({
        "CUMPLE":    "CUMPLE",
        "NO CUMPLE": "NO CUMPLE",
        "PENDIENTE": "SIN DATOS",
        "SIN UMBRAL":"SIN DATOS",
    })

    # Campo Año y Mes (compatibilidad)
    df["Año"] = df["fecha_llamado"].dt.year
    df["Mes"] = df["fecha_llamado"].dt.month

    # ── SAFETY NET: recorrección de prioridad COPEC desde nota_tarea ──────────
    # sync_fracttal_supabase.py YA escribe prioridad_calc correcta desde
    # nota_tarea. Esta corrección en tiempo de lectura es red de seguridad
    # para OTs cuyo sync falló, quedaron desincronizadas, o son históricas
    # anteriores al fix. Si el sync escribió bien, este bloque es idempotente.
    copec_idx = df.index[df["cliente"] == "COPEC"].tolist()
    if copec_idx:
        copec_ids = df.loc[copec_idx, "os_fracttal"].tolist()
        # Cargar nota_tarea en lotes (máx 200 por URL)
        notas: dict[str, str] = {}
        chunk_size = 200
        for i in range(0, len(copec_ids), chunk_size):
            chunk = copec_ids[i : i + chunk_size]
            nota_rows = _query(
                "ordenes_trabajo",
                f"select=id_ot,nota_tarea&id_ot=in.({','.join(chunk)})",
                limit=len(chunk) + 1,
            )
            for r in nota_rows:
                if r.get("nota_tarea"):
                    notas[r["id_ot"]] = r["nota_tarea"]

        # Recalcular prioridad y umbral fila a fila para COPEC
        for idx in copec_idx:
            ot   = df.at[idx, "os_fracttal"]
            nota = notas.get(ot, "")
            zona = str(df.at[idx, "zona"] or "")
            result = _copec_prio_from_nota(nota, zona)
            if result:
                nueva_prio, nuevo_umbral = result
                df.at[idx, "prioridad"]       = nueva_prio
                df.at[idx, "tiempo_resp_esp"] = nuevo_umbral
                # IMPORTANTE: NO recalcular cumplimiento si la OT tiene excepción
                # SLA registrada. La vista v_llamados_sla ya devolvió "CUMPLE" por
                # la excepción — si recalculamos aquí con horas<=umbral, pisamos
                # esa decisión y la OT vuelve a "NO CUMPLE" incorrectamente.
                exc_mot = df.at[idx, "excepcion_motivo"] if "excepcion_motivo" in df.columns else None
                tiene_exc = pd.notna(exc_mot) and str(exc_mot).strip() != ""
                if not tiene_exc:
                    horas = df.at[idx, "horas_resolucion"]
                    if pd.notna(horas) and horas is not None:
                        df.at[idx, "cumplimiento"] = (
                            "CUMPLE" if float(horas) <= nuevo_umbral else "NO CUMPLE"
                        )

    # ── SAFETY NET: recorrección de prioridad ARAMCO desde Cotalker ──────────
    # sync_fracttal_supabase.py YA consulta Cotalker Metabase al momento del
    # sync y escribe prioridad_calc correcta. Esta corrección en tiempo de
    # lectura es red de seguridad para OTs desincronizadas o históricas.
    aramco_idx = df.index[df["cliente"] == "Aramco (Esmax)"].tolist()
    if aramco_idx:
        cotalker_sla = _fetch_cotalker_sla_index()
        if cotalker_sla:
            aramco_ids = df.loc[aramco_idx, "os_fracttal"].tolist()
            notas_a: dict[str, str] = {}
            for i in range(0, len(aramco_ids), 200):
                chunk = aramco_ids[i : i + 200]
                nota_rows = _query(
                    "ordenes_trabajo",
                    f"select=id_ot,nota_tarea&id_ot=in.({','.join(chunk)})",
                    limit=len(chunk) + 1,
                )
                for r in nota_rows:
                    if r.get("nota_tarea"):
                        notas_a[r["id_ot"]] = r["nota_tarea"]

            for idx in aramco_idx:
                ot = df.at[idx, "os_fracttal"]
                nota = notas_a.get(ot, "")
                result = _aramco_prio_from_nota(nota, cotalker_sla)
                if result:
                    nueva_prio, nuevo_umbral = result
                    df.at[idx, "prioridad"]       = nueva_prio
                    df.at[idx, "tiempo_resp_esp"] = nuevo_umbral
                    # IMPORTANTE: NO recalcular cumplimiento si la OT tiene excepción
                    # SLA. La vista v_llamados_sla marca como CUMPLE por la
                    # excepción — recalcular con horas<=umbral rompería esa lógica
                    # y las OTs excepcionadas volverían a NO CUMPLE (bug reportado
                    # con OS-37080: 52h > 24h umbral, pero validada por operaciones
                    # como caso ajeno a Occimiano).
                    exc_mot = df.at[idx, "excepcion_motivo"] if "excepcion_motivo" in df.columns else None
                    tiene_exc = pd.notna(exc_mot) and str(exc_mot).strip() != ""
                    if not tiene_exc:
                        horas = df.at[idx, "horas_resolucion"]
                        if pd.notna(horas) and horas is not None:
                            df.at[idx, "cumplimiento"] = (
                                "CUMPLE" if float(horas) <= nuevo_umbral else "NO CUMPLE"
                            )

    return df


# ═════════════════════════════════════════════════════════════════════════════
# 5. UMBRALES SLA  (reemplaza SLA_HOURS hardcodeado)
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=86400, show_spinner=False)
def load_sla_umbrales_supabase() -> dict:
    """
    Retorna dict compatible con SLA_HOURS de gdrive.py.
    {cliente: {prioridad: {zona: horas}}}
    """
    rows = _query("sla_umbrales_horas", "select=cliente,prioridad,zona,horas", limit=200)
    umbrales: dict = {}
    for r in rows:
        cli  = r["cliente"]
        prio = r["prioridad"]
        zona = r["zona"]
        hrs  = r["horas"]
        umbrales.setdefault(cli, {}).setdefault(prio, {})[zona] = hrs
    return umbrales


# ═════════════════════════════════════════════════════════════════════════════
# 6a. MANTENCIONES CORRECTIVAS (para Uptime SLA)
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def load_correctivas_supabase() -> list:
    """OTs correctivas con campos de paro de equipo."""
    return _query(
        "ordenes_trabajo",
        "select=id_ot,estado,estado_tarea,tipo_tarea,"
        "codigo_activo,nombre_activo,ubicacion,"
        "cliente,estacion,codigo_eds,"
        "responsable,fecha_creacion,fecha_finalizacion,"
        "duracion_real_seg,duracion_estim_seg,"
        "paro_equipo,tiempo_paro_estim_seg,tiempo_paro_real_seg"
        "&tipo_tarea=ilike.*CORRECTIV*"
        "&fecha_creacion=gte.2026-01-01"
        "&order=fecha_creacion.desc",
        limit=10_000,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 6b. MANTENCIONES PREVENTIVAS
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=1800, show_spinner=False, persist="disk")
def load_preventivas_supabase() -> list:
    """OTs preventivas con todos los campos del módulo."""
    return _query(
        "ordenes_trabajo",
        "select=id_ot,estado,estado_tarea,nombre_tarea,tipo_tarea,"
        "activador,fecha_inicio,duracion_estim_seg,duracion_real_seg,"
        "codigo_activo,nombre_activo,ubicacion,clasificacion_2,"
        "cliente,estacion,codigo_eds,"
        "responsable,fecha_creacion,fecha_finalizacion,fecha_programada,"
        "paro_equipo,tiempo_paro_estim_seg,tiempo_paro_real_seg,plan_tareas"
        "&tipo_tarea=ilike.*PREVENTIV*"
        "&fecha_creacion=gte.2026-01-01"
        "&order=fecha_programada.desc",
        limit=10_000,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 7. ÍNDICE COTALKER  (N° Cotalker por OS Fracttal — solo ESMAX/Aramco)
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=1800, show_spinner=False)
def load_notas_tarea_index(ot_list: tuple) -> dict:
    """
    Retorna {id_ot: nota_tarea} para la lista de OTs solicitadas.
    Usado para extraer 'Detalles del incidente' / descripciones de problema.
    Recibe una tupla para que el cache funcione (las listas no son hashables).
    """
    if not ot_list:
        return {}
    result: dict = {}
    # PostgREST tiene límite de URL: traer de a 200 OTs por request
    ot_list = list(ot_list)
    for i in range(0, len(ot_list), 200):
        chunk = ot_list[i:i+200]
        rows = _query(
            "ordenes_trabajo",
            f"select=id_ot,nota_tarea&id_ot=in.({','.join(chunk)})",
            limit=500,
        )
        for r in rows:
            if r.get("id_ot") and r.get("nota_tarea"):
                result[r["id_ot"]] = r["nota_tarea"]
    return result


@st.cache_data(ttl=1800, show_spinner=False)
def load_cotalker_index_supabase() -> dict:
    """
    Retorna {id_ot: str} con el N° de aviso/referencia del cliente para cada OT:
      - ESMAX/Aramco: PRIMER número de nota_tarea (formato "151022 - 169357 - ee_s268 - EDS:...")
                     NOTA: NO usamos el campo n_cotalker porque robot_esmax.py asignó
                     ~22% de los valores a OTs incorrectas (matching por EDS+fecha sin
                     validar el N° real). La nota_tarea contiene el N° verdadero que
                     Fracttal recibió al crear la OT.
      - COPEC: nota_tarea parseado → "No. Aviso: XXXXXXXX"
      - SHELL: nota_tarea parseado → "ID Solicitud: XXXX"
    """
    import re
    _pat_aviso = re.compile(r"No\.\s*Aviso\s*:\s*(\d+)", re.IGNORECASE)
    result: dict = {}

    # 1) Aramco/ESMAX — N° real desde nota_tarea (primer número antes del guión)
    rows_esmax = _query(
        "ordenes_trabajo",
        "select=id_ot,nota_tarea"
        "&nota_tarea=not.is.null"
        "&cliente=eq.ESMAX (Aramco)",
        limit=10_000,
    )
    _pat_cot = re.compile(r"^(\d{5,8})\s*-")
    for r in rows_esmax:
        ot = r.get("id_ot")
        if not ot:
            continue
        m = _pat_cot.match(str(r.get("nota_tarea") or "").strip())
        if m:
            result[ot] = m.group(1)

    # 2) COPEC  → "No. Aviso: XXXXXXXX"
    #    SHELL  → 'ID Solicitud "XXXX"' o "ID Solicitud: XXXX"
    # NOTA: queries separadas porque `cliente=in.(COPEC,SHELL (Enex))` rompe
    # el parser de PostgREST por los paréntesis internos en "SHELL (Enex)".
    _pat_id_sol = re.compile(r'ID\s*Solicitud\s*["\s:]+(\d+)', re.IGNORECASE)

    rows_copec = _query(
        "ordenes_trabajo",
        "select=id_ot,nota_tarea"
        "&nota_tarea=not.is.null"
        "&cliente=eq.COPEC",
        limit=10_000,
    )
    for r in rows_copec:
        ot   = r.get("id_ot")
        nota = str(r.get("nota_tarea") or "")
        if not ot or not nota:
            continue
        m = _pat_aviso.search(nota)
        if m:
            result[ot] = m.group(1)

    rows_shell = _query(
        "ordenes_trabajo",
        "select=id_ot,nota_tarea"
        "&nota_tarea=not.is.null"
        "&cliente=eq.SHELL (Enex)",
        limit=10_000,
    )
    for r in rows_shell:
        ot   = r.get("id_ot")
        nota = str(r.get("nota_tarea") or "")
        if not ot or not nota:
            continue
        m = _pat_id_sol.search(nota)
        if m:
            result[ot] = m.group(1)

    return result


# ═════════════════════════════════════════════════════════════════════════════
# 8. EN VIVO — órdenes actualmente en ejecución
# ═════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=120, show_spinner=False)
def load_ots_en_vivo_supabase() -> list:
    """
    OTs en estado activo (En Progreso / Por Validar / Por Iniciar).
    TTL = 2 min para que los datos sean frescos sin sobrecargar Supabase.
    Incluye preventivas y correctivas desde dic-2025 en adelante.
    """
    return _query(
        "ordenes_trabajo",
        "select=id_ot,estado,estado_tarea,tipo_tarea,nombre_tarea,"
        "responsable,codigo_activo,nombre_activo,ubicacion,cliente,estacion,codigo_eds,"
        "prioridad,prioridad_calc,fecha_creacion,fecha_inicio,fecha_programada,"
        "fecha_finalizacion,duracion_estim_seg,tiene_numeral,tiene_recursos"
        "&estado=in.(En Progreso,Por Validar,Por Iniciar)"
        "&fecha_creacion=gte.2025-12-20",
        limit=500,
    )

"""
asignaciones_db.py — CRUD de asignaciones patente <-> tecnico con vigencia.
==========================================================================
Usado por la herramienta web (mobile_sto.py /vehiculos) y por el dashboard
para resolver "quien conducia la patente P en la fecha F".
"""
import os
from datetime import date, datetime, timedelta

import requests

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
_TABLE = "asignaciones_vehiculo"


def _hdr(extra=None):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _creds_ok():
    return bool(SUPABASE_URL and SUPABASE_KEY)


# ── Lectura ──────────────────────────────────────────────────────────────
def list_all():
    """Todas las asignaciones, ordenadas por patente y fecha_desde desc."""
    if not _creds_ok():
        return []
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{_TABLE}"
        f"?select=*&order=patente.asc,fecha_desde.desc",
        headers=_hdr(), timeout=20,
    )
    if r.status_code != 200:
        return []
    return r.json()


def list_por_patente(patente):
    if not _creds_ok():
        return []
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{_TABLE}"
        f"?select=*&patente=eq.{patente}&order=fecha_desde.desc",
        headers=_hdr(), timeout=15,
    )
    return r.json() if r.status_code == 200 else []


def tecnico_en_fecha(patente, fecha_iso):
    """Devuelve la asignacion (dict) vigente para (patente, fecha) o None.
    Vigente = fecha_desde <= fecha AND (fecha_hasta IS NULL OR fecha >= ... hasta)."""
    if not _creds_ok():
        return None
    # fecha_desde <= fecha
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{_TABLE}"
        f"?select=*&patente=eq.{patente}"
        f"&fecha_desde=lte.{fecha_iso}"
        f"&or=(fecha_hasta.is.null,fecha_hasta.gte.{fecha_iso})"
        f"&order=fecha_desde.desc&limit=1",
        headers=_hdr(), timeout=15,
    )
    if r.status_code != 200:
        return None
    rows = r.json()
    return rows[0] if rows else None


# ── Escritura ────────────────────────────────────────────────────────────
def _parse_date(s):
    if isinstance(s, date):
        return s
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def _solapa(a_desde, a_hasta, b_desde, b_hasta):
    """True si los intervalos [a_desde,a_hasta] y [b_desde,b_hasta] se solapan.
    hasta=None significa infinito (vigente)."""
    a_fin = a_hasta or date(2999, 12, 31)
    b_fin = b_hasta or date(2999, 12, 31)
    return a_desde <= b_fin and b_desde <= a_fin


def validar_solapamiento(patente, fecha_desde, fecha_hasta, excluir_id=None):
    """Devuelve (ok, mensaje). Verifica que el nuevo intervalo no choque con
    otras asignaciones de la MISMA patente."""
    fd = _parse_date(fecha_desde)
    fh = _parse_date(fecha_hasta) if fecha_hasta else None
    if fh and fh < fd:
        return False, "La fecha 'hasta' no puede ser anterior a 'desde'."
    for a in list_por_patente(patente):
        if excluir_id and a.get("id") == excluir_id:
            continue
        ad = _parse_date(a["fecha_desde"])
        ah = _parse_date(a["fecha_hasta"]) if a.get("fecha_hasta") else None
        if _solapa(fd, fh, ad, ah):
            rango_a = f"{ad.strftime('%d-%m-%Y')} → {'vigente' if not ah else ah.strftime('%d-%m-%Y')}"
            return False, (f"Se solapa con otra asignacion de {patente}: "
                           f"{a['nombre_tecnico']} ({rango_a}). "
                           f"Cierra o ajusta esa asignacion primero.")
    return True, ""


def crear(patente, rut, nombre_tecnico, equipo, fecha_desde,
          fecha_hasta=None, nota=None, creado_por=None,
          auto_cerrar_vigente=True):
    """Crea una asignacion. Si auto_cerrar_vigente y existe una asignacion
    ABIERTA (fecha_hasta NULL) para la patente cuya fecha_desde < nueva
    fecha_desde, la cierra en (nueva_desde - 1 dia) antes de insertar."""
    if not _creds_ok():
        return False, "Sin credenciales Supabase."
    fd = _parse_date(fecha_desde)
    # Auto-cerrar la vigente previa si aplica
    if auto_cerrar_vigente:
        for a in list_por_patente(patente):
            if a.get("fecha_hasta") is None and _parse_date(a["fecha_desde"]) < fd:
                nuevo_hasta = (fd - timedelta(days=1)).isoformat()
                actualizar(a["id"], {"fecha_hasta": nuevo_hasta})
    ok, msg = validar_solapamiento(patente, fecha_desde, fecha_hasta)
    if not ok:
        return False, msg
    fila = {
        "patente": patente,
        "rut": rut,
        "nombre_tecnico": nombre_tecnico,
        "equipo": equipo,
        "fecha_desde": fd.isoformat(),
        "fecha_hasta": _parse_date(fecha_hasta).isoformat() if fecha_hasta else None,
        "nota": nota,
        "creado_por": creado_por,
    }
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{_TABLE}",
                      headers=_hdr({"Prefer": "return=minimal"}),
                      json=fila, timeout=20)
    if r.status_code in (200, 201, 204):
        return True, f"Asignacion creada: {patente} → {nombre_tecnico} desde {fd.strftime('%d-%m-%Y')}."
    return False, f"Error al crear (HTTP {r.status_code}): {r.text[:200]}"


def actualizar(asig_id, campos):
    if not _creds_ok():
        return False, "Sin credenciales."
    campos = {**campos, "updated_at": datetime.utcnow().isoformat()}
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{_TABLE}?id=eq.{asig_id}",
                       headers=_hdr({"Prefer": "return=minimal"}),
                       json=campos, timeout=20)
    return (r.status_code in (200, 204)), (r.text[:200] if r.status_code not in (200, 204) else "")


def cerrar(asig_id, fecha_hasta):
    """Cierra una asignacion vigente poniendo su fecha_hasta."""
    fh = _parse_date(fecha_hasta)
    a = None
    for row in list_all():
        if row.get("id") == asig_id:
            a = row
            break
    if a:
        fd = _parse_date(a["fecha_desde"])
        if fh < fd:
            return False, "La fecha de cierre no puede ser anterior a la fecha desde."
    ok, err = actualizar(asig_id, {"fecha_hasta": fh.isoformat()})
    return ok, ("" if ok else err)


def eliminar(asig_id):
    if not _creds_ok():
        return False
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/{_TABLE}?id=eq.{asig_id}",
                        headers=_hdr(), timeout=15)
    return r.status_code in (200, 204)

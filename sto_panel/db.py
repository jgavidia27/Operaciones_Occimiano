"""
db.py — CRUD sobre sto_validaciones_reincidencia.

Upsert por (codigo_eds, fecha_disparo). Cada disparo (3 llamados en una
ventana móvil de 20 días) es una validación independiente.
"""
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

_PARENT = Path(__file__).resolve().parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from supabase_client import _query  # noqa: E402

TABLE = "sto_validaciones_reincidencia"


def _sb_creds() -> tuple[str, str]:
    try:
        url = str(st.secrets["SUPABASE_URL"])
        key = str(st.secrets["SUPABASE_KEY"])
        if url and key:
            return url, key
    except Exception:
        pass
    return os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")


def _upsert(codigo_eds: str, fecha_disparo: date, payload: dict) -> bool:
    url, key = _sb_creds()
    if not url or not key:
        return False
    body = {
        "codigo_eds":    codigo_eds,
        "fecha_disparo": fecha_disparo.isoformat(),
        **payload,
    }
    r = requests.post(
        f"{url}/rest/v1/{TABLE}?on_conflict=codigo_eds,fecha_disparo",
        headers={
            "apikey":        key,
            "Authorization": f"Bearer {key}",
            "Content-Type":  "application/json",
            "Prefer":        "resolution=merge-duplicates,return=minimal",
        },
        json=body,
        timeout=15,
    )
    return r.status_code in (200, 201, 204)


def get(codigo_eds: str, fecha_disparo: date) -> dict | None:
    rows = _query(
        TABLE,
        f"select=*&codigo_eds=eq.{codigo_eds}"
        f"&fecha_disparo=eq.{fecha_disparo.isoformat()}",
        limit=1,
    )
    return rows[0] if rows else None


def upsert_clasificaciones(codigo_eds: str, fecha_disparo: date,
                           clasificaciones: dict) -> bool:
    return _upsert(codigo_eds, fecha_disparo, {"clasificaciones": clasificaciones})


def upsert_textos(codigo_eds: str, fecha_disparo: date,
                  resumen: str, solucion: str) -> bool:
    return _upsert(codigo_eds, fecha_disparo, {
        "resumen_falla":      resumen,
        "solucion_propuesta": solucion,
    })


def firmar(codigo_eds: str, fecha_disparo: date, email: str, nombre: str,
           clasificaciones: dict, resumen: str, solucion: str) -> bool:
    return _upsert(codigo_eds, fecha_disparo, {
        "clasificaciones":     clasificaciones,
        "resumen_falla":       resumen,
        "solucion_propuesta":  solucion,
        "validado":            True,
        "validado_por_email":  email,
        "validado_por_nombre": nombre,
        "validado_at":         datetime.now(timezone.utc).isoformat(),
    })


def estados_por_disparos(pares: list[tuple[str, date]]) -> dict[tuple[str, str], dict]:
    """Recibe [(codigo_eds, fecha_disparo)]. Retorna {(codigo_eds, fecha_iso): fila}.
    Hace UNA sola query filtrando por el conjunto de EDS y luego cruza en Python.
    """
    if not pares:
        return {}
    eds_set = list({e for e, _ in pares})
    eds_in = ",".join(eds_set)
    rows = _query(
        TABLE,
        f"select=codigo_eds,fecha_disparo,validado,validado_por_nombre,validado_at,"
        f"clasificaciones,resumen_falla,solucion_propuesta"
        f"&codigo_eds=in.({eds_in})",
        limit=5000,
    )
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        out[(r["codigo_eds"], r["fecha_disparo"])] = r
    return out


def validaciones_anteriores_eds(codigo_eds: str,
                                antes_de: date) -> list[dict]:
    """Todas las validaciones firmadas de una EDS anteriores a `antes_de`.
    Orden desc por fecha_disparo (la más reciente primero)."""
    rows = _query(
        TABLE,
        f"select=codigo_eds,fecha_disparo,validado_por_nombre,validado_at,clasificaciones"
        f"&codigo_eds=eq.{codigo_eds}"
        f"&validado=eq.true"
        f"&fecha_disparo=lt.{antes_de.isoformat()}"
        f"&order=fecha_disparo.desc",
        limit=100,
    )
    return rows or []


def eds_con_validacion_previa(codigos_eds: list[str],
                              antes_de: date) -> set[str]:
    """Subconjunto de EDS que tienen al menos una validación firmada anterior
    a `antes_de`. Usado para marcar 🔴 reincidentes en el listado del home."""
    if not codigos_eds:
        return set()
    eds_in = ",".join(codigos_eds)
    rows = _query(
        TABLE,
        f"select=codigo_eds"
        f"&codigo_eds=in.({eds_in})"
        f"&validado=eq.true"
        f"&fecha_disparo=lt.{antes_de.isoformat()}",
        limit=5000,
    )
    return {r["codigo_eds"] for r in rows}


def historial(limit: int = 500) -> pd.DataFrame:
    rows = _query(
        TABLE,
        "select=codigo_eds,fecha_disparo,validado_por_nombre,validado_at,"
        "resumen_falla,solucion_propuesta,clasificaciones"
        "&validado=eq.true&order=validado_at.desc",
        limit=limit,
    )
    return pd.DataFrame(rows) if rows else pd.DataFrame()

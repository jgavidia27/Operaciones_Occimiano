"""
db.py — CRUD sobre sto_validaciones_reincidencia.

Usa upsert por (codigo_eds, periodo) vía on_conflict, patrón que _post()
del cliente compartido no expone. Por eso hace HTTP directo con requests,
leyendo credenciales del mismo lugar que supabase_client._get_creds().
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


def _upsert(codigo_eds: str, periodo: date, payload: dict) -> bool:
    url, key = _sb_creds()
    if not url or not key:
        return False
    body = {
        "codigo_eds": codigo_eds,
        "periodo":    periodo.isoformat(),
        **payload,
    }
    r = requests.post(
        f"{url}/rest/v1/{TABLE}?on_conflict=codigo_eds,periodo",
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


def get(codigo_eds: str, periodo: date) -> dict | None:
    rows = _query(
        TABLE,
        f"select=*&codigo_eds=eq.{codigo_eds}"
        f"&periodo=eq.{periodo.isoformat()}",
        limit=1,
    )
    return rows[0] if rows else None


def upsert_clasificaciones(codigo_eds: str, periodo: date,
                           clasificaciones: dict) -> bool:
    return _upsert(codigo_eds, periodo, {"clasificaciones": clasificaciones})


def upsert_textos(codigo_eds: str, periodo: date,
                  resumen: str, solucion: str) -> bool:
    return _upsert(codigo_eds, periodo, {
        "resumen_falla":      resumen,
        "solucion_propuesta": solucion,
    })


def firmar(codigo_eds: str, periodo: date, email: str, nombre: str,
           clasificaciones: dict, resumen: str, solucion: str) -> bool:
    return _upsert(codigo_eds, periodo, {
        "clasificaciones":     clasificaciones,
        "resumen_falla":       resumen,
        "solucion_propuesta":  solucion,
        "validado":            True,
        "validado_por_email":  email,
        "validado_por_nombre": nombre,
        "validado_at":         datetime.now(timezone.utc).isoformat(),
    })


def estados_del_periodo(periodo: date) -> dict[str, dict]:
    """{codigo_eds: fila} para todas las validaciones del periodo."""
    rows = _query(
        TABLE,
        f"select=codigo_eds,validado,validado_por_nombre,validado_at,"
        f"clasificaciones,resumen_falla,solucion_propuesta"
        f"&periodo=eq.{periodo.isoformat()}",
        limit=1000,
    )
    return {r["codigo_eds"]: r for r in rows}


def validaciones_anteriores(periodo_actual: date) -> dict[str, list[dict]]:
    """{codigo_eds: [validaciones firmadas anteriores, orden desc por periodo]}.
    Se usa para detectar EDS 'Reincidente crítica' — las que ya fueron validadas
    en algún mes previo y vuelven a aparecer con reincidencia en el mes actual.
    """
    rows = _query(
        TABLE,
        f"select=codigo_eds,periodo,validado_por_nombre,validado_at,clasificaciones"
        f"&validado=eq.true"
        f"&periodo=lt.{periodo_actual.isoformat()}"
        f"&order=periodo.desc",
        limit=2000,
    )
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["codigo_eds"], []).append(r)
    return out


def historial(limit: int = 500) -> pd.DataFrame:
    rows = _query(
        TABLE,
        "select=codigo_eds,periodo,validado_por_nombre,validado_at,"
        "resumen_falla,solucion_propuesta,clasificaciones"
        "&validado=eq.true&order=validado_at.desc",
        limit=limit,
    )
    return pd.DataFrame(rows) if rows else pd.DataFrame()

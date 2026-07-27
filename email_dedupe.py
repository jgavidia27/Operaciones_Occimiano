"""
email_dedupe.py — Dedupe idempotente para scripts de envío de correos.
======================================================================

Los workflows semanales tienen 3 crons escalonados como backup (por si
GitHub Actions se salta el disparo). Este helper garantiza que si el
primer intento del día funcionó bien, los subsiguientes salgan sin
re-enviar duplicados a los destinatarios.

Usa la tabla `email_threads` de Supabase (topic, mes/fecha, message_id).

Ejemplo:
    from email_dedupe import ya_enviado_hoy, marcar_enviado_hoy
    if ya_enviado_hoy("weekly_indicadores"):
        print("Ya se envió hoy, salgo.")
        sys.exit(0)
    ...envío correos...
    marcar_enviado_hoy("weekly_indicadores", "Resumen semanal — Julio")
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

import requests


TABLE = "email_threads"


def _sb() -> tuple[str, str]:
    return os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")


def _hoy_cl() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")


def ya_enviado_hoy(topic: str, fecha: str | None = None) -> bool:
    """True si ya existe registro en email_threads para (topic + fecha hoy CL).
    Falla silenciosa a False si Supabase no está accesible."""
    url, key = _sb()
    if not url or not key:
        return False
    _hoy = fecha or _hoy_cl()
    try:
        r = requests.get(
            f"{url}/rest/v1/{TABLE}",
            params={"select": "message_id",
                    "topic":  f"eq.{topic}_daily",
                    "mes":    f"eq.{_hoy}", "limit": 1},
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=10,
        )
        return r.status_code == 200 and bool(r.json())
    except Exception:
        return False


def marcar_enviado_hoy(topic: str, subject: str, fecha: str | None = None) -> bool:
    """Guarda la marca de envío de HOY en email_threads. Idempotente
    (upsert on_conflict topic,mes)."""
    url, key = _sb()
    if not url or not key:
        return False
    _hoy = fecha or _hoy_cl()
    try:
        r = requests.post(
            f"{url}/rest/v1/{TABLE}",
            params={"on_conflict": "topic,mes"},
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=[{
                "topic":      f"{topic}_daily",
                "mes":        _hoy,
                "message_id": f"local-{_hoy}",
                "subject":    subject[:200] if subject else "",
                "sent_at":    datetime.now(timezone.utc).isoformat(),
            }],
            timeout=10,
        )
        return r.status_code in (200, 201, 204)
    except Exception:
        return False

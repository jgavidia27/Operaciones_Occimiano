"""
mailer.py — Envío de correos transaccionales vía Resend (https://resend.com).

Se usa para los flujos de autenticación:
  - Invitación a usuario nuevo (setea tu primera contraseña)
  - Reset de contraseña ("olvidé mi contraseña")
  - Notificación de cambio de contraseña

API key: variable de entorno RESEND_API_KEY (o st.secrets["RESEND_API_KEY"]).
Remitente: variable MAIL_FROM (ej. "Occimiano <noreply@occimiano.cl>");
si no está definida, usa "onboarding@resend.dev" (dominio de pruebas de Resend
útil durante desarrollo antes de verificar occimiano.cl).
"""

from __future__ import annotations

import os
import json
import logging
from typing import Optional

import requests
import streamlit as st

_log = logging.getLogger("occim.mailer")

RESEND_ENDPOINT  = "https://api.resend.com/emails"
DEFAULT_FROM     = "Occimiano Dashboard <onboarding@resend.dev>"
DEFAULT_TIMEOUT  = 15


# ── Configuración ────────────────────────────────────────────────────────────

def _get_secret(name: str, default: str = "") -> str:
    """Lee primero st.secrets, luego env var. Vacío si no existe."""
    try:
        v = st.secrets[name]
        if v:
            return str(v).strip()
    except Exception:
        pass
    return os.getenv(name, default).strip()


def _api_key() -> str:
    return _get_secret("RESEND_API_KEY")


def _from_address() -> str:
    return _get_secret("MAIL_FROM", DEFAULT_FROM)


def is_configured() -> bool:
    """True si hay API key de Resend configurada."""
    return bool(_api_key())


# ── Envío crudo ──────────────────────────────────────────────────────────────

def send_email(
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    from_address: Optional[str] = None,
) -> tuple[bool, str]:
    """Envía un correo. Devuelve (ok, mensaje_diagnóstico)."""
    api = _api_key()
    if not api:
        return False, (
            "RESEND_API_KEY no configurada. Agrega la clave en Streamlit Cloud "
            "Secrets (o variable de entorno) y vuelve a intentar."
        )
    sender = (from_address or _from_address()).strip()
    payload = {
        "from":    sender,
        "to":      [to.strip()],
        "subject": subject,
        "html":    html,
    }
    if text:
        payload["text"] = text

    try:
        r = requests.post(
            RESEND_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api}",
                "Content-Type":  "application/json",
            },
            data=json.dumps(payload),
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        _log.exception("Resend network error")
        return False, f"Error de red al contactar Resend: {e!r}"

    if r.status_code in (200, 201, 202):
        return True, "ok"
    _log.warning("Resend HTTP %s: %s", r.status_code, r.text[:300])
    return False, f"Resend devolvió HTTP {r.status_code}: {r.text[:300]}"


# ── Plantillas ───────────────────────────────────────────────────────────────

_BASE_CSS = """
  body { background:#f4f6fa; margin:0; padding:0; font-family:-apple-system,
         Segoe UI, Roboto, Helvetica, Arial, sans-serif; color:#1f2937; }
  .container { max-width:560px; margin:32px auto; background:#ffffff;
               border-radius:12px; padding:32px;
               box-shadow:0 4px 14px rgba(0,0,0,0.06); }
  h1 { font-size:1.35rem; margin:0 0 16px 0; color:#0f172a; }
  p  { font-size:0.95rem; line-height:1.55; margin:8px 0; }
  .btn { display:inline-block; background:#0f172a; color:#ffffff !important;
         text-decoration:none; padding:12px 22px; border-radius:8px;
         font-weight:600; margin:16px 0; }
  .muted { color:#64748b; font-size:0.82rem; }
  .footer { text-align:center; color:#94a3b8; font-size:0.75rem; margin-top:24px; }
  .code { display:inline-block; background:#f1f5f9; border:1px solid #e2e8f0;
          padding:8px 14px; border-radius:6px; font-family:monospace;
          font-size:0.95rem; letter-spacing:0.05em; }
"""

def _render(content_html: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{_BASE_CSS}</style></head>
<body><div class="container">{content_html}
<div class="footer">Occimiano · Dashboard de Operaciones</div>
</div></body></html>"""


def send_invite_email(to_email: str, nombre: str, link: str) -> tuple[bool, str]:
    """Correo de invitación inicial (setea tu primera contraseña)."""
    html = _render(f"""
      <h1>Bienvenido al Dashboard de Operaciones</h1>
      <p>Hola <b>{(nombre or to_email).split()[0]}</b>,</p>
      <p>Tu cuenta en el Dashboard de Occimiano ya está activa. Para empezar,
         necesitas definir tu contraseña personal:</p>
      <p style="text-align:center;">
        <a class="btn" href="{link}">Definir mi contraseña</a>
      </p>
      <p class="muted">Si el botón no funciona, copia este enlace en tu navegador:<br>
        <span class="code">{link}</span></p>
      <p class="muted">El enlace expira en 24 horas. Si no fuiste tú quien
         solicitó el acceso, ignora este correo.</p>
    """)
    text = (f"Bienvenido al Dashboard de Occimiano.\n\n"
            f"Define tu contraseña: {link}\n\n"
            f"El enlace expira en 24 horas.")
    return send_email(to_email, "Define tu contraseña — Dashboard Indicadores Operacionales", html, text)


def send_reset_email(to_email: str, nombre: str, link: str) -> tuple[bool, str]:
    """Correo de recuperación de contraseña."""
    html = _render(f"""
      <h1>Recuperación de contraseña</h1>
      <p>Hola <b>{(nombre or to_email).split()[0]}</b>,</p>
      <p>Recibimos una solicitud para restablecer tu contraseña del Dashboard
         de Occimiano. Haz clic en el botón para crear una nueva:</p>
      <p style="text-align:center;">
        <a class="btn" href="{link}">Restablecer contraseña</a>
      </p>
      <p class="muted">Si el botón no funciona, copia este enlace en tu navegador:<br>
        <span class="code">{link}</span></p>
      <p class="muted">El enlace expira en 15 minutos. Si no solicitaste este
         cambio, puedes ignorar el correo — tu contraseña actual sigue siendo válida.</p>
    """)
    text = (f"Recuperación de contraseña — Dashboard Indicadores Operacionales\n\n"
            f"Restablece tu contraseña: {link}\n\n"
            f"El enlace expira en 15 minutos. Si no fuiste tú, ignora este correo.")
    return send_email(to_email, "Restablece tu contraseña — Dashboard Indicadores Operacionales", html, text)


def send_password_changed_email(to_email: str, nombre: str) -> tuple[bool, str]:
    """Notificación: tu contraseña fue cambiada (alerta de seguridad)."""
    html = _render(f"""
      <h1>Tu contraseña fue cambiada</h1>
      <p>Hola <b>{(nombre or to_email).split()[0]}</b>,</p>
      <p>Te confirmamos que la contraseña de tu cuenta en el Dashboard de
         Occimiano fue actualizada correctamente.</p>
      <p class="muted">Si NO fuiste tú quien hizo este cambio, comunícate con
         el administrador (jgavidia@occimiano.cl) de inmediato.</p>
    """)
    text = ("Tu contraseña del Dashboard Indicadores Operacionales fue actualizada.\n"
            "Si no fuiste tú, contacta a jgavidia@occimiano.cl de inmediato.")
    return send_email(to_email, "Contraseña actualizada — Dashboard Indicadores Operacionales", html, text)

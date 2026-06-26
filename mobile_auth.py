"""
mobile_auth.py — Autenticación por código mágico para la app móvil
==================================================================
Flujo: técnico ingresa correo → recibe código 6 dígitos → ingresa →
sesión persistente (Flask cookie firmada, 30 días).

Filtra datos por (email → técnico → equipo). Admins ven todo.

Requisitos:
  - Tabla Supabase `mobile_auth_codes` (ver SQL al final del archivo).
  - Env vars: SMTP_USER, SMTP_PASS (App Password Gmail), FLASK_SECRET_KEY.
"""

import os
import smtplib
import secrets
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from functools import wraps

import requests
from flask import session, redirect, url_for, request

# ── Mapping email → (full_name, equipo, short_name) ──────────────────────
# Equipos siguen GRUPOS_TERRENO en data.py. Eduardo Toro Ramos ya no es de
# la empresa, no se incluye. Nombres completos vienen del Excel oficial.

USERS = {
    # Equipo Juan Gallardo
    "jgallardo@occimiano.cl":  {"full": "Juan Antonio Gallardo Romero",  "short": "Juan Gallardo",     "team": "Juan Gallardo"},
    "jhein@occimiano.cl":      {"full": "Javier Hein Pacheco",           "short": "Javier Hein",       "team": "Juan Gallardo"},
    "ecarrasco@occimiano.cl":  {"full": "Edison Jhon Carrasco Navarro",  "short": "Edison Carrasco",   "team": "Juan Gallardo"},
    "ivergara@occimiano.cl":   {"full": "Iván Ignacio Vergara Ferrari",  "short": "Ignacio Ferrari",   "team": "Juan Gallardo"},
    # Equipo Luis Pinto
    "lpinto@occimiano.cl":     {"full": "Luis Alberto Pinto Jofre",      "short": "Luis Pinto",        "team": "Luis Pinto"},
    "jtoro@occimiano.cl":      {"full": "Juan Francisco Toro Jimenez",   "short": "Juan Francisco",    "team": "Luis Pinto"},
    "jrodriguez@occimiano.cl": {"full": "Jorge Raúl Rodríguez Fuentes",  "short": "Jorge Rodriguez",   "team": "Luis Pinto"},
    "btoledo@occimiano.cl":    {"full": "Breyans Andres Toledo Quintana","short": "Breyans Toledo",    "team": "Luis Pinto"},
    # Equipo Victor Bahamonde
    "vbahamonde@occimiano.cl": {"full": "Victor Hugo Bahamonde Bustamante","short":"Victor Bahamonde", "team": "Victor Bahamonde"},
    "mflores@occimiano.cl":    {"full": "Martín Ignacio Flores Galaz",   "short": "Martin Flores",     "team": "Victor Bahamonde"},
    # Equipo Carlos Avila Norte
    "cavila@occimiano.cl":     {"full": "Carlos Alberto Avila Palacios", "short": "Carlos Avila",      "team": "Carlos Avila Norte"},
    "eperez@occimiano.cl":     {"full": "Edson José Pérez Henríquez",    "short": "Edson Perez",       "team": "Carlos Avila Norte"},
    "erivera@occimiano.cl":    {"full": "Erwin Maximiliano Rivera Talamilla","short":"Erwin Rivera",   "team": "Carlos Avila Norte"},
    # Equipo Carlos Avila Sur
    "llopez@occimiano.cl":     {"full": "Luis Joel Lopez Isla",          "short": "Luis Lopez",        "team": "Carlos Avila Sur"},
    "gfuller@occimiano.cl":    {"full": "Gastón Eduardo Fuller Quilodrán","short":"Gaston Fuller",     "team": "Carlos Avila Sur"},
}

# Admins ven todos los equipos y todos los técnicos.
ADMINS = {
    "operaciones@occimiano.cl",
    "jgavidia@occimiano.cl",
}

CODE_TTL_MINUTES = 10
SESSION_TTL_DAYS = 30
MAX_ATTEMPTS = 5

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_FROM_NAME = "Indicadores Occimiano"


# ── Helpers ──────────────────────────────────────────────────────────────

def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def is_admin(email: str) -> bool:
    return _norm_email(email) in ADMINS


def get_user_info(email: str) -> dict | None:
    """Devuelve {email, full, short, team, is_admin} o None si no autorizado."""
    e = _norm_email(email)
    if e in ADMINS:
        return {"email": e, "full": "Administrador", "short": "Admin",
                "team": None, "is_admin": True}
    u = USERS.get(e)
    if not u:
        return None
    return {"email": e, **u, "is_admin": False}


def is_authorized(email: str) -> bool:
    return get_user_info(email) is not None


# ── Supabase storage para códigos ────────────────────────────────────────

def _sb_creds() -> tuple[str, str]:
    return os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")


def _save_code(email: str, code: str) -> bool:
    url, key = _sb_creds()
    if not url or not key:
        return False
    exp = (datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)).isoformat()
    payload = {
        "email": _norm_email(email),
        "code": code,
        "expires_at": exp,
        "attempts": 0,
        "used": False,
    }
    r = requests.post(
        f"{url}/rest/v1/mobile_auth_codes",
        headers={
            "apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=payload, timeout=10,
    )
    return r.status_code in (200, 201, 204)


def _read_code(email: str) -> dict | None:
    url, key = _sb_creds()
    if not url or not key:
        return None
    r = requests.get(
        f"{url}/rest/v1/mobile_auth_codes",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params={"email": f"eq.{_norm_email(email)}", "select": "*"},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    rows = r.json()
    return rows[0] if rows else None


def _update_code(email: str, fields: dict) -> bool:
    url, key = _sb_creds()
    if not url or not key:
        return False
    r = requests.patch(
        f"{url}/rest/v1/mobile_auth_codes",
        headers={
            "apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        params={"email": f"eq.{_norm_email(email)}"},
        json=fields, timeout=10,
    )
    return r.status_code in (200, 204)


# ── SMTP ─────────────────────────────────────────────────────────────────

def send_code_email(to_email: str, code: str) -> tuple[bool, str]:
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    if not smtp_user or not smtp_pass:
        return False, "SMTP no configurado (faltan SMTP_USER/SMTP_PASS)."

    body_html = f"""<!DOCTYPE html><html><body style="font-family:-apple-system,sans-serif;background:#f4f4f5;padding:24px;color:#0f172a;">
<div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;padding:32px 28px;border:1px solid #e2e8f0;">
  <h2 style="margin:0 0 8px;color:#0d5e6b;">Indicadores Operacionales</h2>
  <p style="margin:0 0 20px;color:#64748b;font-size:14px;">Tu código de acceso es:</p>
  <div style="background:#f1f5f9;border-radius:8px;padding:20px;text-align:center;letter-spacing:.4em;font-size:32px;font-weight:700;color:#0d5e6b;font-family:monospace;">{code}</div>
  <p style="margin:20px 0 0;color:#94a3b8;font-size:12px;line-height:1.5;">Este código expira en {CODE_TTL_MINUTES} minutos. Si no solicitaste acceso, ignora este correo.</p>
</div>
</body></html>"""

    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"] = f"Código de acceso: {code} — Indicadores Occimiano"
    msg["From"] = f"{SMTP_FROM_NAME} <{smtp_user}>"
    msg["To"] = to_email

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, [to_email], msg.as_string())
        return True, "ok"
    except Exception as e:
        return False, f"Error SMTP: {e}"


# ── API pública ──────────────────────────────────────────────────────────

def request_code(email: str) -> tuple[bool, str]:
    """Genera y envía un código al email. Devuelve (ok, mensaje)."""
    e = _norm_email(email)
    if not is_authorized(e):
        return False, "Este correo no tiene acceso. Contacta a operaciones."
    code = f"{secrets.randbelow(1_000_000):06d}"
    if not _save_code(e, code):
        return False, "Error guardando código. Reintenta."
    ok, msg = send_code_email(e, code)
    if not ok:
        return False, msg
    return True, "Código enviado. Revisa tu correo."


def verify_code(email: str, code: str) -> tuple[bool, str]:
    """Valida el código. Si OK, crea la sesión."""
    e = _norm_email(email)
    c = (code or "").strip()
    if not is_authorized(e):
        return False, "Correo no autorizado."
    rec = _read_code(e)
    if not rec:
        return False, "Solicita un código primero."
    if rec.get("used"):
        return False, "Código ya usado. Solicita uno nuevo."
    if rec.get("attempts", 0) >= MAX_ATTEMPTS:
        return False, "Demasiados intentos. Solicita un código nuevo."
    try:
        exp = datetime.fromisoformat(rec["expires_at"].replace("Z", "+00:00"))
    except Exception:
        return False, "Código inválido."
    if datetime.now(timezone.utc) > exp:
        return False, "Código expirado. Solicita uno nuevo."
    if c != str(rec.get("code", "")):
        _update_code(e, {"attempts": rec.get("attempts", 0) + 1})
        return False, f"Código incorrecto. Intentos restantes: {MAX_ATTEMPTS - rec.get('attempts', 0) - 1}"

    _update_code(e, {"used": True})
    # Crea la sesión
    session.permanent = True
    session["user_email"] = e
    session["logged_at"] = datetime.now(timezone.utc).isoformat()
    return True, "Acceso concedido."


def logout():
    session.clear()


def current_user() -> dict | None:
    e = session.get("user_email")
    if not e:
        return None
    return get_user_info(e)


def requires_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login_page", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


# ── SQL para crear la tabla en Supabase ──────────────────────────────────
SUPABASE_SQL = """
CREATE TABLE IF NOT EXISTS mobile_auth_codes (
    email       TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    attempts    INT  NOT NULL DEFAULT 0,
    used        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

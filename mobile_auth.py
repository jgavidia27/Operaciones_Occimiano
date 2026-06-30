"""
mobile_auth.py — Autenticación por PIN fijo para la app móvil
=============================================================
Flujo: técnico ingresa correo + PIN 4 dígitos → sesión 30 días.
Admin puede gestionar PINs desde /admin/pins.

Filtra datos por (email → técnico → equipo). Admins ven todo.

Requisitos:
  - Tabla Supabase `mobile_user_pins` (ver SQL al final).
  - Env var: FLASK_SECRET_KEY.
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from flask import session, redirect, url_for, request

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

ADMINS = {
    "operaciones@occimiano.cl",
    "jgavidia@occimiano.cl",
    "dhevia@occimiano.cl",
    "jcaceres@occimiano.cl",
    "wsoto@occimiano.cl",
    "mhevia@occimiano.cl",
}

SESSION_TTL_DAYS = 30
MAX_ATTEMPTS = 5


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def is_admin(email: str) -> bool:
    return _norm_email(email) in ADMINS


def get_user_info(email: str) -> dict | None:
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


# ── Supabase helpers ────────────────────────────────────────────────────

def _sb_creds() -> tuple[str, str]:
    return os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")


def _sb_headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def get_pin(email: str) -> str | None:
    url, key = _sb_creds()
    if not url or not key:
        return None
    r = requests.get(
        f"{url}/rest/v1/mobile_user_pins",
        headers=_sb_headers(key),
        params={"email": f"eq.{_norm_email(email)}", "select": "pin"},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    rows = r.json()
    return rows[0]["pin"] if rows else None




def set_pin(email: str, pin: str) -> bool:
    url, key = _sb_creds()
    if not url or not key:
        return False
    r = requests.post(
        f"{url}/rest/v1/mobile_user_pins",
        headers={**_sb_headers(key), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json={"email": _norm_email(email), "pin": pin},
        timeout=10,
    )
    return r.status_code in (200, 201, 204)


def get_all_pins() -> dict:
    url, key = _sb_creds()
    if not url or not key:
        return {}
    r = requests.get(
        f"{url}/rest/v1/mobile_user_pins",
        headers=_sb_headers(key),
        params={"select": "email,pin"},
        timeout=10,
    )
    if r.status_code != 200:
        return {}
    return {row["email"]: row["pin"] for row in r.json()}


def generate_all_pins() -> int:
    count = 0
    existing = get_all_pins()
    all_emails = list(USERS.keys()) + list(ADMINS)
    for email in all_emails:
        if email not in existing:
            pin = f"{secrets.randbelow(10000):04d}"
            if set_pin(email, pin):
                count += 1
    return count


# ── Auth API ────────────────────────────────────────────────────────────

def verify_pin(email: str, pin: str) -> tuple[bool, str]:
    e = _norm_email(email)
    p = (pin or "").strip()
    if not is_authorized(e):
        return False, "Este correo no tiene acceso."
    stored = get_pin(e)
    if not stored:
        return False, "No tienes PIN asignado. Contacta a operaciones."
    if p != stored:
        return False, "PIN incorrecto."
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


def requires_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u or not u.get("is_admin"):
            return redirect(url_for("login_page"))
        return fn(*args, **kwargs)
    return wrapper


SUPABASE_SQL = """
CREATE TABLE IF NOT EXISTS mobile_user_pins (
    email       TEXT PRIMARY KEY,
    pin         TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
GRANT SELECT, INSERT, UPDATE, DELETE ON mobile_user_pins TO anon, service_role, authenticated;
"""

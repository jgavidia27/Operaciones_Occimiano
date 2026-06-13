"""
auth.py — Autenticación para el Dashboard Operacional Occimiano.
================================================================

Cómo agregar usuarios:
  1. Generar hash de la contraseña (ejecutar una sola vez):
       python -c "from auth import hash_password; print(hash_password('la_clave'))"

  2. Copiar el hash y agregarlo a los secrets de Streamlit Cloud:
       [auth_users]
       "nombre@occimiano.cl" = "el_hash_generado"

  3. Para desarrollo local, agregar al archivo .env:
       AUTH_USERS={"nombre@occimiano.cl": "el_hash_generado"}

Solo se permite el dominio @occimiano.cl.
"""

import hashlib
import hmac
import json
import os
import streamlit as st

DOMAIN = "@occimiano.cl"
_SALT  = b"occim_panel_2026_s3cr3t"


# ── Hashing ───────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """
    Genera un hash PBKDF2-SHA256 para almacenar en secrets.
    Llamar desde CLI para crear hashes de contraseñas nuevas:
        python -c "from auth import hash_password; print(hash_password('mi_clave'))"
    """
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        _SALT,
        200_000,
    ).hex()


# ── Lectura de usuarios autorizados ──────────────────────────────────────────

def _load_users() -> dict[str, str]:
    """
    Retorna dict {email_lowercase: password_hash}.
    Fuentes (por orden de prioridad):
      1. st.secrets["auth_users"]  (Streamlit Cloud)
      2. Variable de entorno AUTH_USERS  (JSON, desarrollo local)
    """
    # Fuente 1: st.secrets
    try:
        raw = st.secrets.get("auth_users", {})
        if raw:
            return {k.strip().lower(): v for k, v in dict(raw).items()}
    except Exception:
        pass

    # Fuente 2: variable de entorno (JSON)
    try:
        raw_env = os.getenv("AUTH_USERS", "")
        if raw_env:
            parsed = json.loads(raw_env)
            return {k.strip().lower(): v for k, v in parsed.items()}
    except Exception:
        pass

    return {}


# ── Verificación ─────────────────────────────────────────────────────────────

def _verify(email: str, password: str) -> bool:
    email = email.strip().lower()
    if not email.endswith(DOMAIN):
        return False
    users = _load_users()
    stored = users.get(email)
    if not stored:
        return False
    candidate = hash_password(password)
    return hmac.compare_digest(candidate, stored)


# ── API pública ───────────────────────────────────────────────────────────────

def is_authenticated() -> bool:
    return bool(st.session_state.get("_auth_ok"))


def try_login(email: str, password: str) -> bool:
    """Intenta iniciar sesión. Retorna True si las credenciales son válidas."""
    if _verify(email, password):
        st.session_state["_auth_ok"]    = True
        st.session_state["_auth_email"] = email.strip().lower()
        return True
    return False


def logout() -> None:
    for k in ("_auth_ok", "_auth_email", "_login_failed"):
        st.session_state.pop(k, None)

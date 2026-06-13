"""
auth.py — Autenticación para el Dashboard Operacional Occimiano.
================================================================

Configuración (solo esto se necesita en Streamlit Cloud → Settings → Secrets):

    DASHBOARD_PASSWORD = "la_clave_que_elijas"

Eso es todo. Cualquier correo @occimiano.cl + esa clave puede ingresar.
"""

import streamlit as st
import os

DOMAIN = "@occimiano.cl"


def _get_password() -> str:
    """Lee la contraseña desde st.secrets o variable de entorno."""
    try:
        return str(st.secrets["DASHBOARD_PASSWORD"])
    except Exception:
        pass
    return os.getenv("DASHBOARD_PASSWORD", "")


def is_authenticated() -> bool:
    return bool(st.session_state.get("_auth_ok"))


def try_login(email: str, password: str) -> bool:
    """
    Retorna True si:
      - el correo termina en @occimiano.cl
      - la contraseña coincide con DASHBOARD_PASSWORD en secrets
    """
    email = email.strip().lower()
    if not email.endswith(DOMAIN):
        return False
    master = _get_password()
    if not master:
        return False
    if password.strip() == master.strip():
        st.session_state["_auth_ok"]    = True
        st.session_state["_auth_email"] = email
        return True
    return False


def logout() -> None:
    for k in ("_auth_ok", "_auth_email", "_login_failed"):
        st.session_state.pop(k, None)

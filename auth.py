"""
auth.py — Autenticación con sesión persistente via cookie.

Cookie 'occ_s' guarda "1|email" durante SESSION_HOURS horas.
Al refrescar la página se restaura automáticamente.
Solo se cierra sesión al presionar el botón Salir.

Requiere en Streamlit Cloud Secrets:
    DASHBOARD_PASSWORD = "tu_clave"
"""
import os
import streamlit as st
from datetime import datetime, timedelta

DOMAIN        = "@occimiano.cl"
_COOKIE       = "occ_s"
SESSION_HOURS = 8          # horas de sesión antes de requerir nuevo login


# ── Contraseña ────────────────────────────────────────────────────────────────

def _get_password() -> str:
    try:
        return str(st.secrets["DASHBOARD_PASSWORD"])
    except Exception:
        pass
    return os.getenv("DASHBOARD_PASSWORD", "")


# ── Cookie Manager (singleton por sesión) ────────────────────────────────────

def _cm():
    """Retorna la instancia de CookieManager almacenada en session_state."""
    return st.session_state.get("_occ_cm")


# ── API pública ───────────────────────────────────────────────────────────────

def init_cookie_manager():
    """
    Debe llamarse UNA VEZ al inicio de app.py, antes de cualquier auth check.
    Inicializa el CookieManager y lo guarda en session_state.
    Retorna True cuando las cookies están listas, False si aún se está cargando.
    """
    try:
        import extra_streamlit_components as stx
    except ImportError:
        return True  # Sin librería: sin cookies, continuar normalmente

    if "_occ_cm" not in st.session_state:
        st.session_state["_occ_cm"] = stx.CookieManager(key="_occ_cm")

    # get_all() retorna None en el primer render (componente aún cargando)
    cookies = st.session_state["_occ_cm"].get_all()
    return cookies is not None


def is_authenticated() -> bool:
    """True si hay sesión activa en memoria O cookie válida."""
    if bool(st.session_state.get("_auth_ok")):
        return True
    cm = _cm()
    if cm is None:
        return False
    try:
        val = cm.get(_COOKIE)
        if val and str(val).startswith("1|"):
            email = str(val).split("|", 1)[1]
            st.session_state["_auth_ok"]    = True
            st.session_state["_auth_email"] = email
            return True
    except Exception:
        pass
    return False


def try_login(email: str, password: str) -> bool:
    """Valida credenciales y crea sesión + cookie."""
    email = email.strip().lower()
    if not email.endswith(DOMAIN):
        return False
    master = _get_password()
    if not master:
        return False
    if password.strip() == master.strip():
        st.session_state["_auth_ok"]    = True
        st.session_state["_auth_email"] = email
        cm = _cm()
        if cm:
            try:
                exp = datetime.now() + timedelta(hours=SESSION_HOURS)
                cm.set(_COOKIE, f"1|{email}", expires_at=exp, key="_occ_set")
            except Exception:
                pass
        return True
    return False


def logout() -> None:
    """Elimina sesión en memoria y cookie."""
    cm = _cm()
    if cm:
        try:
            cm.delete(_COOKIE, key="_occ_del")
        except Exception:
            pass
    for k in ("_auth_ok", "_auth_email", "_login_failed"):
        st.session_state.pop(k, None)

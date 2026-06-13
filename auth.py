"""
auth.py — Autenticación con sesión persistente (sin dependencias externas).

Cómo funciona:
- Al hacer login se genera un token aleatorio y se guarda en la URL (?s=TOKEN)
  y en un dict del servidor (cache_resource).
- Al refrescar la página la URL mantiene el token → sesión restaurada.
- Al presionar Salir el token se elimina del servidor → sesión cerrada.
- Expira automáticamente tras SESSION_HOURS horas.

Requiere en Streamlit Cloud Secrets:
    DASHBOARD_PASSWORD = "tu_clave"
"""
import os
import hashlib
import time
import streamlit as st

DOMAIN        = "@occimiano.cl"
SESSION_HOURS = 8
_PARAM        = "s"          # nombre del query param en la URL


# ── Store de sesiones del servidor ───────────────────────────────────────────

@st.cache_resource
def _sessions() -> dict:
    """Dict compartido en memoria: {token: {email, expires}}."""
    return {}


# ── Helpers internos ─────────────────────────────────────────────────────────

def _gen_token() -> str:
    return hashlib.sha256(os.urandom(32)).hexdigest()[:40]


def _get_password() -> str:
    try:
        return str(st.secrets["DASHBOARD_PASSWORD"])
    except Exception:
        pass
    return os.getenv("DASHBOARD_PASSWORD", "")


# ── API pública ───────────────────────────────────────────────────────────────

def init_cookie_manager() -> bool:
    """Compatibilidad con app.py — no hace nada en este enfoque."""
    return True


def is_authenticated() -> bool:
    """True si hay sesión activa en memoria o token válido en la URL."""
    if bool(st.session_state.get("_auth_ok")):
        return True

    token = st.query_params.get(_PARAM, "")
    if not token:
        return False

    session = _sessions().get(token)
    if not session:
        return False
    if session["expires"] < time.time():
        _sessions().pop(token, None)
        return False

    # Restaurar sesión desde token
    st.session_state["_auth_ok"]    = True
    st.session_state["_auth_email"] = session["email"]
    st.session_state["_auth_token"] = token
    return True


def try_login(email: str, password: str) -> bool:
    """Valida credenciales, crea token y lo pone en la URL."""
    email = email.strip().lower()
    if not email.endswith(DOMAIN):
        return False
    master = _get_password()
    if not master:
        return False
    if password.strip() != master.strip():
        return False

    token   = _gen_token()
    expires = time.time() + SESSION_HOURS * 3600
    _sessions()[token] = {"email": email, "expires": expires}

    st.session_state["_auth_ok"]    = True
    st.session_state["_auth_email"] = email
    st.session_state["_auth_token"] = token
    st.query_params[_PARAM]         = token
    return True


def logout() -> None:
    """Elimina el token del servidor y limpia la sesión."""
    token = (st.session_state.get("_auth_token")
             or st.query_params.get(_PARAM, ""))
    if token:
        _sessions().pop(token, None)
    for k in ("_auth_ok", "_auth_email", "_login_failed", "_auth_token"):
        st.session_state.pop(k, None)
    try:
        del st.query_params[_PARAM]
    except Exception:
        pass

"""
auth.py — Autenticación con whitelist Supabase + roles admin/usuario.

Flujo de login:
1. Usuario ingresa email + contraseña maestra
2. Se valida la contraseña (DASHBOARD_PASSWORD en secrets)
3. Se consulta tabla usuarios_dashboard en Supabase:
   - El email debe existir Y tener activo=true
   - Si no está en la tabla → acceso denegado (aunque conozca la clave)
4. Se crea token de sesión en URL + se almacena rol y session_id
5. La sesión se registra en sesiones_dashboard para analítica

Requiere en Streamlit Cloud Secrets:
    DASHBOARD_PASSWORD = "tu_clave"
"""
import os
import hashlib
import time
import uuid
import streamlit as st

SESSION_HOURS = 8
_PARAM        = "s"          # query param en la URL


# ── Store de sesiones del servidor ────────────────────────────────────────────

@st.cache_resource
def _sessions() -> dict:
    """Dict en memoria: {token: {email, expires, rol, nombre, session_id}}."""
    return {}


# ── Helpers internos ──────────────────────────────────────────────────────────

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

    # Restaurar sesión completa desde token
    st.session_state["_auth_ok"]     = True
    st.session_state["_auth_email"]  = session["email"]
    st.session_state["_auth_token"]  = token
    st.session_state["_auth_rol"]    = session.get("rol", "usuario")
    st.session_state["_auth_nombre"] = session.get("nombre", "")
    if "_session_id" not in st.session_state:
        st.session_state["_session_id"] = session.get("session_id", "")
    return True


def try_login(email: str, password: str) -> bool:
    """Valida credenciales + whitelist Supabase y crea token de sesión."""
    email = email.strip().lower()

    # 1. Validar contraseña maestra
    master = _get_password()
    if not master or password.strip() != master.strip():
        return False

    # 2. Validar email contra whitelist Supabase
    try:
        from supabase_client import get_usuario_dashboard, log_session_start
        usuario = get_usuario_dashboard(email)
    except Exception:
        usuario = None

    if usuario is None:
        # Email no registrado o inactivo → acceso denegado
        return False

    # 3. Crear token y sesión
    token      = _gen_token()
    session_id = str(uuid.uuid4())
    expires    = time.time() + SESSION_HOURS * 3600
    rol        = usuario.get("rol", "usuario")
    nombre     = usuario.get("nombre", "")

    _sessions()[token] = {
        "email":      email,
        "expires":    expires,
        "rol":        rol,
        "nombre":     nombre,
        "session_id": session_id,
    }

    st.session_state["_auth_ok"]     = True
    st.session_state["_auth_email"]  = email
    st.session_state["_auth_token"]  = token
    st.session_state["_auth_rol"]    = rol
    st.session_state["_auth_nombre"] = nombre
    st.session_state["_session_id"]  = session_id
    st.query_params[_PARAM]          = token

    # 4. Registrar sesión en Supabase (no-blocking)
    try:
        log_session_start(email, session_id)
    except Exception:
        pass

    return True


def logout() -> None:
    """Elimina el token del servidor y limpia la sesión."""
    token = (st.session_state.get("_auth_token")
             or st.query_params.get(_PARAM, ""))
    if token:
        _sessions().pop(token, None)
    for k in ("_auth_ok", "_auth_email", "_login_failed", "_auth_token",
              "_auth_rol", "_auth_nombre", "_session_id"):
        st.session_state.pop(k, None)
    try:
        del st.query_params[_PARAM]
    except Exception:
        pass

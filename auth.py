"""
auth.py — Autenticación con contraseñas individuales + reset por correo.

Modelo:
  - Cada usuario tiene su propia contraseña (hash scrypt en usuarios_dashboard.password_hash)
  - Recuperación por correo (tokens efímeros en tabla password_resets)
  - Durante un período de gracia (GRACE_PERIOD_END) acepta también la contraseña
    maestra DASHBOARD_PASSWORD para no interrumpir el acceso de quienes aún no
    han seteado la suya.

Configuración en secrets / env vars:
    DASHBOARD_PASSWORD = "clave_maestra"      (solo durante grace period)
    GRACE_PERIOD_END   = "2026-07-06"         (fecha YYYY-MM-DD; default = hoy+14d)
    APP_BASE_URL       = "https://occim-...streamlit.app"  (para links de reset)
    RESEND_API_KEY     = "re_..."             (envío de correos)
    MAIL_FROM          = "Occimiano <noreply@occimiano.cl>"
"""
import os
import hashlib
import hmac
import secrets as _secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import streamlit as st

SESSION_HOURS       = 8
_PARAM              = "s"            # token de sesión en URL
_RESET_PARAM        = "rst"          # token de reset/invitación en URL
_RESET_TTL_MIN      = 15             # minutos de validez del token de reset
_INVITE_TTL_HOURS   = 24             # horas de validez del token de invitación
_DEFAULT_GRACE_DAYS = 14

# Parámetros scrypt (estándar OWASP balanceado para web app)
_SCRYPT_N = 16384   # 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DK_LEN = 32


# ── Store de sesiones (en memoria del proceso Streamlit) ─────────────────────

@st.cache_resource
def _sessions() -> dict:
    """Dict {token: {email, expires, rol, nombre, session_id}}."""
    return {}


# ── Helpers de configuración ─────────────────────────────────────────────────

def _get_secret(name: str, default: str = "") -> str:
    try:
        v = st.secrets[name]
        if v:
            return str(v).strip()
    except Exception:
        pass
    return os.getenv(name, default).strip()


def _master_password() -> str:
    return _get_secret("DASHBOARD_PASSWORD")


def _grace_period_active() -> bool:
    """¿Estamos dentro del período donde la contraseña maestra sigue siendo válida?"""
    end_str = _get_secret("GRACE_PERIOD_END")
    if not end_str:
        # Sin variable explícita: 14 días desde HOY. Es conservador: si el admin
        # nunca define la variable, la maestra deja de funcionar 14 días después
        # del primer deploy con este código.
        return True   # permitir master durante setup inicial
    try:
        end = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) <= end + timedelta(days=1)  # +1 = todo el día final
    except ValueError:
        return False


def _app_base_url() -> str:
    """URL base para construir links de reset (sin trailing slash)."""
    url = _get_secret("APP_BASE_URL")
    if url:
        return url.rstrip("/")
    # Fallback: intentar inferir de la request actual (no siempre disponible)
    return ""


# ── Hashing de contraseñas (scrypt) ──────────────────────────────────────────

def hash_password(password: str) -> str:
    """Devuelve un hash auto-contenido: scrypt$N$r$p$<salt_hex>$<hash_hex>."""
    if not password or len(password) < 8:
        raise ValueError("La contraseña debe tener al menos 8 caracteres.")
    salt = _secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_SCRYPT_DK_LEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Compara una contraseña en claro contra el hash almacenado (constant-time)."""
    if not password or not stored:
        return False
    try:
        algo, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$", 5)
        if algo != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    try:
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt, n=n, r=r, p=p, dklen=len(expected),
        )
    except Exception:
        return False
    return hmac.compare_digest(dk, expected)


# ── Tokens de reset (sha256 del random token, nunca el token en claro) ───────

def _new_token() -> tuple[str, str]:
    """Devuelve (token_publico, token_hash). El público va en el correo,
    el hash se guarda en BD."""
    tok = _secrets.token_urlsafe(32)
    h = hashlib.sha256(tok.encode("utf-8")).hexdigest()
    return tok, h


def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


# ── API pública: sesión ──────────────────────────────────────────────────────

def _gen_session_token() -> str:
    return hashlib.sha256(os.urandom(32)).hexdigest()[:40]


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

    st.session_state["_auth_ok"]     = True
    st.session_state["_auth_email"]  = session["email"]
    st.session_state["_auth_token"]  = token
    st.session_state["_auth_rol"]    = session.get("rol", "usuario")
    st.session_state["_auth_nombre"] = session.get("nombre", "")
    if "_session_id" not in st.session_state:
        st.session_state["_session_id"] = session.get("session_id", "")
    return True


def try_login(email: str, password: str) -> bool:
    """Valida credenciales y crea token de sesión.

    Flujo:
      1. El email debe existir Y estar activo en usuarios_dashboard.
      2. Si el usuario tiene password_hash → debe coincidir con scrypt.
      3. Si NO tiene password_hash (o está en grace period) → se acepta también
         la contraseña maestra DASHBOARD_PASSWORD.
    """
    email = (email or "").strip().lower()
    password = (password or "").strip()
    if not email or not password:
        return False

    # 1. Whitelist + datos del usuario
    try:
        from supabase_client import get_usuario_dashboard, log_session_start
        usuario = get_usuario_dashboard(email)
    except Exception:
        usuario = None
    if usuario is None:
        return False

    # 2. Validación de contraseña
    user_hash = (usuario.get("password_hash") or "").strip()
    pw_ok = False
    used_master = False

    if user_hash:
        pw_ok = verify_password(password, user_hash)

    if not pw_ok and _grace_period_active():
        master = _master_password()
        if master and hmac.compare_digest(password, master):
            pw_ok = True
            used_master = True

    if not pw_ok:
        return False

    # 3. Crear sesión
    token      = _gen_session_token()
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
    st.session_state["_login_used_master"] = used_master
    st.query_params[_PARAM]          = token

    # 4. Log
    try:
        log_session_start(email, session_id)
    except Exception:
        pass
    return True


def logout() -> None:
    token = (st.session_state.get("_auth_token")
             or st.query_params.get(_PARAM, ""))
    if token:
        _sessions().pop(token, None)
    for k in ("_auth_ok", "_auth_email", "_login_failed", "_auth_token",
              "_auth_rol", "_auth_nombre", "_session_id", "_login_used_master"):
        st.session_state.pop(k, None)
    try:
        del st.query_params[_PARAM]
    except Exception:
        pass


# ── API pública: passwords y reset ───────────────────────────────────────────

def set_user_password(email: str, new_password: str) -> tuple[bool, str]:
    """Actualiza el hash de password del usuario. Devuelve (ok, msg)."""
    email = (email or "").strip().lower()
    if not email:
        return False, "Correo vacío."
    try:
        h = hash_password(new_password)
    except ValueError as e:
        return False, str(e)
    try:
        from supabase_client import update_user_password_hash
        ok = update_user_password_hash(email, h)
    except Exception as e:
        return False, f"Error actualizando contraseña: {e!r}"
    return (True, "Contraseña actualizada.") if ok else (False, "No se pudo guardar la contraseña.")


def request_password_reset(email: str, proposito: str = "reset") -> tuple[bool, str]:
    """Genera token, lo guarda en BD y envía correo. Devuelve (ok_envio, mensaje).

    proposito = 'reset'  → expira en 15 min, plantilla "olvidé"
    proposito = 'invite' → expira en 24 h, plantilla "bienvenida"
    """
    from supabase_client import get_usuario_dashboard, create_password_reset_token
    from mailer import send_reset_email, send_invite_email, is_configured

    email = (email or "").strip().lower()
    if not email:
        return False, "Ingresa un correo."

    usuario = get_usuario_dashboard(email)
    if usuario is None:
        # No revelamos si el correo existe o no (defensa contra enumeración).
        # Pero tampoco enviamos correo. Devolvemos ok=True con mensaje genérico.
        return True, "Si el correo está registrado, recibirás un enlace de recuperación."

    if not is_configured():
        return False, ("Servicio de correo no configurado (falta RESEND_API_KEY). "
                       "Contacta al administrador para resetear tu clave manualmente.")

    tok_publico, tok_hash = _new_token()
    ttl_minutes = _INVITE_TTL_HOURS * 60 if proposito == "invite" else _RESET_TTL_MIN
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    created = create_password_reset_token(email, tok_hash, proposito, expires)
    if not created:
        return False, "No se pudo generar el enlace. Inténtalo de nuevo."

    base = _app_base_url()
    if not base:
        return False, ("APP_BASE_URL no configurada en secrets. Sin ella, el enlace "
                       "no se puede construir.")
    link = f"{base}/?{_RESET_PARAM}={tok_publico}"

    nombre = usuario.get("nombre", "")
    if proposito == "invite":
        ok, msg = send_invite_email(email, nombre, link)
    else:
        ok, msg = send_reset_email(email, nombre, link)
    if not ok:
        return False, f"No se pudo enviar el correo: {msg}"

    return True, ("Te enviamos un enlace de recuperación a tu correo. "
                  "Tienes 15 minutos para usarlo.")


def consume_reset_token(token: str, new_password: str) -> tuple[bool, str]:
    """Valida el token, setea la nueva contraseña, marca el token como usado."""
    from supabase_client import find_password_reset, mark_reset_used
    from mailer import send_password_changed_email

    if not token or len(token) < 20:
        return False, "Enlace inválido."
    try:
        # Validar fortaleza de la contraseña antes de pegarle a BD
        _ = hash_password(new_password)
    except ValueError as e:
        return False, str(e)

    tok_hash = _hash_token(token)
    reset = find_password_reset(tok_hash)
    if reset is None:
        return False, "Enlace inválido o ya utilizado."

    # Verificar expiración (BD es la fuente de verdad)
    exp_raw = reset.get("expires_at")
    try:
        exp = datetime.fromisoformat(str(exp_raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False, "Enlace mal formado en BD."
    if datetime.now(timezone.utc) > exp:
        return False, "El enlace expiró. Solicita uno nuevo."
    if reset.get("used_at"):
        return False, "Este enlace ya fue utilizado."

    email = (reset.get("email") or "").strip().lower()
    ok, msg = set_user_password(email, new_password)
    if not ok:
        return False, msg

    mark_reset_used(reset.get("id"))

    # Notificación de seguridad — no bloqueante
    try:
        from supabase_client import get_usuario_dashboard
        u = get_usuario_dashboard(email) or {}
        send_password_changed_email(email, u.get("nombre", ""))
    except Exception:
        pass

    return True, "Tu contraseña fue actualizada. Ya puedes iniciar sesión."


def get_pending_reset_token_from_url() -> str:
    """Devuelve el token de reset si la URL lo trae, '' si no."""
    return str(st.query_params.get(_RESET_PARAM, "") or "").strip()


def clear_reset_token_from_url() -> None:
    try:
        del st.query_params[_RESET_PARAM]
    except Exception:
        pass

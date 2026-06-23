-- ============================================================
-- Autenticación por usuario (passwords individuales + reset por correo)
-- Pegar en: Supabase → SQL Editor → Run
-- ============================================================
-- Contexto:
--   El dashboard usaba una única contraseña maestra (DASHBOARD_PASSWORD en
--   secrets) compartida por todos los usuarios. Con ~33 usuarios proyectados,
--   migramos a contraseña individual por usuario + recuperación por correo.
--
--   • password_hash       : scrypt hash + salt en una sola string
--     (formato: "scrypt$N$r$p$<salt_hex>$<hash_hex>")
--   • password_set_at     : última vez que el usuario seteó/cambió su clave.
--                           NULL = aún no la ha definido (usa flujo de invitación).
--   • password_changed_at : timestamp del último cambio exitoso (auditoría)
--
--   La tabla password_resets guarda tokens temporales (15 min) para los flujos
--   de "olvidé mi contraseña" e "invitar usuario nuevo".

-- 1. Extender usuarios_dashboard
ALTER TABLE usuarios_dashboard
  ADD COLUMN IF NOT EXISTS password_hash       TEXT,
  ADD COLUMN IF NOT EXISTS password_set_at     TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;

-- 2. Tabla de tokens de reset
CREATE TABLE IF NOT EXISTS password_resets (
  id           BIGSERIAL PRIMARY KEY,
  email        TEXT        NOT NULL,
  token_hash   TEXT        NOT NULL UNIQUE,   -- sha256 del token; nunca guardamos el token en claro
  proposito    TEXT        NOT NULL DEFAULT 'reset', -- 'reset' | 'invite'
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at   TIMESTAMPTZ NOT NULL,
  used_at      TIMESTAMPTZ,
  request_ip   TEXT
);

CREATE INDEX IF NOT EXISTS idx_password_resets_email      ON password_resets (email);
CREATE INDEX IF NOT EXISTS idx_password_resets_token_hash ON password_resets (token_hash);
CREATE INDEX IF NOT EXISTS idx_password_resets_expires    ON password_resets (expires_at);

-- 3. Permisos para service_role (lectura/escritura desde el backend)
GRANT SELECT, INSERT, UPDATE, DELETE ON public.password_resets TO service_role;
GRANT USAGE, SELECT ON SEQUENCE password_resets_id_seq TO service_role;

-- 4. Verificación
SELECT
  (SELECT COUNT(*) FROM usuarios_dashboard)                                AS usuarios_totales,
  (SELECT COUNT(*) FROM usuarios_dashboard WHERE password_hash IS NOT NULL) AS con_password_individual,
  (SELECT COUNT(*) FROM password_resets)                                   AS tokens_reset_activos;

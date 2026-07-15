-- ============================================================================
-- Estado persistente de scrapers (cookies, tokens, etc.)
-- ============================================================================
-- Los scripts sync_* guardan aquí las sesiones/cookies que necesitan
-- persistir entre corridas (ej. sesión ctrlit con reCAPTCHA resuelto).

CREATE TABLE IF NOT EXISTS hhee_sync_state (
    script        TEXT PRIMARY KEY,     -- ej. 'sync_ctrlit'
    state_json    JSONB NOT NULL,        -- payload arbitrario (cookies, tokens)
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON hhee_sync_state
  TO anon, authenticated, service_role;

DROP TRIGGER IF EXISTS trg_touch_hhee_sync_state ON hhee_sync_state;
CREATE TRIGGER trg_touch_hhee_sync_state BEFORE UPDATE ON hhee_sync_state
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

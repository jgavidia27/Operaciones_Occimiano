-- ============================================================
-- ALTO: solicitudes_trabajo  +  BAJO: sla_umbrales_horas
-- Pegar en Supabase → SQL Editor → Run
-- ============================================================


-- ══════════════════════════════════════════════════════════════
-- ALTO: SOLICITUDES DE TRABAJO
-- Sync desde /api/work_requests/ de Fracttal
-- Proporciona la fecha_incidente real (T0) para calculo SLA
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS solicitudes_trabajo (
    id_solicitud        TEXT        PRIMARY KEY,   -- id_code de Fracttal
    wo_folio            TEXT        REFERENCES ordenes_trabajo(id_ot) ON DELETE SET NULL,
    fecha_solicitud     TIMESTAMPTZ,               -- date (creacion solicitud)
    fecha_incidente     TIMESTAMPTZ,               -- date_incident = T0 real para SLA
    fecha_solucion      TIMESTAMPTZ,               -- date_solution
    tipo                TEXT,                      -- types_description: LLAMADO DE EMERGENCIA / etc.
    descripcion         TEXT,
    estado              TEXT,                      -- requests_x_status_description
    cliente             TEXT,                      -- accounts_name normalizado
    equipo_eds          TEXT,                      -- items_description (ubicacion)
    solicitado_por      TEXT,                      -- requested_by
    synced_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sol_folio    ON solicitudes_trabajo (wo_folio);
CREATE INDEX IF NOT EXISTS idx_sol_tipo     ON solicitudes_trabajo (tipo);
CREATE INDEX IF NOT EXISTS idx_sol_cliente  ON solicitudes_trabajo (cliente);
CREATE INDEX IF NOT EXISTS idx_sol_fecha    ON solicitudes_trabajo (fecha_incidente);

ALTER TABLE solicitudes_trabajo DISABLE ROW LEVEL SECURITY;
GRANT ALL ON solicitudes_trabajo TO service_role, anon, authenticated;


-- ══════════════════════════════════════════════════════════════
-- BAJO: UMBRALES SLA
-- Reemplaza el dict SLA_HOURS hardcodeado en gdrive.py
-- Permite cambiar los umbrales sin tocar codigo
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS sla_umbrales_horas (
    id          SERIAL  PRIMARY KEY,
    cliente     TEXT    NOT NULL,
    prioridad   TEXT    NOT NULL,
    zona        TEXT    NOT NULL,
    horas       INTEGER NOT NULL,
    UNIQUE (cliente, prioridad, zona)
);

-- Poblar con los valores actuales de SLA_HOURS en gdrive.py
INSERT INTO sla_umbrales_horas (cliente, prioridad, zona, horas) VALUES
    -- COPEC
    ('COPEC', 'P1', 'Santiago',  18),
    ('COPEC', 'P1', 'Regiones',  24),
    ('COPEC', 'P2', 'Santiago',  24),
    ('COPEC', 'P2', 'Regiones',  24),
    ('COPEC', 'P3', 'Santiago',  36),
    ('COPEC', 'P3', 'Regiones',  36),
    ('COPEC', 'P4', 'Santiago',  96),
    ('COPEC', 'P4', 'Regiones',  96),
    -- ESMAX (Aramco)
    ('ESMAX (Aramco)', 'P1', 'Santiago',  18),
    ('ESMAX (Aramco)', 'P1', 'Regiones',  24),
    ('ESMAX (Aramco)', 'P2', 'Santiago',  24),
    ('ESMAX (Aramco)', 'P2', 'Regiones',  24),
    ('ESMAX (Aramco)', 'P3', 'Santiago',  36),
    ('ESMAX (Aramco)', 'P3', 'Regiones',  36),
    ('ESMAX (Aramco)', 'P4', 'Santiago',  96),
    ('ESMAX (Aramco)', 'P4', 'Regiones',  96),
    -- SHELL (Enex)
    ('SHELL (Enex)', 'P1', 'Santiago',  18),
    ('SHELL (Enex)', 'P1', 'Regiones',  24),
    ('SHELL (Enex)', 'P2', 'Santiago',  24),
    ('SHELL (Enex)', 'P2', 'Regiones',  24),
    ('SHELL (Enex)', 'P3', 'Santiago',  36),
    ('SHELL (Enex)', 'P3', 'Regiones',  36),
    ('SHELL (Enex)', 'P4', 'Santiago',  96),
    ('SHELL (Enex)', 'P4', 'Regiones',  96)
ON CONFLICT (cliente, prioridad, zona) DO NOTHING;

ALTER TABLE sla_umbrales_horas DISABLE ROW LEVEL SECURITY;
GRANT ALL ON sla_umbrales_horas TO service_role, anon, authenticated;
GRANT ALL ON SEQUENCE sla_umbrales_horas_id_seq TO service_role, anon, authenticated;


-- ══════════════════════════════════════════════════════════════
-- VERIFICACION
-- ══════════════════════════════════════════════════════════════
SELECT 'solicitudes_trabajo' AS tabla, COUNT(*) AS registros FROM solicitudes_trabajo
UNION ALL
SELECT 'sla_umbrales_horas',           COUNT(*) FROM sla_umbrales_horas;

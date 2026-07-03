-- ============================================================
-- TABLA: programacion_sto
-- Refleja la sección "grid de técnicos" del Excel
-- '2026 UTILIZACIÓN DE TIEMPO.xlsx' en Supabase, para que el
-- dashboard la lea rápido (sin abrir el Excel de Drive cada vez).
--
-- Poblada por sync_programacion_sto.py (cron 2x/día).
-- Fuente: hojas mensuales (MAYO 2026 en adelante).
-- ============================================================

CREATE TABLE IF NOT EXISTS programacion_sto (
    fecha         DATE        NOT NULL,   -- día del cronograma
    tecnico       TEXT        NOT NULL,   -- nombre original del Excel
    actividad     TEXT,                   -- descripción de la celda
    color_excel   TEXT,                   -- hex del color de fondo (#RRGGBB) o NULL
    mes_hoja      TEXT,                   -- 'JULIO 2026' (referencia origen)
    synced_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (fecha, tecnico)
);

CREATE INDEX IF NOT EXISTS idx_prog_sto_fecha   ON programacion_sto (fecha);
CREATE INDEX IF NOT EXISTS idx_prog_sto_tecnico ON programacion_sto (tecnico);
CREATE INDEX IF NOT EXISTS idx_prog_sto_mes     ON programacion_sto (mes_hoja);

ALTER TABLE programacion_sto DISABLE ROW LEVEL SECURITY;
GRANT ALL ON programacion_sto TO service_role, anon, authenticated;

-- Verificación
SELECT 'programacion_sto' AS tabla, COUNT(*) AS registros FROM programacion_sto;

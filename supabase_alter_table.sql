-- ============================================================
-- Ampliar tabla ordenes_trabajo con campos faltantes
-- Pegar en: Supabase → SQL Editor → Run
-- ============================================================

-- 1. Agregar columnas faltantes
ALTER TABLE ordenes_trabajo
  ADD COLUMN IF NOT EXISTS cliente             TEXT,
  ADD COLUMN IF NOT EXISTS estacion            TEXT,
  ADD COLUMN IF NOT EXISTS codigo_eds          TEXT,
  ADD COLUMN IF NOT EXISTS prioridad_calc      TEXT,       -- P1/P2/P3/P4
  ADD COLUMN IF NOT EXISTS causa_raiz          TEXT,
  ADD COLUMN IF NOT EXISTS tipo_falla          TEXT,       -- 01-FNAO / 02-FAO / 04-SIN INFO
  ADD COLUMN IF NOT EXISTS modalidad_atencion  TEXT,       -- ATENDIDO PRESENCIAL / etc.
  ADD COLUMN IF NOT EXISTS nota                TEXT,
  ADD COLUMN IF NOT EXISTS nota_tarea          TEXT,
  ADD COLUMN IF NOT EXISTS tiene_numeral       BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS duracion_real_seg   INTEGER,
  ADD COLUMN IF NOT EXISTS duracion_estim_seg  INTEGER,
  ADD COLUMN IF NOT EXISTS tiene_recursos      BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS completada          BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS estado_tarea        TEXT;      -- DONE / NO_STARTED / IN_PROGRESS

-- 2. Limpiar registros historicos sin datos utiles (cargados por Antigravity, fechas 2020-2024)
DELETE FROM ordenes_trabajo
WHERE fecha_creacion IS NULL
   OR fecha_creacion < '2026-01-01';

-- 3. Indices para las nuevas columnas usadas en filtros frecuentes
CREATE INDEX IF NOT EXISTS idx_ot_cliente       ON ordenes_trabajo (cliente);
CREATE INDEX IF NOT EXISTS idx_ot_tipo          ON ordenes_trabajo (tipo_tarea);
CREATE INDEX IF NOT EXISTS idx_ot_prioridad     ON ordenes_trabajo (prioridad_calc);
CREATE INDEX IF NOT EXISTS idx_ot_eds           ON ordenes_trabajo (codigo_eds);
CREATE INDEX IF NOT EXISTS idx_ot_responsable   ON ordenes_trabajo (responsable);
CREATE INDEX IF NOT EXISTS idx_ot_codigo_activo ON ordenes_trabajo (codigo_activo);

-- 4. Verificar resultado
SELECT
  COUNT(*)                                          AS total_registros,
  COUNT(*) FILTER (WHERE tipo_tarea IS NOT NULL)    AS con_tipo_tarea,
  COUNT(*) FILTER (WHERE responsable IS NOT NULL)   AS con_responsable,
  MIN(fecha_creacion)::date                         AS desde,
  MAX(fecha_creacion)::date                         AS hasta
FROM ordenes_trabajo;

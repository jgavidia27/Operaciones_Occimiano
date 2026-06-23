-- ============================================================
-- Mantenciones Preventivas — columnas adicionales
-- Pegar en: Supabase → SQL Editor → Run
-- ============================================================
-- Agrega los 4 campos que faltaban para la pantalla Mantenciones
-- Preventivas y el cálculo de Uptime:
--
--   • paro_equipo            (bool)  ← Fracttal: stop_assets
--   • tiempo_paro_estim_seg  (int)   ← Fracttal: stop_assets_sec
--   • tiempo_paro_real_seg   (int)   ← Fracttal: real_stop_assets_sec
--   • plan_tareas            (text)  ← Fracttal: groups_tasks_description
--                                       (ej: "PLAN MTTO MSELF GENERAL")

ALTER TABLE ordenes_trabajo
  ADD COLUMN IF NOT EXISTS paro_equipo            BOOLEAN,
  ADD COLUMN IF NOT EXISTS tiempo_paro_estim_seg  INTEGER,
  ADD COLUMN IF NOT EXISTS tiempo_paro_real_seg   INTEGER,
  ADD COLUMN IF NOT EXISTS plan_tareas            TEXT;

-- Índice para filtrar OTs con paro de equipo (cálculo de Uptime)
CREATE INDEX IF NOT EXISTS idx_ordenes_paro_equipo
  ON ordenes_trabajo (paro_equipo)
  WHERE paro_equipo = true;

-- Índice para filtrar por plan
CREATE INDEX IF NOT EXISTS idx_ordenes_plan_tareas
  ON ordenes_trabajo (plan_tareas);

-- Verificación
SELECT
  COUNT(*)                                                                 AS total_ots,
  COUNT(*) FILTER (WHERE paro_equipo IS NOT NULL)                          AS con_dato_paro,
  COUNT(*) FILTER (WHERE plan_tareas IS NOT NULL)                          AS con_plan
FROM ordenes_trabajo;

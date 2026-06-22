-- ============================================================
-- Tiempo estimado y real "neto" (excluyendo subtareas adheridas)
-- Pegar en: Supabase → SQL Editor → Run
-- ============================================================
-- Contexto:
--   En Fracttal una OT preventiva de LAVADORA suele incluir 4 subtareas:
--   lavadora + lavatapiz + bomba + ablandador. La bomba y el ablandador
--   forman parte físicamente del sistema de la lavadora (mantención
--   integrada), pero Fracttal los suma como ~10 min cada uno al estimado
--   total, inflando artificialmente el umbral del 75%.
--
--   Estos dos nuevos campos guardan el tiempo "neto" SIN bomba/ablandador
--   cuando la OT incluye una lavadora. Para OTs sin lavadora, copian los
--   valores originales (duracion_estim_seg / duracion_real_seg).
--
--   Se poblan con sync_estim_neta.py (consulta /api/work_orders/ y detecta
--   subtareas BOMBA / ABLANDADOR cuando la OT tiene una LAVADORA).

-- 1. Agregar columnas
ALTER TABLE ordenes_trabajo
  ADD COLUMN IF NOT EXISTS duracion_estim_neta_seg INTEGER,
  ADD COLUMN IF NOT EXISTS duracion_real_neta_seg  INTEGER;

-- 2. Verificar
SELECT
  COUNT(*)                                                        AS total,
  COUNT(*) FILTER (WHERE duracion_estim_neta_seg IS NOT NULL)     AS con_estim_neta,
  COUNT(*) FILTER (WHERE duracion_estim_neta_seg
                         <> duracion_estim_seg)                   AS ajustadas
FROM ordenes_trabajo;

-- ============================================================
-- Migración: lts_hr_produccion_final
-- Nueva columna en numerales_subtarea para capturar el campo
-- "LT/HRS PRODUCCIÓN FINAL" del formulario Shell (plantilla
-- LAVADORA MSELF, distinta de la plantilla LAVADORA 6 PROGRAMAS
-- que solo tiene TIPO DE BOMBA DOSIFICADORA).
--
-- Se registra en modo texto (los valores vienen así de Fracttal:
-- "125", "3.5", "3.00lph", etc — sin normalizar).
-- ============================================================

ALTER TABLE numerales_subtarea
  ADD COLUMN IF NOT EXISTS lts_hr_produccion_final TEXT,
  ADD COLUMN IF NOT EXISTS form_tiene_produccion   BOOLEAN NOT NULL DEFAULT FALSE;

-- Verificación
SELECT
  COUNT(*) FILTER (WHERE form_tiene_produccion) AS con_form_produccion,
  COUNT(*) FILTER (WHERE lts_hr_produccion_final IS NOT NULL) AS con_valor,
  COUNT(*) AS total
FROM numerales_subtarea;

-- ============================================================
-- Migración: form_tiene_bomba / form_tiene_consumo / form_tiene_tiempo
-- Objetivo: distinguir "plantilla Fracttal vieja (sin el campo)"
--           de "técnico no llenó el campo".
--
-- Contexto: los 3 campos bomba_dosificadora, consumo_insumos y
-- tiempo_fichas_seg empezaron a existir en el formulario Shell el
-- 2026-06-08. Sin embargo, ~95% de las OTs post-08-jun siguen
-- generándose con la plantilla vieja sin esos campos, por lo que
-- salen vacías aunque el técnico haya trabajado la OT.
--
-- Las 3 nuevas columnas marcan si el FORMULARIO incluía cada campo
-- (True) o si la plantilla usada era vieja (False), independiente
-- de si el técnico llenó o no el valor.
-- ============================================================

ALTER TABLE numerales_subtarea
  ADD COLUMN IF NOT EXISTS form_tiene_bomba   BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS form_tiene_consumo BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS form_tiene_tiempo  BOOLEAN NOT NULL DEFAULT FALSE;

-- Verificación
SELECT
  COUNT(*) FILTER (WHERE form_tiene_bomba)   AS con_form_bomba,
  COUNT(*) FILTER (WHERE form_tiene_consumo) AS con_form_consumo,
  COUNT(*) FILTER (WHERE form_tiene_tiempo)  AS con_form_tiempo,
  COUNT(*)                                    AS total
FROM numerales_subtarea;

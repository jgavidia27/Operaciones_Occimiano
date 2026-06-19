-- ============================================================
-- Numerales reales por OT (inicial / final) desde subtareas Fracttal
-- Pegar en: Supabase → SQL Editor → Run
-- ============================================================
-- Contexto:
--   El numeral (lectura del contador de fichas en lavadoras/aspiradoras)
--   se registra como ítem de formulario en la tarea de la OT:
--     - "TOMA DE NUMERAL INICIAL"  → id_task_form_item_type = 3
--     - "TOMA DE NUMERAL FINAL"    → id_task_form_item_type = 5
--   Antes se adivinaba con regex sobre la nota (frágil). Ahora se extrae
--   el valor REAL vía /api/work_orders_subtasks/ y se persiste aquí.

-- 1. Agregar columnas (TEXT: el valor puede traer ruido; se parsea al leer)
ALTER TABLE ordenes_trabajo
  ADD COLUMN IF NOT EXISTS numeral_inicial TEXT,
  ADD COLUMN IF NOT EXISTS numeral_final   TEXT;

-- 2. Verificar
SELECT
  COUNT(*)                                              AS total,
  COUNT(*) FILTER (WHERE numeral_inicial IS NOT NULL)   AS con_inicial,
  COUNT(*) FILTER (WHERE numeral_final   IS NOT NULL)   AS con_final
FROM ordenes_trabajo;

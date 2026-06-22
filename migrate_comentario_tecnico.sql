-- ============================================================
-- Comentario / conclusión del técnico por OT (texto libre del PDF)
-- Pegar en: Supabase → SQL Editor → Run
-- ============================================================
-- Contexto:
--   El formulario de la tarea en Fracttal incluye campos de TEXTO LIBRE
--   (id_task_form_item_type = 1) donde el técnico documenta lo que pasó:
--     - "DESCRIPCIÓN DE LA FALLA ENCONTRADA"
--     - "TRABAJO REALIZADO (PARA SOLUCIONAR PROBLEMA ENCONTRADO)"
--     - "OBSERVACIONES"
--   El campo estructurado `causa_raiz` suele quedar en "SIN CLASIFICAR";
--   este texto libre es la causa raíz REAL y permite rastrear quién deja
--   datos basura (p.ej. un numeral mal tecleado) y por qué.
--   Se extrae vía /api/work_orders_subtasks/ (mismo origen que el numeral)
--   y se persiste aquí con sync_numerales.py.

-- 1. Agregar columnas
--    comentario_tecnico  → texto libre del técnico (falla / trabajo / observaciones)
--    form_tiene_numeral  → si el formulario de la OT incluía el campo "TOMA DE NUMERAL".
--      Permite penalizar en correctivas el numeral vacío SOLO cuando el form lo pedía
--      (si no tenía el campo, dejarlo vacío es justificable, no es error del técnico).
ALTER TABLE ordenes_trabajo
  ADD COLUMN IF NOT EXISTS comentario_tecnico TEXT,
  ADD COLUMN IF NOT EXISTS form_tiene_numeral BOOLEAN;

-- 2. Verificar
SELECT
  COUNT(*)                                                AS total,
  COUNT(*) FILTER (WHERE comentario_tecnico IS NOT NULL
                     AND comentario_tecnico <> '')        AS con_comentario,
  COUNT(*) FILTER (WHERE form_tiene_numeral IS TRUE)      AS form_con_numeral
FROM ordenes_trabajo;

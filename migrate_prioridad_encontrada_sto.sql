-- ============================================================
-- PRIORIDAD ENCONTRADA STO — por subtarea de OT correctiva
-- Pegar en: Supabase → SQL Editor → Run
-- ============================================================
-- Contexto:
--   El formulario de las OT CORRECTIVAS en Fracttal incluye una lista
--   desplegable "PRIORIDAD ENCONTRADA STO" (id_task_form_item_type = 7)
--   donde el técnico registra la prioridad REAL que dedujo en terreno:
--     - "P1 Detenida"
--     - "P2 Funciona a medias / Falta un programa"
--     - "P3 Opera con defectos menores"
--     - "P4 Planificable, no urgente"
--   Permite contrastar la prioridad que declaró el cliente vs la que el STO
--   encontró (ej. "me pediste 50 P1, pero solo 25 eran realmente P1").
--   Se extrae vía /api/work_orders_subtasks/ y se persiste aquí con
--   sync_numerales_subtarea.py. Se muestra en el dashboard:
--   Cumplimiento SLA → "Cumplimiento de SLA por OT", última columna
--   "P encontrada STO" (reemplaza a "Motivo excepción").

ALTER TABLE numerales_subtarea
    ADD COLUMN IF NOT EXISTS prioridad_encontrada_sto text;

COMMENT ON COLUMN numerales_subtarea.prioridad_encontrada_sto IS
    'Prioridad real que el técnico dedujo en terreno (P1..P4) en OT '
    'correctivas — lista "PRIORIDAD ENCONTRADA STO" del formulario Fracttal. '
    'NULL si el plan no incluía la pregunta o el técnico no la respondió.';

-- Después de correr esto: correr el backfill
--   python sync_numerales_subtarea.py --desde 2026-07-01

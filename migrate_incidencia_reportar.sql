-- ============================================================
-- INCIDENCIA A REPORTAR — por subtarea de mantención preventiva
-- Pegar en: Supabase → SQL Editor → Run
-- ============================================================
-- Contexto:
--   El formulario de cada mantención preventiva en Fracttal incluye al
--   final una lista desplegable "INCIDENCIA A REPORTAR"
--   (id_task_form_item_type = 7) donde el técnico reporta un problema
--   percibido en la máquina, por ejemplo:
--     - "Sin sal en Ablandador"
--     - "Filtro - tambor de aspirado sucio y/o tapado"
--     - "Estanque sucio"
--     - "Sin incidencias que reportar"  (opción por defecto = sin problema)
--   Se extrae vía /api/work_orders_subtasks/ (mismo origen que el numeral y
--   los campos FLOWEY) y se persiste aquí con sync_numerales_subtarea.py.
--   Se muestra en el dashboard: Efectividad MP → "Mantenciones Realizadas —
--   desglose por OT", columna "Incidencia reportada" (junto a "Plan").

-- 1. Agregar la columna (idempotente)
ALTER TABLE numerales_subtarea
    ADD COLUMN IF NOT EXISTS incidencia_reportar text;

-- 2. Comentario descriptivo (opcional, documenta el origen del dato)
COMMENT ON COLUMN numerales_subtarea.incidencia_reportar IS
    'Respuesta del técnico a la lista "INCIDENCIA A REPORTAR" del formulario '
    'de mantención preventiva (Fracttal). NULL si el plan no incluía la '
    'pregunta o el técnico no la respondió.';

-- Después de correr esto:
--   • El sync diario (sync_numerales_subtarea.py, GitHub Action) empezará a
--     poblar la columna en cada nueva mantención.
--   • Para poblar el histórico: correr una vez
--       python sync_numerales_subtarea.py --modo completo --desde 2026-01-01
--     (o dejar que el incremental la vaya llenando con las OTs recientes).

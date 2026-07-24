-- Nueva columna: qué subtareas específicas quedaron sin completar en la OT.
-- Se muestra en la columna "Resolución" del panel de Validación En Revisión
-- para que el operador sepa exactamente qué falta y pueda pedirle al técnico
-- que la termine (ej. "Cambio de aceite" pendiente).
-- Formato: lista separada por "; " con "TAREA (activo)".
ALTER TABLE ots_en_revision
    ADD COLUMN IF NOT EXISTS subtareas_pendientes text;

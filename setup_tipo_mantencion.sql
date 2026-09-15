-- Tipo y duración de la mantención preventiva de cada EDS.
--
-- Por qué va en estaciones_servicio y no en una tabla aparte: es un atributo
-- de la estación (qué equipos tiene instalados), no un dato de planificación.
-- Vive donde vive el resto del maestro para que se mantenga junto con él.
--
-- El sync de estaciones (sync_estaciones_from_ots.py) solo INSERTA EDS nuevas,
-- nunca actualiza las existentes, así que estas columnas no se pisan solas.
--
-- Fuente de la semilla: el plan de mantenimiento preventivo de operaciones
-- ("calculo mtto prev.xlsx"). Fracttal NO tiene este dato: su duración
-- estimada es un valor por defecto (mediana 2,67 h para todo) y su plan de
-- tareas no permite deducir el tipo.
--
-- Ejecutar en Supabase → SQL Editor.

ALTER TABLE estaciones_servicio
    ADD COLUMN IF NOT EXISTS tipo_mantencion  TEXT,
    ADD COLUMN IF NOT EXISTS duracion_mp_min  INTEGER;

COMMENT ON COLUMN estaciones_servicio.tipo_mantencion IS
    'SIMPLE | C/HIDROPACK | C/TERMO — determina cuánto dura la visita.';
COMMENT ON COLUMN estaciones_servicio.duracion_mp_min IS
    'Minutos de trabajo de la MP en esta estación, sin traslado. Si la estación '
    'tiene un segundo equipo de lavado (sufijo B en el maestro), es la SUMA de '
    'ambos: es una sola parada con dos máquinas.';

CREATE INDEX IF NOT EXISTS idx_eds_tipo_mantencion
    ON estaciones_servicio (tipo_mantencion);

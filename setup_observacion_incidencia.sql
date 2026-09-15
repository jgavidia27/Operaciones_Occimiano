-- Observación de la condición sub estándar.
--
-- Campo "OBSERVACIÓN DE LA INCIDENCIA (DETALLE)" que operaciones agregó al
-- formulario de las MP en sep-2026. La condición sola no explica qué pasa:
-- quedaba registrado "Estanque sucio" sin decir por qué ni en qué grado. Este
-- campo es la explicación del técnico en sus palabras.
--
-- Texto libre, por eso 500 caracteres y no una lista corta.
--
-- Las MP anteriores a sep-2026 quedan en NULL: la pregunta no existía. En la
-- vista se muestran como "—", y eso no es un error de sincronización.
--
-- Ejecutar en Supabase → SQL Editor.

ALTER TABLE numerales_subtarea
    ADD COLUMN IF NOT EXISTS observacion_incidencia TEXT;

COMMENT ON COLUMN numerales_subtarea.observacion_incidencia IS
    'Explicación del técnico sobre la condición sub estándar reportada '
    '("OBSERVACIÓN DE LA INCIDENCIA (DETALLE)" en Fracttal, desde sep-2026).';

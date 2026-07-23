-- Agrega la columna cubre_fichero a numerales_subtarea.
-- Campo del formulario Shell "¿EL EQUIPO POSEE CUBRE FICHERO?" (SI/NO).
-- NULL = la plantilla de esa OT no incluía el campo (plantilla antigua).
ALTER TABLE numerales_subtarea
    ADD COLUMN IF NOT EXISTS cubre_fichero text;

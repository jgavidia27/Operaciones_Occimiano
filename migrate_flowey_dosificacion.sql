-- migrate_flowey_dosificacion.sql
-- ================================================================
-- Preguntas FLOWEY de dosificacion (Aramco, plantilla 2026-08-03).
-- Reemplazan a la vieja "¿LOS PRODUCTOS FLOWEY ESTAN DILUIDOS CON AGUA?"
-- (mal formulada — todos los productos FLOWEY se diluyen). Lo correcto
-- es medir si la configuracion porcentual de cada bomba es la correcta:
--   • Subtarea 64: ¿CON SHAMPOO DOSIFICADO AL 20%, SALE EL PRODUCTO CORRECTAMENTE?
--   • Subtarea 65: ¿CON CERA DOSIFICADA AL 10%,  SALE EL PRODUCTO CORRECTAMENTE?
-- Valor: SI / NO (desde 'true' / 'false' en Fracttal).
-- ================================================================

ALTER TABLE numerales_subtarea
    ADD COLUMN IF NOT EXISTS flowey_shampoo_20 TEXT,
    ADD COLUMN IF NOT EXISTS flowey_cera_10    TEXT;

COMMENT ON COLUMN numerales_subtarea.flowey_shampoo_20 IS
    '¿Con shampoo dosificado al 20%, sale el producto correctamente? SI/NO (Aramco)';
COMMENT ON COLUMN numerales_subtarea.flowey_cera_10 IS
    '¿Con cera dosificada al 10%, sale el producto correctamente? SI/NO (Aramco)';

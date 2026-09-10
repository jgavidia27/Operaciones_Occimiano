-- Tabla para descontar órdenes de Enlace del ranking de "órdenes no cerradas"
-- cuando el técnico avisó que la orden no le cerraba (y se cerró manualmente).
-- Ejecutar en Supabase → SQL Editor.

CREATE TABLE IF NOT EXISTS enlace_ordenes_avisadas (
    numero_orden TEXT PRIMARY KEY,          -- N° orden de Enlace (12 dígitos)
    tecnico      TEXT,                       -- técnico al que se le descuenta
    marcado_por  TEXT,                       -- quién lo marcó (ej. 'panel')
    nota         TEXT,                       -- nota opcional
    fecha        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON enlace_ordenes_avisadas
    TO anon, service_role, authenticated;

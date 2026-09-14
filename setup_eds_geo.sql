-- Coordenadas de las EDS (para el motor de calendarización y rutas de MP).
-- Se guardan en tabla aparte para que el sync de estaciones_servicio no las pise.
-- Ejecutar en Supabase → SQL Editor.

CREATE TABLE IF NOT EXISTS eds_geo (
    eds_occim    TEXT PRIMARY KEY,        -- código EDS (ej. 60242, SH_323)
    loc_fracttal TEXT,                    -- ubicación Fracttal (ej. LOC-154)
    latitud      DOUBLE PRECISION,
    longitud     DOUBLE PRECISION,
    comuna       TEXT,
    fuente       TEXT,                    -- 'loc_fracttal' | 'match_nombre'
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eds_geo_comuna ON eds_geo (comuna);

GRANT SELECT, INSERT, UPDATE, DELETE ON eds_geo
    TO anon, service_role, authenticated;

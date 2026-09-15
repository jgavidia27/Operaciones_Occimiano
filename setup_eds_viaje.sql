-- Tiempos y distancias REALES de viaje entre estaciones, por calle.
--
-- Por qué una tabla y no una consulta en vivo: las estaciones no se mueven, así
-- que la matriz es estática. Se calcula una vez con sync_matriz_viaje.py y el
-- dashboard nunca depende de un servicio externo para dibujar ni para planificar.
--
-- Por qué hace falta: la distancia en línea recta se equivoca por un factor de
-- entre 1,3 y 5,2 — y al no ser constante, tampoco sirve para comparar rutas
-- entre sí. Medido sobre rutas reales de octubre en Santiago: una jornada de
-- 1,7 km en línea recta son 8,8 km de calle.
--
-- Solo se guardan los pares que pueden aparecer en una misma jornada (por
-- defecto hasta 120 min). Guardar Arica-Punta Arenas no aporta y multiplica
-- la tabla.
--
-- Ejecutar en Supabase → SQL Editor.

CREATE TABLE IF NOT EXISTS eds_viaje (
    eds_origen   TEXT NOT NULL,
    eds_destino  TEXT NOT NULL,
    minutos      REAL NOT NULL,
    metros       REAL,
    fuente       TEXT,                    -- 'osrm' | 'google' | ...
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (eds_origen, eds_destino)
);

CREATE INDEX IF NOT EXISTS idx_eds_viaje_origen ON eds_viaje (eds_origen);

COMMENT ON TABLE eds_viaje IS
    'Matriz de viaje por calle entre EDS. Estática: las estaciones no se mueven. '
    'Se recalcula solo cuando cambian las coordenadas de eds_geo.';
COMMENT ON COLUMN eds_viaje.minutos IS
    'Tiempo de manejo sin tráfico (OSRM usa velocidades libres). Sirve para '
    'comparar y ordenar rutas; NO es una promesa de hora de llegada.';

GRANT SELECT, INSERT, UPDATE, DELETE ON eds_viaje
    TO anon, service_role, authenticated;

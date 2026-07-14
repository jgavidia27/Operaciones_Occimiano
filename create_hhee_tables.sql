-- ============================================================================
-- Módulo HHEE — Validación de Horas Extra
-- ============================================================================
-- Objetivo: cruzar 3 fuentes (Buk, GPS Rastreosat, Fracttal) para determinar
-- si una hora extra declarada por un técnico corresponde o no.
--
-- Uso:
--   psql -h <host> -U postgres -d postgres -f create_hhee_tables.sql
--   o pegarlo en el SQL Editor de Supabase.
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. TÉCNICOS (nómina, patente vehículo, domicilio)
-- ─────────────────────────────────────────────────────────────────────────────
-- Unifica lo que estaba planeado como tecnicos_vehiculos + tecnicos_domicilios.
-- Alimentada por sync_buk_rrhh.py (nómina + dirección) y geocodificador
-- Nominatim (lat/lng). El campo `patente` se pobla manualmente con el mapa STO.

CREATE TABLE IF NOT EXISTS tecnicos_hhee (
    rut                 TEXT PRIMARY KEY,       -- ej. "18.211.653-K"
    nombre_completo     TEXT NOT NULL,
    email               TEXT,
    equipo              TEXT,                   -- Juan Gallardo / Luis Pinto / Victor Bahamonde / Carlos Avila Norte / Carlos Avila Sur
    patente             TEXT,                   -- ej. "PSSX84" — NULL si es admin/no técnico
    tipo_vehiculo       TEXT DEFAULT 'staff',   -- staff / auxiliar / null
    domicilio_direccion TEXT,                   -- "Argomedo 350, Santiago, Chile"
    domicilio_comuna    TEXT,
    domicilio_region    TEXT,
    domicilio_lat       NUMERIC(10, 7),         -- geocodificado por Nominatim
    domicilio_lng       NUMERIC(10, 7),
    activo              BOOLEAN DEFAULT TRUE,
    excluir_hhee        BOOLEAN DEFAULT FALSE,  -- true = no evaluar HHEE (admins, gerencia)
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tec_hhee_patente ON tecnicos_hhee(patente) WHERE patente IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tec_hhee_equipo  ON tecnicos_hhee(equipo);
CREATE INDEX IF NOT EXISTS idx_tec_hhee_email   ON tecnicos_hhee(email);


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. MARCACIONES BUK (entrada / salida de asistencia)
-- ─────────────────────────────────────────────────────────────────────────────
-- Alimentada por sync_ctrlit.py (scraping ctrlit.cl con Playwright).
-- Una fila por evento de marcación.

CREATE TABLE IF NOT EXISTS buk_marcaciones (
    id             BIGSERIAL PRIMARY KEY,
    rut            TEXT NOT NULL,
    fecha          DATE NOT NULL,
    tipo           TEXT NOT NULL CHECK (tipo IN ('entrada', 'salida')),
    hora           TIMESTAMPTZ NOT NULL,       -- timestamp exacto de la marcación
    lat            NUMERIC(10, 7),             -- opcional (si Buk registra ubicación)
    lng            NUMERIC(10, 7),
    fuente         TEXT DEFAULT 'ctrlit_scrape',
    raw_data       JSONB,                       -- fila original del CSV para debug
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(rut, fecha, tipo, hora)
);

CREATE INDEX IF NOT EXISTS idx_buk_marc_rut_fecha ON buk_marcaciones(rut, fecha);
CREATE INDEX IF NOT EXISTS idx_buk_marc_fecha     ON buk_marcaciones(fecha);


-- ─────────────────────────────────────────────────────────────────────────────
-- 3. HORAS EXTRAS DECLARADAS EN BUK
-- ─────────────────────────────────────────────────────────────────────────────
-- Horas extra que ya están registradas/aprobadas en Buk (punto de partida
-- de la validación: es lo que se pagaría si no hay observación).

CREATE TABLE IF NOT EXISTS buk_horas_extras (
    id                  BIGSERIAL PRIMARY KEY,
    rut                 TEXT NOT NULL,
    fecha               DATE NOT NULL,
    horas_declaradas    NUMERIC(5, 2) DEFAULT 0,   -- ej. 1.50
    aprobadas_por       TEXT,                       -- nombre/email supervisor
    estado              TEXT DEFAULT 'pendiente',   -- pendiente/aprobada/rechazada
    raw_data            JSONB,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(rut, fecha)
);

CREATE INDEX IF NOT EXISTS idx_buk_he_rut_fecha ON buk_horas_extras(rut, fecha);


-- ─────────────────────────────────────────────────────────────────────────────
-- 4. RECORRIDOS GPS (Rastreosat)
-- ─────────────────────────────────────────────────────────────────────────────
-- Eventos de posición del vehículo. Alimentada por sync_rastreosat.py.
-- Guardamos EVENTOS clave (motor on/off, paradas largas) — no cada punto
-- para no saturar.

CREATE TABLE IF NOT EXISTS gps_eventos (
    id              BIGSERIAL PRIMARY KEY,
    patente         TEXT NOT NULL,
    fecha           DATE NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL,
    lat             NUMERIC(10, 7) NOT NULL,
    lng             NUMERIC(10, 7) NOT NULL,
    velocidad_kmh   NUMERIC(5, 1),
    evento          TEXT NOT NULL,               -- motor_on / motor_off / parada / posicion
    direccion       TEXT,                        -- opcional: reverse geocode
    duracion_min    INTEGER,                     -- para paradas: cuánto duró
    raw_data        JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(patente, timestamp, evento)
);

CREATE INDEX IF NOT EXISTS idx_gps_patente_fecha ON gps_eventos(patente, fecha);
CREATE INDEX IF NOT EXISTS idx_gps_timestamp     ON gps_eventos(timestamp);


-- ─────────────────────────────────────────────────────────────────────────────
-- 5. VEREDICTOS HHEE (resultado del motor de reglas)
-- ─────────────────────────────────────────────────────────────────────────────
-- Una fila por técnico × día evaluado. Es la tabla que consume el dashboard.

CREATE TABLE IF NOT EXISTS hhee_veredictos (
    id                        BIGSERIAL PRIMARY KEY,
    rut                       TEXT NOT NULL,
    fecha                     DATE NOT NULL,
    -- Datos de la última OT del día (Fracttal)
    ultima_ot_folio           TEXT,
    ultima_ot_fin             TIMESTAMPTZ,
    ultima_ot_lat             NUMERIC(10, 7),
    ultima_ot_lng             NUMERIC(10, 7),
    ultima_ot_eds             TEXT,               -- código EDS / dirección
    -- Datos GPS (llegada a casa detectada)
    llegada_casa_estimada     TIMESTAMPTZ,        -- primera parada larga en domicilio
    ultima_pos_gps            TIMESTAMPTZ,        -- último ping del día
    -- Datos Buk (marcación)
    marca_entrada             TIMESTAMPTZ,
    marca_salida              TIMESTAMPTZ,
    -- Cálculos
    tramo_esperado_min        INTEGER,            -- tiempo OT_final → domicilio (Google/OSRM)
    tramo_real_min            INTEGER,            -- tiempo real observado por GPS
    hhee_declaradas_min       INTEGER,            -- de buk_horas_extras
    hhee_validadas_min        INTEGER,            -- las que sí corresponden
    -- Veredicto
    veredicto                 TEXT CHECK (veredicto IN ('valida', 'dudosa', 'no_corresponde', 'sin_datos')),
    razon                     TEXT,               -- explicación textual
    evidencia_json            JSONB,              -- payload completo para auditoría
    calculado_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(rut, fecha)
);

CREATE INDEX IF NOT EXISTS idx_hhee_rut_fecha  ON hhee_veredictos(rut, fecha);
CREATE INDEX IF NOT EXISTS idx_hhee_fecha      ON hhee_veredictos(fecha);
CREATE INDEX IF NOT EXISTS idx_hhee_veredicto  ON hhee_veredictos(veredicto);


-- ─────────────────────────────────────────────────────────────────────────────
-- 6. LOGS DE SYNCS (para debugging)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hhee_sync_logs (
    id             BIGSERIAL PRIMARY KEY,
    script         TEXT NOT NULL,               -- sync_buk_rrhh / sync_ctrlit / sync_rastreosat / he_evaluator
    inicio         TIMESTAMPTZ DEFAULT NOW(),
    fin            TIMESTAMPTZ,
    estado         TEXT DEFAULT 'running',      -- running / success / error
    filas_upserted INTEGER,
    mensaje        TEXT,
    error_traceback TEXT
);

CREATE INDEX IF NOT EXISTS idx_hhee_logs_script ON hhee_sync_logs(script, inicio DESC);


-- ─────────────────────────────────────────────────────────────────────────────
-- Permisos (Supabase suele necesitar esto para PostgREST)
-- ─────────────────────────────────────────────────────────────────────────────
GRANT SELECT, INSERT, UPDATE, DELETE ON tecnicos_hhee     TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON buk_marcaciones   TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON buk_horas_extras  TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON gps_eventos       TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON hhee_veredictos   TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON hhee_sync_logs    TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON SEQUENCE buk_marcaciones_id_seq  TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON SEQUENCE buk_horas_extras_id_seq TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON SEQUENCE gps_eventos_id_seq      TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON SEQUENCE hhee_veredictos_id_seq  TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON SEQUENCE hhee_sync_logs_id_seq   TO anon, authenticated, service_role;


-- ─────────────────────────────────────────────────────────────────────────────
-- Trigger para updated_at en tecnicos_hhee
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_touch_tec_hhee ON tecnicos_hhee;
CREATE TRIGGER trg_touch_tec_hhee BEFORE UPDATE ON tecnicos_hhee
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

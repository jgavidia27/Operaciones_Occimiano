-- ============================================================
-- TABLAS ADICIONALES SUPABASE — Occimiano Dashboard
-- Pegar en: Supabase → SQL Editor → Run
-- ============================================================


-- ══════════════════════════════════════════════════════════════
-- 1. EQUIPOS — estructura de equipos (editable desde Supabase)
--    Cuando cambien los equipos en 3 meses, solo se edita aquí
--    y el dashboard lee la nueva estructura automáticamente.
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS equipos (
    id              SERIAL       PRIMARY KEY,
    nombre_equipo   TEXT         NOT NULL UNIQUE,  -- clave usada en el dashboard
    senior          TEXT         NOT NULL,          -- jefe del equipo
    zona            TEXT,                           -- RM / Norte / Sur / etc.
    descripcion     TEXT,
    activo          BOOLEAN      DEFAULT TRUE,
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  DEFAULT NOW()
);

-- Equipos actuales (junio 2026)
INSERT INTO equipos (nombre_equipo, senior, zona, descripcion) VALUES
  ('Luis Pinto',        'Luis Pinto',     'RM',    'Equipo Región Metropolitana — Luis Pinto'),
  ('Victor Bahamonde',  'Victor Bahamonde','RM',   'Equipo Región Metropolitana — Victor Bahamonde'),
  ('Juan Gallardo',     'Juan Gallardo',  'RM',    'Equipo Región Metropolitana — Juan Gallardo'),
  ('Carlos Avila Norte','Carlos Avila',   'Norte', 'Equipo Norte — Coquimbo, La Serena y zonas norte'),
  ('Carlos Avila Sur',  'Carlos Avila',   'Sur',   'Equipo Sur — Concepción y zonas sur')
ON CONFLICT (nombre_equipo) DO NOTHING;


-- ══════════════════════════════════════════════════════════════
-- 2. TECNICOS — catálogo de técnicos con su equipo asignado
--    Para cambiar de equipo: UPDATE tecnicos SET equipo_id = X WHERE nombre_corto = 'Y'
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS tecnicos (
    id               SERIAL   PRIMARY KEY,
    nombre_corto     TEXT     NOT NULL UNIQUE,  -- como aparece en Excel de clientes
    nombre_completo  TEXT     NOT NULL,          -- nombre exacto en Fracttal
    equipo_id        INTEGER  REFERENCES equipos(id) ON UPDATE CASCADE ON DELETE SET NULL,
    activo           BOOLEAN  DEFAULT TRUE,
    aplica_bono      BOOLEAN  DEFAULT TRUE,      -- FALSE = AUTEC, externos, etc.
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Poblar técnicos (estructura actual junio 2026)
-- Primero obtenemos los IDs de equipos
WITH eq AS (SELECT id, nombre_equipo FROM equipos)
INSERT INTO tecnicos (nombre_corto, nombre_completo, equipo_id, aplica_bono)
SELECT nombre_corto, nombre_completo, eq.id, aplica_bono FROM (
  VALUES
    -- ── Equipo Luis Pinto ──────────────────────────────────────────────────
    ('Luis Pinto',      'Luis Alberto Pinto Jofre',          'Luis Pinto',        TRUE),
    ('Juan Francisco',  'Juan Francisco Toro Jimenez',       'Luis Pinto',        TRUE),
    ('Jorge Rodriguez', 'Jorge Raul Rodriguez Fuentes',      'Luis Pinto',        TRUE),
    ('Breyans Toledo',  'Breyans Andres Toledo Quintana',    'Luis Pinto',        TRUE),
    -- ── Equipo Victor Bahamonde ────────────────────────────────────────────
    ('Victor Bahamonde','Victor Hugo Bahamonde Bustamante',  'Victor Bahamonde',  TRUE),
    ('Martin Flores',   'Martin Ignacio Flores Galaz',       'Victor Bahamonde',  TRUE),
    ('Eduardo Toro',    'Eduardo Toro Ramos',                'Victor Bahamonde',  TRUE),
    -- ── Equipo Juan Gallardo ───────────────────────────────────────────────
    ('Juan Gallardo',   'Juan Antonio Gallardo Romero',      'Juan Gallardo',     TRUE),
    ('Javier Hein',     'Javier Hein Pacheco',               'Juan Gallardo',     TRUE),
    ('Edison Carrasco', 'Edison Jhon Carrasco Navarro',      'Juan Gallardo',     TRUE),
    ('Ignacio Ferrari', 'Ivan Ignacio Vergara Ferrari',      'Juan Gallardo',     TRUE),
    -- ── Equipo Carlos Avila Norte ──────────────────────────────────────────
    ('Carlos Avila',    'Carlos Alberto Avila Palacios',     'Carlos Avila Norte',TRUE),
    ('Edson Perez',     'Edson Jose Perez Henriquez',        'Carlos Avila Norte',TRUE),
    ('Erwin Rivera',    'Erwin Maximiliano Rivera Talamilla','Carlos Avila Norte',TRUE),
    -- ── Equipo Carlos Avila Sur ────────────────────────────────────────────
    ('Luis Lopez',      'Luis Joel Lopez Isla',              'Carlos Avila Sur',  TRUE),
    ('Gaston Fuller',   'Gaston Eduardo Fuller Quilodran',   'Carlos Avila Sur',  TRUE),
    -- ── No aplican bono (subcontratistas / externos) ───────────────────────
    ('AUTEC',           'AUTEC IQUIQUE',                     NULL,                FALSE),
    ('AUTEC LTDA',      'AUTEC LTDA',                        NULL,                FALSE),
    ('Jaime Ocampo',    'Jaime Humberto Ocampo Romero',      NULL,                FALSE),
    ('Juan Valle',      'Juan Pablo Valle Guerrero',         NULL,                FALSE),
    ('Walter Soto',     'Walter Mauricio Soto Curilen',      NULL,                FALSE),
    ('Ana Guzman',      'Ana Maria Guzman Doddis',           NULL,                FALSE)
) AS t(nombre_corto, nombre_completo, nombre_equipo, aplica_bono)
JOIN eq ON eq.nombre_equipo = t.nombre_equipo
ON CONFLICT (nombre_corto) DO NOTHING;


-- ══════════════════════════════════════════════════════════════
-- 3. ESTACIONES DE SERVICIO — reemplaza el Excel de EDS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS estaciones_servicio (
    eds_occim       TEXT        PRIMARY KEY,   -- código Occimiano (PBR-14, SH_41…)
    cliente         TEXT,                       -- COPEC / ESMAX (Aramco) / SHELL (Enex)
    nombre          TEXT,                       -- nombre de la estación
    direccion       TEXT,
    comuna          TEXT,
    region          TEXT,
    zona            TEXT,                       -- Norte / Sur / RM / etc.
    activa          BOOLEAN     DEFAULT TRUE,
    loc_fracttal    TEXT,                       -- LOC-XXX código Fracttal
    barcode_cliente TEXT,                       -- código que usa el cliente
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Índices
CREATE INDEX IF NOT EXISTS idx_eds_cliente ON estaciones_servicio (cliente);
CREATE INDEX IF NOT EXISTS idx_eds_zona    ON estaciones_servicio (zona);
CREATE INDEX IF NOT EXISTS idx_eds_activa  ON estaciones_servicio (activa);


-- ══════════════════════════════════════════════════════════════
-- 4. VISTAS FILTRADAS DE ÓRDENES DE TRABAJO
--    En Supabase aparecen como "tablas" separadas — fácil de
--    consultar como si fueran hojas distintas.
-- ══════════════════════════════════════════════════════════════

-- Vista: solo correctivas
CREATE OR REPLACE VIEW v_correctivas AS
SELECT * FROM ordenes_trabajo
WHERE tipo_tarea = 'CORRECTIVA'
ORDER BY fecha_creacion DESC;

-- Vista: solo preventivas (todas las variantes)
CREATE OR REPLACE VIEW v_preventivas AS
SELECT * FROM ordenes_trabajo
WHERE tipo_tarea LIKE '%PREVENTIVA%'
ORDER BY fecha_creacion DESC;

-- Vista: órdenes con técnico y equipo enriquecidos desde tabla tecnicos
CREATE OR REPLACE VIEW v_ordenes_enriquecidas AS
SELECT
    ot.*,
    t.nombre_corto      AS tecnico_corto,
    t.aplica_bono,
    e.nombre_equipo     AS equipo,
    e.senior            AS equipo_senior,
    e.zona              AS equipo_zona
FROM ordenes_trabajo ot
LEFT JOIN tecnicos  t ON t.nombre_completo = ot.responsable
LEFT JOIN equipos   e ON e.id = t.equipo_id;

-- Vista: resumen mensual por tipo de OT (para gráficos del dashboard)
CREATE OR REPLACE VIEW v_resumen_mensual AS
SELECT
    DATE_TRUNC('month', fecha_creacion) AS mes,
    cliente,
    tipo_tarea,
    COUNT(*)                            AS total_ots,
    COUNT(*) FILTER (WHERE completada)  AS completadas,
    COUNT(*) FILTER (WHERE tipo_tarea = 'CORRECTIVA' AND prioridad_calc = 'P1') AS p1
FROM ordenes_trabajo
WHERE fecha_creacion IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 1 DESC, 2, 3;


-- ══════════════════════════════════════════════════════════════
-- 5. VERIFICACION FINAL
-- ══════════════════════════════════════════════════════════════
SELECT 'equipos'              AS tabla, COUNT(*) AS registros FROM equipos
UNION ALL
SELECT 'tecnicos',             COUNT(*) FROM tecnicos
UNION ALL
SELECT 'tecnicos con bono',    COUNT(*) FROM tecnicos WHERE aplica_bono = TRUE
UNION ALL
SELECT 'tecnicos sin equipo',  COUNT(*) FROM tecnicos WHERE equipo_id IS NULL
UNION ALL
SELECT 'estaciones_servicio',  COUNT(*) FROM estaciones_servicio
UNION ALL
SELECT 'v_correctivas',        COUNT(*) FROM v_correctivas
UNION ALL
SELECT 'v_preventivas',        COUNT(*) FROM v_preventivas;

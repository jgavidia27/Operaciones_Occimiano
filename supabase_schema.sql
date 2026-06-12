-- ============================================================
-- SCHEMA: ordenes_trabajo
-- Ejecutar en Supabase → SQL Editor antes del primer sync
-- ============================================================

CREATE TABLE IF NOT EXISTS ordenes_trabajo (
    -- Identificadores
    id_tarea          BIGINT        PRIMARY KEY,   -- id_work_orders_tasks (único por tarea)
    wo_folio          TEXT          NOT NULL,       -- OS-XXXXX (agrupa tareas de una misma OT)

    -- Fechas
    fecha_creacion    TIMESTAMPTZ,
    fecha_inicio      TIMESTAMPTZ,
    fecha_fin         TIMESTAMPTZ,
    fecha_cierre_admin TIMESTAMPTZ,
    fecha_incidente   TIMESTAMPTZ,                 -- event_date → T₀ para SLA

    -- Clasificación
    tipo_tarea_raw    TEXT,
    tipo_tarea        TEXT,                        -- PREVENTIVA / CORRECTIVA / INSPECCION / OTRA
    prioridad_raw     TEXT,
    prioridad         TEXT,                        -- P1 / P2 / P3 / P4

    -- Jerarquía
    cliente           TEXT,                        -- COPEC / ESMAX (Aramco) / SHELL (Enex)
    estacion          TEXT,
    jerarquia_raw     TEXT,
    codigo_eds        TEXT,                        -- PBR-14, SH_41…

    -- Equipo / Activo
    codigo_activo     TEXT,                        -- código equipo (para reincidencias)
    nombre_activo     TEXT,

    -- Técnico
    responsable       TEXT,

    -- Estado
    estado_tarea      TEXT,                        -- DONE / NO_STARTED / IN_PROGRESS
    id_estado         INTEGER,
    completada        BOOLEAN DEFAULT FALSE,

    -- KPI Precisión — Tiempo
    duracion_real_seg  INTEGER,                    -- tasks_duration
    duracion_estim_seg INTEGER,                    -- duration (estimado)

    -- KPI Precisión — Causa raíz
    causa_raiz        TEXT,
    tipo_falla        TEXT,                        -- 01.- FNAO / 02.- FAO / 04.- SIN INFO

    -- KPI Precisión — Modalidad de atención
    modalidad_atencion TEXT,                       -- ATENDIDO PRESENCIAL / VÍA REMOTA / …

    -- KPI Precisión — Numeral
    nota              TEXT,
    nota_tarea        TEXT,
    tiene_numeral     BOOLEAN DEFAULT FALSE,       -- detectado por regex \d{4,}

    -- Recursos / Costos
    tiene_recursos    BOOLEAN DEFAULT FALSE,
    parada_minutos    NUMERIC(10,2),
    costo_total       NUMERIC(12,2),
    calificacion      INTEGER,

    -- Metadata sync
    synced_at         TIMESTAMPTZ DEFAULT NOW()
);

-- Índices para consultas frecuentes del dashboard
CREATE INDEX IF NOT EXISTS idx_ot_folio      ON ordenes_trabajo (wo_folio);
CREATE INDEX IF NOT EXISTS idx_ot_fecha      ON ordenes_trabajo (fecha_creacion);
CREATE INDEX IF NOT EXISTS idx_ot_cliente    ON ordenes_trabajo (cliente);
CREATE INDEX IF NOT EXISTS idx_ot_tipo       ON ordenes_trabajo (tipo_tarea);
CREATE INDEX IF NOT EXISTS idx_ot_tecnico    ON ordenes_trabajo (responsable);
CREATE INDEX IF NOT EXISTS idx_ot_equipo     ON ordenes_trabajo (codigo_activo);
CREATE INDEX IF NOT EXISTS idx_ot_eds        ON ordenes_trabajo (codigo_eds);

-- Comentario en tabla
COMMENT ON TABLE ordenes_trabajo IS
    'OTs sincronizadas desde Fracttal One API. Una fila por TAREA (no por OT).
     Agrupar por wo_folio para análisis a nivel de OT.
     Sync automático cada 20 min via script sync_fracttal_supabase.py';

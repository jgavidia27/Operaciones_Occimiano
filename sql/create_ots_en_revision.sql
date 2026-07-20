-- =====================================================================
-- Tabla: ots_en_revision
-- Guarda las OTs que estan En Revision (task_status=DONE + done=True
-- + wo_final_date=NULL) esperando validacion administrativa.
--
-- Se recarga completa cada 30 min desde sync_ots_revision.py.
-- Fuente: Fracttal API /api/work_orders.
--
-- Semaforo pre-calculado en el sync para que la UI solo lea.
-- =====================================================================

CREATE TABLE IF NOT EXISTS ots_en_revision (
    -- Identificacion
    folio               TEXT PRIMARY KEY,           -- OS-38469
    id_wo               BIGINT,                     -- id_work_order interno Fracttal
    tipo                TEXT,                       -- CORRECTIVA / PREVENTIVA M / etc.
    activo              TEXT,                       -- items_log_description
    codigo_activo       TEXT,                       -- EQ-3542
    parent_desc         TEXT,                       -- ruta padre "// COPEC/ EDS/"
    cliente             TEXT,                       -- derivado de parent_desc (primer segmento)
    eds_occim           TEXT,                       -- codigo EDS Occimiano si aplica

    -- Personas
    personnel           TEXT,                       -- tecnico principal
    created_by          TEXT,                       -- quien creo la OT

    -- Fechas (todas UTC)
    creation_date       TIMESTAMPTZ,                -- fecha emision
    event_date          TIMESTAMPTZ,                -- fecha incidente
    initial_date        TIMESTAMPTZ,                -- inicio tecnico
    final_date          TIMESTAMPTZ,                -- fin tecnico
    review_date         TIMESTAMPTZ,                -- paso a En Revision
    dias_en_revision    INTEGER,                    -- calculado: dias desde review_date

    -- Completitud y recursos (Fracttal)
    completed_pct       INTEGER,                    -- 0-100
    tiene_recurso_inv   BOOLEAN,                    -- repuestos
    tiene_recurso_hh    BOOLEAN,                    -- mano obra
    tiene_recurso_hours BOOLEAN,                    -- horas
    tiene_recurso_serv  BOOLEAN,                    -- servicios
    total_cost          NUMERIC,                    -- total_cost_task
    resources_serv_desc TEXT,                       -- texto libre de servicios

    -- Falla (para correctivas)
    tipo_falla          TEXT,                       -- types_description
    causa_raiz          TEXT,                       -- causes_description
    metodo_deteccion    TEXT,                       -- detection_method_description

    -- Notas
    note                TEXT,
    task_note           TEXT,

    -- Semaforo pre-calculado
    color_semaforo      TEXT NOT NULL,              -- VERDE / AMARILLO / ROJO
    motivo_semaforo     TEXT,                       -- "Sin recursos", "Completitud=40%", etc.

    -- Meta
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indices utiles
CREATE INDEX IF NOT EXISTS idx_ots_rev_color   ON ots_en_revision(color_semaforo);
CREATE INDEX IF NOT EXISTS idx_ots_rev_personnel ON ots_en_revision(personnel);
CREATE INDEX IF NOT EXISTS idx_ots_rev_dias    ON ots_en_revision(dias_en_revision DESC);
CREATE INDEX IF NOT EXISTS idx_ots_rev_review  ON ots_en_revision(review_date DESC);

-- Comentarios
COMMENT ON TABLE ots_en_revision IS
    'OTs de Fracttal en estado En Revision (esperando validacion). Actualizada cada 30 min por sync_ots_revision.py.';
COMMENT ON COLUMN ots_en_revision.color_semaforo IS
    'VERDE=lista para cerrar | AMARILLO=cerrable con revision (correctivo sin falla) | ROJO=devolver al tecnico';


-- =====================================================================
-- Tabla: ots_cierres_auditoria
-- Historial de cada intento de cierre automatico (Playwright).
-- Cada corrida del bot registra 1 fila por OT: exito o fallo con motivo.
-- =====================================================================

CREATE TABLE IF NOT EXISTS ots_cierres_auditoria (
    id              BIGSERIAL PRIMARY KEY,
    folio           TEXT NOT NULL,               -- OS-38469
    intento_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ejecutado_por   TEXT,                        -- email del usuario que dispara
    resultado       TEXT NOT NULL,               -- OK / FAIL
    motivo          TEXT,                        -- si FAIL: mensaje de error
    duracion_ms     INTEGER,                     -- tiempo del cierre
    batch_id        TEXT                         -- agrupa cierres en tanda
);

CREATE INDEX IF NOT EXISTS idx_cierres_folio    ON ots_cierres_auditoria(folio);
CREATE INDEX IF NOT EXISTS idx_cierres_at       ON ots_cierres_auditoria(intento_at DESC);
CREATE INDEX IF NOT EXISTS idx_cierres_batch    ON ots_cierres_auditoria(batch_id);
CREATE INDEX IF NOT EXISTS idx_cierres_result   ON ots_cierres_auditoria(resultado);

COMMENT ON TABLE ots_cierres_auditoria IS
    'Auditoria de cada intento de cierre automatico via cierre_ots_playwright.py';

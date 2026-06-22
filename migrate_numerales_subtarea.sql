-- ============================================================
-- Numerales por subtarea (1 fila por OT × activo con campo numeral)
-- Pegar en: Supabase → SQL Editor → Run
-- ============================================================
-- Contexto:
--   Una OT preventiva en Fracttal puede tener varios activos en subtareas
--   (ej. OS-37930: Bomba + Ablandador + Aspiradora + Lavadora). Algunos
--   activos tienen campo NUMERAL en el formulario (lavadora, aspiradora,
--   lavainterior), otros no (bomba, ablandador).
--   La tabla `ordenes_trabajo` persiste solo 1 fila por OT, así que pierde
--   el desglose por activo. Esta tabla nueva guarda explícitamente 1 fila
--   por (OT × activo con numeral) usando id_work_order_task del formulario
--   Fracttal para asociar correctamente cada par INICIAL/FINAL a su activo.
--
--   Esto permite:
--     - Ver en el dashboard 1 fila por (OT, activo) con su numeral propio
--     - El KPI considera la OT "OK en numeral" SOLO si TODAS sus subtareas
--       con numeral están OK
--     - Atribuir el error al activo específico (lavadora vs aspiradora)
--
--   Lo pobla sync_numerales_subtarea.py.

CREATE TABLE IF NOT EXISTS numerales_subtarea (
  id                       BIGSERIAL PRIMARY KEY,
  id_ot                    TEXT      NOT NULL,
  id_work_order_task       BIGINT,
  codigo_activo            TEXT,
  nombre_activo            TEXT,
  tipo_activo              TEXT,        -- 'lavadora' | 'aspiradora' | 'lavainterior'
  numeral_inicial          TEXT,
  numeral_final            TEXT,
  fichas_periodo           INTEGER,
  numeral_ok               BOOLEAN,
  motivo                   TEXT,        -- 'ok' | 'basura' | 'negativo' | 'salto_magnitud' | 'exceso_fichas' | 'sin_numeral'
  updated_at               TIMESTAMPTZ DEFAULT now(),
  UNIQUE (id_ot, id_work_order_task)
);

CREATE INDEX IF NOT EXISTS idx_numerales_subtarea_id_ot
  ON numerales_subtarea (id_ot);

-- Permisos para el service_role (necesarios para insertar desde el sync)
GRANT SELECT, INSERT, UPDATE, DELETE ON public.numerales_subtarea TO service_role;
GRANT USAGE, SELECT ON SEQUENCE numerales_subtarea_id_seq TO service_role;

-- Verificación
SELECT
  COUNT(*)                                    AS total_filas,
  COUNT(DISTINCT id_ot)                       AS ots_con_subtareas,
  COUNT(*) FILTER (WHERE numeral_ok IS TRUE)  AS subtareas_ok,
  COUNT(*) FILTER (WHERE numeral_ok IS FALSE) AS subtareas_con_error
FROM numerales_subtarea;

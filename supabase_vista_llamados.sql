-- ============================================================
-- VISTA: v_llamados_sla  (version 4 - 2026-06-09)
-- Fixes:
--   v3: DISTINCT ON, zona_sla, sla_excepciones
--   v4: T0 con 3 niveles de precision:
--       1. sol.fecha_incidente  = date_incident del work_request (mejor)
--       2. ot.fecha_incidente   = event_date de la OT (admin lleno Fecha Incidente en Fracttal)
--       3. ot.fecha_creacion    = fallback si nada anterior existe
-- ============================================================

DROP VIEW IF EXISTS v_llamados_sla;

CREATE VIEW v_llamados_sla AS
SELECT DISTINCT ON (ot.id_ot)

    -- Identificadores
    ot.id_ot                                                     AS os_fracttal,
    sol.id_solicitud::text                                       AS n_llamado,

    -- Cliente y estacion
    ot.cliente,
    ot.codigo_eds                                                AS eds_occim,
    COALESCE(eds.nombre, ot.estacion)                            AS eds_nombre,
    COALESCE(eds.comuna, '')                                     AS comuna,
    COALESCE(eds.region, '')                                     AS region,

    -- T0 con 3 niveles:
    -- 1. fecha_incidente de la solicitud (date_incident del work_request)
    -- 2. fecha_incidente de la OT (event_date llenado por el admin en Fracttal)
    -- 3. fecha_creacion de la OT (fallback)
    COALESCE(sol.fecha_incidente, ot.fecha_incidente, ot.fecha_creacion)       AS fecha_llamado,
    COALESCE(sol.fecha_incidente, ot.fecha_incidente, ot.fecha_creacion)::TIME AS hora_llamado,
    ot.fecha_finalizacion                                        AS fecha_atencion,
    ot.fecha_finalizacion::TIME                                  AS hora_fin,

    -- Tecnico y equipo
    ot.responsable                                               AS tecnico,
    tec.nombre_corto                                             AS tecnico_corto,
    eq.nombre_equipo                                             AS equipo,
    eq.senior                                                    AS equipo_senior,

    -- Prioridad
    ot.prioridad_calc                                            AS prioridad,

    -- Zona SLA (columna explicita en estaciones_servicio, default Regiones)
    COALESCE(eds.zona_sla, 'Regiones')                          AS zona,

    -- Tiempo de respuesta real (horas desde T0 hasta finalizacion)
    CASE
        WHEN ot.fecha_finalizacion IS NOT NULL
         AND COALESCE(sol.fecha_incidente, ot.fecha_incidente, ot.fecha_creacion) IS NOT NULL
        THEN ROUND(
            EXTRACT(EPOCH FROM (
                ot.fecha_finalizacion
                - COALESCE(sol.fecha_incidente, ot.fecha_incidente, ot.fecha_creacion)
            )) / 3600.0, 2
        )
        ELSE NULL
    END                                                          AS tiempo_resp_horas,

    -- Umbral SLA contractual (horas)
    sla.horas                                                    AS tiempo_resp_esp,

    -- Cumplimiento SLA
    CASE
        WHEN exc.os_fracttal IS NOT NULL
            THEN 'CUMPLE'
        WHEN ot.fecha_finalizacion IS NULL
            THEN 'PENDIENTE'
        WHEN sla.horas IS NULL
            THEN 'SIN UMBRAL'
        WHEN ROUND(
                EXTRACT(EPOCH FROM (
                    ot.fecha_finalizacion
                    - COALESCE(sol.fecha_incidente, ot.fecha_incidente, ot.fecha_creacion)
                )) / 3600.0, 2
             ) <= sla.horas
            THEN 'CUMPLE'
        ELSE 'NO CUMPLE'
    END                                                          AS cumplimiento,

    exc.motivo                                                   AS excepcion_motivo,

    -- Estado y datos adicionales
    ot.estado                                                    AS estado_atencion,
    ot.cliente                                                   AS facturacion,
    ot.tipo_tarea,
    ot.codigo_activo,
    ot.nombre_activo,
    ot.fecha_creacion

FROM ordenes_trabajo ot

-- T0 desde solicitud vinculada (date_incident de Fracttal)
LEFT JOIN solicitudes_trabajo sol
    ON sol.wo_folio = ot.id_ot

-- Datos de la estacion de servicio
LEFT JOIN estaciones_servicio eds
    ON eds.eds_occim = ot.codigo_eds

-- Datos del tecnico
LEFT JOIN tecnicos tec
    ON tec.nombre_completo = ot.responsable

-- Datos del equipo del tecnico
LEFT JOIN equipos eq
    ON eq.id = tec.equipo_id

-- Umbral SLA contractual segun cliente + prioridad + zona
LEFT JOIN sla_umbrales_horas sla
    ON sla.cliente   = ot.cliente
    AND sla.prioridad = ot.prioridad_calc
    AND sla.zona     = COALESCE(eds.zona_sla, 'Regiones')

-- Excepciones manuales aprobadas (marcar como CUMPLE)
LEFT JOIN sla_excepciones exc
    ON exc.os_fracttal = ot.id_ot

WHERE ot.tipo_tarea = 'CORRECTIVA'
  AND ot.cliente IN ('COPEC', 'ESMAX (Aramco)', 'SHELL (Enex)')

-- DISTINCT ON requiere ORDER BY que incluya la columna del DISTINCT
-- Si hay multiples solicitudes para una OT, elegir la mas antigua (T0 real)
ORDER BY
    ot.id_ot,
    COALESCE(sol.fecha_incidente, ot.fecha_incidente, ot.fecha_creacion) ASC NULLS LAST;


-- Permisos
GRANT SELECT ON v_llamados_sla TO service_role, anon, authenticated;

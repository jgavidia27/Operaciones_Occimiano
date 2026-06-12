-- ============================================================
-- FIX RETROACTIVO: Prioridades COPEC junio 2026
-- Fuente: "Llamados correctivos Copec 2024 V2.0" validado por usuario
-- Fecha ejecución: 2026-06-10
-- ============================================================
-- PASO 1: Corregir prioridad → P1 (11 registros)
UPDATE llamados_correctivos
SET prioridad = 'P1'
WHERE cliente = 'COPEC'
  AND os_fracttal IN (
    'OS-37752','OS-37856','OS-37882','OS-37886','OS-37887',
    'OS-37893','OS-37906','OS-37920','OS-37924','OS-37993','OS-37999'
  )
  AND prioridad IS DISTINCT FROM 'P1';

-- PASO 2: Corregir prioridad → P2 (31 registros)
UPDATE llamados_correctivos
SET prioridad = 'P2'
WHERE cliente = 'COPEC'
  AND os_fracttal IN (
    'OS-37741','OS-37768','OS-37777','OS-37802','OS-37803',
    'OS-37804','OS-37826','OS-37842','OS-37854','OS-37855',
    'OS-37861','OS-37863','OS-37878','OS-37884','OS-37888',
    'OS-37892','OS-37898','OS-37904','OS-37907','OS-37909',
    'OS-37910','OS-37917','OS-37925','OS-37929','OS-37960',
    'OS-37966','OS-37968','OS-37969','OS-37984','OS-37991','OS-37996'
  )
  AND prioridad IS DISTINCT FROM 'P2';

-- PASO 3: Confirmar P3 (14 registros – algunos ya estaban bien)
UPDATE llamados_correctivos
SET prioridad = 'P3'
WHERE cliente = 'COPEC'
  AND os_fracttal IN (
    'OS-37848','OS-37853','OS-37859','OS-37864','OS-37890',
    'OS-37914','OS-37919','OS-37922','OS-37926','OS-37964',
    'OS-37965','OS-37967','OS-37992','OS-37997'
  )
  AND prioridad IS DISTINCT FROM 'P3';

-- ============================================================
-- PASO 4: Recalcular umbral_horas con la prioridad corregida
-- Lógica: SLA_COPEC = P1(RM:18/Reg:24) P2(RM:24/Reg:48) P3(RM:36/Reg:72) P4(RM:96/Reg:96)
-- JOIN: lc.eds_codigo (ej: "60338") ↔ es.barcode_cliente (código del cliente en la EDS)
-- Zona: se usa es.zona_sla = 'Santiago' vs 'Regiones'
-- ============================================================
UPDATE llamados_correctivos lc
SET umbral_horas = CASE lc.prioridad
  WHEN 'P1' THEN CASE WHEN es.zona_sla = 'Santiago' THEN 18 ELSE 24 END
  WHEN 'P2' THEN CASE WHEN es.zona_sla = 'Santiago' THEN 24 ELSE 48 END
  WHEN 'P3' THEN CASE WHEN es.zona_sla = 'Santiago' THEN 36 ELSE 72 END
  WHEN 'P4' THEN 96
  ELSE NULL
END
FROM estaciones_servicio es
WHERE lc.eds_codigo = es.barcode_cliente        -- FIX: columna correcta (era es.codigo, no existe)
  AND lc.cliente = 'COPEC'
  AND lc.os_fracttal IN (
    'OS-37752','OS-37856','OS-37882','OS-37886','OS-37887',
    'OS-37893','OS-37906','OS-37920','OS-37924','OS-37993','OS-37999',
    'OS-37741','OS-37768','OS-37777','OS-37802','OS-37803',
    'OS-37804','OS-37826','OS-37842','OS-37854','OS-37855',
    'OS-37861','OS-37863','OS-37878','OS-37884','OS-37888',
    'OS-37892','OS-37898','OS-37904','OS-37907','OS-37909',
    'OS-37910','OS-37917','OS-37925','OS-37929','OS-37960',
    'OS-37966','OS-37968','OS-37969','OS-37984','OS-37991','OS-37996',
    'OS-37848','OS-37853','OS-37859','OS-37864','OS-37890',
    'OS-37914','OS-37919','OS-37922','OS-37926','OS-37964',
    'OS-37965','OS-37967','OS-37992','OS-37997'
  );

-- ============================================================
-- VERIFICACIÓN: Ver distribución resultante (ejecutar después)
-- ============================================================
-- SELECT prioridad, COUNT(*) as total
-- FROM llamados_correctivos
-- WHERE cliente = 'COPEC'
--   AND fecha_llamado >= '2026-06-01'
--   AND fecha_llamado <  '2026-07-01'
-- GROUP BY prioridad
-- ORDER BY prioridad;

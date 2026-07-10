-- ═══════════════════════════════════════════════════════════════════════════
-- FIX PRIORIDAD COPEC 24h — Santiago = P2 (no P1)
-- ═══════════════════════════════════════════════════════════════════════════
-- Ejecutar en Supabase SQL Editor.
--
-- Contexto:
--   El sync_fracttal_supabase.py tenía un mapa constante 24h → P1 que
--   ignoraba la zona. Real: 24h en Santiago = P2, 24h en Regiones = P1.
--
--   El código Python ya está arreglado (commit 6bb0669), pero hay un
--   trigger en la BD que revierte los UPDATE de prioridad_calc que hace
--   PostgREST, así que el backfill se tiene que correr como SQL directo.
--
-- Este script hace 4 cosas:
--   1. Diagnóstico: lista los triggers existentes en ordenes_trabajo
--   2. Deshabilita temporalmente el trigger (session_replication_role=replica)
--   3. Corrige 227 OTs COPEC 2026 mal clasificadas
--   4. Rehabilita el trigger
--
-- Si prefieres eliminar el trigger permanentemente en vez de bypasearlo,
-- descomenta el bloque DROP TRIGGER al final.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── 1) DIAGNÓSTICO: qué triggers existen en ordenes_trabajo ──────────────
SELECT trigger_name, event_manipulation, action_timing, action_statement
FROM information_schema.triggers
WHERE event_object_table = 'ordenes_trabajo';

-- ── 2) BACKFILL con trigger deshabilitado ────────────────────────────────
BEGIN;

-- Bypass triggers en esta sesión
SET LOCAL session_replication_role = 'replica';

-- Preview: contar cuántas filas se van a actualizar
WITH mal AS (
  SELECT id_ot, prioridad_calc, nota_tarea,
    (SELECT comuna FROM estaciones_servicio es
      WHERE es.eds_occim = ot.codigo_eds
         OR es.cod_occim_fracttal = ot.codigo_eds
         OR es.loc_fracttal = ot.codigo_eds
      LIMIT 1) AS comuna
  FROM ordenes_trabajo ot
  WHERE cliente = 'COPEC'
    AND fecha_creacion >= '2026-01-01'
    AND prioridad_calc = 'P1'
    AND nota_tarea ~* 'Tiempo\s+de\s+respuesta\s*:\s*24'
)
SELECT COUNT(*) AS afectadas_santiago
FROM mal
WHERE UPPER(unaccent(COALESCE(comuna,''))) IN (
  'SANTIAGO','LAS CONDES','VITACURA','PROVIDENCIA','NUNOA','LA REINA','MACUL',
  'PENALOLEN','LA FLORIDA','PUENTE ALTO','MAIPU','ESTACION CENTRAL','CERRILLOS',
  'PUDAHUEL','QUILICURA','RENCA','CONCHALI','INDEPENDENCIA','RECOLETA','HUECHURABA',
  'LO BARNECHEA','SAN MIGUEL','SAN JOAQUIN','LA GRANJA','LA CISTERNA','EL BOSQUE',
  'SAN BERNARDO','LO ESPEJO','PEDRO AGUIRRE CERDA','P.A. CERDA','SAN RAMON',
  'LA PINTANA','CERRO NAVIA','LO PRADO','QUINTA NORMAL','BUIN','CALERA DE TANGO',
  'COLINA','LAMPA','TALAGANTE','PENAFLOR','EL MONTE','PADRE HURTADO','MELIPILLA',
  'CURACAVI','MARIA PINTO','ISLA DE MAIPO','SAN PEDRO','ALHUE','PIRQUE','TILTIL',
  'BATUCO','PAINE'
);

-- Update real
UPDATE ordenes_trabajo ot
SET prioridad_calc = 'P2'
WHERE cliente = 'COPEC'
  AND fecha_creacion >= '2026-01-01'
  AND prioridad_calc = 'P1'
  AND nota_tarea ~* 'Tiempo\s+de\s+respuesta\s*:\s*24'
  AND UPPER(unaccent(COALESCE(
        (SELECT comuna FROM estaciones_servicio es
          WHERE es.eds_occim = ot.codigo_eds
             OR es.cod_occim_fracttal = ot.codigo_eds
             OR es.loc_fracttal = ot.codigo_eds
          LIMIT 1),
        ''))) IN (
    'SANTIAGO','LAS CONDES','VITACURA','PROVIDENCIA','NUNOA','LA REINA','MACUL',
    'PENALOLEN','LA FLORIDA','PUENTE ALTO','MAIPU','ESTACION CENTRAL','CERRILLOS',
    'PUDAHUEL','QUILICURA','RENCA','CONCHALI','INDEPENDENCIA','RECOLETA','HUECHURABA',
    'LO BARNECHEA','SAN MIGUEL','SAN JOAQUIN','LA GRANJA','LA CISTERNA','EL BOSQUE',
    'SAN BERNARDO','LO ESPEJO','PEDRO AGUIRRE CERDA','P.A. CERDA','SAN RAMON',
    'LA PINTANA','CERRO NAVIA','LO PRADO','QUINTA NORMAL','BUIN','CALERA DE TANGO',
    'COLINA','LAMPA','TALAGANTE','PENAFLOR','EL MONTE','PADRE HURTADO','MELIPILLA',
    'CURACAVI','MARIA PINTO','ISLA DE MAIPO','SAN PEDRO','ALHUE','PIRQUE','TILTIL',
    'BATUCO','PAINE'
  );

-- Restaurar triggers
SET LOCAL session_replication_role = 'origin';

COMMIT;

-- ── 3) VERIFICACIÓN ──────────────────────────────────────────────────────
SELECT id_ot, prioridad_calc,
  (SELECT comuna FROM estaciones_servicio es
    WHERE es.eds_occim = ot.codigo_eds
       OR es.cod_occim_fracttal = ot.codigo_eds
       OR es.loc_fracttal = ot.codigo_eds
    LIMIT 1) AS comuna
FROM ordenes_trabajo ot
WHERE id_ot IN ('OS-38516','OS-38517','OS-38521','OS-38544','OS-38553',
                'OS-38568','OS-38593','OS-38608','OS-38609','OS-38615',
                'OS-38619','OS-38642','OS-38669','OS-38695')
ORDER BY id_ot;

-- Todas las de Santiago deberían aparecer con prioridad_calc = P2
-- (OS-38544 Curicó queda P1 porque no es Santiago)
-- (OS-38553 Quinta Normal queda P3 porque el correo dice 36 Hr)

-- ═══════════════════════════════════════════════════════════════════════════
-- OPCIONAL: eliminar permanentemente el trigger que bloquea prioridad_calc
-- ═══════════════════════════════════════════════════════════════════════════
-- Descomenta si prefieres que futuros UPDATEs desde PostgREST tampoco se
-- reviertan (por ejemplo, si un script Python necesita corregir prioridades
-- sin tener que ejecutar SQL cada vez).
--
-- ⚠️  Primero identifica el trigger con el SELECT del paso (1) y reemplaza
-- 'nombre_del_trigger' abajo.
--
-- DROP TRIGGER IF EXISTS nombre_del_trigger ON ordenes_trabajo;

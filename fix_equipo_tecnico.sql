-- ============================================================
-- FIX: Poblar equipo en llamados_correctivos
-- Para todos los registros que tienen tecnico pero equipo IS NULL
-- JOIN: llamados_correctivos.tecnico → tecnicos.nombre_completo → equipos.nombre
-- ============================================================

-- translate() normaliza tildes sin extensiones: "Víctor"="Victor", "Rodríguez"="Rodriguez"
UPDATE llamados_correctivos lc
SET equipo = eq.nombre_equipo
FROM tecnicos t
JOIN equipos eq ON eq.id = t.equipo_id
WHERE translate(lower(lc.tecnico),       'áéíóúàèìòùäëïöüâêîôûãõñ', 'aeiouaeiouaeiouaeiouaon')
    = translate(lower(t.nombre_completo), 'áéíóúàèìòùäëïöüâêîôûãõñ', 'aeiouaeiouaeiouaeiouaon')
  AND lc.equipo IS NULL
  AND lc.tecnico IS NOT NULL;

-- Verificación: cuántos quedaron sin equipo después del fix
-- SELECT COUNT(*) FROM llamados_correctivos
-- WHERE tecnico IS NOT NULL AND equipo IS NULL;

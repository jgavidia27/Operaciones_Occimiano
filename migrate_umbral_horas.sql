-- Agregar columna umbral_horas a llamados_correctivos
ALTER TABLE llamados_correctivos
  ADD COLUMN IF NOT EXISTS umbral_horas numeric;

COMMENT ON COLUMN llamados_correctivos.umbral_horas
  IS 'Horas SLA comprometidas con el cliente (24=P1, 48=P2, 72=P3 para ESMAX; depende de zona para COPEC)';

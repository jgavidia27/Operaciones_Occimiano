-- Persiste el Message-ID del primer correo de cada mes/tema, para poder
-- responder en el mismo hilo en Gmail cuando se envía la semana siguiente.
-- Ej. topic='shell_consumos', mes='2026-08' → guarda el ID del correo
-- del lunes 3-ago; los del 10, 17, 24, 31 responden con In-Reply-To a ese.
-- Cuando llega septiembre, se crea un thread nuevo (nuevo message_id).
CREATE TABLE IF NOT EXISTS email_threads (
    topic       text        NOT NULL,
    mes         text        NOT NULL,  -- YYYY-MM
    message_id  text        NOT NULL,  -- del formato <abc@gmail.com>
    subject     text,
    sent_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (topic, mes)
);

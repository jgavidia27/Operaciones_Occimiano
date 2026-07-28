-- =====================================================================
-- Tablas para integración Portal Enlace Copec
-- =====================================================================
-- Ejecutar UNA vez en Supabase → SQL Editor.
-- =====================================================================

-- 1) Estado del token OAuth (una sola fila con id=1)
create table if not exists enlace_auth (
    id              int primary key default 1,
    refresh_token   text not null,
    access_token    text,
    expires_at      timestamptz,
    updated_at      timestamptz default now(),
    last_error      text,
    constraint enlace_auth_singleton check (id = 1)
);

-- 2) Pool de avisos (espejo de /avisos del API Copec)
create table if not exists enlace_avisos (
    id_sap                       text primary key,
    numero_aviso                 text,
    numero_orden                 text,
    tipo_aviso                   text,         -- CORRECTIVO / PREVENTIVO
    tipo_atencion_mantenimiento  text,         -- PRESENCIAL / REMOTA
    estado                       text,         -- ASIGNADO_RESOLUTOR / EN_PROGRESO_EN_CURSO / CERRADO / ...
    prioridad                    text,         -- P1..P4
    descripcion                  text,
    descripcion_falla            text,
    titulo_aviso                 text,
    descripcion_equipo           text,
    descripcion_producto         text,
    descripcion_ubicacion        text,
    descripcion_instalacion      text,
    descripcion_componente       text,
    id_instalacion               text,         -- ej: C-E-13-CE-60066 (últimos 5 dígitos = cod EDS)
    eds_codigo                   text,         -- derivado: 60066
    id_equipo                    text,
    id_producto                  text,
    id_ubicacion                 text,
    id_falla                     text,
    id_grupo_falla               text,
    puesto_trabajo               text,
    ubicacion_tecnica            text,
    nombre_usuario_asignado      text,
    id_usuario_asignado          text,
    razon_social_empresa         text,
    rut_empresa                  text,
    responsable                  text,
    nombre_contacto              text,
    telefono_contacto            text,
    sla                          numeric,
    fecha_creacion               timestamptz,
    fecha_planificada            timestamptz,
    fecha_ultimo_cambio          timestamptz,
    multimedia                   jsonb,
    campos_adicionales           jsonb,
    raw                          jsonb,        -- payload completo por si necesitamos algo más
    sync_at                      timestamptz default now()
);

create index if not exists idx_enlace_avisos_estado         on enlace_avisos(estado);
create index if not exists idx_enlace_avisos_tipo_aviso     on enlace_avisos(tipo_aviso);
create index if not exists idx_enlace_avisos_eds            on enlace_avisos(eds_codigo);
create index if not exists idx_enlace_avisos_fecha_creacion on enlace_avisos(fecha_creacion desc);

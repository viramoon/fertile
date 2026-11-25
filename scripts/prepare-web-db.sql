-- Older bundles used the public search path. Keep their name pointing at the same ledger.
\if :{?finish}
SELECT $sql$CREATE VIEW public._sqlx_migrations WITH (security_invoker=true) AS SELECT * FROM fertile_web._sqlx_migrations;
            COMMENT ON VIEW public._sqlx_migrations IS 'fertile/legacy-sqlx-ledger-v1'$sql$
 WHERE to_regclass('public._sqlx_migrations') IS NULL \gexec
\quit
\endif

-- Invoked only for the supervised Unix-socket database. Never change unrelated roles or passwords.
DO $provision$
DECLARE
    role_oid oid;
    marker constant text := 'fertile/supervised-web-v1';
BEGIN
    SELECT oid INTO role_oid FROM pg_authid WHERE rolname = 'fertile_web';
    IF NOT FOUND THEN
        CREATE ROLE fertile_web LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS NOINHERIT;
        COMMENT ON ROLE fertile_web IS 'fertile/supervised-web-v1';
    ELSIF shobj_description(role_oid, 'pg_authid') IS DISTINCT FROM marker
       OR EXISTS (SELECT FROM pg_authid WHERE oid = role_oid AND
            (NOT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
             OR rolinherit OR rolpassword IS NOT NULL))
       OR EXISTS (SELECT FROM pg_auth_members WHERE member = role_oid OR roleid = role_oid)
    THEN
        RAISE EXCEPTION 'fertile_web exists with an unmanaged identity or unexpected privileges';
    END IF;
END
$provision$;
\if :{?web_database}
\else
\set web_database web
\endif
SELECT format('CREATE DATABASE %I', :'web_database')
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'web_database') \gexec
\connect :web_database
BEGIN;
DO $bootstrap$
DECLARE
    object_owner name;
    schema_name text;
BEGIN
    IF (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()) <> current_user THEN
        RAISE EXCEPTION 'web database has an external owner; configure web-database.env';
    END IF;
    SELECT pg_get_userbyid(nspowner) INTO object_owner FROM pg_namespace WHERE nspname = 'fertile_web';
    IF NOT FOUND THEN
        CREATE SCHEMA fertile_web AUTHORIZATION fertile_web;
    ELSIF object_owner <> 'fertile_web' THEN
        RAISE EXCEPTION 'private web migration schema has an external owner';
    END IF;
    -- Adopt only a recognized v1 database. Leave unrelated databases and schemas untouched.
    IF EXISTS (SELECT FROM pg_namespace WHERE nspname IN ('app', 'blog') AND nspowner = current_user::regrole)
    THEN
        IF to_regclass('public._sqlx_migrations') IS NULL OR to_regclass('app.schema_info') IS NULL THEN
            RAISE EXCEPTION 'existing schema is missing the legacy web migration history';
        END IF;
        IF NOT EXISTS (SELECT FROM public._sqlx_migrations WHERE version = 20261122000000 AND success)
           OR NOT EXISTS (SELECT FROM app.schema_info WHERE component = 'web' AND version = 1) THEN
            RAISE EXCEPTION 'unrecognized web migration history';
        END IF;
    END IF;
    FOREACH schema_name IN ARRAY ARRAY['app','blog'] LOOP
        SELECT pg_get_userbyid(nspowner) INTO object_owner FROM pg_namespace WHERE nspname = schema_name;
        IF FOUND AND object_owner = current_user THEN
            EXECUTE format('ALTER SCHEMA %I OWNER TO fertile_web', schema_name);
        ELSIF FOUND AND object_owner <> 'fertile_web' THEN
            RAISE EXCEPTION 'web application schema has an external owner';
        END IF;
    END LOOP;
    IF to_regclass('app.schema_info') IS NOT NULL AND pg_get_userbyid(
        (SELECT relowner FROM pg_class WHERE oid = to_regclass('app.schema_info'))) = current_user THEN
        ALTER TABLE app.schema_info OWNER TO fertile_web;
    END IF;
    IF EXISTS (SELECT FROM pg_class WHERE oid=to_regclass('public._sqlx_migrations') AND relkind='v'
               AND relowner=current_user::regrole
               AND obj_description(oid, 'pg_class')='fertile/legacy-sqlx-ledger-v1') THEN
        NULL; -- Existing compatibility view.
    ELSIF to_regclass('public._sqlx_migrations') IS NOT NULL THEN
        IF to_regclass('fertile_web._sqlx_migrations') IS NOT NULL OR pg_get_userbyid(
            (SELECT relowner FROM pg_class WHERE oid = to_regclass('public._sqlx_migrations'))) <> current_user THEN
            RAISE EXCEPTION 'ambiguous or externally owned SQLx migration history';
        END IF;
        ALTER TABLE public._sqlx_migrations SET SCHEMA fertile_web;
        ALTER TABLE fertile_web._sqlx_migrations OWNER TO fertile_web;
    END IF;
END
$bootstrap$;
GRANT CONNECT ON DATABASE :"web_database" TO fertile_web;
-- Explicit primary schema also covers clusters where the default search path was customized.
ALTER ROLE fertile_web IN DATABASE :"web_database" SET search_path TO fertile_web, public;
COMMIT;

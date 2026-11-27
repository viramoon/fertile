"""Exercise local PG17 peer isolation, upgrade/restore the previous web migration, and preserve external credentials."""
from hashlib import sha384
import json
import os
import socket
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import supervision

INITIAL = "20261122000000"
W1 = "20261126000000"
APPLIED = ",".join((
    INITIAL,
    W1,
    "20261127000000",  # W3 notifications
    "20261127010000",  # W3 notification replacements
    "20261127020000",  # W4 integration evidence
    "20261127030000",  # W4 SMTP snapshots
    "20261127040000",  # W2a market watchlists
    "20261127074531",  # W5 notification silences
))

def run(*args, env, capture=False, check=True):
    return subprocess.run(args, env=env, text=True, capture_output=capture, check=check)

def sql(env, query):
    return run("psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-c", query, env=env, capture=True).stdout.strip()

def applied_migrations(env, schema):
    return sql(env, f"SELECT string_agg(version::text, ',' ORDER BY version) "
                    f"FROM {schema}._sqlx_migrations WHERE success")

def seed_pre_w1(env):
    migration = REPO / "web/migrations" / f"{INITIAL}_initial_schemas.sql"
    checksum = sha384(migration.read_bytes()).hexdigest()
    # Recreate a pre-W1 database using its original migration and SQLx's ledger
    # format. The current binary applies W1 after this fixture is restored.
    ledger = f"""
        CREATE TABLE public._sqlx_migrations (
            version bigint PRIMARY KEY,
            description text NOT NULL,
            installed_on timestamptz NOT NULL DEFAULT now(),
            success boolean NOT NULL,
            checksum bytea NOT NULL,
            execution_time bigint NOT NULL
        );
        INSERT INTO public._sqlx_migrations (version, description, success, checksum, execution_time)
        VALUES ({INITIAL}, 'initial schemas', true, decode('{checksum}', 'hex'), 0);
    """
    run("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "--single-transaction",
        "-f", migration, "-c", ledger, env=env)

def legacy_bundle(root, source, config):
    # Keep the rollback regression usable in shallow/squashed checkouts. The
    # exact previous release can be supplied for a local upgrade rehearsal.
    legacy = root / "releases/6f16f2e53e0d9f7317ce7029ce0756f693987b42"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin/web").symlink_to(source)
    (legacy / "web").mkdir()
    (legacy / "web/config").symlink_to(config)
    previous = os.environ.get("WEB_DATABASE_TEST_LEGACY_FERTILE")
    if previous:
        (legacy / "fertile").symlink_to(Path(previous).resolve(strict=True))
    else:
        (legacy / "fertile/scripts").mkdir(parents=True)
        (legacy / "fertile/scripts/postgres.sh").symlink_to(REPO / "scripts/postgres.sh")
        (legacy / "fertile/deploy").mkdir()
        old = supervision.yaml.safe_load((REPO / "deploy/services.yaml").read_text())
        for row in old["services"]:
            row.pop("local_database_role", None)
        (legacy / "fertile/deploy/services.yaml").write_text(supervision.yaml.safe_dump(old))
    assert not (legacy / "fertile/scripts/prepare-web-db.sql").exists()
    return legacy


def check_unmanaged_role_refusal(plan):
    # Capture only the intentional provisioning failure. Any other failure
    # must fail this audit and retain its diagnostics.
    run_child = subprocess.run

    def capture_provision(command, **kwargs):
        if command[0] == "psql" and command[-2] == "-f" and Path(command[-1]).name == "prepare-web-db.sql":
            kwargs.update(stderr=subprocess.PIPE, text=True)
        return run_child(command, **kwargs)

    with patch.object(supervision.subprocess, "run", side_effect=capture_provision):
        try:
            supervision.prepare_web(plan)
        except subprocess.CalledProcessError as error:
            diagnostic = "fertile_web exists with an unmanaged identity or unexpected privileges"
            if error.returncode != 3 or diagnostic not in (error.stderr or ""):
                raise AssertionError(f"unexpected provisioning failure: {error.stderr or error}") from error
        else:
            raise AssertionError("unmanaged role was unexpectedly accepted")


def audit():
    source = Path(os.environ["WEB_DATABASE_TEST_BIN"]).resolve(strict=True)
    config = Path(os.environ["WEB_DATABASE_TEST_CONFIG"]).resolve(strict=True)
    admin_name = pwd.getpwuid(os.getuid()).pw_name
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        release = root / "releases" / ("a" * 40)
        (release / "bin").mkdir(parents=True)
        (release / "bin/web").symlink_to(source)
        (release / "fertile").symlink_to(REPO)
        (release / "web").mkdir()
        (release / "web/config").symlink_to(config)
        (root / "current").symlink_to(release)
        (root / "data").mkdir()
        sock = root / "run/postgres"
        # Deployment overrides point at the caller's root. Use this audit's replay defaults.
        os.environ.pop("FERTILE_DATA_ROOT", None)
        os.environ.pop("FERTILE_RESEARCH_ROOT", None)
        os.environ.update(FERTILE_ROOT=tmp, FERTILE_POSTGRES_PORT="55661", FERTILE_MARKET_SOURCE="replay")
        base = dict(os.environ, FERTILE_WEB_PEER="1", PGHOST=str(sock), PGPORT="55661",
                    PGDATABASE="postgres", PGUSER=admin_name)
        supervision.ROOT = root
        supervision.RUN = root / "run"
        # Build the real plan. Only web and postgres are needed in this isolated release.
        plan = supervision.build_plan()
        assert plan["services"]["postgres"]["env"]["FERTILE_WEB_PEER"] == "1"
        legacy = legacy_bundle(root, source, config)
        supervision.switch(root / "current", legacy)
        old_plan = supervision.build_plan()
        services = old_plan["services"]
        assert old_plan["web_database"] == "legacy"
        assert "FERTILE_WEB_PEER" not in services["postgres"]["env"]
        assert services["web"]["env"]["DATABASE_URL"].startswith("postgresql://localhost/web?")
        run(*services["postgres"]["command"][:-1], "init", env=services["postgres"]["env"], capture=True)
        original = [root / "data/postgres" / f"pg_{name}.conf" for name in ("ident", "hba")]
        original_bytes = [p.read_bytes() for p in original]
        log = (root / "pg.log").open("w")
        def start_pg(selected):
            pg = selected["services"]["postgres"]
            handle = subprocess.Popen(pg["command"], env=pg["env"], stdout=log, stderr=log)
            for _ in range(80):
                if run("pg_isready", "-q", env=base, capture=True, check=False).returncode == 0:
                    return handle
                if handle.poll() is not None:
                    raise RuntimeError((root / "pg.log").read_text())
                time.sleep(.1)
            handle.terminate()
            handle.wait(timeout=15)
            raise AssertionError("postgres startup timeout")

        server = start_pg(old_plan)
        try:
            admin = dict(base, PGDATABASE="web")
            web = dict(admin, PGUSER="fertile_web")
            candidate = services["web"]["env"]
            supervision.prepare_web(old_plan)
            assert sql(base, "SELECT count(*) FROM pg_roles WHERE rolname='fertile_web'") == "0"
            assert sql(admin, "SHOW hba_file") == str(original[1])
            assert sql(admin, "SELECT to_regnamespace('app') IS NULL AND to_regnamespace('blog') IS NULL "
                              "AND to_regclass('public._sqlx_migrations') IS NULL") == "t"
            run(source, "migrate", env=candidate)
            assert applied_migrations(admin, "public") == APPLIED
            assert sql(admin, "SELECT to_regclass('app.integration_configs') IS NOT NULL") == "t"
            print("previous bundle: migrated an empty database with the original peer identity and HBA")

            # Recreate the disposable database for the separate pre-W1 restore and upgrade.
            run("dropdb", "web", env=base)
            supervision.prepare_web(old_plan)
            seed_pre_w1(admin)
            assert applied_migrations(admin, "public") == INITIAL
            assert sql(admin, "SELECT to_regclass('app.integration_configs') IS NULL") == "t"
            print("previous bundle: pre-W1 database created, original peer identity and HBA; no managed role")
            sql(admin, "INSERT INTO app.schema_info VALUES ('preserve', 17)")
            sql(admin, "CREATE SCHEMA unrelated; CREATE TABLE unrelated.keep(n int)")
            archive = root / "legacy.dump"
            run("pg_dump", "-Fc", "--create", "-f", archive, env=admin)
            run("dropdb", "web", env=base)
            run("pg_restore", "--exit-on-error", "--create", "-d", "postgres", archive, env=base)
            assert applied_migrations(admin, "public") == INITIAL
            server.terminate()
            server.wait(timeout=15)
            supervision.switch(root / "current", release)
            server = start_pg(plan)
            managed_oid = None
            for label in ("restored-legacy", "idempotent"):
                print(f"{label}: preparing the managed web database")
                supervision.prepare_web(plan)
                observed_oid = sql(admin, "SELECT oid FROM pg_roles WHERE rolname='fertile_web'")
                if managed_oid is None:
                    assert observed_oid.isdecimal()
                    managed_oid = observed_oid
                else:
                    assert observed_oid == managed_oid, "repeated preparation replaced the managed role"
                role = sql(web, "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls,"
                           "has_database_privilege(current_database(),'CREATE'), has_schema_privilege('public','CREATE') "
                           "FROM pg_roles WHERE rolname=current_user")
                assert role == "fertile_web|f|f|f|f|f|f|f", role
                assert applied_migrations(web, "fertile_web") == APPLIED
                assert sql(web, "SELECT to_regclass('app.integration_configs') IS NOT NULL") == "t"
                assert sql(web, "SELECT version FROM app.schema_info WHERE component='preserve'") == "17"
                # The current migrator using the legacy connection sees the same ledger.
                run(source, "migrate", env=candidate)
                assert applied_migrations(admin, "public") == APPLIED
                assert sql(admin, "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='unrelated'") == admin_name
                assert sql(web, "SELECT current_schema()") == "fertile_web"
                assert sql(web, "SELECT has_schema_privilege('app','CREATE'),has_schema_privilege('blog','CREATE')") == "t|t"
                assert run("psql","-XAt","-c","SELECT 1",env=dict(web,PGDATABASE="postgres"),capture=True,check=False).returncode != 0
                print(label, role, f"migrations={APPLIED} preserved=17")
            server.terminate()
            server.wait(timeout=15)
            supervision.switch(root / "current", legacy)
            old_plan = supervision.build_plan()
            server = start_pg(old_plan)
            supervision.prepare_web(old_plan)
            run(source, "migrate", env=old_plan["services"]["web"]["env"])
            assert applied_migrations(admin, "public") == APPLIED
            assert sql(admin, "SELECT version FROM app.schema_info WHERE component='preserve'") == "17"
            print("legacy layout: current migrator used the compatibility view; existing data preserved")
            server.terminate()
            server.wait(timeout=15)
            supervision.switch(root / "current", release)
            server = start_pg(plan)
            run("dropdb", "web", env=base)
            supervision.prepare_web(plan)
            assert applied_migrations(web, "fertile_web") == APPLIED
            assert sql(web, "SELECT version FROM app.schema_info WHERE component='web'") == "1"
            assert sql(web, "SELECT has_database_privilege(current_database(),'CREATE')") == "f"
            run(source, "migrate", env=candidate)
            # The long-lived, deployed HTTP binary uses the same low-privilege identity as migrate.
            with socket.socket() as allocated:
                allocated.bind(("127.0.0.1", 0))
                web_port = allocated.getsockname()[1]
            live_env = dict(plan["services"]["web"]["env"], WEB_PORT=str(web_port))
            live = subprocess.Popen([source, "serve"], cwd=release / "web", env=live_env, stdout=log, stderr=log)
            try:
                for _ in range(60):
                    if sql(admin, "SELECT count(*) FROM pg_stat_activity WHERE datname='web' AND usename='fertile_web'") != "0":
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError("running HTTP process did not connect as fertile_web")
                print("live web HTTP process: pg_stat_activity.usename=fertile_web")
            finally:
                live.terminate()
                live.wait(timeout=25)
            # Simulate an interrupted first provisioning. The next ensure revokes the high-water grant.
            sql(admin, "GRANT CREATE ON DATABASE web TO fertile_web")
            supervision.prepare_web(plan)
            assert sql(web, "SELECT has_database_privilege(current_database(),'CREATE')") == "f"
            print("fresh migration and interrupted grant recovered; database CREATE=false")
            sql(admin, "COMMENT ON ROLE fertile_web IS 'reserved by another operator'")
            check_unmanaged_role_refusal(plan)
            assert sql(admin, "SELECT shobj_description('fertile_web'::regrole,'pg_authid')") == 'reserved by another operator'
            print("unmanaged same-name LOGIN preserved; default provisioning refused")
            secret = root / "secrets/web-database.env"
            secret.parent.mkdir()
            secret.write_text("DATABASE_URL=postgresql://dedicated@external.invalid/application\n")
            secret.chmod(0o600)
            external = supervision.build_plan()
            assert "FERTILE_WEB_PEER" not in external["services"]["postgres"]["env"]
            # Passing an unusable admin PATH proves explicit external configuration performs no provisioning.
            external["services"]["postgres"]["env"]["PATH"] = "/invalid"
            supervision.prepare_web(external)
            assert "external.invalid" not in json.dumps(external)
            assert all(p.read_bytes() == old for p, old in zip(original, original_bytes))
            print("external URL preserved as a private child override; original HBA/ident byte-identical; maintenance identity", sql(admin,"SELECT current_user"))
        finally:
            server.terminate()
            server.wait(timeout=15)
            log.close()

if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    audit()

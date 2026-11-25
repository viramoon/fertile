"""Exercise local PG17 peer isolation, upgrade/restore the previous web migration, and preserve external credentials."""
import json
import os
import socket
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import supervision

def run(*args, env, capture=False, check=True):
    return subprocess.run(args, env=env, text=True, capture_output=capture, check=check)

def sql(env, query):
    return run("psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-c", query, env=env, capture=True).stdout.strip()

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
            run(source, "migrate", env=candidate)
            print("previous bundle: new database created, original peer identity and HBA; no managed role")
            sql(admin, "INSERT INTO app.schema_info VALUES ('preserve', 17)")
            sql(admin, "CREATE SCHEMA unrelated; CREATE TABLE unrelated.keep(n int)")
            archive = root / "legacy.dump"
            run("pg_dump", "-Fc", "--create", "-f", archive, env=admin)
            run("dropdb", "web", env=base)
            run("pg_restore", "--exit-on-error", "--create", "-d", "postgres", archive, env=base)
            server.terminate()
            server.wait(timeout=15)
            supervision.switch(root / "current", release)
            server = start_pg(plan)
            for label in ("restored-legacy", "idempotent"):
                supervision.prepare_web(plan)
                role = sql(web, "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls,"
                           "has_database_privilege(current_database(),'CREATE'), has_schema_privilege('public','CREATE') "
                           "FROM pg_roles WHERE rolname=current_user")
                assert role == "fertile_web|f|f|f|f|f|f|f", role
                assert sql(web, "SELECT count(*) FROM fertile_web._sqlx_migrations WHERE success") == "1"
                assert sql(web, "SELECT version FROM app.schema_info WHERE component='preserve'") == "17"
                # A rollback to the previous candidate, connecting as box, sees the very same history.
                run(source, "migrate", env=candidate)
                assert sql(admin, "SELECT count(*) FROM public._sqlx_migrations WHERE success") == "1"
                assert sql(admin, "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='unrelated'") == admin_name
                assert sql(web, "SELECT current_schema()") == "fertile_web"
                assert sql(web, "SELECT has_schema_privilege('app','CREATE'),has_schema_privilege('blog','CREATE')") == "t|t"
                assert run("psql","-XAt","-c","SELECT 1",env=dict(web,PGDATABASE="postgres"),capture=True,check=False).returncode != 0
                print(label, role, "migrations=1 preserved=17")
            server.terminate()
            server.wait(timeout=15)
            supervision.switch(root / "current", legacy)
            old_plan = supervision.build_plan()
            server = start_pg(old_plan)
            supervision.prepare_web(old_plan)
            run(source, "migrate", env=old_plan["services"]["web"]["env"])
            assert sql(admin, "SELECT count(*) FROM public._sqlx_migrations WHERE success") == "1"
            assert sql(admin, "SELECT version FROM app.schema_info WHERE component='preserve'") == "17"
            print("rollback: previous bundle migrated using the compatibility view; existing data preserved")
            server.terminate()
            server.wait(timeout=15)
            supervision.switch(root / "current", release)
            server = start_pg(plan)
            run("dropdb", "web", env=base)
            supervision.prepare_web(plan)
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
            try:
                supervision.prepare_web(plan)
            except subprocess.CalledProcessError:
                pass
            else:
                raise AssertionError("unmanaged role was unexpectedly accepted")
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
    audit()

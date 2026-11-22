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
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import supervision

def run(*args, env, capture=False, check=True):
    return subprocess.run(args, env=env, text=True, capture_output=capture, check=check)

def sql(env, query):
    return run("psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-c", query, env=env, capture=True).stdout.strip()

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
        (root / "secrets").mkdir()
        sock = root / "run/postgres"
        base = dict(os.environ, FERTILE_ROOT=tmp, FERTILE_WEB_PEER="1", FERTILE_MARKET_SOURCE="replay",
                    FERTILE_POSTGRES_PORT="55661", PGHOST=str(sock), PGPORT="55661", PGDATABASE="postgres", PGUSER=admin_name)
        os.environ.update(FERTILE_ROOT=tmp, FERTILE_POSTGRES_PORT="55661", FERTILE_MARKET_SOURCE="replay")
        supervision.ROOT = root
        supervision.RUN = root / "run"
        # Build the real plan. Only web and postgres are needed in this isolated release.
        plan = supervision.build_plan()
        assert plan["services"]["postgres"]["env"]["FERTILE_WEB_PEER"] == "1"
        pg = REPO / "scripts/postgres.sh"
        run(pg, "init", env=base, capture=True)
        original = [root / "data/postgres" / f"pg_{name}.conf" for name in ("ident", "hba")]
        original_bytes = [p.read_bytes() for p in original]
        log = (root / "pg.log").open("w")
        server = subprocess.Popen([pg, "foreground"], env=base, stdout=log, stderr=log)
        try:
            for _ in range(80):
                if run("pg_isready", "-q", env=base, capture=True, check=False).returncode == 0:
                    break
                if server.poll() is not None:
                    raise RuntimeError((root / "pg.log").read_text())
                time.sleep(.1)
            admin = dict(base, PGDATABASE="web")
            web = dict(admin, PGUSER="fertile_web")
            candidate = dict(base, WEB_CONFIG_DIR=str(config), DATABASE_URL=
                             "postgresql://localhost/web?host=" + quote(str(sock), safe="") + "&port=55661")
            run("createdb", "web", env=base)
            run(source, "migrate", env=candidate)
            sql(admin, "INSERT INTO app.schema_info VALUES ('preserve', 17)")
            sql(admin, "CREATE SCHEMA unrelated; CREATE TABLE unrelated.keep(n int)")
            archive = root / "legacy.dump"
            run("pg_dump", "-Fc", "--create", "-f", archive, env=admin)
            run("dropdb", "web", env=base)
            run("pg_restore", "--exit-on-error", "--create", "-d", "postgres", archive, env=base)
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

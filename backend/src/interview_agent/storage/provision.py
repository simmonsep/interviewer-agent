"""Project-only Docker/PG bootstrap. Prints metadata, never passwords or connection strings."""

import argparse
import json
import secrets
import socket
import subprocess
import time
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


def docker(*args):
    result = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError("docker_command_failed; check Docker Desktop and project container metadata")
    return result.stdout.strip()


def provision(root: Path):
    root = root.resolve()
    config = json.loads((root / "configs/python-postgres.json").read_text(encoding="utf-8"))
    private = root / ".private/postgres"
    private.mkdir(parents=True, exist_ok=True)
    saved = private / "bootstrap.json"
    if saved.exists():
        credentials = json.loads(saved.read_text(encoding="utf-8"))
    else:
        credentials = {role: secrets.token_urlsafe(32) for role in ("admin", "owner", "app")}
        saved.write_text(json.dumps(credentials), encoding="utf-8")
    password_file = private / "admin-password.txt"
    if not password_file.exists():
        password_file.write_text(credentials["admin"], encoding="utf-8")
    label = "io.interview-agent.project"
    existing = docker("ps", "-a", "--filter", f"name=^/{config['container']}$", "--format", "{{.Names}}")
    if existing:
        owner = docker("inspect", "--format", '{{index .Config.Labels "io.interview-agent.project"}}', config["container"])
        if owner != str(root):
            raise RuntimeError("container_not_owned_by_this_project")
        docker("start", config["container"])
    else:
        with socket.socket() as probe:
            if probe.connect_ex((config["host"], config["port"])) == 0:
                raise RuntimeError("postgres_port_in_use; do not stop unknown owner")
        volumes = docker("volume", "ls", "--filter", f"name=^{config['volume']}$", "--format", "{{.Name}}")
        if volumes:
            owner = docker("volume", "inspect", "--format", '{{index .Labels "io.interview-agent.project"}}', config["volume"])
            if owner != str(root):
                raise RuntimeError("volume_not_owned_by_this_project")
        else:
            docker("volume", "create", "--label", f"{label}={root}", config["volume"])
        docker("run", "-d", "--name", config["container"], "--label", f"{label}={root}",
               "-p", f"127.0.0.1:{config['port']}:5432", "--mount", f"type=volume,src={config['volume']},dst=/var/lib/postgresql",
               "--mount", f"type=bind,src={password_file},dst=/run/secrets/admin_password,readonly",
               "-e", "POSTGRES_USER=py_interview_admin", "-e", "POSTGRES_DB=postgres",
               "-e", "POSTGRES_PASSWORD_FILE=/run/secrets/admin_password", config["image"])
    info = make_conninfo(host=config["host"], port=config["port"], dbname="postgres", user="py_interview_admin",
                         password=credentials["admin"], connect_timeout=2)
    deadline = time.monotonic() + 45
    while True:
        try:
            conn = psycopg.connect(info, autocommit=True)
            break
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise RuntimeError("new_postgres_not_ready") from None
            time.sleep(0.5)
    with conn:
        for role, key in [("py_interview_owner", "owner"), ("py_interview_app", "app")]:
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}")
                             .format(sql.Identifier(role), sql.Literal(credentials[key])))
        for database in [config["database"], config["test_database"]]:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {} OWNER py_interview_owner").format(sql.Identifier(database)))
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO py_interview_owner,py_interview_app").format(sql.Identifier(database)))
        server = conn.execute("SHOW server_version").fetchone()[0]
    for name, database, role, key in [
        ("migration.json", config["database"], "py_interview_owner", "owner"),
        ("test-migration.json", config["test_database"], "py_interview_owner", "owner"),
        ("app.json", config["database"], "py_interview_app", "app"),
        ("test.json", config["test_database"], "py_interview_app", "app"),
    ]:
        db = {"host": config["host"], "port": config["port"], "dbname": database, "user": role, "password": credentials[key]}
        content = {"database": db, "mode": "test" if database == config["test_database"] else "development"} if role.endswith("app") else db
        (private / name).write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"container": config["container"], "server_version": server, "bind": f"127.0.0.1:{config['port']}",
                      "databases": [config["database"], config["test_database"]], "config_directory": ".private/postgres", "secrets_printed": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create only this Python project's PostgreSQL resources")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    provision(parser.parse_args().root)

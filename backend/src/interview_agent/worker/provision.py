"""Create this project's independent Redis with file-based password and AOF volume."""

import argparse
import json
import secrets
import socket
import time
from pathlib import Path

from redis import Redis
from redis.exceptions import RedisError

from interview_agent.storage.provision import docker


def provision(root: Path):
    root = root.resolve()
    config = json.loads((root / "configs/python-redis.json").read_text(encoding="utf-8"))
    private = root / ".private/redis"
    private.mkdir(parents=True, exist_ok=True)
    saved = private / "bootstrap.json"
    if not saved.exists():
        saved.write_text(json.dumps({"password": secrets.token_hex(32)}), encoding="utf-8")
    password = json.loads(saved.read_text(encoding="utf-8"))["password"]
    conf = private / "redis.conf"
    if not conf.exists():
        conf.write_text(f'bind 0.0.0.0\nprotected-mode yes\nport 6379\nrequirepass {password}\nappendonly yes\nappendfsync everysec\ndir /data\nsave ""\n', encoding="utf-8")
    existing = docker("ps", "-a", "--filter", f"name=^/{config['container']}$", "--format", "{{.Names}}")
    label = "io.interview-agent.project"
    if existing:
        if docker("inspect", "--format", '{{index .Config.Labels "io.interview-agent.project"}}', config["container"]) != str(root):
            raise RuntimeError("redis_container_not_owned_by_this_project")
        docker("start", config["container"])
    else:
        with socket.socket() as probe:
            if probe.connect_ex((config["host"], config["port"])) == 0:
                raise RuntimeError("redis_port_in_use; do not stop unknown owner")
        volumes = docker("volume", "ls", "--filter", f"name=^{config['volume']}$", "--format", "{{.Name}}")
        if volumes:
            if docker("volume", "inspect", "--format", '{{index .Labels "io.interview-agent.project"}}', config["volume"]) != str(root):
                raise RuntimeError("redis_volume_not_owned_by_this_project")
        else:
            docker("volume", "create", "--label", f"{label}={root}", config["volume"])
        docker("run", "-d", "--name", config["container"], "--label", f"{label}={root}",
               "-p", f"127.0.0.1:{config['port']}:6379", "--mount", f"type=volume,src={config['volume']},dst=/data",
               "--mount", f"type=bind,src={conf},dst=/usr/local/etc/redis/redis.conf,readonly",
               config["image"], "redis-server", "/usr/local/etc/redis/redis.conf")
    client = Redis(host=config["host"], port=config["port"], password=password, socket_connect_timeout=2, socket_timeout=2)
    deadline = time.monotonic() + 30
    try:
        while True:
            try:
                info = client.info("server")
                break
            except RedisError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("new_redis_not_ready") from None
                time.sleep(0.5)
        for name, db, pg in [("worker.json", config["database"], "app.json"), ("test-worker.json", config["test_database"], "test.json")]:
            value = json.loads((root / ".private/postgres" / pg).read_text(encoding="utf-8"))
            value["redis"] = {"host": config["host"], "port": config["port"], "db": db, "password": password}
            (private / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"container": config["container"], "server_version": info["redis_version"], "bind": f"127.0.0.1:{config['port']}",
                          "new_volume": config["volume"], "config_directory": ".private/redis", "secrets_printed": False}))
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Provision only the Python Redis container/volume, never TS Redis")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    provision(parser.parse_args().root)

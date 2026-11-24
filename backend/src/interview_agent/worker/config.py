from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, model_validator
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from interview_agent.contracts.common import Contract
from interview_agent.storage.config import Settings


class RedisSettings(Contract):
    host: Literal["127.0.0.1"]
    port: Annotated[int, Field(ge=1024, le=65535)]
    db: Literal[0, 1]
    password: SecretStr

    def connect(self):
        return Redis(host=self.host, port=self.port, db=self.db, password=self.password.get_secret_value(),
                     decode_responses=True, socket_connect_timeout=3, socket_timeout=3,
                     retry=Retry(NoBackoff(), 0))


class WorkerSettings(Settings):
    redis: RedisSettings
    lease_seconds: Annotated[int, Field(ge=1, le=120)] = 30
    recovery_seconds: Annotated[int, Field(ge=1, le=120)] = 5
    reclaim_idle_ms: Annotated[int, Field(ge=100, le=120000)] = 1000

    @classmethod
    def load(cls, path: Path | str):
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    @model_validator(mode="after")
    def consistent_environment(self):
        if (self.mode == "test") != (self.database.dbname == "py_interview_test") or self.redis.db != (1 if self.mode == "test" else 0):
            raise ValueError("worker_environment_mismatch")
        return self

    @property
    def stream(self):
        return f"py-interview:{self.mode}:jobs:v1"

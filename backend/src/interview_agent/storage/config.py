"""Explicit new-project configuration. No implicit .env or old TS config discovery."""

from pathlib import Path
from typing import Annotated, Literal

from psycopg.conninfo import make_conninfo
from pydantic import Field, SecretStr, model_validator

from interview_agent.contracts.common import Contract


class DatabaseSettings(Contract):
    host: Literal["127.0.0.1"]
    port: Annotated[int, Field(ge=1024, le=65535)]
    dbname: Literal["py_interview", "py_interview_test"]
    user: Literal["py_interview_owner", "py_interview_app"]
    password: SecretStr

    def conninfo(self) -> str:
        return make_conninfo(host=self.host, port=self.port, dbname=self.dbname, user=self.user,
                             password=self.password.get_secret_value(), connect_timeout=5,
                             options="-c timezone=UTC -c search_path=pg_catalog")


class Settings(Contract):
    database: DatabaseSettings
    origin: Literal["http://127.0.0.1:3100"] = "http://127.0.0.1:3100"
    mode: Literal["development", "test"] = "development"
    cookie_name: Literal["py_interview_session"] = "py_interview_session"
    session_ttl_seconds: Annotated[int, Field(ge=300, le=604800)] = 28800
    assessment_mode: Literal["scripted", "live"] = "scripted"
    # F07Z-112: the desktop host starts the materials worker live independently of the
    # interview mode, so the settings pane needs a truthful runtime flag for that worker.
    material_live: bool = False
    written_execution_image: Annotated[str, Field(pattern=r'^eclipse-temurin@sha256:[a-f0-9]{64}$')] | None = None
    coding_execution_image: Annotated[str, Field(pattern=r'^eclipse-temurin@sha256:[a-f0-9]{64}$')] | None = None
    coding_assistant_mode: Literal['scripted', 'live'] = 'scripted'

    @classmethod
    def load(cls, path: Path | str):
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    @model_validator(mode="after")
    def application_role_only(self):
        if self.database.user != "py_interview_app":
            raise ValueError("application_role_required")
        return self

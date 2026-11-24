"""Skill manifest, frozen snapshot and the install request models."""
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Agent = Literal['interview']

MAX_FILES = 12
MAX_FILE_BYTES = 16 * 1024


class SkillManifest(BaseModel):
    id: str = Field(min_length=1, max_length=60, pattern=r'^[a-z0-9][a-z0-9-]*$')
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=200)
    agent: Agent
    modes: tuple[str, ...] = Field(min_length=1)
    entry: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=200)


class FrozenSkill(BaseModel):
    """What a session keeps: the manifest fields plus the exact body and its digest.

    The body is stored so an old session replays the text it started with even after the
    preinstalled version changes; `sha256` makes that replay verifiable.
    """

    id: str
    version: int
    name: str
    agent: Agent
    modes: tuple[str, ...]
    entry: str
    summary: str
    sha256: str
    body: str
    origin: str = 'preinstalled'
    author: str = ''


class SkillFile(BaseModel):
    path: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, Field(max_length=MAX_FILE_BYTES)]


class SkillInstallRequest(BaseModel):
    files: Annotated[list[SkillFile], Field(min_length=1, max_length=MAX_FILES)]


class SkillCommit(BaseModel):
    enabled: bool = True


class SkillSettings(BaseModel):
    enabled: bool = True
    version: int | None = Field(default=None, ge=1)

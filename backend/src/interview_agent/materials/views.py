"""Public material listing (S04A A2).

`text` is the user's own submitted source and is already returned by the API (the pane shows the
original). It is not a model prompt or a hidden tool result. Runs and drafts already select only
their public columns in the repository; nested payloads stay open with typed core fields.
"""
from typing import Literal

from pydantic import ConfigDict

from interview_agent.contracts.common import Contract, PositiveInt
from interview_agent.contracts.practice import UuidString


class MaterialSourceView(Contract):
    model_config = ConfigDict(strict=True, extra="allow")

    id: UuidString
    name: str
    format: str
    text: str
    material_revision: int
    content_sha256: str
    clarification_context: dict | None
    file_reference: dict | None


class MaterialFactView(Contract):
    model_config = ConfigDict(strict=True, extra="allow")

    id: UuidString
    source_id: UuidString
    kind: str
    quote: str
    start_char: int
    end_char: int
    verification: str
    file_locations: list[dict]


class MaterialRunView(Contract):
    model_config = ConfigDict(strict=True, extra="allow")

    id: UuidString
    status: str
    goal_version: PositiveInt
    material_revision: int
    revision: PositiveInt
    model_requests: int
    result: dict | None
    error_code: str | None
    origin: str | None
    mode: Literal["analyze", "draft", "align"]
    stale: bool


class MaterialDraftView(Contract):
    model_config = ConfigDict(strict=True, extra="allow")

    id: UuidString
    run_id: UuidString | None
    version: PositiveInt
    goal_version: PositiveInt
    material_revision: int
    markdown: str
    payload: dict
    stale: bool


class MaterialListingView(Contract):
    goal_version: PositiveInt
    material_revision: int
    target: dict
    model_requests: int
    material_model_requests: int
    sources: list[MaterialSourceView]
    facts: list[MaterialFactView]
    runs: list[MaterialRunView]
    drafts: list[MaterialDraftView]


class MaterialSourceResult(MaterialListingView):
    source_id: UuidString


class MaterialRunResult(MaterialListingView):
    run_id: UuidString


class MaterialConfirmationResult(MaterialListingView):
    confirmation_id: UuidString


class MaterialDraftResult(MaterialListingView):
    draft_id: UuidString

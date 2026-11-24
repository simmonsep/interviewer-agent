import hashlib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator

from interview_agent.contracts.common import Contract
from interview_agent.runtime.store import canonical

AUTHORIZATION_ID = "py-mig-08-20261026-cny2"
MAX_AUTHORIZED_NANO = 2_000_000_000

# The desktop has no external configs/ directory once frozen, so the same conservative
# policy is embedded here. The values mirror configs/ja05-deepseek-flash.json; a test pins
# the digest so the two cannot drift silently. Live calls still need an explicit
# authorization row and a user-supplied key.
DEFAULT_LIVE_POLICY = {
    "profile": "ja05-deepseek-flash",
    "model": "deepseek-flash",
    "observed_model_version": "DeepSeek-V4.1-Flash-1017",
    "endpoint": "https://api.deepseek.com/chat/completions",
    "thinking": "disabled",
    "max_output_tokens": 2048,
    "max_payload_bytes": 32768,
    "input_reservation_tokens": 65536,
    # F07Z-167 (decision A, 2026-11-21): a 10-12 round full simulation needs 39-52 calls; 56 was
    # the first fuse. A later decision raised the per-round ceiling to 5 and the fuse to 72
    # (13 tasks x 5 + margin). The money cap below still bounds actual spending.
    "max_http_attempts": 72,
    "max_operation_attempts": 5,
    "timeout_seconds": 45,
    "input_miss_nano_cny_per_token": 2000,
    "input_hit_nano_cny_per_token": 40,
    "output_nano_cny_per_token": 8000,
    "pricing_checked_at": "2026-10-28",
    "pricing_basis": "Published peak CNY rates bound JA-05 whole-session pilot under one CNY2 authorization; not provider invoice",
    "official_sources": ["https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"],
}


def default_policy():
    """The policy used when the desktop has no configs/ directory to load."""
    return LivePolicy.model_validate(DEFAULT_LIVE_POLICY)


class LivePolicy(Contract):
    profile: str
    model: str
    observed_model_version: str
    endpoint: str
    thinking: Literal["disabled"]
    max_output_tokens: Literal[2048]
    max_payload_bytes: Literal[32768]
    input_reservation_tokens: Literal[65536]
    max_http_attempts: Annotated[int, Field(ge=1, le=96)]
    # Raised from 6 to 8 with the batched rewrite (F07Z-40): four batches plus two reads
    # plus two self-corrections still has to fit under one authorization.
    # The per-run ceiling is derived from the batch count (2 reads + batches + 4);
    # this field bounds it, and the monetary cap bounds spending.
    max_operation_attempts: Annotated[int, Field(ge=1, le=12)]
    timeout_seconds: Literal[45]
    input_miss_nano_cny_per_token: Literal[2000]
    input_hit_nano_cny_per_token: Literal[40]
    output_nano_cny_per_token: Literal[8000]
    pricing_checked_at: str
    pricing_basis: str
    official_sources: list[str]

    @field_validator("profile", "model")
    @classmethod
    def nonempty_identifier(cls, value):
        value = value.strip()
        if not value or "\x00" in value or any(c.isspace() for c in value):
            raise ValueError("policy_identifier_invalid")
        return value

    @field_validator("endpoint")
    @classmethod
    def https_chat_completions(cls, value):
        value = value.strip()
        if not value.startswith("https://") or not value.endswith("/chat/completions"):
            raise ValueError("policy_endpoint_invalid")
        return value

    @classmethod
    def load(cls, path):
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def with_model_settings(self, model_settings: dict) -> "LivePolicy":
        """Return a policy whose endpoint/model come from the user's saved model
        access settings, keeping every other field (pricing, budget, token caps)
        unchanged.

        The pricing fields are a conservative usage ceiling, not a charge: this
        product never bills the user, and the user pays their provider directly.
        """
        base_url = (model_settings.get("base_url") or "").strip()
        model = (model_settings.get("model") or "").strip()
        if not base_url or not model:
            raise ValueError("model_settings_incomplete")
        endpoint = base_url.rstrip("/") + "/chat/completions"
        return self.model_copy(update={"endpoint": endpoint, "model": model})

    @property
    def digest(self):
        return hashlib.sha256(canonical(self.model_dump(mode="json")).encode()).hexdigest()

    @property
    def reservation_nano(self):
        return (self.input_reservation_tokens * self.input_miss_nano_cny_per_token
                + self.max_output_tokens * self.output_nano_cny_per_token)


def load_key(path: Path | str) -> SecretStr:
    # Explicit new-project file only: no environment discovery or interpolation.
    # F07Z-127: the sidecar CLI passes `--credentials <path>` as a string; without the
    # coercion the worker crashed with AttributeError instead of loading the key, and the
    # failure surfaced as a bare `material_failed`.
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        raise ValueError("model_credentials_unavailable") from None
    values = [line.partition("=")[2].strip() for line in lines if line.startswith("DEEPSEEK_API_KEY=")]
    if len(values) != 1 or not values[0] or any(c.isspace() for c in values[0]) or values[0].startswith(('"', "'")):
        raise ValueError("model_credentials_unavailable")
    return SecretStr(values[0])


def read_model_settings(path: Path) -> dict:
    """Read the user's saved model-access file (the same model.json the desktop
    settings pane writes). Returns the raw dict; it never contains the key, which
    lives in the OS credential manager."""
    import json
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}

"""Pure normalization tests: no database or model endpoint."""

import pytest

from interview_agent.billing.config import LivePolicy, load_key
from interview_agent.billing.ledger import usage_upper


@pytest.mark.parametrize("usage", [
    None, {}, {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
    {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 10},
    {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11, "prompt_cache_hit_tokens": 2},
    {"prompt_tokens": 10, "completion_tokens": 2049, "total_tokens": 2059},
])
def test_usage_missing_or_invalid_is_not_free(project_root, usage):
    policy = LivePolicy.load(project_root / "configs/py008-deepseek-flash.json")
    with pytest.raises(ValueError):
        usage_upper(policy, usage)


def test_usage_without_cache_detail_uses_full_miss_rate(project_root):
    policy = LivePolicy.load(project_root / "configs/py008-deepseek-flash.json")
    raw = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    counts, upper = usage_upper(policy, raw)
    assert counts == raw and upper == 100 * 2000 + 20 * 8000


def test_key_loader_does_not_fall_back_to_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "SYNTHETIC-ENV-KEY-MUST-NOT-BE-USED")
    with pytest.raises(ValueError, match="model_credentials_unavailable"):
        load_key(tmp_path / "not-configured.env")


def test_key_loader_accepts_the_string_path_the_sidecar_cli_passes(tmp_path):
    """F07Z-127: `worker --credentials <path>` reaches load_key as a string.

    Before the coercion this raised AttributeError ('str' has no read_text), which the worker
    reported as a bare `material_failed` with zero model calls.
    """
    path = tmp_path / "live.env"
    path.write_text("DEEPSEEK_API_KEY=synthetic-key-for-test\n", encoding="utf-8")
    assert load_key(str(path)).get_secret_value() == "synthetic-key-for-test"

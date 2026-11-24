import json
import os
import tempfile
from pathlib import Path

import pytest

from interview_agent.data.catalog import CATALOG_FILES, load_catalog


def pytest_configure(config):
    # The CLI entry points and the trial controller refuse a state file or workspace outside
    # `.private` (they write a session token / real artifacts). pytest's default `tmp_path` lives
    # in the system temp directory, so those tests used to fail with
    # `session_state_requires_private_directory` / `private_workspace_required`.
    # Point pytest's temp root at the repository's ignored `.private/` instead: the guardrail
    # stays intact and the tests exercise the real code path.
    root = Path(__file__).resolve().parents[2] / ".private" / "pytest-tmp"
    root.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(root)
    tempfile.tempdir = None


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def catalog_data(project_root):
    return {key: json.loads((project_root / "fixtures/pm0-05" / f"{name}.json").read_text(encoding="utf-8"))
            for key, name in CATALOG_FILES.items()}


@pytest.fixture
def catalog(project_root):
    return load_catalog(project_root)


@pytest.fixture(scope="session")
def ts_reference(project_root):
    return json.loads((project_root / "fixtures/python-migration/ts-reference.json").read_text(encoding="utf-8"))

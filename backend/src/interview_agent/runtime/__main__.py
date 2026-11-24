"""Offline graph demonstration: python -m interview_agent.runtime --root ."""

import argparse
import asyncio
import sys
from pathlib import Path

from interview_agent.data.catalog import build_assessment_fixture, load_catalog

from .adapter import InterviewRuntimeAdapter
from .scripted import ScriptedProvider, success_steps
from .store import ScopedAssessmentStore


async def demo(root: Path, answer_id: str):
    prepared = build_assessment_fixture(load_catalog(root), answer_id)
    provider = ScriptedProvider(success_steps(prepared.bundle, prepared.input))
    result = await InterviewRuntimeAdapter(ScopedAssessmentStore(prepared.bundle), provider).run(prepared.input)
    print(result.model_dump_json(indent=2))
    if result.envelope.termination != "candidate_ready":
        raise SystemExit(1)


def main():
    # Windows redirected stdout otherwise follows the legacy locale encoding.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Real LangGraph with a deterministic provider; no model requests")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--answer-id", default="answer-01")
    args = parser.parse_args()
    asyncio.run(demo(args.root, args.answer_id))


if __name__ == "__main__":
    main()

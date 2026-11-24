"""Offline catalog inspection: python -m interview_agent.data --root ."""

import argparse
import json
from pathlib import Path

from .catalog import load_catalog, practice_questions


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the public synthetic candidate catalog; no model calls")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    data = load_catalog(args.root)
    print(json.dumps({
        "status": "valid", "role": data.role.id, "role_status": data.role.status,
        "review": data.role.review.status, "semantic_scoring_eligible": data.role.review.eligible_for_semantic_scoring,
        "counts": {key: len(getattr(data, key)) for key in (
            "sources", "questions", "resumes", "jds", "seeds", "answers", "trajectories")},
        "practice_questions": len(practice_questions(data)),
        "context_modes": sorted({s.context_mode for s in data.seeds}),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

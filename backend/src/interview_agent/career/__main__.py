"""Explicit read-only feasibility probe, no LLM, account or database access."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx

from .sources import endpoints, probe


async def run(args):
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        result = await probe(args.source, args.job_id, client)
    output = Path.cwd() / ".private/ja-00/probes" / f"{uuid4()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    # Employer descriptions remain private evidence, not bundled public fixtures.
    summary = result.model_dump(mode="json", exclude={"job"})
    if result.job:
        summary["job"] = result.job.model_dump(mode="json", exclude={"responsibilities", "requirements"})
    summary["private_evidence"] = str(output)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result.status == "detail_readable" else 1


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["xiaomi-intern", "tme-campus"], required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    try:
        endpoints(args.source, args.job_id)
    except ValueError as error:
        parser.error(str(error))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

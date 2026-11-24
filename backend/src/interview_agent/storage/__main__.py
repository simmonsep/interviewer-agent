import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import DatabaseSettings
from .migrate import migrate
from interview_agent.resources import resource_root


def main():
    parser = argparse.ArgumentParser(description="Apply checksummed migrations to the new Python database")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--root", default=None, type=Path, help="Explicit migration root; defaults to source or installed package resources")
    args = parser.parse_args()
    settings = DatabaseSettings.model_validate_json(args.config.read_text(encoding="utf-8"))
    result = asyncio.run(migrate(settings, (args.root or resource_root()) / "backend/migrations"),
                         loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
    print(json.dumps({"database": settings.dbname, "applied": result}))


if __name__ == "__main__":
    main()

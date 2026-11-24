"""Run the local API with ``interview-api`` or ``python -m interview_agent.api``."""

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn

from interview_agent.storage.config import Settings

from .app import create_app


def selector_loop():
    return asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run health-only API, or SQL/auth API with explicit new-project config")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--static-dir", type=Path, help="Also serve a built desktop frontend at / in this process")
    parser.add_argument("--model-config", type=Path, help="External file for the user's model access settings")
    parser.add_argument("--written-executor-profile", type=Path, help="Opt-in local Java execution image; requires the separately verified Docker Worker")
    parser.add_argument("--coding-executor-profile", type=Path, help="Opt-in controlled AI Coding; separate Worker required")
    parser.add_argument("--coding-assistant-mode", choices=['scripted', 'live'], default='scripted', help='Frozen session capability; Worker and billing independently authorize calls')
    parser.add_argument("--assessment-mode", choices=["scripted", "live"], default="scripted",
                        help="UI capability label only; Worker and SQL authorization independently control execution")
    args = parser.parse_args()
    settings = Settings.load(args.config).model_copy(update={"assessment_mode": args.assessment_mode}) if args.config else None
    if settings: settings = settings.model_copy(update={'coding_assistant_mode': args.coding_assistant_mode})
    if args.written_executor_profile:
        from interview_agent.execution.docker_java import DockerProfile
        if settings is None: raise ValueError('written_requires_application_config')
        settings=settings.model_copy(update={'written_execution_image':DockerProfile.load(args.written_executor_profile).image})
    if args.coding_executor_profile:
        from interview_agent.execution.docker_java import DockerProfile
        if settings is None: raise ValueError('coding_requires_application_config')
        settings=settings.model_copy(update={'coding_execution_image':DockerProfile.load(args.coding_executor_profile).image})
    uvicorn.run(
        create_app(settings, static_dir=args.static_dir, model_config_path=args.model_config),
        host="127.0.0.1",
        port=3100,
        loop="interview_agent.api.__main__:selector_loop",
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()

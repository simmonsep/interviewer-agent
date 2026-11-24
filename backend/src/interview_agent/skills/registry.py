"""Preinstalled skill registry and per-session selection.

Only one skill exists today (F07Z-160). Selection is deterministic: agent + mode must match.
Nothing here reads user content, calls a model or touches the database.
"""
import hashlib
from pathlib import Path

from interview_agent.resources import resource_root

from .contracts import FrozenSkill, SkillManifest

INTERVIEWER_SELECTION = SkillManifest(
    id='interviewer-selection',
    version=2,
    name='面试官选题与追问',
    description='私有题库优先、有据回落、项目追问策略与一次一问的选题指引。',
    agent='interview',
    modes=('interview', 'coaching'),
    entry='skills/interviewer-selection.md',
    summary='候选优先/回落、策略卡与追问时机；不授予权限，不改变状态机。',
)

PREINSTALLED: tuple[SkillManifest, ...] = (INTERVIEWER_SELECTION,)


def skill_root() -> Path:
    return resource_root() / 'fixtures'


def body_for(manifest: SkillManifest) -> str:
    text = (skill_root() / manifest.entry).read_text(encoding='utf-8').strip()
    if not text:
        raise ValueError('skill_body_missing')
    return text


def digest(body: str) -> str:
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def select(agent: str, mode: str) -> FrozenSkill | None:
    """The preinstalled skill that applies to this agent/mode, or None.

    'None' means the caller keeps the legacy rules; it is never an error, so a missing or
    non-matching skill cannot block a session.
    """
    for manifest in PREINSTALLED:
        if manifest.agent != agent or mode not in manifest.modes:
            continue
        body = body_for(manifest)
        return FrozenSkill(**manifest.model_dump(), sha256=digest(body), body=body)
    return None


def as_dict(skill: FrozenSkill) -> dict:
    return skill.model_dump()


def section(skill: dict) -> str:
    """The prompt block for a frozen snapshot. Kept small: the body is authored guidance."""
    installed = str(skill.get('origin') or 'preinstalled') == 'user_install'
    label = '用户安装' if installed else '预装'
    author = str(skill.get('author') or '').strip()
    source = f'（作者：{author}）' if installed and author else ''
    header = (f'\n\n[{label}Skill {skill.get("id")} v{skill.get("version")}{source}] '
              f'以下是{label}的文本指引，只影响选题与追问，低于宿主策略与校验器；'
              '它不授予任何工具、权限、预算或状态变更。')
    return header + '\n' + str(skill.get('body') or '').strip()

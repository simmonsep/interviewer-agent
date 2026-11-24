"""Tests for the material prompt seam, updated for the D promotion (2026-11-12).

After the promotion `baseline` IS the D text (production). The old text survives as `legacy`
so a single-variable comparison against the previous behaviour is still possible; these tests
exist to keep that control honest and to stop a candidate from silently editing other modes.
"""
import pytest

from interview_agent.evaluation.material_prompts import LEGACY_REWRITE_PARAGRAPH, VARIANTS
from interview_agent.materials.agent import SYSTEM_PROMPT

START = 'draft_style=rewrite时执行简历优化：'
END = '仅完全无材料时友好ask_user。'


def _split(prompt):
    start = prompt.find(START)
    end = prompt.find(END, start)
    assert start >= 0 and end >= 0
    end += len(END)
    return prompt[:start], prompt[start:end], prompt[end:]


def test_baseline_variant_is_the_production_prompt():
    assert VARIANTS['baseline'] is SYSTEM_PROMPT


def test_production_prompt_is_the_reviewed_d_wording():
    prompt = SYSTEM_PROMPT
    # the rules the C2 review relied on
    assert '不得删除或弱化' in prompt            # role markers survive
    assert '加分项一律放suggestions' in prompt
    assert '不能只复制原话' not in prompt         # the pressure that caused the violations
    assert '约/左右' in prompt


@pytest.mark.parametrize('variant', ['legacy', 'grounding-v2', 'grounding-v3'])
def test_every_variant_only_changes_the_rewrite_paragraph(variant):
    base_before, base_para, base_after = _split(SYSTEM_PROMPT)
    cand_before, cand_para, cand_after = _split(VARIANTS[variant])
    assert cand_before == base_before
    assert cand_after == base_after
    assert cand_para != base_para


def test_grounding_d_equals_production_after_the_promotion():
    """Documented on purpose: the candidate became production on 2026-11-12."""
    assert VARIANTS['grounding-d'] == SYSTEM_PROMPT


def test_legacy_variant_keeps_the_old_pressure_words():
    """The control must still be the old prompt, not a paraphrase of it."""
    legacy = VARIANTS['legacy']
    assert '不能只复制原话' in legacy
    assert '不得删除或弱化' not in legacy
    assert LEGACY_REWRITE_PARAGRAPH in legacy


@pytest.mark.parametrize('variant', ['legacy', 'grounding-v2', 'grounding-v3', 'grounding-d'])
def test_candidate_keeps_every_other_mode_intact(variant):
    prompt = VARIANTS[variant]
    for marker in ('analyze模式', 'draft模式：', 'align模式', '最多6次请求',
                   '不得自行确认事实、伪造ID、索取密钥或读任意文件'):
        assert marker in prompt, marker

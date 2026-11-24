"""Candidate rewrite prompts for the material generation probe (NOT production).

The production prompt lives in `interview_agent.materials.agent.SYSTEM_PROMPT`. These
candidates are assembled from it by an explicit, asserted replacement so that:
  * only the `draft_style=rewrite` paragraph changes — every other mode (analyze / draft /
    align / tool protocol / budget wording) stays byte-identical to production, which keeps
    the comparison a single-variable experiment;
  * if production changes that paragraph, the candidate raises instead of silently drifting.

Motivation (F07Z-11 C2): 5/8 production rewrites added an activity, widened its scope, or
asserted an outcome the source never stated. The production paragraph both invites that
("突出JD相关技术与用户贡献，不能只复制原话", "按背景/行动/结果组织") and only forbids it for
numbers and candidate designs. The candidate makes "no new activity/scope/outcome" the
explicit rule and says plainly that keeping the original sentence is acceptable.
"""
from interview_agent.materials.agent import SYSTEM_PROMPT

_BASELINE_PARAGRAPH_START = 'draft_style=rewrite时执行简历优化：'
_BASELINE_PARAGRAPH_END = '仅完全无材料时友好ask_user。'

_CANDIDATE_PARAGRAPH = (
    'draft_style=rewrite时执行简历优化：先read_target、read_source读取所用材料，再submit_rewrite。'
    '无需先分析/确认facts；先给可用初稿，不围绕“真是你做的吗”盘问。'
    '改写只允许在**原意不变、活动不增**的前提下重组表达：可以换句式、合并重复、'
    '按背景/行动/结果排列、把原文已有的技术词提到前面。'
    '每一个活动都必须能在source中找到对应表述；source没写的活动不得补写，'
    '已有活动也不得具体化——例如source只说“负责缓存改造”，不得写成“优化缓存读写策略”'
    '或“负责一致性方案的设计与落地”。'
    '不得新增结果、效果或因果断言（如“保障/确保/使得……稳定、可用、可靠、提升”），'
    '除非source原文已经给出该结果。'
    '不得新增频率或周期修饰（日常、定期、持续、长期、实时），除非source已说明。'
    '不得提升责任等级：参与不得写成承接/主导/牵头/独立完成，部分参与不得写成整体推进，'
    '沿用用户自述的角色范围。'
    'source中的数字、单位与“约/左右”等不确定性必须原样保留。'
    '宁短勿全：若无法在不新增任何信息的前提下润色，保留原句即可，不要为显得充实而扩写。'
    'items.text只放可以直接用于简历的项目条目，source_ids指向所用原材料，change_note解释改动。'
    '候选技术方案、未明确的技术分工、待完善事项、进度与后续计划放suggestions或change_note，'
    '不作为正文条目。正式正文不含未核验/事实编号/编辑过程标签。'
    '仅完全无材料时友好ask_user。'
)


def _rewrite_grounding_v2():
    start = SYSTEM_PROMPT.find(_BASELINE_PARAGRAPH_START)
    end = SYSTEM_PROMPT.find(_BASELINE_PARAGRAPH_END, start)
    if start < 0 or end < 0:
        raise RuntimeError('material prompt paragraph changed; candidate needs a review')
    end += len(_BASELINE_PARAGRAPH_END)
    return SYSTEM_PROMPT[:start] + _CANDIDATE_PARAGRAPH + SYSTEM_PROMPT[end:]


# What production ran before 2026-11-12. Kept verbatim so `legacy` stays a real control:
LEGACY_REWRITE_PARAGRAPH = (
    'draft_style=rewrite时执行简历优化：先read_target、read_source读取所用材料，再submit_rewrite。无需先分析/确认facts；先给可用初稿，不围绕“真是你做的吗”盘问。允许改写句式、合并重复、按背景/行动/结果组织，突出JD相关技术与用户贡献，不能只复制原话。items.text只放可以直接用于简历的项目条目，source_ids指向所用原材料，change_note解释改动。候选技术方案、未明确的技术分工、待完善事项、进度与后续计划放suggestions或change_note，不作为正文条目；例如仅提到MySQL/Redis，不自行把事务边界或Redis去重分工写成已完成实现。对框架缺项主动给合理技术方案/实现细节的补充候选，放suggestions；不将未提供的具体数字/结果或候选方案写成已发生的经历。正式正文不含未核验/事实编号/编辑过程标签，保留用户自述的职责和亮点；完整来源与补充建议留在对照信息里。仅完全无材料时友好ask_user。'
)


REWRITE_GROUNDING_V2 = _rewrite_grounding_v2()

# v2 was measured, not assumed: it made the model copy the source verbatim in 7 of 8 cases
# (147 chars out vs 148 chars in), i.e. it passed the grounding gate by removing the task.
# v3 therefore *requires* a rewording while enumerating which changes are expression-only.
_CANDIDATE_PARAGRAPH_V3 = (
    'draft_style=rewrite时执行简历优化：先read_target、read_source读取所用材料，再submit_rewrite。'
    '无需先分析/确认facts；先给可用初稿，不围绕“真是你做的吗”盘问。'
    '允许的改写（只改表达，不改事实）：调整语序、把结果或技术前置；合并重复、拆开长句；'
    '去掉口语与冗余（“了”“一些”“然后”）；统一术语与英文技术名词写法；'
    '按“负责什么/做了什么/结果如何”重排，但每一句都能在source找到对应。'
    '禁止新增（source没有就不能写）：新的工作活动，或把已有活动具体化——'
    'source只说“负责缓存改造”时，不得写成“优化缓存读写策略”或“负责一致性方案的设计与落地”；'
    '结果、效果或因果断言（“保障/确保/使得……稳定、可用、可靠、提升”）；'
    '频率或周期（日常、定期、持续、长期、实时）；'
    '责任等级（参与→承接/主导/牵头/独立完成，部分参与→整体推进）；'
    '数字、单位、百分比，source的“约/左右”必须原样保留。'
    '每条都必须改变句式：不得把source原句直接作为items.text（source本身已是完整条目时除外）；'
    '若某条确实没有改写空间，在change_note说明“该条已是最简表述”并保留原句。'
    'items.text只放可以直接用于简历的项目条目，source_ids指向所用原材料，change_note解释改动。'
    '候选技术方案、未明确的技术分工、待完善事项、进度与后续计划放suggestions或change_note，'
    '不作为正文条目。正式正文不含未核验/事实编号/编辑过程标签。'
    '仅完全无材料时友好ask_user。'
)


def _rewrite_grounding_v3():
    start = SYSTEM_PROMPT.find(_BASELINE_PARAGRAPH_START)
    end = SYSTEM_PROMPT.find(_BASELINE_PARAGRAPH_END, start)
    if start < 0 or end < 0:
        raise RuntimeError('material prompt paragraph changed; candidate needs a review')
    end += len(_BASELINE_PARAGRAPH_END)
    return SYSTEM_PROMPT[:start] + _CANDIDATE_PARAGRAPH_V3 + SYSTEM_PROMPT[end:]


REWRITE_GROUNDING_V3 = _rewrite_grounding_v3()

# D (product decision, 2026-11-12): the body only restates what the source supports; all the
# value that made the old version "look better" moves into `suggestions`, which the user
# explicitly selects before it can enter a draft (draft_revisions.py). The old paragraph's
# "不能只复制原话 / 突出JD相关技术" pressure is what pushed activities into the body.
_CANDIDATE_PARAGRAPH_D = (
    'draft_style=rewrite时执行简历优化：先read_target、read_source读取所用材料，再submit_rewrite。'
    '无需先分析/确认facts；先给可用初稿，不围绕“真是你做的吗”盘问。'
    '正文（items.text）只写source能支持的内容：允许重排语序、合并重复、去掉口语与冗余、统一术语、'
    '把source已有的技术与结果前置；每个活动、结果、频率、责任等级都必须能在source找到对应。'
    '不得新增活动，也不得把已有活动具体化——source只说“负责缓存改造”时，'
    '不得写成“优化缓存读写策略”或“负责一致性方案的设计与落地”；'
    '不得新增结果、效果或因果断言（“保障/确保/使得……稳定、可用、可靠、提升”）；'
    '不得新增频率或周期（日常、定期、持续、长期、实时）；'
    '不得提升责任等级（参与→承接/主导/牵头/独立完成，部分参与→整体推进）。'
    '不得删除或弱化source中的角色／参与范围表述：source写了“参与／配合／协助／一部分”时，这些限定必须在正文中保留，不能改写成不带限定的标题式陈述。'
    'source中的数字、单位与“约/左右”必须原样保留。'
    '每个source至少产出一条正文条目：source本身已是结果、指标或职责陈述时，正文保留该内容（可调整措辞），不要因为“没有可改写的活动”而省略。'
    '加分项一律放suggestions（最多4条，每条不超过300字）：写出让这份材料更有竞争力的具体补充，'
    '例如可量化的结果方向、可验证的验证方式、可补充的技术方案与取舍。'
    'suggestions是待用户确认的候选，用“可补充：”起步，并写明需要用户确认什么；'
    '不得写成已经发生的事实，也不得把候选方案说成已实施。'
    'items.text只放可以直接用于简历的项目条目，source_ids指向所用原材料，change_note解释改动。'
    '正式正文不含未核验/事实编号/编辑过程标签。仅完全无材料时友好ask_user。'
)


def _rewrite_grounding_d():
    start = SYSTEM_PROMPT.find(_BASELINE_PARAGRAPH_START)
    end = SYSTEM_PROMPT.find(_BASELINE_PARAGRAPH_END, start)
    if start < 0 or end < 0:
        raise RuntimeError('material prompt paragraph changed; candidate needs a review')
    end += len(_BASELINE_PARAGRAPH_END)
    return SYSTEM_PROMPT[:start] + _CANDIDATE_PARAGRAPH_D + SYSTEM_PROMPT[end:]


# `grounding-d` was promoted to production on 2026-11-12 and then extended with the section
# coverage rule (F07Z-22). It is now an alias of production rather than a rebuild: keeping a
# second copy of the paragraph is what let the two drift in the first place.
REWRITE_GROUNDING_D = SYSTEM_PROMPT


def _legacy_variant():
    """The pre-2026-11-12 production prompt, rebuilt by swapping only the rewrite paragraph.

    Kept as a runnable control: after the D paragraph became production, `baseline` and
    `grounding-d` are the same text, so without this the single-variable comparison is gone.
    """
    start = SYSTEM_PROMPT.find(_BASELINE_PARAGRAPH_START)
    end = SYSTEM_PROMPT.find(_BASELINE_PARAGRAPH_END, start)
    if start < 0 or end < 0:
        raise RuntimeError('material prompt paragraph changed; legacy control is stale')
    end += len(_BASELINE_PARAGRAPH_END)
    return SYSTEM_PROMPT[:start] + LEGACY_REWRITE_PARAGRAPH + SYSTEM_PROMPT[end:]


LEGACY_VARIANT = _legacy_variant()

VARIANTS = {'baseline': SYSTEM_PROMPT, 'legacy': LEGACY_VARIANT,
            'grounding-v2': REWRITE_GROUNDING_V2,
            'grounding-v3': REWRITE_GROUNDING_V3, 'grounding-d': REWRITE_GROUNDING_D}

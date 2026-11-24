"""F07Z-118: a source without headings must not be force-fitted into 教育经历.

Live evidence (F07Z-117 handoff 5): a 7,457-character spoken transcript produced 29 bullets,
all rendered under `# 教育经历`, because the model put one guessed anchor there. Code now
decides: record-shaped rows may be inferred, and a source that opens no section at all renders
as one honest list instead of four fabricated ones.
"""
from uuid import uuid4

import pytest

from interview_agent.materials.contracts import MaterialResult, render_draft, validate_result
from interview_agent.materials.segments import (heading_less, plan_batches, pre_heading_ids,
                                                rewrite_observation, scope_segments,
                                                section_candidates)
from test_material_repository import material_context  # noqa: F401  (fixture re-exported)
from test_postgres import pg_settings  # noqa: F401  (fixture re-exported)
from test_worker import worker_settings  # noqa: F401  (fixture re-exported)

SOURCE_ID = str(uuid4())

TRANSCRIPT = (
    '行，那我就从头说吧。反正你也知道我这人讲话没什么条理，想到哪儿说到哪儿，你回头自己整理，'
    '别按我说的顺序来，我那顺序是乱的。我29，刚过完生日，虚岁算30了，本科，计算机科学与技术。\n'
    '你要问我为什么突然去搞AI，说起来也挺没意思的，不是什么我看到了时代的浪潮，真不是，'
    '就是有一天开会，领导说我们要做一个智能客服，然后他看了我一眼，说你牵头做一下，我当时就懵了。\n'
    '后来我想通一件事，就是我不能再这么学了，我拿我熟的JS逻辑去对Python，就是翻译，'
    '这个办法救了我，我大概花了一个星期，就能看懂FastAPI的文档了，不是精通，是能看懂、能跑。\n'
)

DATED = ('2023.09 - 2027.06 某大学 计算机科学与技术 本科\n'
         '2025.07 - 2025.10 某电商公司 后端开发实习\n'
         '负责订单查询接口，用 Java 17 与 Spring Boot 重写慢查询，P95 从 1.2s 降到 300ms。\n'
         '技能：Java、Spring Boot、MySQL、Redis、Git\n')

RESUME = ('教育经历\n2014.09 - 2019.07 本科 某大学 计算机科学与技术\n'
          '工作经历\n2018.10 - 2019.02 某公司 前端开发\n')


def scope(text):
    return {'mode': 'draft', 'draft_style': 'rewrite',
            'sources': [{'id': SOURCE_ID, 'name': '简历.md', 'text': text}],
            'target': {'kind': 'user_jd', 'text': '前端开发', 'content_sha256': 'x'}}


def narrative_of(text):
    segments = scope_segments(scope(text))
    header = set(pre_heading_ids(segments))
    return segments, [s for s in segments if s['id'] not in header]


def test_a_transcript_yields_no_section_candidates():
    segments, narrative = narrative_of(TRANSCRIPT)
    assert heading_less(segments) is True
    # Every line is a long narration; promoting one would invent a section.
    assert section_candidates(narrative) == {}


def test_dated_records_without_headings_are_detected():
    segments, narrative = narrative_of(DATED)
    candidates = section_candidates(narrative)
    assert set(candidates) == {'education', 'experience', 'skills'}
    by_id = {s['id']: s['text'] for s in segments}
    assert by_id[candidates['education'][0]].startswith('2023.09')
    assert by_id[candidates['experience'][0]].startswith('2025.07')
    assert by_id[candidates['skills'][0]].startswith('技能：')


def test_a_long_narrative_line_is_never_promoted():
    long_line = ('我在一家公司做前端，负责过很多项目，也做过一些系统，'
                 '这段话说得很长，包含公司、项目、系统这些词，但它不是一条可用的记录行，'
                 '不应该被当成工作经历或项目经历的锚点，因为它的长度和叙述方式都不符合，'
                 '而且它其实是在讲故事，不是在陈述任职记录，所以我希望它保持原样。')
    assert len(long_line) > 120
    segments, narrative = narrative_of(long_line)
    assert section_candidates(narrative) == {}


def test_heading_source_is_not_heading_less():
    segments, _ = narrative_of(RESUME)
    assert heading_less(segments) is False


def test_heading_less_draft_renders_one_neutral_section():
    segments, narrative = narrative_of(TRANSCRIPT)
    first = segments[0]
    result = MaterialResult(action='draft',
                            rewrite_items=[{'text': '牵头推进智能客服方向工作。',
                                            'source_ids': [SOURCE_ID], 'change_note': '合并口语。',
                                            'evidence_segments': [segments[1]['id']]}],
                            education=[first['text']],
                            structured_sections={'education': [first['id']]})
    markdown, payload = render_draft(scope(TRANSCRIPT), result)
    assert markdown.startswith('# 经历要点')
    assert '# 教育经历' not in markdown
    # Nothing is lost: the structured line and the bullet both survive, in source order.
    assert '牵头推进智能客服' in markdown
    assert markdown.index(first['text'][:12]) < markdown.index('牵头推进智能客服')
    assert payload['structure_note'] == 'heading_less'
    assert payload['missing_sections'] == []


def test_sectioned_draft_still_renders_the_four_sections():
    result = MaterialResult(action='draft',
                            rewrite_items=[{'text': '参与订单服务改造。', 'source_ids': [SOURCE_ID],
                                            'change_note': '重排表达。'}],
                            education=['2014.09 - 2019.07 本科 某大学'],
                            experience=['2018.10 - 2019.02 某公司 前端开发'])
    markdown, payload = render_draft(scope(RESUME), result)
    assert '# 教育经历' in markdown and '# 工作经历' in markdown
    assert payload['structure_note'] == ''


def test_heading_less_source_does_not_force_a_section():
    # The transcript does mention 本科, but it declares no section. Demanding 教育经历 here is
    # what made a live draft park an arbitrary segment there and pull 29 bullets under it.
    result = validate_result(scope(TRANSCRIPT), {
        'action': 'draft',
        'rewrite_items': [{'text': '牵头推进智能客服方向工作。', 'source_ids': [SOURCE_ID],
                           'change_note': '合并口语。'}],
        'education': [], 'experience': [], 'projects': [], 'skills': []})
    assert result.education == []


def test_source_with_a_heading_still_requires_its_section():
    sectioned = '教育经历\n本科 某大学 计算机科学与技术\n工作经历\n2018.10 某公司 前端开发\n'
    with pytest.raises(ValueError, match='education_section_missing'):
        validate_result(scope(sectioned), {
            'action': 'draft',
            'rewrite_items': [{'text': '参与前端开发。', 'source_ids': [SOURCE_ID],
                               'change_note': '重排表达。'}],
            'education': [], 'experience': [], 'projects': [], 'skills': []})


def test_scripted_transcript_run_renders_a_neutral_draft(material_context, worker_settings):
    """The release default is scripted: a heading-less transcript must still produce a draft.

    Before F07Z-118 this exact shape failed with `material_model_limit`, because the scripted
    provider was asked to fill 教育经历 (the text mentions 本科) and could not.
    """
    from test_material_repository import process, source, start, view
    from test_postgres import arun
    c, g = material_context
    text = '\n'.join([TRANSCRIPT.strip()] * 3)
    assert source(c, g, text=text).status_code == 201
    assert start(c, g, 1, mode='draft', draft_style='rewrite').status_code == 202
    arun(process(worker_settings, provider='scripted'))
    run = view(c, g)['runs'][0]
    assert run['status'] == 'SUCCEEDED', run
    draft = view(c, g)['drafts'][0]
    assert draft['markdown'].startswith('# 经历要点')
    assert '# 教育经历' not in draft['markdown']
    assert draft['payload']['structure_note'] == 'heading_less'


def test_observation_carries_the_detected_sections():
    segments, narrative = narrative_of(DATED)
    batches, _ = plan_batches(narrative)
    observation = rewrite_observation(segments[0], narrative, batches, 0,
                                      section_candidates(narrative))
    assert observation['detected_sections']['experience']
    assert 'detected_sections' in observation['rule']


def test_observation_omits_detection_when_nothing_is_record_shaped():
    _, narrative = narrative_of(TRANSCRIPT)
    batches, _ = plan_batches(narrative)
    observation = rewrite_observation({'id': SOURCE_ID, 'name': 'r'}, narrative, batches)
    assert 'detected_sections' not in observation

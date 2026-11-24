"""Coverage ledger (F07Z-37).

External review F07Z-36 accepted `evidence_segments` as provenance only and made the hard
invariant "every eligible narrative segment is covered or explicitly omitted". These tests
pin the deterministic half: the ledger identity, the structural rejections, and the fact
that an incomplete ledger is surfaced on the draft rather than thrown away.
"""
import pytest

from interview_agent.materials.contracts import (MaterialResult, OmittedSegment, RewriteItem,
                                                 render_draft)
from interview_agent.materials.segments import (accounting, check_evidence, missing_sections,
                                                narrative_batches, omission_audit, placement,
                                                scope_segments)

SOURCE = '教育经历\n2014.09 本科 某大学\n工作经历\n2018.10 某公司 前端开发\n项目经历\n即时聊天系统 2017.08\n技能\nNode'


def scope(text=SOURCE):
    return {'mode': 'draft', 'draft_style': 'rewrite', 'target': {'direction': 'x'}, 'facts': [],
            'sources': [{'id': 'aaaa1111-1111-4111-8111-111111111111', 'name': '简历.md',
                         'content_sha256': 'x', 'text': text}]}


def item(text='一条叙述。', evidence=(), source_id='aaaa1111-1111-4111-8111-111111111111'):
    return RewriteItem.model_validate({'text': text, 'source_ids': [source_id],
                                       'change_note': '按原文整理。',
                                       'evidence_segments': list(evidence)})


def draft(items, omitted=(), structured=()):
    return MaterialResult(action='draft', rewrite_items=list(items),
                          education=[], experience=[], projects=[], skills=[],
                          omitted_segments=list(omitted),
                          structured_segment_ids=list(structured))


def test_accounting_identity_excludes_headings_and_structured_lines():
    segments = scope_segments(scope())
    ids = [s['id'] for s in segments]
    # S001/S003/S005/S007 are headings; S002/S004/S006/S008 carry content.
    assert [s['heading'] for s in segments] == [True, False, True, False, True, False, True, False]
    assert ids == ['S001', 'S002', 'S003', 'S004', 'S005', 'S006', 'S007', 'S008']

    report = accounting(segments, structured_ids=['S002'], evidence_ids=['S004'],
                        omitted_ids=['S006', 'S008'])
    assert report == {'eligible': 3, 'covered': 1, 'omitted': 2, 'unaccounted': 0,
                      'missing_segment_ids': []}


def test_accounting_reports_the_segments_nobody_mentioned():
    segments = scope_segments(scope())
    report = accounting(segments, structured_ids=[], evidence_ids=['S002'], omitted_ids=[])
    assert report['eligible'] == 4 and report['unaccounted'] == 3
    assert report['missing_segment_ids'] == ['S004', 'S006', 'S008']


def test_an_unknown_evidence_id_is_rejected_not_dropped():
    segments = scope_segments(scope())
    with pytest.raises(ValueError, match='evidence_segment_not_in_scope'):
        check_evidence(segments, [item(evidence=['S999'])], [], [])


def test_a_structured_line_cannot_be_narrative_evidence():
    segments = scope_segments(scope())
    with pytest.raises(ValueError, match='evidence_segment_is_structured'):
        check_evidence(segments, [item(evidence=['S002'])], [], ['S002'])


def test_one_segment_cannot_be_reused_by_two_items():
    segments = scope_segments(scope())
    with pytest.raises(ValueError, match='evidence_segment_reused'):
        check_evidence(segments, [item(evidence=['S004']), item(evidence=['S004'])], [], [])


def test_evidence_may_not_span_two_source_blocks():
    segments = scope_segments(scope())
    with pytest.raises(ValueError, match='cross_block_evidence'):
        check_evidence(segments, [item(evidence=['S004', 'S006'])], [], [])


def test_omission_rejects_headings_and_conflicts():
    segments = scope_segments(scope())
    with pytest.raises(ValueError, match='omitted_heading_not_allowed'):
        check_evidence(segments, [item(evidence=['S004'])],
                       [OmittedSegment(segment_id='S001', reason='duplicate')], [])
    with pytest.raises(ValueError, match='omitted_segment_conflict'):
        check_evidence(segments, [item(evidence=['S004'])],
                       [OmittedSegment(segment_id='S004', reason='duplicate')], [])


def test_omission_reason_is_a_closed_enum():
    with pytest.raises(ValueError):
        OmittedSegment.model_validate({'segment_id': 'S001', 'reason': '因为我觉得不重要'})


def test_items_are_placed_under_the_block_their_evidence_came_from():
    segments = scope_segments(scope())
    grouped = placement(segments, [item(evidence=['S004']), item(evidence=['S006']), item()])
    assert grouped == {'basics': [], 'education': [], 'experience': [0], 'projects': [1, 2], 'skills': []}


RICH = ('教育经历\n2014.09 本科 某大学\n工作经历\n2018.10 某公司 前端开发\n负责缓存改造\n'
        '项目经历\n即时聊天系统 2017.08\n实现即时通信\n技能\nNode')


def rich_draft(items, omitted=()):
    return MaterialResult(action='draft', rewrite_items=list(items),
                          education=['2014.09 本科 某大学'],
                          experience=['2018.10 某公司 前端开发'],
                          projects=['即时聊天系统 2017.08'],
                          skills=['Node'],
                          omitted_segments=list(omitted),
                          structured_segment_ids=['S002', 'S004', 'S007', 'S010'])


def test_render_puts_work_and_project_bullets_in_their_own_sections():
    markdown, payload = render_draft(scope(RICH), rich_draft(
        [item('负责缓存改造。', ['S005']), item('实现即时通信。', ['S008'])]))
    work = markdown.split('# 工作经历')[1].split('# 项目经历')[0]
    project = markdown.split('# 项目经历')[1].split('# 技能')[0]
    assert '负责缓存改造' in work and '实现即时通信' not in work
    assert '实现即时通信' in project and '负责缓存改造' not in project
    assert payload['coverage']['unaccounted'] == 0


def test_an_incomplete_ledger_is_flagged_on_the_draft_not_discarded():
    markdown, payload = render_draft(scope(RICH), rich_draft([item('负责缓存改造。', ['S005'])]))
    assert payload['candidate_status'] == 'coverage_incomplete'
    assert payload['coverage']['unaccounted'] == 1
    assert payload['coverage']['missing_segment_ids'] == ['S008']
    assert '负责缓存改造' in markdown      # the paid result is kept


def test_an_unsupported_merge_claim_is_flagged_with_its_overlap():
    """The real failure: the model said `merged_elsewhere` for `socket.io ...` but nothing
    in the body contains it. The ledger stays closed; the claim is marked unsupported."""
    text = ('项目经历\n即时聊天系统 2017.08\n通过 socket.io 模块实现 WebSocket 通信\n'
            '实现实时消息推送与断线重连\n技能\nNode')
    segments = scope_segments(scope(text))
    items = [item('实现实时消息推送与断线重连。', ['S004'])]
    omitted = [OmittedSegment(segment_id='S003', reason='merged_elsewhere')]
    audit = omission_audit(segments, items, omitted)
    assert len(audit) == 1
    assert audit[0]['audited'] is True and audit[0]['supported'] is False
    assert audit[0]['overlap'] < 0.5


def test_a_genuine_merge_is_supported():
    text = '项目经历\n即时聊天系统 2017.08\n通过 socket.io 模块实现 WebSocket 通信\n技能\nNode'
    segments = scope_segments(scope(text))
    items = [item('通过 socket.io 模块实现 WebSocket 通信，支持断线重连。', ['S002'])]
    omitted = [OmittedSegment(segment_id='S003', reason='merged_elsewhere')]
    audit = omission_audit(segments, items, omitted)
    assert audit[0]['supported'] is True and audit[0]['overlap'] >= 0.5


def test_only_the_checkable_reason_is_audited():
    segments = scope_segments(scope())
    audit = omission_audit(segments, [item(evidence=['S004'])],
                           [OmittedSegment(segment_id='S006', reason='low_relevance')])
    assert audit[0]['audited'] is False and audit[0]['supported'] is None


def test_batches_never_overlap_and_only_split_a_block_that_is_too_large():
    text = ('教育经历\n本科 某大学\n工作经历\n' + '\n'.join(f'工作要点 {i}' for i in range(14))
            + '\n项目经历\n项目要点 A\n项目要点 B\n技能\nNode')
    segments = scope_segments(scope(text))
    batches = narrative_batches(segments, size=8)
    flat = [sid for batch in batches for sid in batch]
    assert len(flat) == len(set(flat))                     # every segment exactly once
    assert sorted(flat) == sorted(s['id'] for s in segments if not s['heading'])
    by_id = {s['id']: s for s in segments}
    sizes = {}
    for batch in batches:
        for sid in batch:
            sizes[by_id[sid]['block']] = sizes.get(by_id[sid]['block'], 0) + 1
    appearances = {}
    for batch in batches:
        for block in {by_id[sid]['block'] for sid in batch}:
            appearances[block] = appearances.get(block, 0) + 1
    for block, count in appearances.items():
        # small blocks travel together; only an oversized block may span batches
        assert count == 1 or sizes[block] > 10, (block, count, sizes[block])
    assert [len(batch) for batch in batches] == [1, 8, 8, 1]


def test_a_block_bigger_than_the_window_is_cut_into_contiguous_windows():
    text = '工作经历\n' + '\n'.join(f'要点 {i}' for i in range(25))
    segments = scope_segments(scope(text))
    batches = narrative_batches(segments, size=10)
    assert [len(batch) for batch in batches] == [10, 10, 5]
    assert batches[0][-1] != batches[1][0]


def test_a_dated_segment_cannot_be_omitted():
    """F07Z-40 dropped a whole job line as `structural_noise`; a period is an anchor."""
    text = '工作经历\n2018.10 - 2019.02 某公司\n2018.05 - 2018.08 某电商公司\n负责缓存改造'
    segments = scope_segments(scope(text))
    items = [item('负责缓存改造。', ['S004'])]
    with pytest.raises(ValueError, match='dated_segment_omitted'):
        check_evidence(segments, items, [OmittedSegment(segment_id='S003', reason='structural_noise')], [])
    # covering it, or assigning it to a structured section, stays allowed
    check_evidence(segments, [item('负责缓存改造。', ['S003'])], [], [])
    check_evidence(segments, [item('负责缓存改造。', ['S004'])], [], ['S003'])


def test_missing_sections_are_heading_driven_so_nothing_is_invented():
    assert missing_sections(scope_segments(scope(RICH))) == []
    assert missing_sections(scope_segments(scope('教育经历\n2014 本科 某大学\n工作经历\n2018 某公司'))) \
        == ['projects', 'skills']


def test_the_payload_says_which_sections_the_source_never_opened():
    markdown, payload = render_draft(scope(RICH), rich_draft(
        [item('负责缓存改造。', ['S005']), item('实现即时通信。', ['S008'])]))
    assert payload['missing_sections'] == []
    assert '# 项目经历' in markdown


def test_the_audit_reaches_the_payload_and_names_the_segments():
    _, payload = render_draft(scope(RICH), rich_draft(
        [item('负责缓存改造。', ['S005'])],
        omitted=[OmittedSegment(segment_id='S008', reason='merged_elsewhere')]))
    assert payload['unsupported_omissions'] == ['S008']
    assert payload['omission_audit'][0]['overlap'] is not None
    _, payload = render_draft(scope(RICH), rich_draft(
        [item('负责缓存改造。', ['S005'])],
        omitted=[OmittedSegment(segment_id='S008', reason='low_relevance')]))
    assert payload['candidate_status'] == 'editable_suggestion'
    assert payload['coverage'] == {'eligible': 2, 'covered': 1, 'omitted': 1, 'unaccounted': 0,
                                   'missing_segment_ids': []}

def test_basics_render_before_education_and_stay_editable():
    """F07Z-48: job intent / graduation / city get their own section instead of falling
    into a project bullet when the resume has no basic-info heading."""
    text = '求职意向：AI Agent 研发工程师\n教育经历\n2022.09 本科 某大学\n技能\nPython\n熟悉 Agent 开发'
    result = MaterialResult(action='draft', rewrite_items=[item('熟悉 Agent 开发。', ['S006'])],
                            basics=['求职意向：AI Agent 研发工程师'],
                            education=['2022.09 本科 某大学'], skills=['Python'],
                            structured_segment_ids=['S001', 'S003', 'S005'],
                            structured_sections={'basics': ['S001'], 'education': ['S003'], 'skills': ['S005']})
    markdown, payload = render_draft(scope(text), result)
    assert markdown.startswith('# 基本信息')
    assert markdown.index('# 基本信息') < markdown.index('# 教育经历')
    assert payload['basics'] == ['求职意向：AI Agent 研发工程师']
    assert payload['coverage']['unaccounted'] == 0

def test_a_tech_stack_line_cannot_be_omitted():
    """F07Z-50: the only place a project's tools are named must survive."""
    text = '项目经历\n即时聊天系统 2017.08\n技术栈：Python、FastAPI、Redis\n实现实时通信'
    segments = scope_segments(scope(text))
    items = [item('实现实时通信。', ['S004'])]
    with pytest.raises(ValueError, match='tech_stack_segment_omitted'):
        check_evidence(segments, items, [OmittedSegment(segment_id='S003', reason='structural_noise')], [])
    check_evidence(segments, items, [], ['S003'])

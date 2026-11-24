"""收尾 01: the retest suggestion is derived, not guessed.

These are pure tests: no database, no model. The question text must be exactly the authored
retest prompt of the frozen topic, and nothing may turn "no evidence" into a known weakness.
"""
from interview_agent.interviews import catalog
from interview_agent.interviews.retest import suggestions

TOPIC = 'http-idempotency'


def observation(**overrides):
    base = {
        'id': 'obs-1',
        'answer_id': 'answer-1',
        'topic_id': TOPIC,
        'label': 'needs_probe',
        'summary': '把 HTTP 幂等当成业务去重，没有请求身份。',
        'answer_quote': '每次重试都再创建一笔订单，不保存请求身份。',
        'evidence_id': TOPIC + '-reference',
        'evidence_quote': '应用自定义的幂等键及事务性去重需要自行定义和实现。',
        'pack_version': 'ja05-role-pack-v1',
        'assisted': False,
        'correction': None,
    }
    base.update(overrides)
    return base


def test_needs_probe_with_reference_yields_the_authored_retest():
    [item] = suggestions([observation()])
    assert item['question'] == catalog.TOPICS[TOPIC]['retest']
    assert item['topic_title'] == catalog.TOPICS[TOPIC]['title']
    assert item['reference_url'] == catalog.TOPICS[TOPIC]['url']
    assert item['observation_id'] == 'obs-1'
    assert item['answer_id'] == 'answer-1'
    assert item['topic_id'] == TOPIC
    assert item['pack_version'] == 'ja05-role-pack-v1'
    assert item['reason'] == 'needs_probe_with_reference'
    assert item['assisted'] is False


def test_supported_and_inconclusive_are_not_weaknesses():
    assert suggestions([
        observation(label='supported_in_this_answer'),
        observation(label='inconclusive'),
    ]) == []


def test_retracted_observation_is_skipped():
    assert suggestions([observation(correction='用户指出该观察不成立')]) == []


def test_without_a_stored_reference_there_is_no_suggestion():
    assert suggestions([observation(evidence_quote='')]) == []
    assert suggestions([observation(evidence_id=None)]) == []


def test_project_deep_dive_and_user_bank_topics_are_not_fixed_questions():
    assert suggestions([observation(topic_id='project')]) == []
    assert suggestions([observation(topic_id='bank-item-42')]) == []


def test_assisted_retest_keeps_its_mark():
    [item] = suggestions([observation(assisted=True)])
    assert item['assisted'] is True


def test_every_emitted_field_is_copied_from_the_observation_or_the_pack():
    [item] = suggestions([observation()])
    assert set(item) == {
        'observation_id', 'answer_id', 'topic_id', 'topic_title', 'question',
        'reference_url', 'pack_version', 'assisted', 'reason',
    }

def frozen_pack():
    return {'version': 'bank-selection-v1-abc', 'topics': {
        'b-0c08bd84': {
            'title': '工具参数契约与业务权限',
            'url': 'https://json-schema.org/understanding-json-schema/reference/object',
            'retest': '一个旧Agent任务格式合法、但用户已经修改了岗位目标；如何阻止它读新资源或提交旧结果？',
            'bank_item': {'origin': 'builtin', 'item_id': 'agent-tools'},
            'has_reference': True,
        },
    }}


def test_selected_bank_topic_id_resolves_through_the_frozen_item():
    [item] = suggestions([observation(topic_id='b-0c08bd84', pack_version='bank-selection-v1-abc')], frozen_pack())
    assert item['question'].startswith('一个旧Agent任务')
    assert item['topic_title'] == '工具参数契约与业务权限'
    assert item['pack_version'] == 'bank-selection-v1-abc'
    assert item['topic_id'] == 'b-0c08bd84'


def test_bank_topic_is_not_guessed_without_the_frozen_pack():
    assert suggestions([observation(topic_id='b-0c08bd84')]) == []


def test_one_drill_per_topic_even_when_base_and_probe_are_both_flagged():
    first = observation(id='o1', answer_id='a1')
    second = observation(id='o2', answer_id='a2')
    out = suggestions([first, second])
    assert len(out) == 1
    assert out[0]['observation_id'] == 'o1', 'the first stored observation keeps the drill'

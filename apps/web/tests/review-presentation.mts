// F07Z-143 / F07Z-144 presentation contract (spec §97, §101, §119-121).
// Pure functions only: no browser, no backend, no model. Run with
//
//   node --experimental-strip-types apps/web/tests/review-presentation.mts
import assert from 'node:assert/strict';
import {getInterviewReviewPresentation, reviewStopReason, REVIEW_COLLAPSE_THRESHOLD} from '../src/reviewPresentation.ts';
import {getGoalPresentation} from '../src/workbenchCopy.ts';
import {getAsyncActionCopy, type AsyncActionKind, type AsyncActionState} from '../src/asyncActionCopy.ts';
import {getApplicationEntryAction} from '../src/applicationEntry.ts';
import type {InterviewSession} from '../src/interviewContracts.ts';

let passed = 0;
const check = (name: string, run: () => void) => {
  run();
  passed += 1;
  console.log(`  ok ${passed}. ${name}`);
};

function session(overrides: Partial<InterviewSession> = {}): InterviewSession {
  return {
    id: 'sess-0001', version: 3, revision: 5, status: 'COMPLETED', goal_version: 2,
    policy: {role: 'interviewer', mode: 'interview', max_questions: 2, duration_seconds: 900},
    pack_version: 'p1', deadline_at: '2026-11-18T12:15:00Z',
    question_banks: [{bank_id: 'b1', version: 1, origin: 'builtin', enabled: true, revision: 1,
      content_sha256: 'x', name: 'Agent开发岗位试运行', item_count: 12}],
    events: [
      {id: 'e1', seq: 1, kind: 'question', payload: {topic_id: 't1', question_id: 'q1', text: 'LangGraph 的 state 是什么？', kind: 'open', pack_version: 'p1', assisted: false}},
      {id: 'e2', seq: 2, kind: 'answer', payload: {text: 'state 是图内共享的运行状态。', question_id: 'e1', topic_id: 't1', assisted: false}},
      {id: 'e3', seq: 3, kind: 'question', payload: {topic_id: 't2', question_id: 'q2', text: 'interrupt 如何恢复？', kind: 'open', pack_version: 'p1', assisted: false}},
      {id: 'e4', seq: 4, kind: 'answer', payload: {text: '用 Command(resume=...) 恢复。', question_id: 'e3', topic_id: 't2', assisted: false}},
    ],
    observations: [],
    report: {
      stop_reason: 'question_limit', model_summary: null, questions: 2, answers: 2, assisted_answers: 0,
      observations: [{
        id: 'o1', answer_id: 'a1', label: 'supported_in_this_answer', summary: '回答给出了 state 的作用。',
        answer_quote: 'state 是图内共享的运行状态。', evidence_id: 'doc#1', evidence_quote: 'State is shared memory.',
        origin: 'scripted_protocol',
      }],
      semantic_scoring_eligible: false, unreviewed_answer_ids: [], next_actions: [],
    },
    run: {id: 'r1', status: 'SUCCEEDED', error_code: null, model_requests: 0, origin: 'scripted_protocol',
      tool_log: [], diagnostic: null},
    ...overrides,
  };
}

function sessionWithAnswer(text: string): InterviewSession {
  const value = session();
  const answer = value.events.find(event => event.id === 'e2')!;
  value.events = value.events.map(event => event.id === 'e2'
    ? {...answer, payload: {...(answer.payload as Record<string, unknown>), text}} : event);
  return value;
}

// ---------------------------------------------------------------- review, scripted report
check('免费脚本报告：分层展示真实字段，状态只说「复盘可查看」', () => {
  const review = getInterviewReviewPresentation({session: session(), goalTitle: 'Agent 开发实习'});
  assert.equal(review.state, 'ready');
  assert.equal(review.mode, 'scripted');
  assert.equal(review.modeLabel, '免费脚本复盘');
  assert.match(review.modeNote, /仅整理作答记录/);
  assert.equal(review.statusLabel, '复盘可查看');
  assert.equal(review.goalTitle, 'Agent 开发实习');
  assert.equal(review.facts?.questions, 2);
  assert.equal(review.facts?.answers, 2);
  assert.equal(review.facts?.observations, 1);
  assert.equal(reviewStopReason(review.facts?.stopReason ?? null), '题数已达到本场上限');
  assert.equal(review.completion.title, '本次模拟面试已完成');
  assert.equal(review.completion.detail, '2 / 2 道题已作答');
});

check('用户作答逐题配对，明确标为「你的回答」，未作答不计成回答', () => {
  const review = getInterviewReviewPresentation({session: session()});
  assert.deepEqual(review.answers.map(item => [item.index, item.questionId, item.answer]), [
    [1, 'e1', 'state 是图内共享的运行状态。'],
    [2, 'e3', '用 Command(resume=...) 恢复。'],
  ]);
  assert.equal(review.unanswered, 0);
});

check('PART E：长回答只做视觉折叠，原文一个字都不删', () => {
  const long = '第一段结论与依据。'.repeat(40);
  assert.ok(long.length > REVIEW_COLLAPSE_THRESHOLD);
  const session = sessionWithAnswer(long);
  const review = getInterviewReviewPresentation({session});
  assert.equal(review.answers[0].answer, long, 'the full text must stay in the model');
  assert.equal(review.answers[0].fold, true);
  const short = sessionWithAnswer('两句话的回答。');
  assert.equal(getInterviewReviewPresentation({session: short}).answers[0].fold, false);
});

check('空白题（没有 answer 事件）显示为未作答，不被补写', () => {
  const noAnswer = session();
  noAnswer.events = noAnswer.events.filter(event => event.id !== 'e4');
  noAnswer.report = {...noAnswer.report!, answers: 1, unreviewed_answer_ids: ['a1']};
  const review = getInterviewReviewPresentation({session: noAnswer});
  assert.equal(review.answers[1].answer, null);
  assert.equal(review.unanswered, 1);
  assert.equal(review.facts?.unreviewed, 1);
});

check('陪练里标记为已辅导的回答被单独计数', () => {
  const coaching = session();
  coaching.events = coaching.events.map(event => event.id === 'e2'
    ? {...event, payload: {...(event.payload as Record<string, unknown>), assisted: true}} : event);
  coaching.report = {...coaching.report!, assisted_answers: 1};
  const review = getInterviewReviewPresentation({session: coaching});
  assert.equal(review.facts?.assisted, 1);
  assert.equal(review.answers[0].assisted, true);
  assert.equal(review.completion.detail, '2 / 2 道题已作答 · 1 条标记为已辅导');
});

// ---------------------------------------------------------------- AI report
check('AI 报告：明确标记「AI 复盘」，只有在存在模型输出时', () => {
  const live = session();
  live.run = {...live.run!, origin: 'real_model'};
  live.observations = [];
  live.report = {...live.report!, model_summary: '两次作答都给了依据，可以再补一个具体例子。',
    observations: [{...live.report!.observations[0], origin: 'real_model'}]};
  const review = getInterviewReviewPresentation({session: live});
  assert.equal(review.mode, 'ai');
  assert.equal(review.modeLabel, 'AI 复盘');
  assert.match(review.modeNote, /基于本次真实作答/);
});

check('脚本标记优先：带 scripted_protocol 时不会被说成 AI 复盘', () => {
  const mixed = session();
  mixed.report = {...mixed.report!, model_summary: '脚本写的一段综述'};
  const review = getInterviewReviewPresentation({session: mixed});
  assert.equal(review.mode, 'scripted');
});

// ---------------------------------------------------------------- no report / failed
check('无报告：显示「复盘正在准备」，不伪造处理状态，真实计数仍可见', () => {
  const pending = session({status: 'COMPLETED', report: null});
  const review = getInterviewReviewPresentation({session: pending});
  assert.equal(review.state, 'preparing');
  assert.equal(review.statusLabel, '复盘正在准备');
  assert.equal(review.facts?.questions, 2);
  assert.equal(review.answers.length, 2);
  assert.equal(review.observations.length, 0);
});

check('报告生成失败：说明作答已保存，并点明这版不能重新生成', () => {
  const failed = session({status: 'FAILED', report: null,
    run: {id: 'r1', status: 'FAILED', error_code: 'interview_failed', model_requests: 0,
      origin: 'scripted_protocol', tool_log: [], diagnostic: null}});
  const review = getInterviewReviewPresentation({session: failed});
  assert.equal(review.state, 'failed');
  assert.equal(review.statusLabel, '复盘生成失败');
  assert.equal(review.completion.title, '本场面试已结束');
});

// ---------------------------------------------------------------- next action + no scores
check('下一步：有岗位时先记录投递，其次另开一场（F07Z-142 §119 产品规则）', () => {
  const review = getInterviewReviewPresentation({session: session(), hasSelectedJob: true});
  assert.equal(review.next.kind, 'record-application');
  assert.equal(review.next.primary, '记录一次投递');
  assert.equal(review.next.secondary, '另开一场');
  assert.match(review.next.description, /投不投由你决定/);
});

check('F07Z-145：没有选岗时入口改成「选择岗位并记录投递」，不把用户送进会被拒绝的表单', () => {
  const review = getInterviewReviewPresentation({session: session(), hasSelectedJob: false});
  assert.equal(review.next.kind, 'select-position-first');
  assert.equal(review.next.primary, '选择岗位并记录投递');
  assert.match(review.next.description, /岗位研究/);
  assert.match(review.next.description, /选一个/);
  // Unknown is not "yes": the shell must confirm the fact before promising a record button.
  const unknown = getInterviewReviewPresentation({session: session()});
  assert.equal(unknown.next.kind, 'select-position-first');
});

check('岗位入口解析器只有两种答案，不猜岗位', () => {
  const withJob = getApplicationEntryAction({hasSelectedJob: true});
  assert.deepEqual([withJob.kind, withJob.label], ['record-application', '记录一次投递']);
  const withoutJob = getApplicationEntryAction({hasSelectedJob: false});
  assert.deepEqual([withoutJob.kind, withoutJob.label], ['select-position-first', '选择岗位并记录投递']);
  assert.match(withoutJob.description, /不会|由你/);
});

check('选岗前说明去岗位研究，选岗后不再提这一步', () => {
  const review = getInterviewReviewPresentation({session: session(), hasSelectedJob: false});
  assert.match(review.next.description, /岗位研究/);
  assert.equal(review.next.primary, '选择岗位并记录投递');
  const ready = getInterviewReviewPresentation({session: session(), hasSelectedJob: true});
  assert.doesNotMatch(ready.next.description, /岗位研究/);
});

check('展示层不出现评分、匹配度、通过概率或百分比', () => {
  const review = getInterviewReviewPresentation({session: session()});
  const text = JSON.stringify(review);
  for (const forbidden of ['能力评分', '分数', '匹配度', '通过概率', '%']) {
    assert.equal(text.includes(forbidden), false, `forbidden ${forbidden}`);
  }
});

check('没有任何会话时不编造题目或目标名', () => {
  const review = getInterviewReviewPresentation({session: null});
  assert.equal(review.goalTitle, null);
  assert.equal(review.facts, null);
  assert.equal(review.answers.length, 0);
  assert.equal(review.statusLabel, '复盘正在准备');
});

// ---------------------------------------------------------------- goal title (PART C)
check('目标标题：选了真实岗位就用岗位名，用户原句留在下面', () => {
  const goal = {conditions: {direction: '想找 AI 应用方向的实习', cities: ['上海']}, status: 'COMPLETED'};
  const view = getGoalPresentation(goal, 'AI 应用开发实习生');
  assert.equal(view.title, 'AI 应用开发实习生');
  assert.equal(view.source, 'job');
  assert.equal(view.detail, '想找 AI 应用方向的实习');
  assert.match(view.meta, /上海/);
  assert.match(view.meta, /目标已确认/);
});

check('目标标题：没有选岗时用用户自己的方向，不改写成岗位名', () => {
  const goal = {conditions: {direction: '想找 AI 应用方向的实习', cities: []}, status: 'COMPLETED'};
  const view = getGoalPresentation(goal, null);
  assert.equal(view.title, '想找 AI 应用方向的实习');
  assert.equal(view.source, 'direction');
  assert.equal(view.detail, null);
});

check('目标标题：方向也没有时保留「我的求职目标」，不伪造', () => {
  const goal = {conditions: {}, status: 'WAITING_FOR_USER'};
  const view = getGoalPresentation(goal, '   ');
  assert.equal(view.title, '我的求职目标');
  assert.equal(view.source, 'none');
  assert.equal(view.status, '还有信息需要确认');
});

// ---------------------------------------------------------------- async action copy (PART B)
const kinds: AsyncActionKind[] = ['goal-create', 'material-save', 'application-save'];
const states: AsyncActionState[] = ['idle', 'submitting', 'success', 'error'];

check('写操作四态：闲置文案是具体动作，提交中禁用态有「正在…」', () => {
  for (const kind of kinds) {
    const idle = getAsyncActionCopy(kind, 'idle');
    const submitting = getAsyncActionCopy(kind, 'submitting');
    assert.ok(idle.buttonLabel.length > 0, `${kind} idle label`);
    assert.notEqual(submitting.buttonLabel, idle.buttonLabel, `${kind} submitting label`);
    assert.match(submitting.buttonLabel, /^正在.+…$/, `${kind} submitting wording`);
    assert.match(submitting.feedbackBody, /禁用/, `${kind} submitting body`);
  }
});

check('成功反馈描述结果，不用「操作成功 / 请求成功」', () => {
  for (const kind of kinds) {
    const success = getAsyncActionCopy(kind, 'success');
    assert.ok(success.feedbackTitle.length > 0, `${kind} success title`);
    for (const vague of ['操作成功', '请求成功', '提交成功']) {
      assert.equal(success.feedbackTitle.includes(vague), false, `${kind} vague ${vague}`);
    }
  }
  assert.equal(getAsyncActionCopy('material-save', 'success', {name: '实习经历'}).feedbackTitle, '已保存「实习经历」');
  assert.equal(getAsyncActionCopy('application-save', 'success').feedbackTitle, '投递已记录为草稿');
});

check('错误反馈说明数据是否安全、用户可以做什么', () => {
  for (const kind of kinds) {
    const error = getAsyncActionCopy(kind, 'error');
    assert.match(error.feedbackBody, /(保留|没有变化|还在本机|仍在)/, `${kind} must say what survived`);
    assert.match(error.feedbackBody, /(重试|重新)/, `${kind} must offer a next step`);
  }
});

// ---------------------------------------------------------------- 收尾 01: retest suggestion
function sessionWithRetest(overrides: Partial<InterviewSession> = {}): InterviewSession {
  const value = session();
  value.report = {
    ...value.report!,
    retest: [{
      observation_id: 'o1', answer_id: 'a1', topic_id: 'http-idempotency',
      topic_title: 'HTTP语义与应用幂等',
      question: '支付接口已提交数据库却丢失响应，客户端重试；请设计请求身份、结果保存与冲突处理。',
      reference_url: 'https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2',
      pack_version: 'p1', assisted: false, reason: 'needs_probe_with_reference',
    }],
  };
  return {...value, ...overrides};
}

check('收尾 01：有出处的复测建议只在服务端解析出来时显示，逐字使用题库题干', () => {
  const review = getInterviewReviewPresentation({session: sessionWithRetest()});
  assert.equal(review.retests.length, 1);
  const [drill] = review.retests;
  assert.equal(drill.observationId, 'o1');
  assert.equal(drill.topicTitle, 'HTTP语义与应用幂等');
  assert.equal(drill.packVersion, 'p1');
  assert.equal(drill.assisted, false);
  assert.match(drill.question, /^支付接口已提交数据库却丢失响应/);
  assert.match(drill.referenceUrl, /^https:\/\//);
});

check('收尾 01：没有服务端建议时不得凭观察自己编题', () => {
  const review = getInterviewReviewPresentation({session: session()});
  assert.deepEqual(review.retests, []);
});

console.log(`${passed}/${passed} passed`);

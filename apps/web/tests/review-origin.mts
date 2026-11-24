// F07Z-162: the review shows where each question came from and which skill versions were frozen.
// Pure presentation test: no browser, no server.
import assert from 'node:assert/strict';
import {getInterviewReviewPresentation} from '../src/reviewPresentation.ts';

const session = {
  id: 's', version: 1, revision: 1, status: 'COMPLETED', goal_version: 1,
  policy: {role: 'java-backend', mode: 'interview', max_questions: 2, duration_seconds: 1200},
  pack_version: 'pack-v1', deadline_at: '', question_banks: [],
  skills: [{id: 'interviewer-selection', version: 1, name: '面试官选题与追问', origin: 'preinstalled'},
           {id: 'user:slow', version: 2, name: '慢查询排查', origin: 'user_install', author: '某同学'}],
  strategies: [{id: 'idempotency-failure', title: '重试与重复提交'}],
  events: [
    {id: 'q1', seq: 1, kind: 'question', payload: {topic_id: 'b-1', question_id: 'b-1-base',
      text: '慢查询怎么定位？', kind: 'base', pack_version: 'pack-v1', assisted: false,
      source: 'private_bank'}},
    {id: 'q2', seq: 3, kind: 'question', payload: {topic_id: 'project', question_id: 'project-contribution',
      text: '项目里你亲自做了什么？', kind: 'project', pack_version: 'pack-v1', assisted: false,
      source: 'project_material', strategy_id: 'idempotency-failure'}},
  ],
  observations: [],
  report: {stop_reason: 'agent_finished', model_summary: null, questions: 2, answers: 0,
    assisted_answers: 0, observations: [], semantic_scoring_eligible: false,
    unreviewed_answer_ids: [], next_actions: []},
  run: null,
};

const review = getInterviewReviewPresentation({session: session as never});
assert.equal(review.answers[0].source, 'private_bank');
assert.equal(review.answers[0].strategyId, null);
assert.equal(review.answers[1].source, 'project_material');
assert.equal(review.answers[1].strategyId, 'idempotency-failure');
assert.equal(review.answers[1].strategyTitle, '重试与重复提交');
assert.deepEqual(review.skills.map(item => `${item.id}@${item.version}`),
  ['interviewer-selection@1', 'user:slow@2']);
assert.equal(review.skills[1].origin, 'user_install');

// Older sessions carry neither field: the review must stay silent, not invent a source.
const legacy = {...session, skills: undefined,
  events: [{id: 'q1', seq: 1, kind: 'question',
    payload: {topic_id: 'sql-isolation', question_id: 'sql-isolation-base', text: '事务隔离？',
      kind: 'base', pack_version: 'pack-v1', assisted: false}}]};
const old = getInterviewReviewPresentation({session: legacy as never});
assert.equal(old.answers[0].source, null);
assert.equal(old.answers[0].strategyId, null);
assert.equal(old.answers[0].strategyTitle, null);
assert.deepEqual(old.skills, []);

console.log(JSON.stringify({status: 'passed', cases: 2}));

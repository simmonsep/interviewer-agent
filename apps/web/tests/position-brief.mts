// F07Z-151 unit contract: the Position Brief reads only what the snapshot and the checks contain.
// No invention, every requirement keeps its quote, skills are literal token hits, and "unknown"
// is an explicit outcome. Pure functions only.
//
//   node --experimental-strip-types apps/web/tests/position-brief.mts
import assert from 'node:assert/strict';
import {getPositionBrief, getBriefKeyPoints, type BriefItem, type BriefMatch} from '../src/positionBrief.ts';

let passed = 0;
const check = (name: string, run: () => void) => { run(); passed += 1; console.log(`  ok ${passed}. ${name}`); };

const manual: BriefItem = {
  snapshot_id: 's1', run_id: 'r1', source_id: 'user-jd', source_url: null,
  observed_at: '2026-11-18T08:00:00Z', content_sha256: 'abcdef0123456789',
  payload: {
    title: 'Java 后端开发实习', company: '合成科技', cities: ['上海'], recruitment_label: '',
    responsibilities: '', requirements: '',
    raw_text: '负责订单查询接口开发。\n熟悉 Java、Spring Boot、MySQL；了解 Redis 优先。',
    visible_text: '负责订单查询接口开发。\n熟悉 Java、Spring Boot、MySQL；了解 Redis 优先。',
    content_warnings: [],
  },
};

check('手工岗位：事实来自用户输入，技能来自原文词面匹配且带出处', () => {
  const brief = getPositionBrief({item: manual, match: null});
  assert.equal(brief.title, 'Java 后端开发实习');
  assert.equal(brief.company, '合成科技');
  assert.deepEqual(brief.facts.map(fact => fact.label), ['公司', '岗位', '城市', '招聘类型']);
  assert.equal(brief.facts[2].value, '上海');
  const names = brief.skills.map(hit => hit.name);
  for (const expected of ['Java', 'Spring Boot', 'MySQL', 'Redis']) {
    assert.ok(names.includes(expected), `skill_missing ${expected} in ${names.join(',')}`);
  }
  const java = brief.skills.find(hit => hit.name === 'Java');
  assert.ok(java && java.quote.includes('Java'), 'skill_hit_needs_a_quote');
});

check('没有推断：JD 没写的技术不会出现在技能词里', () => {
  const brief = getPositionBrief({item: manual, match: null});
  for (const invented of ['Kubernetes', '微服务', '高并发', 'RAG']) {
    assert.equal(brief.skills.some(hit => hit.name === invented), false, `invented_skill ${invented}`);
  }
});

check('未知项是显式的：未分栏、未官网核验、无链接、在招状态未确认', () => {
  const brief = getPositionBrief({item: manual, match: null});
  const unknowns = brief.unknowns.join('\n');
  assert.match(unknowns, /没有分栏/);
  assert.match(unknowns, /未做官网核验/);
  assert.match(unknowns, /没有可回访的原始链接/);
  assert.match(unknowns, /在招状态未确认/);
  assert.equal(brief.mustHave.length, 0);
  assert.equal(brief.niceToHave.length, 0);
});

check('抓取岗位：必须项 / 加分项来自后端的硬性标记，并保留引用', () => {
  const fetched: BriefItem = {
    ...manual,
    source_id: 'tme-campus', source_url: 'https://example.com/jobs/9', content_sha256: 'ffffffffffffaaaa',
    payload: {
      title: 'AI 应用开发实习生', company: '示例公司', cities: ['深圳'], recruitment_label: 'intern',
      responsibilities: '负责 Agent 应用开发与工具接入。',
      requirements: '熟悉 Python 与 LangGraph；有 RAG 经验者优先。',
      raw_text: '负责 Agent 应用开发与工具接入。熟悉 Python 与 LangGraph；有 RAG 经验者优先。',
      visible_text: '',
      content_warnings: ['在招状态未确认'],
    },
  };
  const match: BriefMatch = {
    snapshot_id: 's1', outcome: 'needs_verification',
    checks: [
      {field: 'skills', status: 'match', hard: true, source_quote: '熟悉 Python 与 LangGraph',
        note: '', user_ref: {field: 'skills', value: 'Python'}},
      {field: 'skills', status: 'match', hard: false, source_quote: '有 RAG 经验者优先',
        note: '', user_ref: {field: 'skills', value: 'RAG'}},
      {field: 'cities', status: 'unknown', hard: false, source_quote: null, note: '城市待核验'},
    ],
  };
  const brief = getPositionBrief({item: fetched, match});
  assert.equal(brief.mustHave.length, 1);
  assert.equal(brief.mustHave[0].value, 'Python', 'the line should name the skill, not repeat the quote');
  assert.match(brief.mustHave[0].quote ?? '', /LangGraph/);
  assert.equal(brief.niceToHave.length, 1, 'city checks are conditions, not requirements');
  assert.equal(brief.niceToHave[0].value, 'RAG');
  assert.equal([...brief.mustHave, ...brief.niceToHave].some(line => /城市/.test(line.value)), false,
    'a condition must never be listed as a requirement');
  assert.match(brief.unknowns.join('\n'), /仍需核验/);
  assert.equal(brief.sources.find(source => source.label === '来源')?.value, 'tme-campus');
  assert.equal(brief.sources.find(source => source.label === '快照指纹')?.value, 'ffffffffffff');
  assert.match(brief.responsibilities ?? '', /工具接入/);
});

check('没有 JD 时不伪造技能：显式说明没有匹配到已知技能词', () => {
  const empty: BriefItem = {...manual, payload: {...manual.payload, raw_text: '', visible_text: ''}};
  const brief = getPositionBrief({item: empty, match: null});
  assert.equal(brief.skills.length, 0);
  assert.match(brief.unknowns.join('\n'), /没有匹配到已知技能词/);
});

check('材料/面试的轻摘要与完整简报同源（最多 6 个技能词）', () => {
  const brief = getPositionBrief({item: manual, match: null});
  const points = getBriefKeyPoints(brief);
  assert.ok(points);
  assert.equal(points!.title, 'Java 后端开发实习');
  // Order follows the token list; "Spring" collapses into "Spring Boot" when both match.
  assert.deepEqual(points!.skills.slice(0, 3), ['Java', 'Spring Boot', 'MySQL']);
  assert.ok(points!.skills.includes('Redis'));
  assert.equal(points!.skills.includes('Spring'), false, 'Spring Boot should collapse the bare Spring hit');
  assert.equal(points!.skills.length <= 6, true);
  assert.equal(getBriefKeyPoints(null), null);
});

console.log(`${passed}/${passed} passed`);

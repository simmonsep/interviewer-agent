// F07Z-142 unit test: the career-progress model is pure, so it is checked without a browser,
// a database or the dev server. Every rule in it is derived from facts the panels already hold.
import assert from 'node:assert/strict';
import {getCareerProgress, shouldExplainOptionalStages, OPTIONAL_STAGE_HINT,
  type CareerProgressInput} from '../src/careerProgress.ts';

const base: CareerProgressInput = {
  hasGoal: true,
  materialCount: 0,
  draftCount: 0,
  completedSessions: 0,
  answeredQuestions: 0,
  applicationCount: 0,
  activeSession: false,
  waitingForUser: false,
};

const passed: string[] = [];
const check = (name: string, run: () => void) => { run(); passed.push(name); };

check('没有目标：当前阶段是目标，下一步是创建求职目标', () => {
  const progress = getCareerProgress({...base, hasGoal: false});
  assert.equal(progress.currentStage, 'goal');
  assert.equal(progress.nextAction.kind, 'create-goal');
  assert.equal(progress.nextAction.label, '创建求职目标');
  assert.deepEqual(progress.stages.map(stage => stage.state), ['current', 'upcoming', 'upcoming', 'upcoming', 'upcoming', 'upcoming']);
  assert.match(progress.summary, /还没有开始/);
});

check('有目标没有材料：下一步是添加材料，目标已标记完成', () => {
  const progress = getCareerProgress(base);
  assert.equal(progress.currentStage, 'materials');
  assert.equal(progress.nextAction.kind, 'add-materials');
  assert.equal(progress.stages[0].state, 'completed');
  assert.equal(progress.stages[1].state, 'current');
});

check('有材料没有初稿：下一步是继续完善简历，说明里带真实材料份数', () => {
  const progress = getCareerProgress({...base, materialCount: 3});
  assert.equal(progress.currentStage, 'resume');
  assert.equal(progress.nextAction.kind, 'continue-resume');
  assert.match(progress.nextAction.description, /3 份材料/);
  assert.match(progress.summary, /材料/);
});

check('有初稿：下一步是开始模拟面试', () => {
  const progress = getCareerProgress({...base, materialCount: 2, draftCount: 1});
  assert.equal(progress.currentStage, 'interview');
  assert.equal(progress.nextAction.kind, 'start-interview');
  assert.equal(progress.nextAction.label, '开始模拟面试');
});

check('面试已完成但还没有投递：下一步是查看复盘（复盘没有 reviewed 标记，不冒充已读）', () => {
  const progress = getCareerProgress({...base, materialCount: 2, draftCount: 1, completedSessions: 1, answeredQuestions: 12});
  assert.equal(progress.currentStage, 'review');
  assert.equal(progress.nextAction.kind, 'view-review');
  assert.match(progress.nextAction.description, /12 题/);
  assert.equal(progress.stages[4].state, 'current');
  assert.equal(progress.stages[5].state, 'upcoming');
});

check('已有投递：复盘与投递都算完成，下一步是继续记录投递', () => {
  const progress = getCareerProgress({...base, materialCount: 2, draftCount: 1, completedSessions: 1, answeredQuestions: 6, applicationCount: 2});
  assert.equal(progress.currentStage, 'application');
  assert.equal(progress.nextAction.kind, 'record-application');
  assert.match(progress.nextAction.description, /2 条投递/);
  assert.equal(progress.stages.every(stage => stage.state === 'completed'), true);
});

check('面试等你作答：interview 显示 attention，下一步是继续面试', () => {
  const progress = getCareerProgress({...base, materialCount: 1, draftCount: 1, activeSession: true, waitingForUser: true});
  assert.equal(progress.currentStage, 'interview');
  assert.equal(progress.nextAction.kind, 'continue-interview');
  assert.equal(progress.stages[3].state, 'attention');
  assert.match(progress.nextAction.description, /等你作答/);
});

check('面试进行中但不在等你：仍优先回到面试，状态是 current 不是 attention', () => {
  const progress = getCareerProgress({...base, materialCount: 1, draftCount: 1, activeSession: true, waitingForUser: false});
  assert.equal(progress.nextAction.kind, 'continue-interview');
  assert.equal(progress.stages[3].state, 'current');
  assert.match(progress.nextAction.description, /正在进行/);
});

check('跳过材料直接面试：完成后下一步仍是查看复盘，不把用户推进材料表单', () => {
  const progress = getCareerProgress({...base, completedSessions: 1, answeredQuestions: 2});
  assert.equal(progress.currentStage, 'review');
  assert.equal(progress.nextAction.kind, 'view-review');
  assert.equal(progress.stages[1].state, 'upcoming');
  assert.equal(progress.stages[3].state, 'completed');
  assert.equal(progress.stages[4].state, 'current');
});

check('F07Z-145：投递阶段的文案来自同一个解析器（没有岗位就先选岗）', () => {
  const withJob = getCareerProgress({...base, materialCount: 2, draftCount: 1, completedSessions: 1,
    applicationCount: 2, hasSelectedJob: true});
  assert.equal(withJob.nextAction.label, '记录一次投递');
  const withoutJob = getCareerProgress({...base, materialCount: 2, draftCount: 1, completedSessions: 1,
    applicationCount: 2, hasSelectedJob: false});
  assert.equal(withoutJob.nextAction.kind, 'record-application');
  assert.equal(withoutJob.nextAction.label, '选择岗位并记录投递');
});

check('PART D：只有真的出现“后段已完成、前段仍空”时才解释可选步骤', () => {
  const skipped = getCareerProgress({...base, completedSessions: 1, answeredQuestions: 2});
  assert.equal(shouldExplainOptionalStages(skipped), true);
  assert.equal(OPTIONAL_STAGE_HINT, '部分准备步骤可以按需跳过。');
  const linear = getCareerProgress({...base, materialCount: 2, draftCount: 1});
  assert.equal(shouldExplainOptionalStages(linear), false);
  const fresh = getCareerProgress(base);
  assert.equal(shouldExplainOptionalStages(fresh), false);
  // The copy never claims the user *chose* to skip anything.
  assert.doesNotMatch(OPTIONAL_STAGE_HINT, /你跳过/);
});

check('阶段顺序固定：目标 → 材料 → 简历 → 面试 → 复盘 → 投递', () => {
  const progress = getCareerProgress(base);
  assert.deepEqual(progress.stages.map(stage => stage.key), ['goal', 'materials', 'resume', 'interview', 'review', 'application']);
  assert.deepEqual(progress.stages.map(stage => stage.label), ['目标', '材料', '简历', '面试', '复盘', '投递']);
});

for (const name of passed) console.log('PASS', name);
console.log(`${passed.length}/${passed.length} passed`);

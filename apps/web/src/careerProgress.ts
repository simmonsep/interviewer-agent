// F07Z-142: a presentation-only model of the job-preparation chain. It is derived from the
// facts the panels already hold (goal, materials, drafts, sessions, applications) and it never
// replaces the interview/agent state machine. Two rules from the spec are kept deliberately:
//   * no threshold is invented (a stage flips on "存在" not on "至少 N 份");
//   * 复盘 has no persisted "reviewed" flag, so it stays current until a real application is
//     recorded - the UI never claims the user has read something it cannot observe.

import {getApplicationEntryAction} from './applicationEntry.ts';

export type CareerStage = 'goal' | 'materials' | 'resume' | 'interview' | 'review' | 'application';
export type StageState = 'completed' | 'current' | 'upcoming' | 'attention';

export interface CareerProgressInput {
  hasGoal: boolean;
  materialCount: number;
  draftCount: number;
  completedSessions: number;
  answeredQuestions: number;
  applicationCount: number;
  /** A session that is created but not finished (PENDING/RUNNING/WAITING_FOR_USER...). */
  activeSession: boolean;
  /** True when that active session is waiting for the user's own answer. */
  waitingForUser: boolean;
  /** F07Z-145: the server's answer to "is a job already bound to this goal?". */
  hasSelectedJob?: boolean;
}

export type NextActionKind =
  | 'create-goal'
  | 'add-materials'
  | 'continue-resume'
  | 'start-interview'
  | 'continue-interview'
  | 'view-review'
  | 'record-application';

export interface NextAction {
  kind: NextActionKind;
  label: string;
  description: string;
}

export interface CareerProgress {
  currentStage: CareerStage;
  stages: {key: CareerStage; label: string; state: StageState}[];
  nextAction: NextAction;
  summary: string;
}

/** F07Z-145 PART D: one sentence, only when a later stage is done while an earlier one is not. */
export const OPTIONAL_STAGE_HINT = '部分准备步骤可以按需跳过。';

/**
 * True only for the honest "skipped" shape (✓ ○ ○ ✓ ● ○). It never claims the user *chose* to
 * skip anything - the product has no skip event - so the copy stays about the steps, not the user.
 */
export function shouldExplainOptionalStages(progress: CareerProgress): boolean {
  const states = progress.stages.map(stage => stage.state);
  const lastCompleted = states.lastIndexOf('completed');
  if (lastCompleted < 0) return false;
  return states.slice(0, lastCompleted).some(state => state === 'upcoming');
}

const LABELS: Record<CareerStage, string> = {
  goal: '目标',
  materials: '材料',
  resume: '简历',
  interview: '面试',
  review: '复盘',
  application: '投递',
};

const ORDER: CareerStage[] = ['goal', 'materials', 'resume', 'interview', 'review', 'application'];

export function getCareerProgress(input: CareerProgressInput): CareerProgress {
  const done: Record<CareerStage, boolean> = {
    goal: input.hasGoal,
    materials: input.hasGoal && input.materialCount > 0,
    resume: input.materialCount > 0 && input.draftCount > 0,
    interview: input.completedSessions > 0,
    // A recorded application is the only observable proof that the user moved past 复盘.
    review: input.applicationCount > 0,
    application: input.applicationCount > 0,
  };

  // An unfinished session outranks everything else: the user has an interview on the table.
  const waiting = input.activeSession;
  // F07Z-143 (spec §22): the current stage is the first stage AFTER the furthest one already
  // reached, not the first unfinished one. A user may legitimately hold an interview without
  // materials ("不带简历直接开一场"), and in that case the step waiting for them is the review -
  // not a material form they deliberately skipped. Linear flows are unaffected.
  const frontier = ORDER.reduce((last, stage, index) => (done[stage] ? index : last), -1);
  let currentStage: CareerStage = ORDER[frontier + 1] ?? 'application';
  if (waiting) currentStage = 'interview';

  const stages = ORDER.map(key => {
    let state: StageState;
    if (waiting && key === 'interview') state = input.waitingForUser ? 'attention' : 'current';
    else if (done[key]) state = 'completed';
    else if (key === currentStage) state = 'current';
    else state = 'upcoming';
    return {key, label: LABELS[key], state};
  });

  const nextAction = actionFor(input, currentStage, done);
  const completed = ORDER.filter(stage => done[stage]).map(stage => LABELS[stage]);
  const summary = completed.length
    ? `已完成：${completed.join('、')} · 现在：${LABELS[currentStage]}`
    : '还没有开始，先创建求职目标';

  return {currentStage, stages, nextAction, summary};
}

function actionFor(
  input: CareerProgressInput,
  currentStage: CareerStage,
  done: Record<CareerStage, boolean>,
): NextAction {
  if (input.activeSession) {
    return {
      kind: 'continue-interview',
      label: '继续面试',
      description: input.waitingForUser
        ? '有一场面试在等你作答，答完这一题会继续。'
        : '有一场面试正在进行，回到面试页继续。',
    };
  }
  switch (currentStage) {
    case 'goal':
      return {
        kind: 'create-goal',
        label: '创建求职目标',
        description: '写一句求职意向即可开始，不需要先准备简历。',
      };
    case 'materials':
      return {
        kind: 'add-materials',
        label: '添加真实材料',
        description: '先加入已有简历、项目或实习经历；后续改写只会使用这些真实材料。',
      };
    case 'resume':
      return {
        kind: 'continue-resume',
        label: '继续完善简历',
        description: `已保存 ${input.materialCount} 份材料，可以基于它们生成第一版简历；不会编造未发生的事。`,
      };
    case 'interview':
      return {
        kind: 'start-interview',
        label: '开始模拟面试',
        description: input.draftCount > 0
          ? '简历已有初稿，建议先完成一次真实作答；出题后需要你本人回答。'
          : '可以不带简历直接开一场，先熟悉流程。',
      };
    case 'review':
      return {
        kind: 'view-review',
        label: '查看本次复盘',
        description: input.answeredQuestions > 0
          ? `面试已结束，你共作答 ${input.answeredQuestions} 题；复盘只依据这些真实作答。`
          : '面试已结束，复盘会依据你的真实作答记录生成。',
      };
    case 'application':
    default: {
      // F07Z-145: the same resolver the review uses. Without a selected job the workbench must
      // not push the user into a submission form that will refuse them.
      const entry = getApplicationEntryAction({hasSelectedJob: input.hasSelectedJob === true});
      return {
        kind: 'record-application',
        label: entry.label,
        description: input.applicationCount > 0
          ? `已有 ${input.applicationCount} 条投递记录，可以继续记录新的岗位。`
          : entry.kind === 'select-position-first'
            ? entry.description
            : '准备流程已完成，可以开始记录真实求职进度。',
      };
    }
  }
}

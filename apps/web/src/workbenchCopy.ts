// F07Z-141: presentation layer for the workbench. The business state machine is untouched:
// these two functions only turn the fields that already exist into words a job seeker reads.
// Keeping them pure makes the mapping testable without a browser or a backend.

export type GoalTone = 'neutral' | 'active' | 'warning' | 'success';

export interface GoalPresentation {
  /** The goal title as the user should see it (never an internal status value). */
  title: string;
  /** One readable sentence about where the goal stands. */
  status: string;
  /** City / type metadata followed by the status, or the "still deciding" sentence. */
  meta: string;
  /** F07Z-143: the user's own sentence, shown under a title that came from a real job. */
  detail: string | null;
  tone: GoalTone;
  /** Where the title came from, so the UI never has to guess whether it is confirmed. */
  source: 'job' | 'direction' | 'none';
}

interface GoalLike {
  conditions: { direction?: string; cities?: string[] };
  status: string;
}

const STATUS: Record<string, { status: string; tone: GoalTone }> = {
  PENDING: { status: '正在整理目标', tone: 'active' },
  RUNNING: { status: '正在整理目标', tone: 'active' },
  WAITING_FOR_USER: { status: '还有信息需要确认', tone: 'warning' },
  WAITING_FOR_TOOL: { status: '正在准备下一步', tone: 'active' },
  WAITING_CREDENTIALS: { status: '模型调用已暂停', tone: 'warning' },
  COMPLETED: { status: '目标已确认', tone: 'success' },
  SUCCEEDED: { status: '目标已确认', tone: 'success' },
  FAILED: { status: '这次整理没完成', tone: 'warning' },
  CANCELLED: { status: '已结束', tone: 'neutral' },
};

const NONE: GoalPresentation = {
  title: '我的求职目标',
  status: '正在确认方向',
  meta: '写一句求职意向即可开始，不需要先准备简历。',
  detail: null,
  tone: 'neutral',
  source: 'none',
};

/**
 * F07Z-143 / spec PART C: once a real job is selected on the server, that job title is the
 * target's name. A selected job is confirmed data; the user's own sentence stays visible
 * underneath instead of being rewritten into a job title we did not confirm.
 */
export function getGoalPresentation(goal: GoalLike | null, jobTitle?: string | null): GoalPresentation {
  if (!goal) return NONE;
  const direction = (goal.conditions.direction ?? '').trim();
  const job = (jobTitle ?? '').trim();
  const cities = (goal.conditions.cities ?? []).map(city => city.trim()).filter(Boolean);
  const mapped = STATUS[goal.status] ?? { status: '正在整理目标', tone: 'neutral' as GoalTone };
  if (job) {
    return {
      title: job,
      status: mapped.status,
      meta: [cities.join('、'), mapped.status].filter(Boolean).join(' · '),
      detail: direction && direction !== job ? direction : null,
      tone: mapped.tone,
      source: 'job',
    };
  }
  if (!direction && cities.length === 0) {
    return { title: '我的求职目标', status: mapped.status, meta: '求职方向正在确认中', detail: null,
      tone: mapped.tone, source: 'none' };
  }
  return {
    title: direction || '我的求职目标',
    status: mapped.status,
    meta: [cities.join('、'), mapped.status].filter(Boolean).join(' · '),
    detail: null,
    tone: mapped.tone,
    source: direction ? 'direction' : 'none',
  };
}

export type WorkbenchActionKind = 'create-goal' | 'prepare' | 'continue-interview' | 'open-session';

export interface WorkbenchAction {
  kind: WorkbenchActionKind;
  label: string;
}

/**
 * Which single action the workbench should push. Derived from data the panel already holds;
 * the caller maps `kind` back onto an existing handler, so no callback is stored in state.
 */
export function getWorkbenchPrimaryAction(input: {
  hasGoal: boolean;
  activeSession: boolean;
  sessionIsOpen: boolean;
}): WorkbenchAction {
  if (!input.hasGoal) return { kind: 'create-goal', label: '创建求职目标' };
  if (input.activeSession) {
    return input.sessionIsOpen
      ? { kind: 'continue-interview', label: '继续面试' }
      : { kind: 'open-session', label: '打开进行中的场次' };
  }
  return { kind: 'prepare', label: '开始面试' };
}

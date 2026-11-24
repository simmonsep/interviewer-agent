// F07Z-143: presentation model for one finished interview. It reads the session the API already
// returned and decides only what the review screen says. Three rules are deliberate:
//   * no score / match rate / pass probability - the scripted flow cannot measure ability;
//   * no invented facts (no duration, no date, no "reviewed" state) - only fields that exist;
//   * "复盘可查看" is the strongest claim available, because a report existing is not proof
//     that the user read it (there is no reviewed_at in the product yet).
import {stopReasonLabel,type AnswerPayload,type InterviewSession,type Observation,
  type QuestionPayload} from './interviewContracts.ts';
import {getApplicationEntryAction,type ApplicationEntryKind} from './applicationEntry.ts';

export type ReviewMode = 'scripted' | 'ai';
/** ready = a report exists; preparing = terminal but no report yet; failed = the run failed. */
export type ReviewState = 'ready' | 'preparing' | 'failed';

export interface ReviewAnswer {
  /** 1-based position in this session, not a score. */
  index: number;
  questionId: string;
  question: string;
  /** The user's own words, or null when this question was never answered. */
  answer: string | null;
  /** True when the answer was given in 陪练 after a topic was explained. */
  assisted: boolean;
  /** F07Z-145 PART E: display-only fold; the text itself is never shortened. */
  fold: boolean;
  /** F07Z-159/162: where the question came from, and the strategy card if one was used. */
  source: 'role_pack' | 'builtin_bank' | 'private_bank' | 'project_material' | null;
  strategyId: string | null;
  /** F07Z-163: the frozen card title, or null when the id is unknown/absent. */
  strategyTitle: string | null;
}

export interface ReviewFacts {
  questions: number;
  answers: number;
  assisted: number;
  observations: number;
  /** Answers the backend explicitly marked as not turned into an observation. */
  unreviewed: number;
  stopReason: string | null;
}

export interface ReviewNext {
  kind: ApplicationEntryKind;
  primary: string;
  secondary: string;
  description: string;
}

/** 收尾 01: one observation, resolved by the server to the authored retest question. */
export interface ReviewRetest {
  observationId: string;
  topicTitle: string;
  question: string;
  referenceUrl: string;
  packVersion: string;
  /** Coached answers never become an independent-ability claim. */
  assisted: boolean;
}

export interface ReviewPresentation {
  /** Real job title from the goal / selected job; null means the UI must not invent one. */
  goalTitle: string | null;
  mode: ReviewMode;
  modeLabel: string;
  modeNote: string;
  state: ReviewState;
  /** "复盘可查看" / "复盘正在准备" / "复盘生成失败" - never "复盘已完成" without a report. */
  statusLabel: string;
  completion: {title: string; detail: string};
  /** Present whenever a session is loaded, so the preparing state can still show real counts. */
  facts: ReviewFacts | null;
  answers: ReviewAnswer[];
  unanswered: number;
  modelSummary: string | null;
  observations: Observation[];
  /** 新增业务 1: what the interviewer actually took from the user's bank. */
  bankUsage: {bankQuestions: number; totalQuestions: number;
    declaration: 'used' | 'not_relevant' | 'unknown' | null;
    consistent: boolean | null} | null;
  /** Keyed by observation id; a suggestion only exists where the server resolved one. */
  retests: ReviewRetest[];
  /** F07Z-162: skill versions this session actually froze (no bodies). */
  skills: {id: string; version: number; name: string; origin: string}[];
  next: ReviewNext;
}

/** F07Z-145/149: the single display threshold; the data itself is never truncated. */
export const REVIEW_COLLAPSE_THRESHOLD = 320;

export const observationLabel: Record<string, string> = {
  needs_probe: '需继续追问',
  supported_in_this_answer: '本回答有依据',
  inconclusive: '暂无定论',
};

const SCRIPTED_NOTE = '本次复盘仅整理作答记录，不提供真实能力评价。';

function collectAnswers(session: InterviewSession | null): ReviewAnswer[] {
  if (!session) return [];
  const questions = session.events.filter(event => event.kind === 'question');
  const answers = new Map<string, AnswerPayload>();
  for (const event of session.events) {
    if (event.kind !== 'answer') continue;
    const payload = event.payload as AnswerPayload;
    // The backend writes the answer's question_id as the QUESTION EVENT id
    // (interviews/repository.py:267), not the bank item id in the payload.
    answers.set(payload.question_id, payload);
  }
  const strategyTitles = new Map<string, string>(
    (session?.strategies ?? []).map(item => [item.id, item.title]));
  return questions.map((event, index) => {
    const payload = event.payload as QuestionPayload;
    const given = answers.get(event.id) ?? answers.get(payload.question_id) ?? null;
    return {
      index: index + 1,
      questionId: event.id,
      question: payload.text,
      answer: given ? given.text : null,
      assisted: Boolean(given?.assisted),
      // A visual threshold, not a data cut: the full text stays in the DOM and behind the toggle.
      fold: Boolean(given && given.text.length > REVIEW_COLLAPSE_THRESHOLD),
      source: (payload.source as ReviewAnswer['source']) ?? null,
      strategyId: payload.strategy_id ?? null,
      strategyTitle: payload.strategy_id ? (strategyTitles.get(payload.strategy_id) ?? null) : null,
    };
  });
}

/** Conservative: only claim AI when there is model output and no scripted marker. */
function reviewMode(session: InterviewSession | null): ReviewMode {
  if (!session) return 'scripted';
  const observations = session.report?.observations ?? session.observations ?? [];
  const scriptedEvidence = session.run?.origin === 'scripted_protocol'
    || observations.some(item => item.origin === 'scripted_protocol');
  const modelEvidence = session.run?.origin === 'real_model'
    || observations.some(item => item.origin === 'real_model')
    || Boolean(session.report?.model_summary);
  return !scriptedEvidence && modelEvidence ? 'ai' : 'scripted';
}

export function getInterviewReviewPresentation(input: {
  session: InterviewSession | null;
  goalTitle?: string | null;
  /** True when the server already has a selected job; a投递 must be bound to one. */
  hasSelectedJob?: boolean;}): ReviewPresentation {
  const session = input.session;
  const report = session?.report ?? null;
  const answers = collectAnswers(session);
  const unanswered = answers.filter(item => item.answer === null).length;
  const mode = reviewMode(session);
  const state: ReviewState = report ? 'ready' : session?.status === 'FAILED' ? 'failed' : 'preparing';

  const questions = report?.questions ?? answers.length;
  const answered = report?.answers ?? answers.filter(item => item.answer !== null).length;
  const assisted = report?.assisted_answers ?? answers.filter(item => item.assisted).length;
  const facts: ReviewFacts | null = session ? {
    questions,
    answers: answered,
    assisted,
    observations: report?.observations.length ?? 0,
    unreviewed: report?.unreviewed_answer_ids.length ?? 0,
    stopReason: report?.stop_reason ?? null,
  } : null;

  const stateTitle = state === 'ready' ? '复盘可查看'
    : state === 'failed' ? '复盘生成失败' : '复盘正在准备';
  const stateBody = state === 'ready' ? '以下内容来自本次场次的真实记录。'
    : state === 'failed'
      ? '你的面试作答已经保存，不会丢失。这版还不能重新生成复盘：报告由服务端在本场结束时生成。'
      : '你的作答记录已经保存，报告还没写到这里；可以先看下面的作答原文，稍后点「刷新状态」再回读。';

  const completionTitle = state === 'failed' ? '本场面试已结束' : '本次模拟面试已完成';
  const detailParts = [`${answered} / ${questions} 道题已作答`];
  if (assisted > 0) detailParts.push(`${assisted} 条标记为已辅导`);

  return {
    goalTitle: (input.goalTitle ?? '').trim() || null,
    mode,
    modeLabel: mode === 'ai' ? 'AI 复盘' : '免费脚本复盘',
    modeNote: mode === 'ai' ? '基于本次真实作答的 AI 复盘。' : SCRIPTED_NOTE,
    state,
    statusLabel: stateTitle,
    completion: {title: completionTitle, detail: detailParts.join(' · ')},
    facts,
    answers,
    unanswered,
    modelSummary: report?.model_summary ?? null,
    observations: report?.observations ?? [],
    bankUsage: report?.bank_usage
      ? {bankQuestions: report.bank_usage.bank_questions,
         totalQuestions: report.bank_usage.total_questions,
         declaration: report.bank_usage.declaration ?? null,
         // Derived on the client, because the server does not report it: a 'used' claim with zero
         // bank questions is inconsistent, and so is 'not_relevant' while bank questions were asked.
         // The observed count stays the fact.
         consistent: (report.bank_usage.declaration ?? null) === null
           ? null
           : (report.bank_usage.declaration === 'used') === (report.bank_usage.bank_questions > 0)}
      : null,
    retests: (report?.retest ?? []).map(item => ({
      observationId: item.observation_id,
      topicTitle: item.topic_title,
      question: item.question,
      referenceUrl: item.reference_url,
      packVersion: item.pack_version,
      assisted: item.assisted,
    })),
    skills: (session?.skills ?? []).map(item => ({
      id: item.id, version: item.version, name: item.name, origin: item.origin ?? 'preinstalled',
    })),
    next: (() => {
      // F07Z-145: one resolver decides the label and the destination; this view only supplies
      // the fact it needs (is a job already selected?) and the wording for a pending report.
      const entry = getApplicationEntryAction({hasSelectedJob: input.hasSelectedJob === true});
      return {
        kind: entry.kind,
        primary: entry.label,
        secondary: '另开一场',
        description: entry.kind === 'select-position-first'
          ? entry.description
          : state === 'ready'
            ? entry.description
            : '报告还在准备时也可以先记录已经发生的投递；它和复盘是两件事。',
      };
    })(),
  };
}

/** Human wording for the end reason, kept here so the review never prints a raw code. */
export function reviewStopReason(reason: string | null): string {
  if (!reason) return '未记录';
  return stopReasonLabel[reason] ?? reason;
}

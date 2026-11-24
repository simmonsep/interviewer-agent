// F07Z-144: one vocabulary for write actions. Pure presentation only - it never fetches,
// never writes storage and never decides whether an action is allowed. Each call site keeps
// its own handler and its own busy lock; this helper only makes the four visible states say
// the same kinds of things everywhere (specific verb / "正在…" + disabled / result worded as
// what happened / error that says whether the input survived).
export type AsyncActionState = 'idle' | 'submitting' | 'success' | 'error';

export type AsyncActionKind =
  | 'goal-create'
  | 'goal-update'
  | 'material-save'
  | 'material-import'
  | 'resume-generate'
  | 'interview-start'
  | 'answer-submit'
  | 'application-save';

export interface AsyncActionCopy {
  buttonLabel: string;
  feedbackTitle: string;
  feedbackBody: string;
}

interface Detail {name?: string}

const BASE: Record<AsyncActionKind, AsyncActionCopy> = {
  'goal-create': {
    buttonLabel: '创建求职目标',
    feedbackTitle: '目标已创建',
    feedbackBody: '下一步：添加你的真实材料。',
  },
  'goal-update': {
    buttonLabel: '保存目标条件',
    feedbackTitle: '目标条件已保存',
    feedbackBody: '新条件会用于后续的岗位研究与出题。',
  },
  'material-save': {
    buttonLabel: '保存这段材料',
    feedbackTitle: '材料已保存',
    feedbackBody: '下一步：在「分析与初稿」里生成简历初稿。',
  },
  'material-import': {
    buttonLabel: '导入为材料',
    feedbackTitle: '材料已导入',
    feedbackBody: '导入的是你上传原文里的文字，可以逐句核对。',
  },
  'resume-generate': {
    buttonLabel: '生成简历初稿',
    feedbackTitle: '简历初稿已生成',
    feedbackBody: '初稿需要你逐句审阅，旧版本仍保留。',
  },
  'interview-start': {
    buttonLabel: '开始整场面试',
    feedbackTitle: '面试已开始',
    feedbackBody: '出题后需要你本人作答，考中不看提示。',
  },
  'answer-submit': {
    buttonLabel: '提交这一答',
    feedbackTitle: '这一答已保存',
    feedbackBody: '已写入本场记录，会用于本场结束后的复盘。',
  },
  'application-save': {
    buttonLabel: '记录为草稿',
    feedbackTitle: '投递已记录为草稿',
    feedbackBody: '状态变化请手动更新；之后可以在投递详情里补记录。',
  },
};

const SUBMITTING: Record<AsyncActionKind, string> = {
  'goal-create': '正在创建…',
  'goal-update': '正在保存…',
  'material-save': '正在保存…',
  'material-import': '正在导入…',
  'resume-generate': '正在生成…',
  'interview-start': '正在开场…',
  'answer-submit': '正在提交…',
  'application-save': '正在记录…',
};

const ERROR: Record<AsyncActionKind, string> = {
  'goal-create': '目标没有创建成功。你写的内容仍保留在页面上，可以稍后重试。',
  'goal-update': '目标条件没有保存成功。你改的内容仍保留在页面上，可以稍后重试。',
  'material-save': '材料保存失败。你输入的内容还保留在页面上，可以稍后重新保存。',
  'material-import': '文件导入失败。文件还在本机，选中后可以重新导入。',
  'resume-generate': '简历初稿没有生成。原材料没有变化，可以重试。',
  'interview-start': '面试没有开始。已选的题库和模式仍在，可以重试。',
  'answer-submit': '这一答没有保存。你的输入仍保留，请先刷新状态核对。',
  'application-save': '投递没有记录成功。公司、职位和链接仍保留，可以稍后重试。',
};

/**
 * Copy for one action in one state. `detail.name` only personalises the success title
 * ("已保存「实习经历」"); it never changes the state machine.
 */
export function getAsyncActionCopy(
  kind: AsyncActionKind,
  state: AsyncActionState,
  detail: Detail = {},
): AsyncActionCopy {
  const base = BASE[kind];
  switch (state) {
    case 'idle':
      return base;
    case 'submitting':
      return {
        buttonLabel: SUBMITTING[kind],
        feedbackTitle: '',
        feedbackBody: '按钮已禁用，避免重复提交。',
      };
    case 'success': {
      const name = (detail.name ?? '').trim();
      const titled = name && (kind === 'material-save' || kind === 'material-import')
        ? `已保存「${name}」`
        : base.feedbackTitle;
      return {buttonLabel: base.buttonLabel, feedbackTitle: titled, feedbackBody: base.feedbackBody};
    }
    case 'error':
    default:
      return {buttonLabel: base.buttonLabel, feedbackTitle: '操作没有完成', feedbackBody: ERROR[kind]};
  }
}

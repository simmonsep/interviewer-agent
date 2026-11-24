// F07Z-145: where "记录一次投递" should go depends on ONE existing fact - whether the goal is
// already bound to a selected job (applications are enforced server-side to need one). This is
// presentation only: it reads a boolean the panels already know, never fetches, never selects a
// job for the user and never changes the workflow. Both the review and the workbench use it so
// the two surfaces cannot drift apart.

export type ApplicationEntryKind = 'record-application' | 'select-position-first';

export interface ApplicationEntryAction {
  kind: ApplicationEntryKind;
  label: string;
  description: string;
}

const RECORD: ApplicationEntryAction = {
  kind: 'record-application',
  label: '记录一次投递',
  description: '复盘只是记录，投不投由你决定。记录一次真实投递后，工作台会把进度推进到「投递」。',
};

const SELECT_FIRST: ApplicationEntryAction = {
  kind: 'select-position-first',
  label: '选择岗位并记录投递',
  description: '记录投递前需要先有一个具体岗位。先到「岗位研究」选一个，选好后这里会出现「记录一次投递」；是否投递由你自己决定。',
};

/**
 * `hasSelectedJob` must be the server's own answer (there is no "maybe"): unknown is treated as
 * "not yet", because sending the user to a form that will refuse the submission is the bug this
 * function exists to prevent.
 */
export function getApplicationEntryAction(input: {hasSelectedJob: boolean}): ApplicationEntryAction {
  return input.hasSelectedJob ? RECORD : SELECT_FIRST;
}

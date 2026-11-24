// F07Z-151: the Position Brief. One pure reading of a job snapshot the server already stores:
// what the posting literally says, which requirements are hard vs. nice-to-have (the checks the
// backend already computed), which words actually appear in the JD, and what is still unknown.
//
// Rules it must never break:
//   * no invention - a line exists only if the snapshot or a check contains it;
//   * every requirement carries its source quote when the backend has one;
//   * skills come from a literal token match on the JD text, not from a judgement about level;
//   * "unknown" is a real outcome, not an empty string.

export interface BriefCheck {
  field: string;
  status: string;
  hard: boolean;
  source_quote: string | null;
  note?: string;
  /** What the user declared for this field (the skill name, the city, ...). */
  user_ref?: {field: string; value: unknown};
}

export interface BriefItem {
  snapshot_id: string;
  run_id: string;
  source_id: string;
  source_url: string | null;
  observed_at: string;
  content_sha256: string;
  payload: {
    title: string;
    company: string | null;
    cities: string[];
    recruitment_label?: string | null;
    responsibilities?: string;
    requirements?: string;
    raw_text?: string;
    visible_text?: string;
    content_warnings?: string[];
  };
}

export interface BriefMatch {
  snapshot_id: string;
  outcome: string;
  checks: BriefCheck[];
}

export interface BriefLine {
  label: string;
  value: string;
  quote: string | null;
}

export interface SkillHit {
  name: string;
  quote: string;
}

export interface PositionBrief {
  title: string;
  company: string | null;
  facts: {label: string; value: string}[];
  mustHave: BriefLine[];
  niceToHave: BriefLine[];
  skills: SkillHit[];
  unknowns: string[];
  sources: {label: string; value: string}[];
  rawJd: string;
  responsibilities: string | null;
  requirements: string | null;
  warnings: string[];
}

// Common technology words the product can recognise *literally*. A hit only means "this token
// appears in the JD"; it says nothing about the user or their level.
const SKILL_TOKENS: {name: string; token: string}[] = [
  {name: 'Java', token: 'java'},
  {name: 'Python', token: 'python'},
  {name: 'TypeScript', token: 'typescript'},
  {name: 'JavaScript', token: 'javascript'},
  {name: 'React', token: 'react'},
  {name: 'Node.js', token: 'node.js'},
  {name: 'Spring Boot', token: 'spring boot'},
  {name: 'Spring', token: 'spring'},
  {name: 'MySQL', token: 'mysql'},
  {name: 'PostgreSQL', token: 'postgresql'},
  {name: 'Redis', token: 'redis'},
  {name: 'Kafka', token: 'kafka'},
  {name: 'Docker', token: 'docker'},
  {name: 'Kubernetes', token: 'kubernetes'},
  {name: 'Linux', token: 'linux'},
  {name: 'Git', token: 'git'},
  {name: 'SQL', token: 'sql'},
  {name: 'LangGraph', token: 'langgraph'},
  {name: 'LangChain', token: 'langchain'},
  {name: 'Agent', token: 'agent'},
  {name: 'RAG', token: 'rag'},
  {name: '分布式', token: '分布式'},
  {name: '微服务', token: '微服务'},
  {name: '高并发', token: '高并发'},
  {name: '算法', token: '算法'},
  {name: '数据结构', token: '数据结构'},
];

function sentences(text: string): string[] {
  return text
    .split(/(?<=[。！？!?])|\n+/)
    .map(part => part.trim())
    .filter(Boolean);
}

/** The first sentence that contains the token, so a hit can always be checked by eye. */
function quoteFor(text: string, token: string): string {
  const lower = text.toLowerCase();
  const needle = token.toLowerCase();
  const at = lower.indexOf(needle);
  if (at < 0) return '';
  const sentence = sentences(text).find(part => part.toLowerCase().includes(needle)) ?? text;
  const start = Math.max(0, sentence.toLowerCase().indexOf(needle) - 40);
  return sentence.slice(start, start + 160).trim();
}

function recruitmentText(item: BriefItem): string {
  const label = item.payload.recruitment_label?.trim();
  if (!label) return '未标注';
  return label;
}

export function getPositionBrief(input: {item: BriefItem; match: BriefMatch | null}): PositionBrief {
  const {item, match} = input;
  const payload = item.payload;
  const jd = (payload.visible_text || payload.raw_text || '').trim();
  const checks = match?.checks ?? [];

  const requirements = payload.requirements?.trim() || null;
  const responsibilities = payload.responsibilities?.trim() || null;

  // Only requirement-like checks belong in 岗位要求. City / recruitment / availability are
  // conditions the product verifies, not things the posting demands; listing them here read as
  // if the JD required "本来源仅证明详情可读".
  const requirementChecks = checks.filter(check => ['skills', 'experience', 'education'].includes(check.field));
  const lineValue = (check: BriefCheck): string => {
    const declared = check.user_ref?.value;
    if (Array.isArray(declared) && declared.length) return declared.join('、');
    if (typeof declared === 'string' && declared.trim()) return declared.trim();
    // The backend's note for a skill is "<skill>: <explanation>"; the name is the useful part.
    const note = check.note?.trim() ?? '';
    const head = note.split(/[:：]/)[0]?.trim() ?? '';
    if (head && head.length <= 24) return head;
    return note || (check.field === 'skills' ? '技能' : check.field);
  };
  const toLine = (check: BriefCheck): BriefLine => ({
    label: check.field === 'skills' ? '技能' : check.field,
    value: lineValue(check),
    quote: check.source_quote?.trim() || null,
  });
  const unique = (lines: BriefLine[]) => lines.filter((line, index) =>
    lines.findIndex(other => other.label === line.label && other.value === line.value) === index);
  // Hard vs. nice-to-have comes from the backend's own flags; nothing is promoted here.
  const mustHave = unique(requirementChecks.filter(check => check.hard).map(toLine));
  const niceToHave = unique(requirementChecks.filter(check => !check.hard).map(toLine));

  // Literal token hits in the JD text (requirements first if the source split them out).
  const skillSource = [requirements, jd].filter(Boolean).join('\n');
  const skills: SkillHit[] = [];
  for (const {name, token} of SKILL_TOKENS) {
    const quote = quoteFor(skillSource, token);
    if (quote && !skills.some(hit => hit.name === name)) skills.push({name, quote});
  }

  // "Spring" is a prefix of "Spring Boot"; when the specific product is named, keep only that.
  const filtered = skills.filter(hit => hit.name !== 'Spring' || !skills.some(other => other.name === 'Spring Boot'));
  skills.length = 0;
  skills.push(...filtered);

  const unknowns: string[] = [];
  if (!requirements && !responsibilities) unknowns.push('原文没有分栏的「职责 / 要求」，只能按 JD 原文阅读');
  if (skills.length === 0) unknowns.push('JD 中没有匹配到已知技能词（不代表没有要求）');
  if (/unknown/i.test(payload.recruitment_label ?? '') || !payload.recruitment_label) unknowns.push('招聘类型未标注');
  if (match?.outcome === 'needs_verification') unknowns.push('城市或招聘类型仍需核验');
  if (item.source_id === 'user-jd') unknowns.push('来源为用户输入，未做官网核验');
  if (!item.source_url) unknowns.push('没有可回访的原始链接');
  // No snapshot field says "still open", so this stays unknown even when a recruitment label
  // exists: the label describes the type of posting, not whether it is open today.
  unknowns.push('在招状态未确认');

  return {
    title: payload.title,
    company: payload.company,
    facts: [
      {label: '公司', value: payload.company?.trim() || '未提供'},
      {label: '岗位', value: payload.title},
      {label: '城市', value: payload.cities?.length ? payload.cities.join('、') : '未提供'},
      {label: '招聘类型', value: recruitmentText(item)},
    ],
    mustHave,
    niceToHave,
    skills,
    unknowns,
    sources: [
      {label: '来源', value: item.source_id === 'user-jd' ? '用户输入' : item.source_id},
      {label: '链接', value: item.source_url || '无'},
      {label: '读取时间', value: item.observed_at},
      {label: '快照指纹', value: item.content_sha256.slice(0, 12)},
    ],
    rawJd: jd,
    responsibilities,
    requirements,
    warnings: payload.content_warnings ?? [],
  };
}

/** The one or two lines the materials / interview surfaces show. Same source as the full brief. */
export function getBriefKeyPoints(brief: PositionBrief | null): {title: string; skills: string[]} | null {
  if (!brief) return null;
  return {title: brief.title, skills: brief.skills.slice(0, 6).map(hit => hit.name)};
}

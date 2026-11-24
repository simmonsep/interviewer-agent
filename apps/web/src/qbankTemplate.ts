// 新增业务 1 · 节点 A1: one Markdown document -> the question-bank JSON the product already
// imports. Pure and deterministic: the panel shows the preview, the user confirms, and the
// existing import/commit path stores a new version.
//
// Format (deliberately small; one `## 标题` per question):
//
//   # 题库名称
//   ## 标题
//   题干：必填
//   参考：可选
//   追问：可选
//   复测：可选
//   来源：https://...（可选）
//   说明：来源说明（可选）
//   标签：Python, LangGraph（可选）
//
// Nothing here scores or rewrites a question; a missing reference only produces a note, because a
// good question without an answer is still a good question.

export type BankRole = 'java-backend' | 'ai-application';

export interface TemplateProblem {
  /** 1-based line in the submitted document, pointing at the user's own text. */
  line: number;
  code: 'missing_title' | 'missing_question' | 'duplicate_title' | 'too_many_items'
    | 'question_too_long' | 'reference_too_long' | 'no_items';
  message: string;
}

export interface TemplateQuestion {
  item_id: string;
  kind: 'knowledge';
  roles: BankRole[];
  title: string;
  question: string;
  reference: string;
  probe: string;
  retest: string;
  source_url: string;
  source_note: string;
  tags: string[];
}

export interface TemplateDocument {
  schema_version: 1;
  name: string;
  items: TemplateQuestion[];
}

export interface TemplatePreview {
  document: TemplateDocument;
  problems: TemplateProblem[];
  /** Questions that are usable as-is; a missing reference is not one of the problems. */
  withoutReference: number;
}

export const MAX_ITEMS = 50;
const MAX_QUESTION = 1000;
const MAX_REFERENCE = 1200;

const FIELD_PREFIX: Record<string, keyof TemplateQuestion | 'tags'> = {
  题干: 'question', 参考: 'reference', 追问: 'probe', 复测: 'retest',
  来源: 'source_url', 说明: 'source_note', 标签: 'tags',
};

export function questionBankTemplate(name = '我的岗位题库'): string {
  return `# ${name}

> 一个 \`## 标题\` 对应一题；题干必填，其余可空。保存前会先预览。

## 示例：LangGraph 的 state 与 checkpointer 有什么区别？
题干：同一个用户的多个独立会话，state、checkpointer 和 store 各自负责什么？
参考：（可空。正确的题没有参考也可以出；没有参考时不会判你答得对不对）
追问：如果进程重启后要接着聊，需要满足什么条件？
复测：并发两个会话写入同一 thread_id 会发生什么？
来源：https://docs.langchain.com/oss/python/langgraph/persistence
说明：依据官方文档自拟，非官方题库。
标签：Python, LangGraph
`;
}

function slug(index: number): string {
  return `q-${index}`;
}

/** Parse the template. Problems never throw: the panel decides what to show and what to block. */
export function parseQuestionBankTemplate(text: string, role: BankRole, name?: string): TemplatePreview {
  const problems: TemplateProblem[] = [];
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/);
  const heading = lines.find(line => /^#\s+\S/.test(line))?.replace(/^#\s+/, '').trim();

  const items: TemplateQuestion[] = [];
  const seenTitles = new Map<string, number>();
  let current: {line: number; title: string; fields: Record<string, string>} | null = null;

  const flush = () => {
    if (!current) return;
    const question = (current.fields['question'] ?? '').trim();
    const title = current.title.trim();
    if (!title) problems.push({line: current.line, code: 'missing_title', message: '这一题的标题是空的'});
    if (!question) {
      problems.push({line: current.line, code: 'missing_question', message: `「${title || '未命名'}」缺少题干（题干：…）`});
    }
    if (question.length > MAX_QUESTION) {
      problems.push({line: current.line, code: 'question_too_long', message: `题干超过 ${MAX_QUESTION} 字`});
    }
    const reference = (current.fields['reference'] ?? '').trim();
    if (reference.length > MAX_REFERENCE) {
      problems.push({line: current.line, code: 'reference_too_long', message: `参考超过 ${MAX_REFERENCE} 字`});
    }
    if (!title || !question) {
      current = null;
      return;
    }
    if (seenTitles.has(title)) {
      problems.push({line: current.line, code: 'duplicate_title',
        message: `「${title}」与第 ${seenTitles.get(title)} 行重名，导入前请改名`});
      current = null;
      return;
    }
    seenTitles.set(title, current.line);
    items.push({
      item_id: slug(items.length + 1),
      kind: 'knowledge',
      roles: [role],
      title,
      question,
      reference,
      probe: (current.fields['probe'] ?? '').trim(),
      retest: (current.fields['retest'] ?? '').trim(),
      source_url: (current.fields['source_url'] ?? '').trim(),
      source_note: (current.fields['source_note'] ?? '').trim(),
      tags: (current.fields['tags'] ?? '').split(/[,，、]/).map(tag => tag.trim()).filter(Boolean).slice(0, 8),
    });
    current = null;
  };

  for (const [index, raw] of lines.entries()) {
    const line = raw.trim();
    const lineNumber = index + 1;
    if (!line || line.startsWith('>')) continue;
    if (/^##\s+/.test(line)) {
      flush();
      current = {line: lineNumber, title: line.replace(/^##\s+/, ''), fields: {}};
      continue;
    }
    if (/^#\s+/.test(line)) continue;
    if (!current) continue;
    const match = line.match(/^([^：:]{1,4})[：:]\s*(.*)$/);
    if (!match) continue;
    const key = FIELD_PREFIX[match[1].trim()];
    if (!key) continue;
    // Multi-line values are not part of this small format: each instruction is one line.
    current.fields[key] = match[2];
  }
  flush();

  if (items.length > MAX_ITEMS) {
    problems.push({line: 1, code: 'too_many_items', message: `一题一行，最多 ${MAX_ITEMS} 题，请拆分文档`});
  }
  if (items.length === 0 && problems.length === 0) {
    problems.push({line: 1, code: 'no_items', message: '没有识别到任何题目：请用一个 ## 标题 开始一题'});
  }

  const withoutReference = items.filter(item => !item.reference).length;
  return {
    document: {
      schema_version: 1,
      name: (name ?? heading ?? '我的岗位题库').trim().slice(0, 80) || '我的岗位题库',
      items: items.slice(0, MAX_ITEMS),
    },
    problems,
    withoutReference,
  };
}

/** Title of a `### 组名（N 题 · P0）` heading, with the counts stripped. */
function groupTitle(heading: string): string {
  return heading.replace(/^###\s+/, '')
    .replace(/[（(]\s*\d+\s*题[^）)]*[）)]\s*$/, '')
    .trim()
    .slice(0, 80);
}

export interface StructuredDocument {
  document: TemplateDocument;
  problems: TemplateProblem[];
  withoutReference: number;
  /** Groups with a 主问题 became items; groups without one are reported, not guessed. */
  skippedGroups: number;
}

/**
 * Parse the "topic-group / main question / variants" structure used by curated interview banks.
 * No answer is invented: these documents ship questions, so `reference` stays empty and the
 * review is told there is nothing to judge against (plan §1.1: a good question without an answer
 * is still a good question).
 */
export function parseStructuredQuestionBank(text: string, role: BankRole, name?: string): StructuredDocument {
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/);
  const heading = lines.find(line => /^#\s+\S/.test(line))?.replace(/^#\s+/, '').trim();
  const problems: TemplateProblem[] = [];
  const items: TemplateQuestion[] = [];
  const seen = new Map<string, number>();

  // Two passes: collect every group (title + 主问题 + variant count), then build items. Variants
  // follow the main question, so counting them inside the same pass would always come out zero.
  interface Group { line: number; title: string; category: string; main: string | null; variants: number }
  const groups: Group[] = [];
  let category = '';
  let current: Group | null = null;
  const open = () => current ? groups.push(current) : undefined;

  for (const [index, raw] of lines.entries()) {
    const line = raw.trim();
    if (!line || line.startsWith('>')) continue;
    if (/^##\s+/.test(line)) { open(); current = null; category = line.replace(/^##\s+/, '').trim(); continue; }
    if (/^###\s+/.test(line)) {
      open();
      current = {line: index + 1, title: groupTitle(line), category, main: null, variants: 0};
      continue;
    }
    if (!current) continue;
    const main = line.match(/^\*\*主问题\*\*\s*(.*)$/);
    if (main) { current.main = main[1]; continue; }
    if (/^[-*]\s*\[/.test(line)) { current.variants += 1; continue; }
  }
  open();

  for (const group of groups) {
    if (!group.main) continue;  // reported below as a skipped group, never guessed
    const source = group.main.match(/^\[([^\]]+)\]\s*(.+)$/s);
    const sourceTag = source?.[1] ?? '';
    const body = (source?.[2] ?? group.main).trim();
    if (!body) {
      problems.push({line: group.line, code: 'missing_question', message: `「${group.title}」的主问题是空的`});
      continue;
    }
    if (seen.has(group.title)) {
      problems.push({line: group.line, code: 'duplicate_title', message: `「${group.title}」与第 ${seen.get(group.title)} 行的主题组重名`});
      continue;
    }
    seen.set(group.title, group.line);
    const note = [sourceTag, group.variants ? `含 ${group.variants} 条同主题变体` : '', group.category]
      .filter(Boolean).join(' · ').slice(0, 300);
    items.push({
      item_id: slug(items.length + 1),
      kind: 'knowledge',
      roles: [role],
      title: group.title,
      question: body.slice(0, MAX_QUESTION),
      reference: '',
      probe: '',
      retest: '',
      source_url: '',
      source_note: note,
      tags: group.category
        ? [group.category.replace(/[（(]\s*\d+\s*题[^）)]*[）)]\s*$/, '').trim().slice(0, 30)]
        : [],
    });
  }

  const skippedGroups = groups.filter(group => !group.main).length;
  if (items.length > MAX_ITEMS) {
    problems.push({line: 1, code: 'too_many_items', message: `一题一题导入，最多 ${MAX_ITEMS} 题，请拆分文档`});
  }
  if (items.length === 0 && problems.length === 0) {
    problems.push({line: 1, code: 'no_items', message: '没有识别到「**主问题**」行'});
  }
  return {
    document: {
      schema_version: 1,
      name: (name ?? heading ?? '岗位面经题库').trim().slice(0, 80) || '岗位面经题库',
      items: items.slice(0, MAX_ITEMS),
    },
    problems,
    withoutReference: items.length,
    skippedGroups,
  };
}

/** Auto-detect: 主问题 structure wins when present, otherwise the simple template. */
export function parseQuestionBankDocument(text: string, role: BankRole, name?: string): TemplatePreview & {skippedGroups?: number} {
  if (/^\*\*主问题\*\*/m.test(text)) return parseStructuredQuestionBank(text, role, name);
  return parseQuestionBankTemplate(text, role, name);
}

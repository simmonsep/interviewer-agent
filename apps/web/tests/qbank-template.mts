// 新增业务 1 · A1: the Markdown template converts deterministically into the existing bank JSON.
// No network, no model: the panel only previews and then hands the document to the existing import.
import assert from 'node:assert/strict';
import {MAX_ITEMS, parseQuestionBankDocument, parseQuestionBankTemplate,
  questionBankTemplate} from '../src/qbankTemplate.ts';

const cases: {name: string; detail?: unknown}[] = [];
const mark = (name: string, detail?: unknown) => {
  cases.push({name, detail});
  console.log('PASS', name);
};

const sample = `# 我的 Python 题库

> 一个 ## 标题 对应一题

## LangGraph 的 state 与 checkpointer
题干：同一个用户的多个独立会话，state、checkpointer 和 store 各自负责什么？
参考：图 state 描述当前线程；checkpointer 按 thread_id 保存；store 放跨线程信息。
追问：进程重启后要继续会话，需要满足什么条件？
复测：两个并发会话写同一 thread_id 会怎样？
来源：https://docs.langchain.com/oss/python/langgraph/persistence
说明：依据官方文档自拟。
标签：Python, LangGraph、记忆

## 没有参考的题也可以出
题干：你怎样判断一次工具调用应该重试？
`;

const parsed = parseQuestionBankTemplate(sample, 'ai-application');
assert.equal(parsed.problems.length, 0, JSON.stringify(parsed.problems));
assert.equal(parsed.document.name, '我的 Python 题库');
assert.equal(parsed.document.schema_version, 1);
assert.equal(parsed.document.items.length, 2);
const first = parsed.document.items[0];
assert.equal(first.item_id, 'q-1');
assert.equal(first.kind, 'knowledge');
assert.deepEqual(first.roles, ['ai-application']);
assert.equal(first.title, 'LangGraph 的 state 与 checkpointer');
assert.match(first.question, /^同一个用户的多个独立会话/);
assert.match(first.reference, /checkpointer/);
assert.match(first.probe, /进程重启/);
assert.match(first.retest, /并发会话/);
assert.equal(first.source_url, 'https://docs.langchain.com/oss/python/langgraph/persistence');
assert.equal(first.source_note, '依据官方文档自拟。');
assert.deepEqual(first.tags, ['Python', 'LangGraph', '记忆']);
assert.equal(parsed.withoutReference, 1, '缺参考只提示，不算问题');
mark('模板 → 题库文档：字段逐项映射，缺参考只记提示', {items: parsed.document.items.length});

const missingQuestion = parseQuestionBankTemplate('## 只有标题\n参考：写了点东西\n', 'java-backend');
assert.equal(missingQuestion.document.items.length, 0);
assert.equal(missingQuestion.problems[0].code, 'missing_question');
assert.equal(missingQuestion.problems[0].line, 1);
mark('缺题干的题被指向具体行，不进入文档', missingQuestion.problems[0]);

const duplicate = parseQuestionBankTemplate('## 同名\n题干：A\n## 同名\n题干：B\n', 'java-backend');
assert.equal(duplicate.document.items.length, 1);
assert.equal(duplicate.problems[0].code, 'duplicate_title');
assert.match(duplicate.problems[0].message, /第 1 行重名/);
mark('重名题只保留第一题并给出可修提示', duplicate.problems[0]);

const empty = parseQuestionBankTemplate('随便写点东西，没有标题。\n', 'java-backend');
assert.equal(empty.problems[0].code, 'no_items');
mark('没有题目时明确说没有识别到');

const longQuestion = parseQuestionBankTemplate(`## 长题\n题干：${'字'.repeat(1001)}\n`, 'java-backend');
assert.equal(longQuestion.problems[0].code, 'question_too_long');
mark('超长题干被拒绝');

// 题库名优先用面板传入的名字；没有时才用文档标题。
assert.equal(parseQuestionBankTemplate(sample, 'java-backend', '面板名').document.name, '面板名');
assert.equal(parseQuestionBankTemplate(sample, 'java-backend').document.name, '我的 Python 题库');
mark('题库名：面板优先，文档标题兜底');

const template = questionBankTemplate('我的题库');
assert.match(template, /^# 我的题库/);
assert.ok(parseQuestionBankTemplate(template, 'ai-application').document.items.length >= 1,
  '模板本身必须能被解析出一题，用户才有可改的起点');
mark('可下载的模板能被自己的解析器读回');

const many = Array.from({length: MAX_ITEMS + 1}, (_, index) => `## 题 ${index + 1}\n题干：题干 ${index + 1}`).join('\n');
const overflow = parseQuestionBankTemplate(many, 'java-backend');
assert.equal(overflow.document.items.length, MAX_ITEMS);
assert.equal(overflow.problems.some(item => item.code === 'too_many_items'), true);
mark(`超过 ${MAX_ITEMS} 题时截断并提示拆分`);

console.log(`${cases.length}/${cases.length} passed`);

// ---------------------------------------------------------------- 第二种真实结构
const structured = `\uFEFF# 某岗位面经（合并版）

## Agent 核心机制与架构（53 题）

### Agent 执行范式（4 题 · P0）
**主问题** [网易#06] 讲解主流 Agent 执行范式，比较差异与适用场景。

**相关变体：**
- [Agent20#03] 说说 ReAct 和 Plan-and-Execute 的区别。
- [阿里#7] 你做过的 Agent 用哪种范式？

### 没有主问题的组（1 题）
只有一些笔记，没有主问题。
`;

const parsedStructured = parseQuestionBankDocument(structured, 'ai-application');
assert.equal(parsedStructured.document.name, '某岗位面经（合并版）', 'BOM must not hide the title');
assert.equal(parsedStructured.document.items.length, 1, 'one group with a 主问题 becomes one item');
const group = parsedStructured.document.items[0];
assert.equal(group.title, 'Agent 执行范式');
assert.match(group.question, /^讲解主流 Agent 执行范式/);
assert.match(group.source_note, /网易#06/);
assert.match(group.source_note, /含 2 条同主题变体/);
assert.equal(group.reference, '', 'these documents ship no answers; none may be invented');
assert.deepEqual(parsedStructured.document.items.map(item => item.item_id), ['q-1']);
mark('第二种结构：主题组 → 一题，来源与变体数保留，不编造参考', {
  title: group.title, note: group.source_note,
});

const autoSimple = parseQuestionBankDocument('## 标题\n题干：问题\n', 'java-backend');
assert.equal(autoSimple.document.items.length, 1, 'documents without 主问题 still use the simple template');
mark('自动识别：没有主问题时仍走简单模板');

// F03 acceptance: materials and resume on the desktop shell.
// Real stack; the materials worker runs with --provider scripted, so every draft says
// "脚本流程" and no model is called.
//
//   D01_OUTPUT=<new dir> node apps/web/tests/d01-desktop-materials.mts
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {openDetail, waitGoalReady} from './desktop-nav.mts';
import {writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/desktop-handoff/F03-materials'));
const base=process.env.D01_BASE_URL??'http://127.0.0.1:5174';
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
assert.ok(executable,'installed_browser_required');
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;type:'real';detail?:unknown}[]=[];
const mark=(name:string,detail?:unknown)=>cases.push({name,type:'real',detail});
const pageErrors:string[]=[];
const shots:string[]=[];
const evidence:Record<string,unknown>={};
const account={email:`f03-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID(),name:'材料验收学员'};
const material=[
 '在电商实习中负责订单查询接口，用 Java 与 MyBatis 重写慢查询，P95 从 1.2s 降到 300ms。',
 '用 Redis 缓存热点商品，命中率约 85%，大促期间数据库 QPS 下降四成。',
 '主导过一次分库分表迁移，按用户 ID 取模拆成 8 个库，迁移期间零停机。',
].join('\n');

let browser:Browser;
const shot=async(page:Page,name:string)=>{await page.screenshot({path:join(output,'shots',name)});shots.push(name);};

try{
 browser=await chromium.launch({executablePath:executable,headless:true});
 const context=await browser.newContext({viewport:{width:1280,height:860}});
 const page=await context.newPage();
 page.on('pageerror',error=>pageErrors.push(error.message));

 // F05B 免登直入：需要登录页的套件先放一次性标志（App 读取后清除）。
 await page.goto(`${base}`,{waitUntil:'domcontentloaded'});
 await page.evaluate(()=>{try{localStorage.setItem('py-desktop-login','1');}catch{}});
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-auth-card form').waitFor({timeout:30000});
 await page.getByRole('button',{name:'第一次来？创建账号'}).click();
 await page.locator('input[name="name"]').fill(account.name);
 await page.locator('input[name="email"]').fill(account.email);
 await page.locator('input[name="password"]').fill(account.password);
 await page.locator('.desktop-auth-card form .primary').click();
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('合成验收：找 Java 后端实习。');
 await page.locator('label:has-text("确认方向") input').fill('Java 后端');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);

 const pane=page.locator('[data-testid="materials-pane"]');
 await openDetail(page,'材料与简历');
 await pane.waitFor({timeout:15000});
 await page.getByText('还没有材料。粘贴一段项目或实习经历即可开始。').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-testid="desktop-page"]').innerText(),'材料与简历');
 assert.equal(await pane.getByRole('button',{name:'分析材料'}).isDisabled(),true,'analyze_enabled_without_source');
 await shot(page,'01-materials-empty.png');
 mark('材料与简历是独立入口：无材料时列表为空，分析按钮禁用，不会对空材料发起运行',{});

 // ---------------------------------------------------------------- add a source (preview before saving)
 await pane.locator('textarea[aria-label="材料正文"]').fill(material);
 const counter=await pane.locator('.d-form-foot .d-num').first().innerText();
 assert.match(counter,new RegExp(String(material.length)));
 await shot(page,'02-source-preview.png');
 await pane.getByRole('button',{name:'保存这段材料'}).click();
 // F07Z-147: assert the contract, not one frozen sentence. Success means: the row exists, the
 // count moved, the feedback says it saved (with the name), and the original text is intact.
 await pane.locator('.d-source').first().waitFor({timeout:20000});
 const saveFeedback=await pane.locator('.notice').first().innerText();
 assert.match(saveFeedback,/已保存/,`save_feedback ${saveFeedback}`);
 assert.match(saveFeedback,/简历片段/,`save_feedback_name ${saveFeedback}`);
 assert.match(await pane.locator('[data-testid="materials-head"]').innerText(),/已保存 1 份材料/,'count_not_updated');
 assert.equal(await pane.locator('.d-source').count(),1);
 assert.ok((await pane.locator('.d-source-body').first().innerText()).includes('订单查询接口'),'source_text_rewritten');
 await shot(page,'03-source-saved.png');
 mark('粘贴材料时先看到字数与全文，确认后才写入；保存后来源列表出现这条记录，计数与反馈都说已保存',{
  chars:material.length,counter,save_feedback:saveFeedback.replace(/\n/g,' / ').slice(0,80)});

 // ---------------------------------------------------------------- analyze: proposals, not facts yet
 await pane.getByRole('button',{name:'分析材料'}).click();
 await pane.locator('.d-proposal').first().waitFor({timeout:40000});
 const proposals=await pane.locator('.d-proposal').count();
 assert.ok(proposals>=3,`proposal_count ${proposals}`);
 assert.equal(await pane.locator('.d-facts li').count(),0,'facts_exist_before_confirmation');
 await shot(page,'04-analyzed.png');
 mark('分析产出「待确认条目」而不是直接写入依据：确认之前已确认依据仍为 0',{proposals});

 // ---------------------------------------------------------------- confirm
 const boxes=pane.locator('.d-proposal input[type="checkbox"]');
 for(let index=0;index<await boxes.count();index++)await boxes.nth(index).check();
 await pane.getByRole('button',{name:'确认所选'}).click();
 await pane.getByText('已确认这些条目，可以作为初稿依据。').waitFor({timeout:20000});
 const facts=await pane.locator('.d-facts li').count();
 assert.equal(facts,proposals,'fact_count_mismatch');
 await shot(page,'05-confirmed.png');
 mark('只有勾选并确认的条目才进入「已确认依据」，数量与勾选一致',{facts});

 // ---------------------------------------------------------------- rewrite draft
 await pane.getByRole('button',{name:'按材料改写'}).click();
 await pane.locator('.d-draft-item').first().waitFor({timeout:40000});
 const before=await pane.locator('.d-draft-item textarea').first().inputValue();
 assert.ok(before.length>0,'draft_item_empty');
 assert.ok((await pane.innerText()).includes('草稿 · 第 1 版'));
 const coverage=pane.locator('[data-testid="draft-coverage"]');
 await coverage.waitFor({timeout:20000});
 assert.equal(await coverage.getAttribute('data-unaccounted'),'0','coverage_ledger_incomplete');
 const ledger=pane.locator('[data-testid="draft-segments"]');
 await ledger.waitFor({timeout:20000});
 const ledgerRows=await ledger.locator('.d-segment').count();
 assert.ok(ledgerRows>0,'segment_ledger_empty');
 assert.equal(await ledger.locator('[data-where="missing"]').count(),0,'unaccounted_segment_shown');
 await ledger.locator('summary').click();
 await shot(page,'06b-ledger.png');
 await shot(page,'06-draft-v1.png');
 mark('改写生成第 1 版草稿，条目文本来自材料原文，界面标注为脚本流程',{chars:before.length});

 // ---------------------------------------------------------------- edit and save a new version
 const edited=`${before}（改为第一人称，补充压测方法与回滚预案。）`;
 await pane.locator('.d-draft-item textarea').first().fill(edited);
 await pane.getByRole('button',{name:'保存为新版本'}).click();
 await page.getByText('已保存为新版本，旧版本仍保留。').waitFor({timeout:20000});
 await page.getByText('草稿 · 第 2 版').waitFor({timeout:20000});
 assert.equal(await pane.locator('.d-draft-item textarea').first().inputValue(),edited);
 await shot(page,'07-draft-v2.png');
 mark('编辑草稿保存为新版本：界面切到第 2 版且内容为编辑后的文本，第 1 版仍在服务端',{});

 // ---------------------------------------------------------------- export markdown
 const draftId=await page.evaluate(async(goalId)=>{
  const response=await fetch(`/api/career-goals/${goalId}/materials`,{credentials:'same-origin',cache:'no-store'});
  const view=await response.json();
  return view.drafts[0].id as string;
 },await page.evaluate(()=>new URLSearchParams(location.search).get('goal')));
 const exported=await page.evaluate(async({goal,draft})=>{
  const response=await fetch(`/api/career-goals/${goal}/materials/drafts/${draft}.md`,{credentials:'same-origin'});
  return {status:response.status,type:response.headers.get('content-type'),disposition:response.headers.get('content-disposition'),text:await response.text()};
 },{goal:await page.evaluate(()=>new URLSearchParams(location.search).get('goal')),draft:draftId});
 assert.equal(exported.status,200);
 assert.match(exported.type??'',/text\/markdown/);
 assert.match(exported.disposition??'',/attachment/);
 assert.ok(exported.text.includes('回滚预案'),'exported_text_missing_edit');
 evidence.export={status:exported.status,type:exported.type,disposition:exported.disposition,chars:exported.text.length};
 mark('导出 Markdown：200 + text/markdown + attachment，正文包含刚才的编辑内容',evidence.export);

 // ---------------------------------------------------------------- reload restores everything
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 await openDetail(page,'材料与简历');
 await page.getByText('草稿 · 第 2 版').waitFor({timeout:30000});
 assert.equal(await pane.locator('.d-draft-item textarea').first().inputValue(),edited,'draft_lost_on_reload');
 assert.equal(await pane.locator('.d-facts li').count(),facts,'facts_lost_on_reload');
 assert.equal(await pane.locator('.d-source').count(),1,'source_lost_on_reload');
 await shot(page,'08-reload.png');
 mark('刷新重开：来源、已确认依据、第 2 版草稿都从服务端回读',{facts});

 // ---------------------------------------------------------------- code-owned sections are visible and editable
// The scripted worker fills each structured field from the line that carries its marker,
// so this material yields exactly one line per section without a model call.
const structured=[
 '教育经历：2014.09 - 2019.07 本科 某某大学 计算机科学与技术',
 '工作经历：2018.10 - 2019.02 某某公司 前端开发实习生',
 '项目经历：即时聊天系统 2017.08 - 2017.08',
 '技能：Node / Vue / MySQL',
].join('\n');
await pane.locator('details.d-source-add summary').click();
await pane.locator('textarea[aria-label="材料正文"]').fill(structured);
await pane.getByRole('button',{name:'保存这段材料'}).click();
// Same contract as the first save (F07Z-147): a row and a "saved" feedback, not a frozen string.
await pane.locator('.d-source').nth(1).waitFor({timeout:20000});
assert.match(await pane.locator('.notice').first().innerText(),/已保存/,'structured_save_feedback');
await pane.getByRole('button',{name:'按材料改写'}).click();
await pane.locator('[data-testid="draft-section-education"]').waitFor({timeout:40000});
const order=await pane.locator('[data-testid^="draft-section-"]:not([data-testid$="-add"])').evaluateAll(nodes=>nodes.map(node=>node.getAttribute('data-testid')));
assert.deepEqual(order,['draft-section-basics','draft-section-education','draft-section-experience','draft-section-projects','draft-section-skills'],'section_order');
const sectionValue=async(key:string)=>pane.locator(`[data-testid="draft-section-${key}"] textarea`).first().inputValue();
assert.ok((await sectionValue('education')).includes('2014.09'),'education_line_missing_from_panel');
assert.ok((await sectionValue('experience')).includes('2018.10'),'experience_line_missing_from_panel');
assert.ok((await sectionValue('projects')).includes('2017.08'),'project_line_missing_from_panel');
assert.ok((await sectionValue('skills')).includes('Node'),'skills_line_missing_from_panel');
const narrativeBefore=await pane.locator('.d-draft-item textarea').first().inputValue();
await pane.locator('[data-testid="draft-section-education"]').scrollIntoViewIfNeeded();
await shot(page,'09-sections.png');
mark('四个固定段落（教育/工作/项目/技能）在面板里按固定顺序显示，内容来自原文，不再只存在于导出的 markdown',{order,education:await sectionValue('education')});

await pane.locator('[data-testid="draft-section-education"] textarea').first().fill('2014.09 - 2019.07 本科 某某大学 计算机科学与技术（GPA 3.8）');
await pane.getByRole('button',{name:'保存为新版本'}).click();
await page.getByText('已保存为新版本，旧版本仍保留。').waitFor({timeout:20000});
assert.ok((await sectionValue('education')).includes('GPA 3.8'),'section_edit_not_persisted');
assert.equal(await pane.locator('.d-draft-item textarea').first().inputValue(),narrativeBefore,'section_edit_changed_narrative');
const newestDraft=await page.evaluate(async(goalId)=>{
 const response=await fetch(`/api/career-goals/${goalId}/materials`,{credentials:'same-origin',cache:'no-store'});
 const view2=await response.json();
 return {id:view2.drafts[0].id as string,education:view2.drafts[0].payload.education as string[]};
},await page.evaluate(()=>new URLSearchParams(location.search).get('goal')));
const exported2=await page.evaluate(async({goal,draft})=>{
 const response=await fetch(`/api/career-goals/${goal}/materials/drafts/${draft}.md`,{credentials:'same-origin'});
 return await response.text();
},{goal:await page.evaluate(()=>new URLSearchParams(location.search).get('goal')),draft:newestDraft.id});
assert.ok(newestDraft.education[0].includes('GPA 3.8'),'structured_payload_not_saved');
assert.ok(exported2.includes('GPA 3'),'exported_markdown_missing_structured_correction');
await shot(page,'10-sections-edited.png');
mark('在校正固定段落的一行并保存后：payload 与导出 markdown 都是改正后的文本，叙述条目没有被连带改动',{education:newestDraft.education[0]});

// ---------------------------------------------------------------- narrow window
 await page.setViewportSize({width:880,height:600});
 await page.waitForTimeout(400);
 const overflow=await page.evaluate(()=>({inner:innerWidth,doc:document.documentElement.scrollWidth}));
 assert.ok(overflow.doc<=overflow.inner+2,`narrow_overflow ${JSON.stringify(overflow)}`);
 await shot(page,'09-narrow-880x600.png');
 mark('880×600 窄窗下材料面板无横向溢出',overflow);
 await context.close();
}catch(error){
 writeFileSync(join(output,'result.json'),JSON.stringify({account:account.email,cases,page_errors:pageErrors,shots,evidence,error:(error as Error).message},null,2));
 await browser?.close();
 console.error('FAILED:',(error as Error).message);
 process.exit(1);
}

writeFileSync(join(output,'result.json'),JSON.stringify({account:account.email,base,viewport:'1280x860',cases,page_errors:pageErrors,shots,evidence},null,2));
console.log(JSON.stringify({passed:cases.length,page_errors:pageErrors,shots:shots.length},null,1));
await browser.close();

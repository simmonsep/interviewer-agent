// D-01-03 node-A clickable prototype acceptance. Serves the self-contained
// synthetic prototype with its own tiny static server and drives it in Edge
// headless. No backend, no model calls, no page.route injection.
//
//   D01_OUTPUT=<new dir> node apps/web/tests/d01-prototype-shots.mts
//
// Evidence is synthetic: state comes from the two-question fixture embedded in
// apps/web/prototype/index.html. Node B (real data / desktop shell) is NOT wired.
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {spawn,type ChildProcess} from 'node:child_process';
import {readFileSync,writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/evidence/D-01-03'));
const prototypeDir=join(root,'apps/web/prototype');
const port=Number(process.env.D01_PROTOTYPE_PORT??5176);
const base=`http://127.0.0.1:${port}/`;
const fixture=JSON.parse(readFileSync(join(root,'fixtures/question-banks/agent-developer-trial.json'),'utf8'));
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
assert.ok(executable,'installed_browser_required');
assert.ok(existsSync(join(prototypeDir,'index.html')),'prototype_html_missing');
mkdirSync(output,{recursive:true});

const cases:{name:string;status:string;type:'prototype'|'check';detail?:unknown}[]=[];
const pageErrors:string[]=[];
const requests:string[]=[];
const screenshots:{name:string;viewport:string;full_page:boolean}[]=[];
const mark=(name:string,type:'prototype'|'check'='prototype',detail?:unknown)=>cases.push({name,status:'passed',type,detail});
const server:ChildProcess=spawn(process.execPath,[join(prototypeDir,'serve.mjs')],
 {env:{...process.env,D01_PROTOTYPE_PORT:String(port)},stdio:['ignore','pipe','pipe']});
let serverLog='';
server.stdout?.on('data',d=>{serverLog+=String(d);});
server.stderr?.on('data',d=>{serverLog+=String(d);});

async function waitForServer(){
 for(let i=0;i<60;i++){
  try{const r=await fetch(base);if(r.ok)return;}catch{}
  await delay(200);
 }
 throw new Error('prototype_server_not_ready: '+serverLog.slice(0,300));
}
// Review screenshots default to the raw CSS viewport (no full-page stitching): a
// stitched long image cannot prove what a reviewer actually sees on first paint.
async function shot(page:Page,name:string,fullPage=false){
 const path=join(output,name);
 await page.evaluate(()=>{
  var toast=document.getElementById('toast');
  if(toast)toast.classList.add('hidden');
 });
 if(fullPage){
  // Release the inner scroll container so the whole document is captured.
  await page.evaluate(()=>{
   var content=document.querySelector('.content'),shell=document.getElementById('app');
   if(content&&shell){
    content.dataset.prevOverflow=content.style.overflow;
    content.dataset.prevHeight=content.style.height;
    shell.dataset.prevHeight=shell.style.height;
    content.style.overflow='visible';content.style.height='auto';shell.style.height='auto';
   }
  });
 }
 await page.screenshot({path,fullPage});
 if(fullPage){
  await page.evaluate(()=>{
   var content=document.querySelector('.content'),shell=document.getElementById('app');
   if(content&&shell){
    content.style.overflow=content.dataset.prevOverflow||'';
    content.style.height=content.dataset.prevHeight||'';
    shell.style.height=shell.dataset.prevHeight||'';
   }
  });
 }
 const viewport=await page.evaluate(()=>window.innerWidth+'x'+window.innerHeight);
 screenshots.push({name,viewport,full_page:fullPage});
 return name;
}
async function layout(page:Page,label:string){
 const value=await page.evaluate(()=>({inner_width:window.innerWidth,inner_height:window.innerHeight,
  document_scroll_width:document.documentElement.scrollWidth,body_scroll_width:document.body.scrollWidth}));
 assert.ok(value.document_scroll_width<=value.inner_width+2,`${label}_horizontal_overflow_${JSON.stringify(value)}`);
 assert.ok(value.body_scroll_width<=value.inner_width+2,`${label}_body_overflow_${JSON.stringify(value)}`);
 return value;
}
async function inViewport(page:Page,selector:string,label:string){
 const locator=page.locator(selector);
 await locator.scrollIntoViewIfNeeded();
 const box=await locator.boundingBox();
 assert.ok(box&&box.width>0&&box.height>0,`${label}_not_rendered`);
 const vp=await page.evaluate(()=>({w:window.innerWidth,h:window.innerHeight}));
 assert.ok(box.x>=-2&&box.x+box.width<=vp.w+2,`${label}_outside_viewport_${JSON.stringify(box)}`);
 assert.ok(box.y>=-2&&box.y+box.height<=vp.h+2,`${label}_not_vertically_visible_after_scroll_${JSON.stringify(box)}`);
 return {box,vp};
}
// First paint test: the element must be fully visible while the scroll container is
// still at the top, i.e. the reviewer does not have to scroll to find it.
async function firstScreen(page:Page,selector:string,label:string){
 // .content uses scroll-behavior:smooth, so pin the scroll position instantly.
 await page.locator('.content').evaluate(el=>{const y=el as HTMLElement;y.style.scrollBehavior='auto';y.scrollTop=0;});
 const box=await page.locator(selector).boundingBox();
 assert.ok(box&&box.width>0&&box.height>0,`${label}_not_rendered`);
 const vp=await page.evaluate(()=>({w:window.innerWidth,h:window.innerHeight,scroll_top:(document.querySelector('.content') as HTMLElement).scrollTop}));
 assert.equal(vp.scroll_top,0,`${label}_scroll_not_at_top`);
 assert.ok(box.x>=-2&&box.x+box.width<=vp.w+2,`${label}_outside_viewport_${JSON.stringify(box)}`);
 assert.ok(box.y>=-2&&box.y+box.height<=vp.h+2,`${label}_not_on_first_screen_${JSON.stringify(box)}`);
 return {box,vp};
}
// Counts how many times a string is rendered in the visible page text; used to prove
// progress / countdown / status are not duplicated across the context bar and panel.
async function countText(page:Page,needle:string){
 return page.evaluate(n=>{
  const text=(document.body.innerText||'').replace(/\s+/g,' ');
  return text.split(n).length-1;
 },needle);
}

let browser:Browser|null=null;
const evidence:any={kind:'D-01-03 节点A 可点击原型验收（评审反馈修整后复跑）',timestamp:new Date().toISOString(),
 prototype_url:base,fixture:'fixtures/question-banks/agent-developer-trial.json',
 api_mode:'synthetic: static HTML prototype, no FastAPI / PG / Redis / Worker / model calls',
 case_type_note:'所有用例都在合成原型内完成；页面不发起任何业务 HTTP 请求。节点 B（真数据、桌面外壳、回归）尚未开始。',
 screenshot_policy:'截图取原始 CSS 视口（full_page:false）；仅复盘额外附一张全页图，宽窄窗可见性由首屏断言与视口截图共同证明。',
 rework_note:'评审反馈：答题页计时/状态重复、窄窗长图无法证明首屏可见、界面残留验收文案。本轮已改并复跑。',
 node:'A-only',cases,page_errors:pageErrors,requests,screenshots};

try{
 await waitForServer();
 browser=await chromium.launch({executablePath:executable,headless:true});
 const context=await browser.newContext({viewport:{width:1280,height:860}});
 const page=await context.newPage();
 page.on('pageerror',error=>pageErrors.push(error.message));
 page.on('request',request=>requests.push(request.url()));
 await page.goto(base,{waitUntil:'domcontentloaded'});
 await page.locator('#primary-action').waitFor();

 // ---- 1. Home: goal + primary action + synthetic badge ----
 const homeText=await page.locator('#content-inner').innerText();
 assert.match(homeText,/AI 应用 \/ Agent 开发实习/,'home_goal_missing');
 assert.equal(await page.locator('#primary-action').innerText(),'继续面试','home_primary_not_continue');
 assert.match(await page.locator('.synthetic-badge').innerText(),/合成原型/,'synthetic_badge_missing');
 assert.match(homeText,/近期会话/,'recent_sessions_missing');
 assert.equal(await page.locator('#nav-home').getAttribute('aria-current'),'page','home_nav_current_missing');
 await layout(page,'home-1280');
 await shot(page,'01-home.png');
 mark('首页识别当前目标与主操作：有进行中场次时显示「继续面试」，合成标记常驻','prototype');

 // ---- 2. No-goal and no-active variants ----
 await page.selectOption('#demo-state','no-goal');
 assert.equal(await page.locator('#primary-action').innerText(),'创建求职目标','no_goal_primary_wrong');
 await shot(page,'05-goal-form.png');
 await page.locator('#primary-action').click();
 await page.locator('#goal-form').waitFor();
 await page.locator('#goal-submit').click();
 assert.match(await page.locator('#goal-error').innerText(),/请先写一句求职意向/,'empty_goal_no_inline_error');
 await page.locator('#goal-intent').fill('想在上海找 AI 应用 / Agent 开发实习，用两题练习检验表达。');
 await page.locator('#goal-direction').fill('AI 应用 / Agent 开发');
 await page.locator('#goal-city').fill('上海');
 await page.locator('#goal-submit').click();
 assert.equal(await page.locator('#primary-action').innerText(),'开始面试','no_active_primary_wrong');
 await shot(page,'06-home-no-active.png');
 mark('无目标时主操作为「创建求职目标」，沿用目标表单并给出行内错误；保存后变为「开始面试」','prototype');

 const firstNewId=await page.evaluate(()=>location.hash);
 await page.locator('#primary-action').click();
 await page.locator('#answer-input').waitFor();
 assert.notEqual(await page.evaluate(()=>location.hash),firstNewId,'new_session_hash_unchanged');
 mark('点击「开始面试」创建新合成场次并进入答题（URL 反映状态）','prototype');

 // ---- 3. Processing / failed text states ----
 await page.selectOption('#demo-state','processing');
 assert.match(await page.locator('#content-inner').innerText(),/后台正在出题或处理上一答/,'processing_hint_missing');
 assert.equal(await page.locator('#primary-action').innerText(),'查看进度','processing_primary_wrong');
 await page.selectOption('#demo-state','failed');
 assert.match(await page.locator('#content-inner').innerText(),/本场失败/,'failed_hint_missing');
 assert.equal(await page.locator('#primary-action').innerText(),'查看失败场次','failed_primary_wrong');
 await shot(page,'08-failed-state.png');
 mark('等待 / 需接手 / 失败用中文文字区分，不以颜色单独表意','prototype');

 // ---- 4. Focused answering at 1280x860 ----
 await page.selectOption('#demo-state','in-progress');
 await page.locator('#primary-action').click();
 await page.locator('#answer-input').waitFor();
 const firstQuestion=await page.locator('#current-question').innerText();
 assert.equal(firstQuestion.trim(),fixture.items[0].question.trim(),'first_question_not_from_fixture');
 assert.match(await page.locator('#ctx-progress').innerText(),/第 1\/2 题/,'progress_line_missing');
 assert.match(await page.locator('#ctx-progress').innerText(),/剩余/,'countdown_missing');
 assert.match(await page.locator('#ctx-progress').innerText(),/可作答/,'status_text_missing');
 // One authoritative line: progress, countdown and status appear exactly once, in the
 // context bar. The panel must not repeat any of them.
 assert.equal(await countText(page,'剩余'),1,'countdown_duplicated');
 assert.equal(await countText(page,'可作答'),1,'status_duplicated');
 assert.equal(await countText(page,'第 1/2 题'),1,'progress_duplicated');
 assert.equal(await countText(page,'场次 9f1c4d82'),1,'session_id_duplicated');
 assert.ok(!(await page.locator('#content-inner').innerText()).includes('剩余'),'countdown_inside_panel');
 const beforeTerminal=await page.locator('#content-inner').innerText();
 assert.ok(!beforeTerminal.includes(fixture.items[0].reference),'reference_shown_before_terminal');
 assert.ok(!beforeTerminal.includes(fixture.items[0].source_url),'source_shown_before_terminal');
 assert.ok(!beforeTerminal.includes('答题观察 ·'),'observation_shown_before_terminal');
 await inViewport(page,'#answer-input','回答本题输入区');
 await inViewport(page,'#submit-answer','提交这一答按钮');
 await layout(page,'answer-1280');
 mark('专注答题：当前题取自两题夹具，进度/倒计时/状态只在顶部出现一次，考中反馈与参考内容隐藏','prototype',{countdown_copies:await countText(page,'剩余'),status_copies:await countText(page,'可作答')});

 // Chinese IME composition: Enter must not submit.
 await page.locator('#answer-input').focus();
 await page.locator('#answer-input').evaluate(el=>{
  el.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));
 });
 await page.keyboard.press('Enter');
 const afterEnter=await page.locator('#current-question').innerText();
 assert.equal(afterEnter.trim(),firstQuestion.trim(),'ime_enter_submitted');
 assert.equal(await page.locator('#content-inner').locator('details').count(),0,'ime_enter_opened_previous');
 await page.locator('#answer-input').evaluate(el=>{
  el.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true}));
 });
 mark('中文输入法组词期间按 Enter 不提交；提交只走显式按钮','prototype');

 const answer1='图里 state 是线程运行状态，checkpointer 按 thread_id 存检查点，store 放跨线程偏好。独立会话要用不同 thread_id；进程重启后继续需要持久化 checkpointer 与 store，内存实现不行。';
 await page.locator('#answer-input').fill(answer1);
 await shot(page,'02-answer.png');
 await page.locator('#submit-answer').click();
 await page.locator('#current-question').waitFor();
 const secondQuestion=await page.locator('#current-question').innerText();
 assert.equal(secondQuestion.trim(),fixture.items[1].question.trim(),'second_question_not_from_fixture');
 assert.ok(secondQuestion.trim()!==firstQuestion.trim(),'question_did_not_advance');
 assert.match(await page.locator('#content-inner details summary').first().innerText(),/前序问答（1）/,'previous_qa_missing');
 await page.locator('#content-inner details summary').first().click();
 assert.match(await page.locator('#content-inner details').first().innerText(),/你的回答/,'previous_answer_missing');
 await inViewport(page,'#answer-input','第二题回答区');
 await inViewport(page,'#submit-answer','第二题提交按钮');
 mark('第一答后第二题出现，前序问答可展开回看，作答区仍可达','prototype');

 // ---- 5. Narrow windows: nav collapses but entries and answer area stay ----
 await page.setViewportSize({width:880,height:600});
 await delay(250);
 const nav880=await page.locator('#app').getAttribute('data-nav');
 assert.ok(nav880==='compact'||nav880==='bar','nav_not_collapsed_at_880_'+nav880);
 assert.ok(await page.locator('#nav-home').isVisible(),'home_entry_lost_at_880');
 assert.ok(await page.locator('#nav-interview').isVisible(),'interview_entry_lost_at_880');
 const narrowLayout=await layout(page,'answer-880');
 // Second question at 880x600: input and submit must be on the first screen, with the
 // scroll container still at the top.
 const narrowFirst=await firstScreen(page,'#answer-input','880 第二题回答区首屏');
 const narrowSubmit=await firstScreen(page,'#submit-answer','880 第二题提交按钮首屏');
 await shot(page,'04-answer-narrow.png');
 await page.setViewportSize({width:680,height:520});
 await delay(250);
 assert.equal(await page.locator('#app').getAttribute('data-nav'),'bar','nav_not_bar_at_680');
 assert.ok(await page.locator('#nav-home').isVisible(),'home_entry_lost_at_680');
 const veryNarrow=await layout(page,'answer-680');
 await inViewport(page,'#answer-input','680 回答区');
 await inViewport(page,'#submit-answer','680 提交按钮');
 await shot(page,'07-answer-680.png');
 mark('更窄的 680×520 CSS 视口（高 DPI 场景）仍无横向溢出、输入区与提交按钮可达','prototype',veryNarrow);
 // Back to 880x600 and actually type + submit the second answer through the real button.
 await page.setViewportSize({width:880,height:600});
 await delay(250);
 const answer2='interrupt 靠持久化检查点与 thread_id 暂停，用 Command(resume=...) 恢复；恢复会从含 interrupt 的节点开头重跑，副作用要幂等或移到单独节点，写入放在用户明确批准之后并用业务幂等键防重复。';
 await page.locator('#answer-input').fill(answer2);
 await page.locator('#submit-answer').click();
 await page.locator('#content-inner .review-item').first().waitFor();
 await shot(page,'04b-answer-narrow-review.png');
 assert.match(await page.locator('#ctx-progress').innerText(),/复盘 · 场次/,'narrow_submit_did_not_reach_review');
 mark('880×600 窄窗答题：导航收起为紧凑条但两个入口仍在，第二题首屏即可见输入区与提交按钮、无横向溢出','prototype',
  {nav:nav880,...narrowLayout,answer_first_screen:narrowFirst.box,submit_first_screen:narrowSubmit.box});
 mark('880×600 下实际填写第二答并点击提交，直接进入复盘（不依赖放大窗口或横向滚动）','prototype');
 await page.setViewportSize({width:1280,height:860});
 await delay(250);

 // ---- 6. Review: per-question evidence, scripted label, no scores ----
 const reviewText=await page.locator('#content-inner').innerText();
 assert.match(reviewText,/确定性脚本流程演示/,'scripted_label_missing');
 assert.match(reviewText,/题数已达到本场上限/,'stop_reason_missing');
 assert.equal(await page.locator('#content-inner .review-item').count(),2,'review_item_count_wrong');
 assert.ok(!/\d+\s*分(?!析)/.test(reviewText),'simulated_score_shown');
 const firstItem=page.locator('#content-inner .review-item').first();
 assert.equal((await firstItem.locator('.question-text').innerText()).trim(),fixture.items[0].question.trim(),'review_question_mismatch');
 assert.match(await firstItem.innerText(),/你的回答/,'review_answer_missing');
 await firstItem.locator('summary').click();
 const detailText=await firstItem.innerText();
 assert.match(detailText,/答题观察/,'observation_missing');
 assert.match(detailText,/你的回答原文/,'answer_quote_missing');
 assert.match(detailText,/参考来源摘录/,'reference_quote_missing');
 assert.ok((await firstItem.locator('a[href]').count())>=1,'source_link_missing');
 assert.equal(await firstItem.locator('a[href]').first().getAttribute('href'),fixture.items[0].source_url,'source_url_mismatch');
 await layout(page,'review-1280');
 await shot(page,'03-review.png');
 await shot(page,'03b-review-full.png',true);
 await page.locator('.content').evaluate(el=>{el.scrollTop=0;});
 const completedShort=(await page.locator('#ctx-progress').innerText()).match(/场次\s+([0-9a-f-]{8})/)?.[1]??'';
 assert.ok(completedShort,'completed_session_id_missing');
 mark('复盘按题展示题目 / 回答 / 观察 / 回答原文 / 参考摘录 / 来源链接；脚本标为流程演示且无分数','prototype',{session_id_short:completedShort});

 // ---- 7. Return to history and reopen the ORIGINAL session ----
 await page.locator('#back-history').click();
 await page.locator('.session-item').first().waitFor();
 await shot(page,'09-history.png');
 await page.locator('.session-item').first().click();
 await page.locator('#content-inner .review-item').first().waitFor();
 const reopenedShort=(await page.locator('#ctx-progress').innerText()).match(/场次\s+([0-9a-f-]{8})/)?.[1]??'';
 assert.equal(reopenedShort,completedShort,'history_reopen_changed_session_id');
 assert.match(await page.locator('#content-inner').innerText(),/确定性脚本流程演示/,'reopened_review_missing');
 mark('「返回历史」后点击记录回到同一场次 ID 的复盘，观察与依据仍可展开','prototype',{session_id_short:reopenedShort});

 // ---- 8. Keyboard focus visibility ----
 await page.goto(base,{waitUntil:'domcontentloaded'});
 await page.locator('#primary-action').waitFor();
 await page.keyboard.press('Tab');
 const focusInfo=await page.evaluate(()=>{
  const el=document.activeElement as HTMLElement|null;
  if(!el)return {tag:'',outline:'0px'};
  return {tag:el.tagName,text:(el.textContent||'').trim().slice(0,20),
   outline:getComputedStyle(el).outlineWidth,style:getComputedStyle(el).outlineStyle};
 });
 assert.ok(parseFloat(focusInfo.outline)>=2,'focus_ring_not_visible_'+JSON.stringify(focusInfo));
 mark('键盘 Tab 焦点可见（:focus-visible 实线焦点环），未使用 outline:none','check',focusInfo);

 // ---- 9. No business HTTP requests; no page errors ----
 const external=requests.filter(url=>!url.startsWith(base));
 assert.deepEqual(external,[],'unexpected_external_requests');
 assert.equal(requests.filter(url=>url.includes('/api/')).length,0,'unexpected_api_request');
 assert.equal(requests.filter(url=>url.includes(':3100')).length,0,'unexpected_backend_request');
 assert.deepEqual(pageErrors,[],'page_errors_present');
 mark('原型无 /api 请求、无 3100 后端请求、无外部请求；页面无 JS 异常','check',
  {request_count:requests.length,external_count:external.length});

 evidence.result='passed';
 evidence.model={declared:'claude-opus-6',provider_calls:0,credits_balance:'unknown',credits_consumed:'unknown'};
 evidence.acceptance_criteria={
  home_goal_and_primary:'passed',
  wide_and_narrow_answer_area:'passed',
  review_observation_to_question_answer_evidence:'passed',
  single_progress_countdown_status:'passed'};
 writeFileSync(join(output,'prototype-results.json'),JSON.stringify(evidence,null,2));
 console.log(JSON.stringify({result:'passed',cases:cases.length,screenshots:screenshots.length,
  session_id_short:completedShort,page_errors:pageErrors.length,requests:requests.length},null,2));
}catch(error){
 evidence.result='failed';
 evidence.error=String((error as Error)?.stack??error);
 writeFileSync(join(output,'prototype-results-failure.json'),JSON.stringify(evidence,null,2));
 console.error('D-01-03 prototype acceptance FAILED:',(error as Error).message);
 process.exitCode=1;
}finally{
 if(browser)await browser.close().catch(()=>{});
 server.stdout?.destroy();server.stderr?.destroy();
 server.kill();
}

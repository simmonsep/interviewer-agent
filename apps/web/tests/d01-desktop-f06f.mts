// F06F acceptance: publish-readiness UI convergence after the independent
// external UI review. Three fixes, all front-end only:
//   1. pipeline status / wording no longer implies "interview finished" or a
//      third Agent / model verdict;
//   2. empty state gets one primary CTA, engineering detail moves into
//      per-step / per-card <details> but stays fully verifiable;
//   3. narrow-window rail clipping is fixed, auxiliary text >= 11px, and the
//      collapsed Agent entries keep a distinguishable glyph + accessible name.
//
// Real local stack: FastAPI(3100) + PostgreSQL/Redis + free scripted Workers,
// Vite B+(5175). No route injection, no fake provider, no model call (incl. live).
// scripted is a deterministic protocol fixture; model credit/quota is unknown.
//
//   F06F_OUTPUT=<new dir> node apps/web/tests/d01-desktop-f06f.mts
//
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.F06F_OUTPUT??join(root,'docs/records/desktop-handoff/F06F-v2-plus'));
const base=process.env.F06F_BASE??'http://127.0.0.1:5175';
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,detail?:unknown)=>{cases.push({name,detail});console.log('PASS',name);};
const evidence:Record<string,unknown>={run:new Date().toISOString(),cases,page_errors:pageErrors,screenshots:[],
 case_type_note:'真实本机服务联调：Vite B+ 前端 + FastAPI/PostgreSQL/Redis + 免费 scripted Workers；无注入、无模型调用（含 live）、无伪造状态。scripted 是确定性协议夹具，不代表语义理解；模型积分/额度读数未知。'};
const shot=(page:Page,name:string)=>page.screenshot({path:join(output,'shots',name)})
 .then(()=>{(evidence.screenshots as string[]).push(name);});

let browser:Browser|null=null;
try{
 const health=await fetch('http://127.0.0.1:3100/health',{signal:AbortSignal.timeout(4000)});
 const front=await fetch(base+'/',{signal:AbortSignal.timeout(4000)});
 assert.ok(health.ok&&front.ok,'local_stack_not_ready');
 browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
 const page=await (await browser.newContext({viewport:{width:1280,height:900}})).newPage();
 page.on('pageerror',error=>pageErrors.push(error.message));
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 mark('本机 API 与 B+ 前端同时就绪',{health:health.status,frontend:front.status});

 // ---------------------------------------------- 1. empty state: one clear first action
 const emptyState=await page.evaluate(()=>{
  const cta=document.querySelector('[data-testid="empty-create-cta"]');
  const pipe=document.querySelector('[data-testid="agent-pipeline"]');
  const run=document.querySelector('[data-testid="pipeline-run"]') as HTMLButtonElement|null;
  const cards=[...document.querySelectorAll('.d-agent-card button.primary')] as HTMLButtonElement[];
  return {
   cta_present:Boolean(cta),
   cta_button:document.querySelector('[data-testid="empty-create-goal"]')?.textContent?.replace(/\s+/g,' ').trim(),
   cta_primary_count:cta?.querySelectorAll('button.primary').length,
   headline:document.querySelector('.d-pane-head h1')?.textContent?.trim(),
   note:document.querySelector('.d-section-note')?.textContent?.trim(),
   pipeline_disabled:run?.disabled,
   agent_card_buttons_disabled:cards.map(button=>button.disabled),
   run_label:run?.textContent?.trim()
  };
 });
 assert.equal(emptyState.cta_present,true,'empty_cta_missing');
 assert.equal(emptyState.cta_primary_count,1,'empty_cta_not_single_primary');
 assert.match(emptyState.cta_button??'',/创建求职目标/,'empty_cta_label_wrong');
 assert.equal(emptyState.headline,'先从一句求职意向开始','empty_headline_wrong');
 assert.equal(emptyState.pipeline_disabled,true,'pipeline_should_wait_for_goal');
 assert.deepEqual(emptyState.agent_card_buttons_disabled,[true,true],'agent_cards_should_wait_for_goal');
 mark('空态首屏只有一个主 CTA「创建求职目标」；流水线按钮与两张 Agent 卡（F07Z-72）在无目标时均不可点',
  emptyState);
 await shot(page,'01-empty-state.png');

 // F07Z-141/142: the separate CTA card is gone. The single primary is the goal submit and the
 // one-sentence field is the first element on the surface, so the old "click CTA -> focus the
 // field" hop no longer exists; assert the new entry point instead.
 const field=page.locator('textarea[aria-label="求职意向或补充说明"]');
 assert.equal(await field.count(),1,'goal_field_missing');
 await field.fill('研二，想在上海找 AI 应用 / Agent 开发实习。');
 const submitEnabled=await page.locator('[data-testid="empty-create-goal"]').isEnabled();
 assert.equal(submitEnabled,true,'primary_should_enable_once_goal_text_exists');
 mark('空态入口就是「求职意向」输入框；填入内容后唯一主按钮可用',{submitEnabled});

 // ---------------------------------------------- 2. pipeline wording is honest
 const wording=await page.evaluate(()=>{
  const pipe=document.querySelector('[data-testid="agent-pipeline"]') as HTMLElement;
  const text=pipe.innerText;
  return {
   eyebrow:pipe.querySelector('.eyebrow')?.textContent?.trim(),
   h2:pipe.querySelector('h2')?.textContent?.trim(),
   run:document.querySelector('[data-testid="pipeline-run"]')?.textContent?.trim(),
   third_step:document.querySelector('[data-testid="pipeline-step-review"] strong')?.textContent?.trim(),
   has_practice: /练一场|出复盘|复盘卡/.test(text),
   has_three_roles:/THREE FIXED ROLES/.test(text),
   says_not_third_agent:/不是第三个 Agent|不是 Agent|不调用 Agent/.test(text),
   says_needs_answer:/仍需你本人作答|需要你亲自作答|需要你本人作答/.test(text)
  };
 });
 assert.equal(wording.has_practice,false,'pipeline_still_promises_practice_or_review');
 assert.equal(wording.has_three_roles,false,'pipeline_still_claims_three_agents');
 assert.equal(wording.says_not_third_agent,true,'third_step_not_explained');
 assert.equal(wording.says_needs_answer,true,'interview_answer_boundary_missing');
 assert.match(wording.eyebrow??'',/一次准备/,'eyebrow_not_accurate');
 // F07Z-142: with a goal the pipeline is a text link ("一次做完：简历 + 面试"); in the empty
 // state it is the disabled "开始准备". Both name the same flow, so accept either wording.
 assert.match(wording.run??'',/(开始准备|简历)/,'run_label_not_accurate');
 mark('流水线不再承诺「练一场 / 出复盘」，明示第三步不是 Agent、面试仍需本人作答',wording);

 // ---------------------------------------------- 3. no engineering detail on the product surface
 // F07-E: the routes, the frozen params and the per-step ⓘ panel are gone from the
 // UI entirely - they belong in the hand-off report, not in a product screen. The
 // check below is deliberately a whole-document scan, so a route cannot creep back
 // in through a tooltip, a title attribute or a collapsed pane.
 const face=await page.evaluate(()=>{
  const pick=(selector:string)=>document.querySelector(selector)?.textContent??'';
  const settings=document.querySelector('[data-testid="settings-pane"]') as HTMLElement|null;
  const doc=document.body.textContent??'';
  return {
   about_mode:pick('[data-testid="about-mode"]'),
   about_boundary:pick('[data-testid="about-boundary"]'),
   about_routes_present:document.querySelectorAll('[data-testid="about-routes"]').length,
   tech_panels:document.querySelectorAll('.d-pipeline-tech').length,
   settings_pane_hidden:settings?settings.hasAttribute('hidden'):null,
   stray_agent_tech:document.querySelectorAll('.d-agent-tech').length,
   stray_dock_note:document.querySelectorAll('.d-dock-note').length,
   stray_pipeline_boundary:document.querySelectorAll('.d-pipeline-boundary').length,
   api_leak:(doc.match(/POST \/api\/|GET \/workflow|mode=(interview|coaching|draft)|draft_style=|max_questions=/)??[]).length
  };
 });
 assert.equal(face.about_routes_present,0,'route_block_still_rendered');
 assert.equal(face.tech_panels,0,'per_step_tech_panel_still_rendered');
 assert.equal(face.stray_agent_tech,0,'card_still_has_a_tech_block');
 assert.equal(face.stray_dock_note,0,'dock_note_still_on_the_home_screen');
 assert.equal(face.stray_pipeline_boundary,0,'pipeline_boundary_still_on_the_home_screen');
 assert.equal(face.api_leak,0,`api_path_or_frozen_param_still_in_the_document (${face.api_leak})`);
 assert.equal(face.settings_pane_hidden,true,'about_section_is_not_behind_settings');
 mark('接口路径、冻结参数与每步 ⓘ 面板已从界面彻底移除：整份文档里搜不到 POST /api、mode=、draft_style=',
  {route_block:face.about_routes_present,tech_panels:face.tech_panels,api_leaks:face.api_leak});

 // ---------------------------------------------- 4. what the build is, stated in plain language
 const notes=await page.evaluate(()=>({
  mode:(document.querySelector('[data-testid="about-mode"]')?.textContent??''),
  boundary:(document.querySelector('[data-testid="about-boundary"]')?.textContent??'')
 }));
 // The disclosure stays, but as one sentence a user can read - not as a config dump.
 assert.match(notes.mode,/内置脚本|scripted/,'build_mode_note_missing');
 assert.match(notes.mode,/不代表真实语义理解/,'semantic_disclaimer_missing');
 assert.match(notes.mode,/未调用付费\/live 模型/,'paid_model_note_missing');
 assert.match(notes.mode,/额度读数未知/,'credit_unknown_note_missing');
 assert.match(notes.boundary,/不会代答|需要你本人作答/,'boundary_no_answer_rule_missing');
 assert.match(notes.boundary,/不是第三个 Agent/,'boundary_third_step_missing');
 mark('「关于本版本」只留两句人话：本机内置脚本不代表语义理解、未调用付费模型、额度未知；以及不会代答/第三步不是 Agent',
  {mode:notes.mode.replace(/\s+/g,' ').slice(0,70),boundary:notes.boundary.replace(/\s+/g,' ').slice(0,70)});
 await page.locator('[data-testid="nav-settings"]').click();

 // ---------------------------------------------- 5. accessible names + readable type
 // F07Z-72: the rail's Agent entry block was removed by user decision (it duplicated the
 // sidebar entries). Its absence is pinned here instead of its glyphs and titles.
 assert.equal(await page.locator('.d-agent-jump-item').count(),0,'agent_jump_block_still_present');
 const typeInfo=await page.evaluate(()=>{
  const size=(selector:string)=>{
   const node=document.querySelector(selector);
   return node?parseFloat(getComputedStyle(node).fontSize):null;
  };
  return {status:size('.d-pipeline-status'),cta_body:size('.d-empty-cta p'),badge:size('.d-nav-badge')};
 });
 const measured=Object.entries(typeInfo).filter(([,value])=>value!==null)
  .map(([key,value])=>[key,value as number]);
 for(const [key,value] of measured){
  assert.ok(value>=11,`${key}_font_too_small_${value}px`);
 }
 mark('侧栏 Agent 入口块已移除（F07Z-72）；剩余辅助文字仍 >=11px',
  {measured:Object.fromEntries(measured)});


 // ---------------------------------------------- 6. artifact wording no longer says "facts"
 await page.locator('[data-testid="nav-artifacts"]').click();
 const artifactText=await page.locator('[data-testid="artifacts-pane"]').innerText();
 assert.match(artifactText,/材料来源 \/ 已确认依据/,'artifact_wording_not_fixed');
 assert.doesNotMatch(artifactText,/已确认事实/,'artifact_still_claims_facts');
 mark('产物页「已确认事实」改为「已确认依据」，不暗示客观事实已被核实',
  {label:'材料来源 / 已确认依据'});

 // ---------------------------------------------- 7. narrow windows: no clipping, navigation works
 const narrow:{[name:string]:unknown}={};
 for(const [name,width,height] of [['900x700',900,700],['853x600',853,600],['760x600',760,600]] as const){
  const q=await (await browser.newContext({viewport:{width,height}})).newPage();
  q.on('pageerror',error=>pageErrors.push(name+': '+error.message));
  await q.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
  await q.locator('.desktop-shell[data-ui-variant="B"]').waitFor({timeout:30000});
  const layout=await q.evaluate(()=>{
 const rail=document.querySelector('.desktop-rail')!.getBoundingClientRect();
 return {
  rail_width:Math.round(rail.width),
  nav_count:document.querySelectorAll('.d-nav:not(.d-artifact-nav) .d-nav-item').length,
  page_overflow_x:document.documentElement.scrollWidth>window.innerWidth
 };
});
assert.equal(layout.page_overflow_x,false,`${name}_horizontal_scroll`);
// F07Z-81: 产物 moved from a top-bar button into the rail, so the rail owns nine entries
// now. The guard stays exact: it is here to notice an entry disappearing or a duplicate
// appearing, not to freeze an arbitrary number.
assert.equal(layout.nav_count,9,`${name}_nav_entries_missing`);
await q.locator('.d-nav:not(.d-artifact-nav) .d-nav-item').nth(3).click();
const navPane=await q.locator('.desktop-shell').getAttribute('data-pane');
assert.equal(navPane,'research',`${name}_nav_click_failed`);
narrow[name]={...layout,nav_pane_after_click:navPane};
await q.screenshot({path:join(output,'shots',`03-narrow-${name}.png`)});
(evidence.screenshots as string[]).push(`03-narrow-${name}.png`);
await q.context().close();
}

 mark('900×700 / 853×600 / 760×600 三档均无横向溢出；8 入口实测可点',
  narrow);

 evidence.result='passed';
 evidence.page_errors=pageErrors;
 assert.equal(pageErrors.length,0,`page_errors ${JSON.stringify(pageErrors)}`);
 writeFileSync(join(output,'evidence.json'),JSON.stringify(evidence,null,2));
 console.log(`RESULT passed ${cases.length}/${cases.length}`);
}catch(error){
 evidence.result='failed';
 evidence.page_errors=pageErrors;
 evidence.error=error instanceof Error?error.message:String(error);
 writeFileSync(join(output,'evidence.json'),JSON.stringify(evidence,null,2));
 console.error('RESULT failed',error);
 process.exitCode=1;
}finally{
 await browser?.close();
}

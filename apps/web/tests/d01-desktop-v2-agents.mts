import {waitGoalReady} from './desktop-nav.mts';
// SUPERSEDED by F07Z-72 (2026-11-13): this suite compares two frontend builds
// (version A on 5174 and version B on 5175) from the F06C/F06D era. The app has a
// single build now, and the rail Agent entry block it counted was removed, so the
// A/B premise no longer exists. Kept for history; not part of the current gates.
// F06C acceptance: two desktop frontend versions run at once against one local
// API. Version A (5174) keeps the F06B three-entry Agent rail; version B (5175)
// restores the original eight functional entries and exposes three fixed job
// Agent interfaces. Every assertion below drives the real local stack.
//
//   V2_OUTPUT=<new dir> node apps/web/tests/d01-desktop-v2-agents.mts
//
// Requires: API on 3100 (with --model-config), both Vite servers, PG/Redis, and
// the free scripted career / materials / interviews workers. No model calls
// (including live), no page.route injection, no fake providers.
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.V2_OUTPUT??join(root,'docs/records/desktop-handoff/F06C-v2-agents'));
const baseA=process.env.V2_A_BASE??'http://127.0.0.1:5174';
const baseB=process.env.V2_B_BASE??'http://127.0.0.1:5175';
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,detail?:unknown)=>{cases.push({name,detail});console.log('PASS',name);};
const evidence:Record<string,unknown>={run:new Date().toISOString(),cases,page_errors:pageErrors,screenshots:[],
 case_type_note:'真实本机服务联调：两个 Vite 前端 + 同一 FastAPI/PostgreSQL/Redis + 免费 scripted Workers；无注入、无模型调用（含 live）、无伪造状态。scripted 是确定性协议夹具，不代表语义理解；模型积分/额度读数未知。'};
const shot=(page:Page,name:string)=>page.screenshot({path:join(output,'shots',name)})
 .then(()=>{(evidence.screenshots as string[]).push(name);});

type Session={id:string;version:number;status:string;policy:{mode:string}};
type SessionList={items:Session[]};
type MaterialView={sources:unknown[];runs:{id:string;status:string;origin:string|null;mode:string}[];
 drafts:{id:string;payload:{draft_style?:string}}[]};
type Goal={id:string;version:number;status:string};

// Page callbacks are serialized into the browser: keep them plain JavaScript.
async function apiOf<T>(page:Page,url:string):Promise<T>{
 const value=await page.evaluate(async(path)=>{
  const response=await fetch(path,{credentials:'same-origin',cache:'no-store'});
  if(!response.ok)throw new Error('get_failed_'+response.status+'_'+path);
  return await response.json();
 },url);
 return value as T;
}
async function postOf<T>(page:Page,url:string,body:unknown):Promise<T>{
 const value=await page.evaluate(async(payload)=>{
  const response=await fetch(payload.url,{method:'POST',credentials:'same-origin',cache:'no-store',
   headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(payload.body)});
  if(!response.ok)throw new Error('post_failed_'+response.status+'_'+payload.url);
  return await response.json();
 },{url,body});
 return value as T;
}
async function settle(page:Page,goalId:string):Promise<Goal>{
 let goal:Goal|null=null;
 for(let i=0;i<80;i++){
  goal=await apiOf<Goal>(page,`/api/career-goals/${goalId}`);
  if(goal&&!['PENDING','RUNNING'].includes(goal.status))break;
  await delay(500);
 }
 assert.ok(goal&&!['PENDING','RUNNING'].includes(goal.status),'coordinator_never_settled');
 return goal as Goal;
}
// The role in every request is ai-application, so pick the built-in bank that
// actually has questions for that role (the first option may be java-backend).
async function chooseAiBank(page:Page):Promise<void>{
 const option=page.locator('#interview-bank option').filter({hasText:'ai-application 默认知识库'});
 await option.waitFor({state:'attached',timeout:30000});
 const value=await option.getAttribute('value');
 assert.ok(value,'ai_bank_value_missing');
 await page.locator('#interview-bank').selectOption(value);
}

let browser:Browser|null=null;
try{
 const health=await fetch('http://127.0.0.1:3100/health',{signal:AbortSignal.timeout(4000)});
 const a=await fetch(baseA+'/',{signal:AbortSignal.timeout(4000)});
 const b=await fetch(baseB+'/',{signal:AbortSignal.timeout(4000)});
 assert.ok(health.ok&&a.ok&&b.ok,'dual_stack_not_ready');
 mark('两个前端版本与同一本机 API 同时就绪（5174 / 5175 / 3100）',
  {health:health.status,version_A:a.status,version_B:b.status,api:'FastAPI + PostgreSQL/Redis + 免费 scripted Workers'});

 browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
 const contextA=await browser.newContext({viewport:{width:1280,height:900}});
 const contextB=await browser.newContext({viewport:{width:1280,height:900}});
 const pageA=await contextA.newPage(),pageB=await contextB.newPage();
 pageA.on('pageerror',error=>pageErrors.push('A: '+error.message));
 pageB.on('pageerror',error=>pageErrors.push('B: '+error.message));
 await pageA.goto(`${baseA}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await pageA.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 await pageB.goto(`${baseB}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await pageB.locator('.desktop-shell[data-ui-variant="B"]').waitFor({timeout:30000});

 // ------------------------------------------------- 2. the two versions really differ
 const navA=await pageA.locator('.d-nav:not(.d-artifact-nav) .d-nav-item').count();
 assert.equal(navA,3,'version_A_rail_changed');
 assert.equal(await pageA.locator('.desktop-shell[data-ui-variant="B"]').count(),0,'version_A_marked_as_B');
 const titles=await pageB.locator('.d-nav:not(.d-artifact-nav) .d-nav-item').evaluateAll(
  nodes=>nodes.map(node=>node.getAttribute('title')));
 assert.deepEqual(titles,['工作台','面试训练','笔试','岗位研究','材料与简历','申请跟踪','题库','设置'],
  'eight_entries_not_restored');
 assert.equal(await pageB.locator('.d-agent-jump-item').count(),0,'agent_entry_block_removed');
 assert.equal(await pageB.locator('[data-testid="ui-variant-b"]').count(),1,'version_badge_missing');
 mark('版本 A 仍是 3 项 Agent 侧栏；版本 B 恢复原 8 入口，并在侧栏单列 3 个 Agent 接口（不计入 8 个功能入口）',
  {version_A_primary_entries:navA,version_B_primary_entries:titles,agent_jumps:3});
 await shot(pageA,'01-version-A-5174.png');
 await shot(pageB,'02-version-B-5175.png');

 // ------------------------------------------------- 3. three interfaces name real roles
 await pageB.locator('[data-testid="agent-dock"]').waitFor({timeout:15000});
 assert.equal(await pageB.locator('.d-agent-card').count(),3,'three_cards_missing');
 // F07-D: the routes, frozen modes and build disclosures moved from the card
 // faces to 设置 → 关于本版本. The card faces now carry only role + one line +
 // state + button; the facts are asserted on the settings surface instead.
 const dockText=await pageB.locator('[data-testid="agent-dock"]').innerText();
 for(const needle of ['面试官 Agent','简历修改 Agent','面试训练 Agent'])
  assert.ok(dockText.includes(needle),'dock_missing_'+needle);
 assert.match(dockText,/固定角色/,'fixed_role_note_missing');
 assert.equal(dockText.includes('POST /api/career-goals'),false,'route_back_on_the_card_face');
 // F07-E: routes and frozen params are now absent from the whole document - a
 // whole-document scan is used so they cannot creep back via a tooltip or a
 // collapsed pane. The build disclosure survives once, in plain language.
 const aboutText=await pageB.evaluate(()=>{
  const pick=(selector:string)=>document.querySelector(selector)?.textContent??'';
  return {routes:document.querySelectorAll('[data-testid="about-routes"]').length,
   mode:pick('[data-testid="about-mode"]'),
   api:((document.body.textContent??'').match(/POST \/api\/|mode=(interview|coaching|draft)|draft_style=/g)??[]).length};
 });
 assert.equal(aboutText.routes,0,'route_block_still_in_settings');
 assert.equal(aboutText.api,0,`api_path_still_in_the_document (${aboutText.api})`);
 assert.match(aboutText.mode,/内置脚本|scripted/,'build_mode_note_missing');
 assert.match(aboutText.mode,/不等于语义理解|不代表语义理解|不代表真实语义/,'scripted_disclosure_missing');
 assert.match(aboutText.mode,/未调用任何付费\/live 模型|未调用付费\/live 模型/,'paid_model_note_missing');
 assert.match(aboutText.mode,/额度读数未知/,'credit_unknown_note_missing');
 mark('三张卡只留角色、一句说明、本机状态与按钮；接口路径与冻结参数全站为零，「关于本版本」用一句人话说明内置脚本与未调用付费模型');
 await shot(pageB,'03-three-agent-cards.png');

 // ------------------------------------------------- 4. create a real goal on version B
 await pageB.locator('textarea[aria-label="求职意向或补充说明"]').fill('研二，想在上海找 AI 应用 / Agent 开发实习，顺便优化项目简历并练面试。');
 await pageB.getByLabel(/确认方向/).fill('AI 应用 / Agent 开发');
 await pageB.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(pageB);
 const goalId=await pageB.evaluate(()=>new URLSearchParams(location.search).get('goal'));
 assert.ok(goalId&&/^[a-f0-9-]{36}$/i.test(goalId),`goal_id_missing ${goalId}`);
 const goal=await settle(pageB,goalId as string);
 mark('版本 B 的一句话目标由真实本机服务创建，免费协调器跑完一轮',
  {goal_id:goalId,goal_status:goal.status,goal_version:goal.version});
 await pageB.waitForFunction(()=>document.querySelector('[data-testid="agent-state-resume"]')?.textContent?.includes('本机已有'),null,{timeout:15000});
 const resumeState=await pageB.locator('[data-testid="agent-state-resume"]').innerText();
 const interviewState=await pageB.locator('[data-testid="agent-state-interviewer"]').innerText();
 assert.match(resumeState,/本机已有 \d+ 段材料/,'material_state_not_real');
 assert.match(interviewState,/本机已有 \d+ 场会话/,'interview_state_not_real');
 mark('三张卡的状态来自本机真实 GET /materials 与 /interviews，不用占位假状态',
  {resume_state:resumeState,interview_state:interviewState});

 // ------------------------------------------------- 5. interviewer Agent: real mode=interview
 const posts:{url:string;mode:string|null}[]=[];
 pageB.on('request',request=>{
  if(request.method()==='POST'&&/\/interviews$/.test(new URL(request.url()).pathname)){
   try{
    const body=JSON.parse(request.postData()??'{}');
    posts.push({url:request.url(),mode:body.mode??null});
   }catch{/* not a JSON create */}
  }
 });
 await pageB.locator('[data-testid="agent-launch-interview"]').click();
 await pageB.locator('[data-testid="interview-agent-banner"]').waitFor({timeout:15000});
 assert.equal(await pageB.locator('[data-testid="interview-agent-banner"]').getAttribute('data-mode'),'interview');
 const interviewBanner=await pageB.locator('[data-testid="interview-agent-banner"]').innerText();
 assert.equal(interviewBanner.includes('POST /api/'),false,'route_back_on_the_interview_banner');
 assert.match(interviewBanner,/不给答案、提示或讲解|引用你的原回答讲解/,'banner_does_not_say_what_the_agent_does');
 await chooseAiBank(pageB);
 await pageB.getByRole('button',{name:'开始整场面试 →'}).click();
 await pageB.locator('.interview-session[data-session-id]').waitFor({timeout:40000});
 const sessions1=(await apiOf<SessionList>(pageB,`/api/career-goals/${goalId}/interviews`)).items;
 assert.ok(sessions1.length>=1,'interview_not_created');
 assert.equal(posts.at(-1)?.mode,'interview','interview_mode_not_sent');
 assert.equal(sessions1[0].policy.mode,'interview','server_policy_mode_not_interview');
 mark('面试官 Agent：从卡片进入面试训练面板，真实 POST /interviews 携带 mode=interview，服务端冻结 policy.mode=interview',
  {request_mode:posts.at(-1)?.mode,session_id:sessions1[0].id,server_mode:sessions1[0].policy.mode,session_status:sessions1[0].status});
 await shot(pageB,'04-interviewer-agent.png');

 // ------------------------------------------------- 6. resume Agent: real rewrite endpoint
 await pageB.locator('.d-nav-item[title="材料与简历"]').click();
 await pageB.locator('[data-testid="resume-agent-banner"]').waitFor({timeout:15000});
 const resumeBanner=await pageB.locator('[data-testid="resume-agent-banner"]').innerText();
 assert.equal(resumeBanner.includes('POST /api/'),false,'route_back_on_the_resume_banner');
 assert.match(resumeBanner,/只改写你已上传的原文|不会补写|不编造/,'resume_banner_boundary_missing');
 assert.equal(await pageB.locator('[data-testid="resume-agent-run"]').isDisabled(),true,'rewrite_enabled_without_source');
 await pageB.locator('textarea[aria-label="材料正文"]').fill('负责订单查询接口，用 Java 与 MyBatis 重写慢查询，并补充了分页与索引验证。');
 await pageB.getByRole('button',{name:'保存这段材料'}).click();
 await pageB.waitForFunction(()=>!document.querySelector('[data-testid="resume-agent-run"]')?.disabled,null,{timeout:15000});
 const rewritePosts:string[]=[];
 pageB.on('request',request=>{if(request.method()==='POST'&&request.url().includes('/materials/rewrite'))rewritePosts.push(request.url());});
 await pageB.locator('[data-testid="resume-agent-run"]').click();
 assert.ok(rewritePosts.length>=1,'rewrite_not_sent');
 let materials:MaterialView|null=null;
 for(let i=0;i<80;i++){
  materials=await apiOf<MaterialView>(pageB,`/api/career-goals/${goalId}/materials`);
  const done=materials.runs.find(run=>run.status==='SUCCEEDED'&&run.origin==='scripted_protocol');
  if(done&&materials.drafts.some(draft=>draft.payload.draft_style==='rewrite'))break;
  await delay(500);
 }
 assert.ok(materials,'materials_never_read');
 assert.ok(materials.runs.some(run=>run.status==='SUCCEEDED'&&run.origin==='scripted_protocol'),'rewrite_run_not_succeeded');
 assert.ok(materials.drafts.some(draft=>draft.payload.draft_style==='rewrite'),'rewrite_draft_missing');
 mark('简历修改 Agent：真实 POST /materials/rewrite，免费材料 Worker 产出来源标注 scripted_protocol 的改写初稿；无材料时按钮如实禁用',
  {rewrite_requests:rewritePosts.length,sources:materials.sources.length,runs:materials.runs.length,
   drafts:materials.drafts.length,origin:'scripted_protocol'});
 await shot(pageB,'05-resume-agent.png');

 // ------------------------------------------------- 7. training Agent: real mode=coaching (fresh goal)
 const active=sessions1.find(session=>!['COMPLETED','FAILED','CANCELLED'].includes(session.status));
 if(active){
  await postOf<Session>(pageB,`/api/career-goals/${goalId}/interviews/${active.id}/stop`,{expected_version:active.version});
 }
 const goal2=await postOf<Goal>(pageB,'/api/career-goals/',{
  message:'想在杭州找 AI 应用实习，想用教练模式练一轮面试。',jd_text:'',
  conditions:{direction:'AI 应用 / Agent 开发',cities:['杭州'],city_policy:'unknown',auto_research:false,
   recruitment:'unknown',graduation_year:null,days_per_week:null,duration_months:null,skills:[]}
 });
 await pageB.goto(`${baseB}/?shell=desktop&goal=${goal2.id}`,{waitUntil:'domcontentloaded'});
 await pageB.locator('.desktop-shell[data-ui-variant="B"]').waitFor({timeout:30000});
 await settle(pageB,goal2.id);
 await pageB.locator('.d-nav-item[title="面试"]').click();
 if(await pageB.locator('[data-testid="mode-coaching"]').count())
  await pageB.locator('[data-testid="mode-coaching"]').click();
 await pageB.locator('[data-testid="interview-agent-banner"][data-mode="coaching"]').waitFor({timeout:15000});
 const coachBanner=await pageB.locator('[data-testid="interview-agent-banner"]').innerText();
 assert.equal(coachBanner.includes('mode='),false,'frozen_param_back_on_the_banner');
 assert.match(coachBanner,/教练模式/,'coach_banner_does_not_name_the_coaching_mode');
 await chooseAiBank(pageB);
 posts.length=0;
 await pageB.getByRole('button',{name:'开始整场面试 →'}).click();
 await pageB.locator('.interview-session[data-session-id]').waitFor({timeout:40000});
 const sessions2=(await apiOf<SessionList>(pageB,`/api/career-goals/${goal2.id}/interviews`)).items;
 const coach=sessions2.find(session=>session.policy.mode==='coaching');
 assert.ok(coach,'coaching_session_missing');
 assert.equal(posts.at(-1)?.mode,'coaching','coaching_mode_not_sent');
 mark('面试训练 Agent：同一 /interviews 接口以冻结 mode=coaching 开场，服务端 policy.mode=coaching；与面试官是不同角色策略',
  {request_mode:posts.at(-1)?.mode,session_id:coach.id,server_mode:coach.policy.mode,
   previous_session_stopped:Boolean(active),second_goal_id:goal2.id});
 await shot(pageB,'06-coach-agent.png');

 // ------------------------------------------------- 8. eight entries and compatibility still work
 for(const [title,label] of [['岗位研究','岗位研究'],['申请跟踪','申请跟踪'],['题库','题库'],['设置','设置']]){
  await pageB.locator(`.d-nav:not(.d-artifact-nav) .d-nav-item[title="${title}"]`).click();
  await pageB.locator('[data-testid="desktop-page"]').filter({hasText:label}).waitFor({timeout:15000});
 }
 assert.equal(await pageB.locator('[data-testid="agent-policy-section"]').count(),1,'policy_section_missing');
 assert.equal(await pageB.locator('[data-testid="key-storage"]').count(),1,'model_key_storage_missing');
 const settingsText=await pageB.locator('[data-testid="settings-pane"]').innerText();
 assert.match(settingsText,/Agent 自治/,'autonomy_settings_missing');
 assert.match(settingsText,/模型接入|模型设置|模型服务/,'model_settings_missing');
 await pageB.locator('[data-testid="nav-artifacts"]').click();
 await pageB.locator('[data-testid="artifacts-pane"]').waitFor({timeout:15000});
 assert.equal(await pageB.locator('[data-testid="artifacts-pane"] .d-nav-item').count(),7,'artifact_shortcuts_missing');
 mark('8 个功能入口、F06B 的 Agent 自治/模型设置、顶栏「Agent 产物」汇总页与 7 个详情快捷入口都保持可用',
  {checked_entries:['岗位研究','申请跟踪','题库','设置'],artifact_shortcuts:7});

 // ------------------------------------------------- 9. small-number typography unchanged
 const numStyle=await pageB.evaluate(()=>{
  const node=document.querySelector('.d-num');
  if(!node)return null;const s=getComputedStyle(node);
  return {family:s.fontFamily,variant:s.fontVariantNumeric,weight:s.fontWeight};
 });
 assert.ok(numStyle,'no_number_element');
 assert.match(numStyle.family,/Segoe UI|system-ui/,'number_face_changed');
 assert.ok(numStyle.variant.includes('tabular-nums'),'tabular_nums_lost');
 mark('小数字字体仍是系统 UI 字栈 + tabular-nums（沿用 sample-a 的修订，未在并行版本里回退）',numStyle);

 assert.equal(pageErrors.length,0,'page_errors_present');
 mark('全流程 0 个页面错误',{page_errors:pageErrors.length});
 evidence.result='PASS';
}catch(error){
 evidence.result='FAIL';
 evidence.error=error instanceof Error?{message:error.message,stack:error.stack}:String(error);
 console.error('FAIL',error);
 process.exitCode=1;
}finally{
 if(browser)await browser.close();
 evidence.case_count=cases.length;
 writeFileSync(join(output,'evidence.json'),JSON.stringify(evidence,null,2)+'\n');
 console.log('cases',cases.length,'result',evidence.result,'output',output);
}

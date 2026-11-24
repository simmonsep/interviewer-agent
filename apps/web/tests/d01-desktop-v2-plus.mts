import {waitGoalReady} from './desktop-nav.mts';
// SUPERSEDED by F07Z-72 (2026-11-13): this suite compares two frontend builds
// (version A on 5174 and version B on 5175) from the F06C/F06D era. The app has a
// single build now, and the rail Agent entry block it counted was removed, so the
// A/B premise no longer exists. Kept for history; not part of the current gates.
// F06D acceptance: the B+ main line (5175) keeps eight functional entries,
// puts one-sentence Agent input in the top bar, turns rail entries into real
// result views, and orchestrates the three fixed job Agents in one click.
// Every assertion below drives the real local stack: FastAPI + PostgreSQL/Redis
// + free scripted Workers. No route injection, no fake providers, no model
// calls (including live). scripted is a deterministic protocol fixture, not
// semantic understanding; model credit/quota readings are unknown.
//
//   V2P_OUTPUT=<new dir> node apps/web/tests/d01-desktop-v2-plus.mts
//
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.V2P_OUTPUT??join(root,'docs/records/desktop-handoff/F06D-v2-plus'));
const baseA=process.env.V2P_A_BASE??'http://127.0.0.1:5174';
const baseB=process.env.V2P_B_BASE??'http://127.0.0.1:5175';
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,detail?:unknown)=>{cases.push({name,detail});console.log('PASS',name);};
const evidence:Record<string,unknown>={run:new Date().toISOString(),cases,page_errors:pageErrors,screenshots:[],
 case_type_note:'真实本机服务联调：两个 Vite 前端 + 同一 FastAPI/PostgreSQL/Redis + 免费 scripted Workers；无注入、无模型调用（含 live）、无伪造状态。scripted 是确定性协议夹具，不代表语义理解；模型积分/额度读数未知。'};
const shot=(page:Page,name:string)=>page.screenshot({path:join(output,'shots',name)})
 .then(()=>{(evidence.screenshots as string[]).push(name);});

type Goal={id:string;version:number;status:string};
type MaterialView={sources:unknown[];runs:{id:string;status:string;origin:string|null}[];
 drafts:{id:string}[];material_revision:number};
type SessionList={items:{id:string;status:string;policy:{mode:string}}[]};
type WorkflowView={selected_job:{title:string}|null;interviews:unknown[];written_tests:unknown[]};

async function apiOf<T>(page:Page,url:string):Promise<T>{
 return await page.evaluate(async(path)=>{
  const response=await fetch(path,{credentials:'same-origin',cache:'no-store'});
  if(!response.ok)throw new Error('get_failed_'+response.status+'_'+path);
  return await response.json();
 },url) as T;
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

let browser:Browser|null=null;
try{
 const health=await fetch('http://127.0.0.1:3100/health',{signal:AbortSignal.timeout(4000)});
 const [a,b]=await Promise.all([
  fetch(baseA+'/',{signal:AbortSignal.timeout(4000)}),
  fetch(baseB+'/',{signal:AbortSignal.timeout(4000)})]);
 assert.ok(health.ok&&a.ok&&b.ok,'dual_stack_not_ready');
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
 mark('两个前端与同一本机 API 同时就绪（5174 / 5175 / 3100）',
  {health:health.status,version_A:a.status,version_B:b.status});

 // ------------------------------------------------ 2. B+ structure, A untouched
 const navA=await pageA.locator('.d-nav:not(.d-artifact-nav) .d-nav-item').count();
 const titles=await pageB.locator('.d-nav:not(.d-artifact-nav) .d-nav-item').evaluateAll(
  nodes=>nodes.map(node=>node.getAttribute('title')));
 assert.equal(navA,3,'version_A_rail_changed');
 assert.deepEqual(titles,['工作台','面试训练','笔试','岗位研究','材料与简历','申请跟踪','题库','设置'],
  'eight_entries_not_restored');
 assert.equal(await pageB.locator('.d-agent-jump-item').count(),0,'agent_entry_block_removed');
 assert.equal(await pageB.locator('.d-agent-card').count(),3,'three_cards_missing');
 assert.equal(await pageB.locator('[data-testid="agent-omnibox"]').count(),1,'omnibox_missing');
 assert.equal(await pageB.locator('[data-testid="agent-pipeline"]').count(),1,'pipeline_missing');
 assert.equal(await pageB.locator('.d-pipeline-steps li').count(),3,'pipeline_phases_missing');
 assert.equal(await pageA.locator('[data-testid="agent-omnibox"]').count(),0,'version_A_changed');
 mark('B+ 保留 8 入口与 3 个 Agent 接口，新增顶栏一句话与三步流水线；A 版 3 项侧栏与代码保持不动',
  {version_A_primary_entries:navA,version_B_primary_entries:titles,agent_jumps:3,pipeline_phases:3});
 await shot(pageA,'01-version-A-unchanged.png');
 await shot(pageB,'02-version-B-plus.png');

 // ------------------------------------------------ 3. top-bar one sentence is real
 const created:string[]=[];
 pageB.on('request',request=>{
  if(request.method()==='POST'&&new URL(request.url()).pathname==='/api/career-goals/')created.push(request.url());
 });
 await pageB.locator('[data-testid="agent-omnibox"] input').fill('研二，想在上海找 AI 应用实习；帮我把项目经历改成简历再练一场。');
 await pageB.locator('[data-testid="agent-omnibox-send"]').click();
 await waitGoalReady(pageB);
 const goalId=await pageB.evaluate(()=>new URLSearchParams(location.search).get('goal'));
 assert.ok(goalId&&/^[a-f0-9-]{36}$/i.test(goalId),`goal_id_missing ${goalId}`);
 assert.ok(created.length>=1,'top_bar_did_not_post_real_create');
 const goal=await settle(pageB,goalId as string);
 mark('顶栏一句话真实 POST /api/career-goals/ 创建目标，免费协调器跑完一轮',
  {goal_id:goalId,goal_status:goal.status,goal_version:goal.version,create_requests:created.length});
 assert.equal(await pageB.locator('[data-testid="nav-badge-materials"]').count(),0,'badge_invented_before_result');
 assert.equal(await pageB.locator('[data-testid="nav-badge-interviews"]').count(),0,'badge_invented_before_session');
 mark('目标下尚无初稿与场次时侧栏不渲染任何角标（没有读数就不猜数字）',
  {materials_badges:0,interviews_badges:0});
 await shot(pageB,'03-topbar-one-sentence.png');

 // ------------------------------------------------ 4. pipeline refuses to invent material
 const rewritePosts:string[]=[];
 const interviewPosts:{mode:string|null}[]=[];
 pageB.on('request',request=>{
  const path=new URL(request.url()).pathname;
  if(request.method()==='POST'&&path.endsWith('/materials/rewrite'))rewritePosts.push(path);
  if(request.method()==='POST'&&path.endsWith('/interviews')){
   try{
    const body=JSON.parse(request.postData()??'{}');
    interviewPosts.push({mode:body.mode??null});
   }catch{/* not a JSON create */}
  }
 });
 await pageB.locator('[data-testid="pipeline-run"]').click();
 await pageB.locator('[data-testid="pipeline-error"]').waitFor({timeout:15000});
 const refusal=await pageB.locator('[data-testid="pipeline-error"]').innerText();
 assert.match(refusal,/材料原文/,'preflight_reason_missing');
 assert.equal(rewritePosts.length,0,'rewrite_sent_without_source');
 assert.equal(interviewPosts.length,0,'interview_sent_without_source');
 assert.equal(await pageB.locator('[data-testid="pipeline-step-rewrite"]').getAttribute('data-phase'),'failed');
 assert.equal(await pageB.locator('[data-testid="pipeline-step-review"]').getAttribute('data-phase'),'skipped');
 mark('流水线在无材料时如实拒绝：给出具体原因、后续步骤标记未执行，且不向后端写任何任务',
  {reason:refusal,rewrite_requests:rewritePosts.length,interview_requests:interviewPosts.length});

 // ------------------------------------------------ 5. add a real material source
 await pageB.locator('.d-nav-item[title="材料与简历"]').click();
 await pageB.locator('[data-testid="resume-agent-banner"]').waitFor({timeout:15000});
 await pageB.locator('textarea[aria-label="材料正文"]').fill('负责订单查询接口，用 Java 与 MyBatis 重写慢查询，并补充了分页与索引验证。');
 await pageB.getByRole('button',{name:'保存这段材料'}).click();
 await pageB.waitForFunction(()=>!document.querySelector('[data-testid="resume-agent-run"]')?.disabled,null,{timeout:20000});
 const materialBefore=await apiOf<MaterialView>(pageB,`/api/career-goals/${goalId}/materials`);
 assert.ok(materialBefore.sources.length>=1,'material_source_not_saved');
 await pageB.locator('[data-testid="agent-home"]').click();
 mark('材料原文由真实 POST /materials/sources 保存，简历修改 Agent 的运行按钮随之解禁',
  {sources:materialBefore.sources.length,material_revision:materialBefore.material_revision});

 // ------------------------------------------------ 6. one click drives three real endpoints
 await pageB.locator('[data-testid="pipeline-run"]').click();
 await pageB.locator('[data-testid="pipeline-step-review"][data-phase="done"]').waitFor({timeout:120000});
 assert.equal(await pageB.locator('[data-testid="pipeline-error"]').count(),0,'pipeline_unexpected_error');
 assert.ok(rewritePosts.length>=1,'rewrite_not_sent');
 assert.ok(interviewPosts.some(item=>item.mode==='interview'),'interview_mode_not_sent');
 const rewritePhase=await pageB.locator('[data-testid="pipeline-step-rewrite"]').getAttribute('data-phase');
 const interviewPhase=await pageB.locator('[data-testid="pipeline-step-interview"]').getAttribute('data-phase');
 assert.ok(rewritePhase==='done'||rewritePhase==='waiting',`rewrite_phase_unexpected ${rewritePhase}`);
 assert.equal(interviewPhase,'done','interview_phase_not_done');
 // F07-E: the step reports what happened in product language, and carries no
 // route or request parameter - the frozen mode is proven by the server response
 // asserted below, not by printing it on the page.
 const interviewDetail=await pageB.locator('[data-testid="pipeline-step-interview"]').innerText();
 assert.equal(/POST \/api\/|mode=|policy\./.test(interviewDetail),false,'route_back_in_the_step_detail');
 assert.match(interviewDetail,/待作答/,`interview_step_not_marked_as_unanswered: ${interviewDetail.replace(/\s+/g,' ')}`);
 const sessions=(await apiOf<SessionList>(pageB,`/api/career-goals/${goalId}/interviews`)).items;
 assert.ok(sessions.length>=1,'session_not_created');
 assert.equal(sessions.find(item=>!['COMPLETED','FAILED','CANCELLED'].includes(item.status))?.policy.mode,'interview',
  'server_policy_mode_not_interview');
 mark('一键流水线按顺序真实调用 rewrite → interviews(mode=interview) → 本机记录汇总，服务端冻结 policy.mode=interview',
  {rewrite_requests:rewritePosts.length,rewrite_phase:rewritePhase,interview_mode:interviewPosts.at(-1)?.mode,
   sessions:sessions.length,server_mode:sessions[0].policy.mode});
 // F06F: an interface success is NOT a finished interview. The top-level tag
 // must say "created / waiting for your answers", never "已完成".
 const interviewTag=await pageB.locator('[data-testid="pipeline-tag-interview"]').innerText();
 assert.match(interviewTag,/待作答/,'interview_tag_missing_pending');
 assert.doesNotMatch(interviewTag,/已完成/,'interview_tag_implies_finished');
 const pending=sessions.find(item=>!['COMPLETED','FAILED','CANCELLED'].includes(item.status));
 assert.ok(pending,'pending_session_not_found');
 const summaryTitle=await pageB.locator('[data-testid="pipeline-summary"] h3').innerText();
 assert.doesNotMatch(summaryTitle,/复盘卡/,'summary_still_called_review_card');
 mark('已创建但未作答的面试标为「面试已创建 · 待作答」而非「已完成」；摘要不再叫复盘卡',
  {tag:interviewTag,server_status:pending.status,summary_title:summaryTitle});
 await shot(pageB,'04-pipeline-complete.png');

 // ------------------------------------------------ 7. review numbers and badges are real
 const [materialsAfter,workflow]=await Promise.all([
  apiOf<MaterialView>(pageB,`/api/career-goals/${goalId}/materials`),
  apiOf<WorkflowView>(pageB,`/api/career-goals/${goalId}/workflow`)
 ]);
 // innerText inserts line breaks between the number and its label; compare on
 // whitespace-free text so the assertion still checks the exact real count.
 const summaryText=(await pageB.locator('[data-testid="pipeline-summary"]').innerText()).replace(/\s+/g,'');
 assert.ok(summaryText.includes(`${materialsAfter.drafts.length}版改写初稿`),'summary_draft_count_wrong');
 assert.ok(summaryText.includes(`${sessions.length}场面试会话`),'summary_session_count_wrong');
 assert.match(summaryText,/不是能力判定或模型评语|不是模型生成的评语/,'review_disclaimer_missing');
 assert.ok((materialsAfter.runs??[]).some(item=>item.origin==='scripted_protocol'),'scripted_origin_missing');
 await pageB.waitForFunction(()=>document.querySelector('[data-testid="nav-badge-materials"]')?.textContent?.includes('初稿'),
  null,{timeout:20000});
 const materialBadge=await pageB.locator('[data-testid="nav-badge-materials"]').innerText();
 const interviewBadge=await pageB.locator('[data-testid="nav-badge-interviews"]').innerText();
 assert.ok(materialBadge.includes(String(materialsAfter.drafts.length)),'material_badge_not_real');
 assert.ok(interviewBadge.includes(String(sessions.length)),'interview_badge_not_real');
 const badgeFont=await pageB.evaluate(()=>{
  const node=document.querySelector('[data-testid="nav-badge-materials"]');
  if(!node)return null;
  const style=getComputedStyle(node);
  return {family:style.fontFamily,numeric:style.fontVariantNumeric};
 });
 assert.ok(badgeFont&&/Segoe UI|system-ui|Microsoft YaHei UI/.test(badgeFont.family),'badge_font_not_system_ui');
 assert.equal(badgeFont?.numeric,'tabular-nums','badge_tabular_nums_missing');
 mark('本次执行摘要与侧栏角标的数字都来自真实 GET /materials、/interviews、/workflow；角标沿用系统 UI 字栈与 tabular-nums',
  {drafts:materialsAfter.drafts.length,sessions:sessions.length,written:(workflow.written_tests??[]).length,
   material_badge:materialBadge,interview_badge:interviewBadge,badge_font:badgeFont,
   scripted_origin:'scripted_protocol'});
 await shot(pageB,'05-rail-result-badges.png');

 // ------------------------------------------------ 8. fixed roles + coordinator still reachable
 await pageB.locator('.d-nav-item[title="面试"]').click();
 if(await pageB.locator('[data-testid="mode-coaching"]').count())
  await pageB.locator('[data-testid="mode-coaching"]').click();
 await pageB.locator('[data-testid="interview-agent-banner"][data-mode="coaching"]').waitFor({timeout:15000});
 const coachBanner=await pageB.locator('[data-testid="interview-agent-banner"]').innerText();
 assert.equal(coachBanner.includes('mode='),false,'frozen_param_back_on_the_banner');
 assert.match(coachBanner,/教练模式/,'coach_banner_does_not_name_the_coaching_mode');
 await pageB.locator('[data-testid="agent-rail-jump"]').click();
 await pageB.locator('[data-testid="agent-console"]').waitFor({state:'visible',timeout:15000});
 assert.equal(await pageB.locator('[data-testid="agent-policy-section"]').count(),1,'policy_section_missing');
 await pageB.locator('[data-testid="nav-artifacts"]').click();
 assert.equal(await pageB.locator('[data-testid="artifacts-pane"] .d-nav-item').count(),7,'artifact_shortcuts_missing');
 mark('三个固定 Agent 入口、顶栏运行轨道、F06B 自治设置与 7 个产物快捷入口在 B+ 上仍可用',
  {coach_mode:'coaching',artifact_shortcuts:7});
 await shot(pageB,'06-version-B-plus-final.png');

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

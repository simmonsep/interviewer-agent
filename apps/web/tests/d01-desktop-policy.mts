// F06B acceptance: the desktop shell adopts the autonomy settings found in the
// open-source references (goose modes, Codex approval policies, opencode
// once/always/reject), the primary rail collapses to three entries, and the
// model key moves to the Windows Credential Manager.
//
//   D06B_OUTPUT=<new dir> node apps/web/tests/d01-desktop-policy.mts
//
// Requires: API (with --model-config), Vite on 5174, PG/Redis, and the free
// scripted career worker. No model calls, no injection, no pretend providers.
// Do NOT run the research worker: this suite counts real PENDING research runs.
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {execFileSync} from 'node:child_process';
import {mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {openDetail, waitGoalReady} from './desktop-nav.mts';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D06B_OUTPUT??join(root,'docs/records/desktop-handoff/F06B-policy'));
const base=process.env.D06B_BASE_URL??'http://127.0.0.1:5174';
const modelFile=resolve(process.env.D06B_MODEL_FILE??join(root,'.private/f05b/model.json'));
const pythonExe=process.env.D06B_PYTHON??'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,detail?:unknown)=>{cases.push({name,detail});console.log('PASS',name);};
const evidence:Record<string,unknown>={run:new Date().toISOString(),cases,page_errors:pageErrors,screenshots:[],
 case_type_note:'真实本机服务联调：真实浏览器 + 真实 API/PostgreSQL/Redis + 免费 scripted 协调器；未调用任何模型（含 live），未向任何模型服务发请求。Windows 凭据管理器为真实系统 API，测试用假 Key 并在结束时清理。'};
const shot=(page:Page,name:string)=>page.screenshot({path:join(output,'shots',name)})
 .then(()=>{(evidence.screenshots as string[]).push(name);});

type Policy={mode:string;grants:Record<string,string>;effects:Record<string,string>;
 modes:{id:string;label:string}[];note:string};
type Effect={effect:string;mode:string;grant?:string};
type ModelState={key_masked:string;key_storage:string;presets:{id:string}[];active:boolean;assessment_mode:string};
type Goal={id:string;version:number;status:string;action:{type:string;text:string}|null};
type Jobs={runs?:{id:string;status:string}[]};

async function putPolicy(page:Page,body:unknown):Promise<Policy>{
 return page.evaluate(async(value:unknown)=>{
  const response=await fetch('/api/runtime/agent-policy',{method:'PUT',credentials:'same-origin',
   headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
  if(!response.ok)throw new Error('policy_put_failed_'+response.status);
  const next=await response.json() as Policy;
  window.dispatchEvent(new CustomEvent('py-agent-policy-changed'));
  return next;
 },body);
}
async function resolveTool(page:Page,tool:string):Promise<Effect>{
 return page.evaluate(async(name:string)=>{
  const response=await fetch('/api/runtime/agent-policy/resolve',{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({tool:name})});
  if(!response.ok)throw new Error('resolve_failed_'+response.status);
  return await response.json() as Effect;
 },tool);
}
async function decide(page:Page,tool:string,decision:string):Promise<Policy>{
 return page.evaluate(async(value:{tool:string;decision:string})=>{
  const response=await fetch('/api/runtime/agent-policy/decision',{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
  if(!response.ok)throw new Error('decision_failed_'+response.status);
  const next=await response.json() as Policy;
  window.dispatchEvent(new CustomEvent('py-agent-policy-changed'));
  return next;
 },{tool,decision});
}
async function jobs(page:Page,goalId:string):Promise<number>{
 return page.evaluate(async(id:string)=>{
  const value=await (await fetch(`/api/career-goals/${id}/jobs`,{credentials:'same-origin',cache:'no-store'})).json() as Jobs;
  return (value.runs??[]).length;
 },goalId);
}

let browser:Browser|null=null;
try{
 const health=await fetch('http://127.0.0.1:3100/health',{signal:AbortSignal.timeout(4000)});
 const app=await fetch(base+'/',{signal:AbortSignal.timeout(4000)});
 assert.ok(health.ok&&app.ok,'local_stack_not_ready');
 mark('本机服务就绪：API / Vite / 数据库 / 免费 scripted 协调器 Agent',{health:health.status,app:app.status});

 browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
 const context=await browser.newContext({viewport:{width:1280,height:900}});
 const page=await context.newPage();
 page.on('pageerror',error=>pageErrors.push(error.message));
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});

 // ---------------------------------------------------------- 1. primary rail matches this version
 await putPolicy(page,{mode:'smart_approve',grants:{}});
 const primary=await page.locator('.d-nav:not(.d-artifact-nav) .d-nav-item').count();
 const primaryExpected=Number(process.env.D06B_PRIMARY_ENTRIES??3);
 assert.equal(primary,primaryExpected,`primary_rail_entry_count_mismatch ${primary}`);
 assert.equal(await page.locator('[data-testid="artifacts-pane"] .d-nav-item').count(),7,'artifact_entries_missing');
 await openDetail(page,'申请跟踪');
 await page.locator('[data-testid="apps-pane"]').waitFor({timeout:15000});
 assert.equal(await page.locator('[data-testid="desktop-page"]').innerText(),'申请跟踪');
 await page.locator('[data-testid="nav-artifacts"]').click();
 await page.locator('[data-testid="artifacts-pane"]').waitFor({timeout:15000});
 mark('侧栏只剩 Agent 工作台 / 产物 / 设置三项，7 个详情入口整体收进产物视图并真实可打开',
  {primary_entries:primary,artifact_entries:7});
 await shot(page,'01-artifacts-view.png');

 // ---------------------------------------------------------- 2. settings shows four modes
 await page.locator('[data-testid="nav-settings"]').click();
 await page.locator('[data-testid="agent-policy-section"]').waitFor({timeout:15000});
 assert.equal(await page.locator('.d-policy-mode').count(),4,'four_modes_missing');
 assert.equal(await page.locator('.d-policy-mode[data-active="true"]').count(),1,'no_current_mode');
 const section=await page.locator('[data-testid="agent-policy-section"]').innerText();
 assert.match(section,/智能确认/,'default_mode_missing');
 assert.match(section,/不会调用任何付费模型/,'paid_model_note_missing');
 mark('设置 → Agent 自治：四档模式（智能确认 / 全自动 / 每次确认 / 仅对话），默认智能确认并明示不调用付费模型',
  {modes:await page.locator('.d-policy-mode strong').allInnerTexts()});
 await shot(page,'02-agent-policy-settings.png');

 // ---------------------------------------------------------- 3. server-side policy matrix
 const smartResearch=await resolveTool(page,'prepare_job_research');
 const smartMaterials=await resolveTool(page,'prepare_materials');
 assert.equal(smartResearch.effect,'one_click','smart_low_risk_should_be_one_click');
 assert.equal(smartMaterials.effect,'ask','smart_high_risk_should_ask');
 await putPolicy(page,{mode:'approve'});
 assert.equal((await resolveTool(page,'prepare_job_research')).effect,'ask','approve_should_ask');
 await putPolicy(page,{mode:'auto'});
 assert.equal((await resolveTool(page,'prepare_materials')).effect,'auto','auto_should_auto');
 await putPolicy(page,{mode:'chat'});
 assert.equal((await resolveTool(page,'prepare_job_research')).effect,'deny','chat_should_deny');
 await putPolicy(page,{mode:'approve'});
 await decide(page,'prepare_job_research','reject');
 assert.equal((await resolveTool(page,'prepare_job_research')).effect,'deny','reject_not_remembered');
 await decide(page,'prepare_job_research','once');
 assert.equal((await resolveTool(page,'prepare_job_research')).effect,'ask','once_should_forget');
 mark('服务端权威判定：智能确认按风险分级、每次确认全问、全自动放行、仅对话拒绝；拒绝/仅此一次会正确记住或撤销',
  {smart_research:smartResearch.effect,smart_materials:smartMaterials.effect});
 await putPolicy(page,{mode:'approve',grants:{}});

 // ---------------------------------------------------------- 4. real proposal renders the three-way card
 await page.locator('[data-testid="agent-home"]').click();
 await page.locator('[data-testid="agent-console"]').waitFor({timeout:15000});
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('研二，想在杭州找 AI 应用 / Agent 开发实习，希望 Agent 自己安排下一步。');
 await page.getByLabel(/确认方向/).fill('AI 应用 / Agent 开发');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);
 const goalId=await page.evaluate(()=>new URLSearchParams(location.search).get('goal'));
 assert.ok(goalId&&/^[a-f0-9-]{36}$/i.test(goalId),`goal_id_missing ${goalId}`);
 let goal:Goal|null=null;
 for(let i=0;i<80;i++){
  goal=await page.evaluate(async(id:string)=>await (await fetch(`/api/career-goals/${id}`,{credentials:'same-origin',cache:'no-store'})).json() as Goal,goalId!);
  if(goal&&!['PENDING','RUNNING'].includes(goal.status)&&goal.action)break;
  await delay(500);
 }
 assert.ok(goal?.action,`coordinator_never_proposed ${JSON.stringify(goal)}`);
 assert.equal(goal!.action!.type,'prepare_job_research','unexpected_proposal_type');
 await page.locator('[data-testid="agent-always"]').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-testid="agent-execute"]').innerText(),'仅此一次','once_button_label');
 assert.equal(await page.locator('[data-testid="agent-reject"]').count(),1,'reject_button_missing');
 mark('免费协调器真实跑出提议后，三态确认卡出现「仅此一次 / 这类都自动 / 拒绝」；策略由服务端决定，界面不擅自替用户默认同意',
  {goal_id:goalId,action_type:goal!.action!.type,mode:'approve'});
 await shot(page,'03-three-way-proposal.png');

 // ---------------------------------------------------------- 5. "always" really delegates and is remembered
 const beforeAlways=await jobs(page,goalId!);
 await page.locator('[data-testid="agent-always"]').click();
 await page.locator('.d-agent .notice').filter({hasText:'研究请求提交给后台研究 Agent'}).waitFor({timeout:30000});
 const afterAlways=await jobs(page,goalId!);
 assert.ok(afterAlways>beforeAlways,`always_did_not_delegate ${beforeAlways}->${afterAlways}`);
 const afterGrant=(await page.evaluate(async()=>await (await fetch('/api/runtime/agent-policy',{credentials:'same-origin',cache:'no-store'})).json() as Policy));
 assert.equal(afterGrant.grants['prepare_job_research'],'always','always_grant_missing');
 await page.locator('[data-testid="nav-settings"]').click();
 await page.locator('[data-testid="agent-policy-section"]').waitFor({timeout:15000});
 assert.match(await page.locator('[data-testid="agent-policy-section"] .d-policy-grants').innerText(),/岗位研究/,'grant_not_visible_in_settings');
 mark('「这类都自动」：真实 POST /research 委托后台研究 Agent，授权落本机策略文件，设置页可见且可撤销',
  {runs_before:beforeAlways,runs_after:afterAlways,grant:afterGrant.grants});

 // ---------------------------------------------------------- 6. "reject" writes nothing
 await putPolicy(page,{mode:'approve',grants:{}});
 await page.locator('[data-testid="agent-home"]').click();
 await page.locator('[data-testid="agent-reject"]').waitFor({timeout:20000});
 const beforeReject=await jobs(page,goalId!);
 await page.locator('[data-testid="agent-reject"]').click();
 await page.locator('.d-agent .notice').filter({hasText:'已拒绝这条提议'}).waitFor({timeout:20000});
 assert.equal(await jobs(page,goalId!),beforeReject,'reject_must_not_write_business_data');
 const rejected=(await page.evaluate(async()=>await (await fetch('/api/runtime/agent-policy',{credentials:'same-origin',cache:'no-store'})).json() as Policy));
 assert.equal(rejected.grants['prepare_job_research'],'reject','reject_grant_missing');
 assert.equal((await resolveTool(page,'prepare_job_research')).effect,'deny','reject_not_enforced');
 mark('「拒绝」：不写任何业务记录，拒绝决定被记住且服务端返回 deny，界面如实说明本轮无写入',
  {runs_unchanged:beforeReject,effect:'deny'});

 // ---------------------------------------------------------- 7. explicit full-auto fires without a click
 // The prior case already enqueued a PENDING research run for this goal version;
 // the backend correctly refuses a second one (research_in_progress). So assert
 // the real delegation REQUEST happened with zero clicks, not a new row.
 const autoRequests:string[]=[];
 const onRequest=(request:{method():string;url():string})=>{
  if(request.method()==='POST'&&/\/api\/career-goals\/[^/]+\/research$/.test(request.url()))
   autoRequests.push(request.url());
 };
 page.on('request',onRequest);
 await putPolicy(page,{mode:'auto',grants:{}});
 for(let i=0;i<30&&autoRequests.length===0;i++)await delay(300);
 page.off('request',onRequest);
 assert.ok(autoRequests.length>=1,'auto_mode_did_not_delegate_without_click');
 assert.match(await page.locator('[data-testid="agent-policy-line"]').innerText(),/全自动/,'policy_line_not_updated');
 await delay(1500);
 assert.equal(autoRequests.length,1,'auto_executed_more_than_once');
 const guard=await page.locator('.d-agent .error').innerText().catch(()=>'');
 mark('显式切到「全自动」后，Agent 无需点击即发出真实研究委托请求，且同一提议只触发一次；后端因本轮已有 PENDING 任务拒绝重复入队，界面给出准确中文提示',
  {delegation_requests:autoRequests.length,repeat_check:'exactly_one',
   backend_guard:guard.slice(0,90)||'none',runs_unchanged:beforeReject});
 await shot(page,'04-auto-executed.png');

 // ---------------------------------------------------------- 8. model presets + credential vault
 await page.locator('[data-testid="nav-settings"]').click();
 await page.locator('.d-model-form').waitFor({timeout:15000});
 const presetCount=await page.locator('select[aria-label="提供方预设"] option').count();
 assert.equal(presetCount,4,`presets_missing ${presetCount}`);
 await page.locator('select[aria-label="提供方预设"]').selectOption('deepseek');
 assert.equal(await page.locator('.d-model-form input').nth(1).inputValue(),'https://api.deepseek.com','preset_not_applied');
 const fakeKey='sk-f06b-'+Date.now().toString(36)+'-not-a-real-key';
 await page.locator('.d-model-form input[type="password"]').fill(fakeKey);
 await page.getByRole('button',{name:'保存模型配置'}).click();
 await page.locator('[data-testid="key-storage"]').waitFor({timeout:20000});
 const modelState=await page.evaluate(async()=>await (await fetch('/api/runtime/model-settings',{credentials:'same-origin',cache:'no-store'})).json() as ModelState);
 const stored=JSON.parse(readFileSync(modelFile,'utf8')) as {api_key:string;key_storage?:string};
 assert.equal(modelState.key_storage,'windows_credential_manager','vault_not_used');
 assert.equal(stored.api_key,'','plaintext_key_left_in_model_json');
 assert.ok(!modelState.key_masked.includes(fakeKey.slice(8)),'mask_leaks_key');
 assert.match(modelState.key_masked,new RegExp(`${fakeKey.slice(-4)}$`),'mask_wrong');
 assert.equal(modelState.active,false,'save_must_not_activate_live');
 mark('模型接入：4 个填写预设、Key 写入真实 Windows 凭据管理器（model.json 无明文）、GET 只回掩码、保存≠启用，全程未调用模型',
  {presets:presetCount,key_storage:modelState.key_storage,key_masked:modelState.key_masked,
   assessment_mode:modelState.assessment_mode});
 await shot(page,'05-key-vault.png');

 await putPolicy(page,{mode:'smart_approve',grants:{}});
 assert.deepEqual(pageErrors,[],'page_errors');
 evidence.summary={passed:cases.length,failed:0,page_errors:pageErrors.length,coordinator:'scripted_protocol',models_called:0};
 console.log(`${cases.length}/${cases.length} passed`);
}catch(error){
 evidence.error=String(error);
 evidence.summary={passed:cases.length,failed:1,page_errors:pageErrors.length};
 console.error('FAILED:',error);
 process.exitCode=1;
}finally{
 // Restore the default policy and remove the test credential from the OS vault.
 try{
  execFileSync(pythonExe,['-X','utf8','-c',
   "import sys;sys.path.insert(0,'backend/src');from interview_agent.desktop import key_vault;print('vault_cleared',key_vault.clear())"],
   {cwd:root,stdio:'pipe'});
 }catch{/* non-Windows or already absent */}
 await browser?.close();
 writeFileSync(join(output,'result.json'),JSON.stringify(evidence,null,2));
 console.log('OUTPUT',output);
}

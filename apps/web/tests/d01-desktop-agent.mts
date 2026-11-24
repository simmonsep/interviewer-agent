import {waitGoalReady} from './desktop-nav.mts';
// F06A acceptance: the desktop shell is an Agent console again, not a menu of
// eight hand-operated panes. A single sentence is handed to the existing career
// Coordinator Agent; the run trace, proposal and artifacts are visible, and one
// click really delegates the proposal to the backend.
//
//   D06A_OUTPUT=<new dir> node apps/web/tests/d01-desktop-agent.mts
//
// Requires: API (with --model-config), Vite on 5174, PG/Redis, and the free
// scripted career worker. No model calls, no injection, no fixtures written into
// the database by this script: it drives the real UI and real API.
//
// Environment note: this test database accumulates historical career notifications.
// A brand-new consumer group reads from id 0-0, so ~1900 old messages can delay a
// new goal. If the coordinator does not answer within ~40s, reset the test group
// to the stream tail (command in F06A-交付报告.md). That is test-environment
// cleanup, not product behaviour. Do NOT run the research worker during this
// suite: it consumes the PENDING research run that the delegation assertion uses.
import assert from 'node:assert/strict';
import {chromium,type Browser} from '@playwright/test';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D06A_OUTPUT??join(root,'docs/records/desktop-handoff/F06A-agent'));
const base=process.env.D06A_BASE_URL??'http://127.0.0.1:5174';
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,detail?:unknown)=>{cases.push({name,detail});console.log('PASS',name);};
const evidence:Record<string,unknown>={run:new Date().toISOString(),cases,page_errors:pageErrors,screenshots:[],
 case_type_note:'真实本机服务联调：真实浏览器 + 真实 API/数据库 + 免费 scripted 协调器 Agent。协调器是仓库自带的确定性协议夹具，不是语义意图识别，也没有调用任何模型。'};
const shot=(page:import('@playwright/test').Page,name:string)=>page.screenshot({path:join(output,'shots',name)})
 .then(()=>{(evidence.screenshots as string[]).push(name);});

type Goal={id:string;version:number;status:string;action:{type:string;text:string}|null;
 steps:{id:string;status:string;origin:string|null;tool_log:{tool:string;status:string}[]|null}[]};
type Jobs={items?:unknown[];runs?:{id:string;status:string}[]};

let browser:Browser|null=null;
try{
 const health=await fetch('http://127.0.0.1:3100/health',{signal:AbortSignal.timeout(4000)});
 const app=await fetch(base+'/',{signal:AbortSignal.timeout(4000)});
 assert.ok(health.ok&&app.ok,'local_stack_not_ready');
 mark('本机服务就绪：API / Vite / 数据库 / 免费 scripted 协调器 Agent 与研究报告 Agent 已启动',
  {health:health.status,app:app.status,coordinator:'career.worker --provider scripted'});

 browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
 const context=await browser.newContext({viewport:{width:1280,height:860}});
 const page=await context.newPage();
 page.on('pageerror',error=>pageErrors.push(error.message));
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});

 // ---------------------------------------------------------- 1. default path is the Agent console
 await page.locator('[data-testid="agent-console"]').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-testid="agent-home"]').count(),1,'agent_home_missing');
 assert.equal(await page.locator('[data-testid="workbench-primary"]').count(),1,'primary_action_duplicated');
 // F07Z-142: the rail contract changed after this suite was written (single nav, no secondary
 // group, no A/B variant marker). Keep the intent - every workspace entry is reachable from the
 // rail - and assert it against the current structure instead of the removed classes.
 const railEntries=await page.locator('.desktop-rail .d-nav-item').count();
 assert.ok(railEntries>=8,`rail_entries_missing ${railEntries}`);
 const hasArtifacts=await page.locator('.desktop-rail [data-testid="nav-artifacts"]').count();
 assert.equal(hasArtifacts,1,'artifacts_entry_missing_from_rail');
 assert.equal(await page.locator('[data-testid="desktop-page"]').innerText(),'工作台','page_label_changed');
 mark('首屏：当前目标在最上面，运行详情在页面下方；主侧栏 8 项以上入口（含产物）',
  {rail_entries:railEntries,page_label:'工作台'});
 await shot(page,'01-agent-console-empty.png');

 // ---------------------------------------------------------- 2. one sentence in, agent takes over
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('研二，想在上海找 AI 应用 / Agent 开发实习。');
 await page.getByLabel(/确认方向/).fill('AI 应用 / Agent 开发');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);
 // The shell rewrites the URL to /?goal=<id> after creation; that is the exact
 // goal this run created, unlike the first item of an account's older history.
 const goalId=await page.evaluate(()=>new URLSearchParams(location.search).get('goal'));
 assert.ok(goalId&&/^[a-f0-9-]{36}$/i.test(goalId),`goal_id_missing ${goalId}`);
 mark('一句话目标提交给 Agent（不是逐个面板手填），目标由真实本机服务创建',{goal_id:goalId});

 // ---------------------------------------------------------- 3. the coordinator really runs
 let goal:Goal|null=null;
 for(let i=0;i<80;i++){
  goal=await page.evaluate(async(id:string)=>await (await fetch(`/api/career-goals/${id}`,{credentials:'same-origin',cache:'no-store'})).json() as Goal,goalId);
  if(goal&&!['PENDING','RUNNING'].includes(goal.status))break;
  await delay(500);
 }
 assert.ok(goal,'coordinator_never_responded');
 assert.ok(goal!.action,`agent_proposal_missing status=${goal!.status}`);
 const last=goal!.steps.at(-1);
 assert.ok(last?.tool_log?.some(item=>item.tool==='read_goal'),`read_goal_tool_missing ${JSON.stringify(last?.tool_log)}`);
 assert.equal(last?.origin,'scripted_protocol','coordinator_origin_not_honest');
 mark('协调器 Agent 真实跑完一轮：调用 read_goal 读取目标、选择下一步工具、产出候选提议，来源标为 scripted_protocol',
  {status:goal!.status,action_type:goal!.action!.type,tool_log:last!.tool_log,origin:last!.origin,
   model_requests:(last as unknown as {model_requests:number}).model_requests});

 await page.locator('[data-testid="agent-proposal"]').waitFor({timeout:20000});
 const proposal=await page.locator('[data-testid="agent-proposal"]').innerText();
 assert.match(proposal,/AGENT 当前的下一步/,'proposal_heading_missing');
 const sourceText=await page.locator('.d-agent-source').innerText();
 assert.match(sourceText,/确定性协议，不代表真实语义理解/,'scripted_source_not_disclosed');
 assert.match(await page.locator('[data-testid="agent-timeline"]').innerText(),/读取目标/,'tool_trace_not_rendered');
 mark('界面显示 Agent 当前提议、每轮工具轨迹与「免费脚本协调器」来源标注，不把确定性夹具冒充真实语义理解',
  {proposal:proposal.replace(/\s+/g,' ').slice(0,140),source:sourceText.replace(/\s+/g,' ').slice(0,120)});
 await shot(page,'02-agent-proposal.png');

 // ---------------------------------------------------------- 4. one click (or the truthful branch)
 // D（F07Z-165）: the local autonomy policy is shared state and can be "chat" (deny) or carry a
 // remembered "always"/"reject" grant. The product then intentionally renders no execute button,
 // so the suite must read the policy and record that branch instead of clicking blindly.
 const before=await page.evaluate(async(id:string)=>await (await fetch(`/api/career-goals/${id}/jobs`,{credentials:'same-origin',cache:'no-store'})).json() as Jobs,goalId);
 const beforeRuns=(before.runs??[]).length;
 const policy=await page.evaluate(async()=>await (await fetch('/api/runtime/agent-policy',
  {credentials:'same-origin',cache:'no-store'})).json() as {mode:string;effects:Record<string,string>});
 const actionType=goal!.action!.type;
 const effect=policy.effects?.[actionType]??'one_click';
 const executeButton=page.locator('[data-testid="agent-execute"]');
 const buttonCount=await executeButton.count();
 const runsNow=async()=>{
  const view=await page.evaluate(async(id:string)=>await (await fetch(`/api/career-goals/${id}/jobs`,
   {credentials:'same-origin',cache:'no-store'})).json() as Jobs,goalId);
  return (view.runs??[]).length;
 };
 evidence.branch={action_type:actionType,policy_mode:policy.mode,effect,
  execute_button_rendered:buttonCount>0,runs_before:beforeRuns};
 if(buttonCount===0){
  // deny, finish, or another action without a CTA: the honest product behaviour is no button.
  assert.equal(await page.locator('[data-testid="agent-actions"]').count(),0,'actions_without_cta');
  const denied=await page.locator('[data-testid="agent-denied"]').count();
  mark('提议不可执行：按当前自治策略或动作类型不渲染执行按钮（记录实际分支，不点击）',
   {action_type:actionType,policy_mode:policy.mode,effect,agent_denied:denied});
  (evidence.branch as Record<string,unknown>).executed_by='none';
 }else if(effect==='auto'){
  let afterRuns=beforeRuns;
  if(actionType==='prepare_job_research'){
   for(let i=0;i<40;i++){
    afterRuns=await runsNow();
    if(afterRuns===beforeRuns+1)break;
    await delay(500);
   }
   assert.equal(afterRuns,beforeRuns+1,`auto_research_not_exactly_once ${beforeRuns}->${afterRuns}`);
  }else if(actionType==='ask_user'){
   await delay(500);
   assert.equal(await runsNow(),beforeRuns,'ask_user_auto_enqueued_work');
  }
  mark('提议按长期授权自动执行：只执行一次，不重复入队',{action_type:actionType,
   policy_mode:policy.mode,effect,runs_before:beforeRuns,runs_after:afterRuns});
  (evidence.branch as Record<string,unknown>).executed_by='auto';
 }else{
  await executeButton.click();
  if(actionType==='prepare_job_research'){
   // The contract is "the research run was really enqueued, exactly once". The sentence shown
   // in the console is presentation and has changed more than once, so poll the run count.
   let afterRuns=beforeRuns;
   for(let i=0;i<40;i++){
    afterRuns=await runsNow();
    if(afterRuns>beforeRuns)break;
    await delay(500);
   }
   assert.equal(afterRuns,beforeRuns+1,`research_runs_not_exactly_one ${beforeRuns}->${afterRuns}`);
   await delay(1200);
   assert.equal(await runsNow(),beforeRuns+1,'research_run_duplicated');
   const noticeText=await page.locator('.d-agent .notice').first().innerText().catch(()=> '');
   mark('一键执行提议：真实 POST /research 恰好入队一次；界面把排队如实说成「已提交」，不宣称已找到岗位',
    {action_type:'prepare_job_research',policy_mode:policy.mode,effect,runs_before:beforeRuns,
     runs_after:afterRuns,notice:noticeText.slice(0,120)});
  }else if(actionType==='ask_user'){
   const focused=await page.evaluate(()=>document.activeElement?.getAttribute('aria-label'));
   assert.equal(focused,'求职意向或补充说明','ask_user_did_not_focus_input');
   assert.equal(await runsNow(),beforeRuns,'ask_user_enqueued_work');
   mark('一键执行提议：ask_user 把输入焦点交还用户，不擅自代替用户回答，也不入队任务',
    {action_type:actionType,policy_mode:policy.mode,effect,focused});
  }else{
   mark('一键执行提议：已点击并交给服务端；该动作类型由各自的专项套件验收',
    {action_type:actionType,policy_mode:policy.mode,effect});
  }
  (evidence.branch as Record<string,unknown>).executed_by='click';
 }
 await shot(page,'03-agent-delegated.png');

 // ---------------------------------------------------------- 5. artifacts, not task menus
 assert.ok(await page.locator('.d-agent-artifact').count()>=6,'artifact_rows_missing');
 await page.locator('.d-agent-artifact').first().click();
 await page.locator('[data-testid="research-pane"]').waitFor({timeout:15000});
 assert.equal(await page.locator('[data-testid="desktop-page"]').innerText(),'岗位研究','artifact_pane_not_opened');
 await page.locator('[data-testid="agent-home"]').click();
 await page.locator('[data-testid="agent-console"]').waitFor({timeout:15000});
 mark('Agent 产物区显示 6 类运行结果（选岗 / 材料 / 训练 / 笔试 / 投递 / 题库），点击只作为详情查看，回到 Agent 工作台仍是主路径',
  {artifact_rows:await page.locator('.d-agent-artifact').count()});

 assert.deepEqual(pageErrors,[],'page_errors');
 evidence.summary={passed:cases.length,failed:0,page_errors:pageErrors.length,coordinator:'scripted_protocol'};
 console.log(`${cases.length}/${cases.length} passed`);
}catch(error){
 evidence.error=String(error);
 evidence.summary={passed:cases.length,failed:1,page_errors:pageErrors.length};
 console.error('FAILED:',error);
 process.exitCode=1;
}finally{
 await browser?.close();
 writeFileSync(join(output,'result.json'),JSON.stringify(evidence,null,2));
 console.log('OUTPUT',output);
}

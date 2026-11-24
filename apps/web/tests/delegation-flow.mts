import assert from 'node:assert/strict';
import {chromium,expect,type Page} from '@playwright/test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {randomUUID} from 'node:crypto';

const run=process.env.JA03_RUN_ID!,output=process.env.JA03_OUTPUT!,python=process.env.JA03_PYTHON!,live=process.env.JA03_LIVE==='1';
const root=resolve(import.meta.dirname,'../../..'),execute=promisify(execFile);
const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
const cases:string[]=[],errors:string[]=[];
try{
 const context=await browser.newContext({viewport:{width:1440,height:1100}}),page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 async function signup(p:Page){await p.getByRole('button',{name:'第一次来？创建账号'}).click();await p.getByLabel('称呼').fill('合成求职学员');await p.getByLabel('邮箱').fill(`pyweb-${run}-${randomUUID()}@example.com`);await p.getByLabel('密码').fill('Synthetic-password-'+randomUUID());await p.getByRole('button',{name:'创建账号',exact:true}).click();await p.getByRole('button',{name:'退出登录'}).waitFor();}
 const id=()=>new URL(page.url()).searchParams.get('goal')!;
 async function jobs(goal=id()){return await context.request.get(`http://127.0.0.1:5174/api/career-goals/${goal}/jobs`).then(r=>r.json());}
 async function goal(goalId=id()){return await context.request.get(`http://127.0.0.1:5174/api/career-goals/${goalId}`).then(r=>r.json());}
 async function worker(phase:'coordinator'|'researcher',scenario='ok'){
  const args=['-X','utf8','scripts/delegation-web-support.py','--goal',id(),'--run-id',run,'--phase',phase,'--source-scenario',scenario,...(live?['--live']:[])];
  const result=await execute(python,args,{cwd:root,windowsHide:true,timeout:270000});writeFileSync(join(output,`worker-${cases.length}-${phase}.json`),result.stdout);return JSON.parse(result.stdout);
 }
 await page.goto('http://127.0.0.1:5174/?shell=web');await signup(page);await page.getByRole('button',{name:'求职目标',exact:true}).click();
 await page.getByLabel('求职意向或补充说明').fill('请直接按已确认的Java后端实习方向和城市研究岗位。缺少的毕业/到岗信息先列待核验，不要阻塞初步检索。不需要简历，研究后给出待核验项。');
 await page.getByLabel('确认方向（可选）').fill('Java后端');await page.getByLabel('确认城市（可选，多个用顿号分隔）').fill(live?'深圳':'上海');await page.getByLabel('城市要求').selectOption('required');
 await page.getByText('补充匹配条件（可留空，无简历也能使用）',{exact:true}).click();await page.getByLabel('招聘类型',{exact:true}).selectOption('intern');
 if(!live){await page.getByLabel('毕业年份',{exact:true}).fill('2027');await page.getByLabel('每周可到岗天数').fill('4');await page.getByLabel('可实习月数').fill('3');}await page.getByLabel('自述技能（多个用顿号分隔）').fill('Java、SQL、Redis');
 await page.getByLabel('自动研究并匹配（受限公开来源）').check();await page.getByRole('button',{name:'保存求职目标',exact:true}).click();await page.waitForURL(u=>u.searchParams.has('goal'));
 const first=id();assert.ok(first);
 assert.equal((await worker('coordinator')).goal_status,'WAITING_FOR_TOOL');
 await expect.poll(async()=>(await jobs()).runs.length).toBe(1);
 let view=await jobs();assert.equal(view.runs[0].mode,'agent');assert.ok(view.runs[0].parent_step_id);
 assert.equal((await worker('researcher')).status,'SUCCEEDED');
 let panel=page.getByRole('region',{name:'岗位来源与列表'});await panel.getByRole('status').filter({hasText:'读取完成'}).waitFor({timeout:12000});
 await expect.poll(async()=>(await goal()).status).toBe('WAITING_FOR_USER');
 view=await jobs();let current=view.runs[0];assert.equal(current.agent_origin,live?'real_model':'scripted_protocol');
 assert.ok(current.result.agent.tool_log.some((t:{tool:string})=>t.tool==='read_job'));assert.ok((await goal()).model_requests<=12);assert.ok(current.source_requests<=12);
 cases.push('coordinator commits one version-bound child; independent research graph reads scoped sources/details and returns with shared counters');
 const allowed=current.result.matches.find((m:{outcome:string})=>m.outcome!=='excluded');assert.ok(allowed,'need a readable non-conflicting sample');
 const item=view.items.find((x:{snapshot_id:string})=>x.snapshot_id===allowed.snapshot_id);assert.ok(item);
 assert.equal(allowed.score,null);
 for(const check of allowed.checks){assert.equal(check.user_ref.goal_version,1);if(check.source_quote&&check.source_path==='requirements')assert.ok(item.payload.requirements.includes(check.source_quote));}
 const card=panel.locator('.job-card').filter({has:page.locator(`a[href="${item.source_url}"]`)});await card.getByRole('table').waitFor();
 await card.getByText('缺证/待核验',{exact:true}).first().waitFor();
 cases.push('shortlist binds exact source evidence to confirmed user fields, preserving unknown vacancy status and no score');
 const choose=card.getByRole('button',{name:'选择该岗位',exact:true});assert.equal(await choose.isDisabled(),true);
 await card.getByLabel('我知悉仍有待核验项，选岗仅用于后续准备').check();await choose.click();await panel.getByRole('status').filter({hasText:'已选择岗位用于后续准备'}).waitFor();
 const before=(await goal()).model_requests;await page.reload();panel=page.getByRole('region',{name:'岗位来源与列表'});await panel.getByRole('status').filter({hasText:'已选择岗位用于后续准备'}).waitFor();
 assert.equal((await jobs()).selected.snapshot_id,allowed.snapshot_id);assert.equal((await goal()).model_requests,before);
 writeFileSync(join(output,'selected-snapshot.json'),JSON.stringify(await jobs(),null,2));
 cases.push('explicit acknowledgement required for unknowns; selected immutable snapshot survives refresh without new model calls');
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:join(output,'shortlist-mobile.png'),fullPage:true});
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
 if(overflow)writeFileSync(join(output,'layout-overflow.json'),JSON.stringify(await page.evaluate(()=>[...document.querySelectorAll('*')].filter(x=>x.getBoundingClientRect().right>innerWidth+1).map(x=>({tag:x.tagName,classes:x.className,width:x.getBoundingClientRect().width,minWidth:getComputedStyle(x).minWidth,overflow:getComputedStyle(x).overflowX}))),null,2));
 assert.equal(overflow,false);await page.setViewportSize({width:1440,height:1100});
 await page.screenshot({path:join(output,'shortlist-desktop.png'),fullPage:true});cases.push('390px evidence table scrolls within its panel without page overflow');
 if(!live){
  const other=await browser.newContext(),p2=await other.newPage();await p2.goto('http://127.0.0.1:5174/?shell=web');await signup(p2);
  assert.equal((await other.request.get(`http://127.0.0.1:5174/api/career-goals/${first}/jobs`)).status(),404);
  assert.equal((await other.request.post(`http://127.0.0.1:5174/api/career-goals/${first}/selection`,{headers:{Origin:'http://127.0.0.1:3100'},data:{expected_version:1,run_id:current.id,snapshot_id:allowed.snapshot_id,acknowledge_unknown:true}})).status(),404);await other.close();
  cases.push('another authenticated account cannot read or select the private shortlist');
  await page.getByLabel('求职意向或补充说明').fill('改为北京，按新条件继续研究。');await page.getByLabel('确认城市（可选，多个用顿号分隔）').fill('北京');await page.getByRole('button',{name:'保存补充与确认条件'}).click();
  await expect.poll(async()=>(await goal()).version).toBe(2);assert.equal((await jobs()).selected,null);assert.equal((await jobs()).items.length,0);
  assert.equal((await worker('coordinator')).goal_status,'WAITING_FOR_TOOL');assert.equal((await worker('researcher','rate_limited')).status,'FAILED');
  panel=page.getByRole('region',{name:'岗位来源与列表'});await panel.getByText(/研究需要补充/).waitFor();await expect.poll(async()=>(await goal()).status).toBe('WAITING_FOR_USER');
  const waiting=(await goal()).model_requests;await page.reload();await page.getByRole('region',{name:'岗位来源与列表'}).getByText(/研究需要补充/).waitFor();assert.equal((await goal()).model_requests,waiting);
  cases.push('new goal version clears active choice; failed source causes a persisted question and no model calls while waiting');
  await page.getByRole('button',{name:'新建求职目标',exact:true}).click();await page.getByLabel('职位说明 JD',{exact:true}).fill('合成JD：Java接口、SQL事务。');await page.getByLabel('自动研究并匹配（受限公开来源）').check();await page.getByRole('button',{name:'保存求职目标',exact:true}).click();await expect.poll(()=>id()!==first&&Boolean(id())).toBe(true);
  await worker('coordinator');assert.equal((await goal()).action.type,'prepare_training');assert.equal((await jobs()).runs.length,0);
  cases.push('direct JD with auto mode still skips exploration; no resume or unnecessary researcher is required');
  const jdGoal=id();await page.getByRole('button',{name:'新建求职目标',exact:true}).click();await page.getByLabel('求职意向或补充说明').fill('先研究Java方向');await page.getByLabel('确认方向（可选）').fill('Java');await page.getByLabel('自动研究并匹配（受限公开来源）').check();await page.getByRole('button',{name:'保存求职目标',exact:true}).click();await expect.poll(()=>id()!==jdGoal&&Boolean(id())).toBe(true);
  await worker('coordinator');await worker('researcher','ask_before_query');panel=page.getByRole('region',{name:'岗位来源与列表'});await panel.getByText(/请先确认研究方向/).waitFor();
  assert.equal((await jobs()).runs[0].source_requests,0);assert.equal((await jobs()).runs[0].result.executed_request,null);assert.equal(await panel.getByText(/本次查询未返回/).count(),0);assert.equal(await panel.getByText(/本次关键词/).count(),0);
  cases.push('expert can clarify before reading any source; UI does not describe an unexecuted query as an empty search');
  const clarifyGoal=id();await page.getByRole('button',{name:'新建求职目标',exact:true}).click();await page.getByLabel('求职意向或补充说明').fill('按Java方向查岗位');await page.getByLabel('确认方向（可选）').fill('Java');await page.getByLabel('自动研究并匹配（受限公开来源）').check();await page.getByRole('button',{name:'保存求职目标',exact:true}).click();await expect.poll(()=>id()!==clarifyGoal&&Boolean(id())).toBe(true);
  await worker('coordinator');await page.getByRole('button',{name:'结束此目标'}).click();assert.equal((await worker('researcher')).status,'CANCELLED');
  assert.equal((await jobs()).runs[0].model_requests,0);
  cases.push('cancelling after delegation prevents the pending expert from using either model or source tools');
 }
 assert.deepEqual(errors,[]);writeFileSync(join(output,'receipt.json'),JSON.stringify({stage:'JA-03',live,status:'passed',cases,pageErrors:errors,realBrowser:true},null,2));console.log(JSON.stringify({status:'passed',cases:cases.length,live}));
}finally{await browser.close();}

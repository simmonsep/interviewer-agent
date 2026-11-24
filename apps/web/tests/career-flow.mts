import assert from 'node:assert/strict';
import {chromium,expect,type Page} from '@playwright/test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const runId=process.env.JA01_RUN_ID!,output=process.env.JA01_OUTPUT!,python=process.env.JA01_PYTHON!,live=process.env.JA01_LIVE==='1';
assert.ok(runId&&output&&python,'use check-career-web.py');
const root=resolve(import.meta.dirname,'../../..'),execute=promisify(execFile);
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1100}}),page=await context.newPage();
const errors:string[]=[],cases:string[]=[];page.on('pageerror',e=>errors.push(e.message));
async function signup(p:Page){await p.getByRole('button',{name:'第一次来？创建账号'}).click();await p.getByLabel('称呼').fill('合成求职学员');await p.getByLabel('邮箱').fill(`pyweb-${runId}-${randomUUID()}@example.com`);await p.getByLabel('密码').fill('Synthetic-password-'+randomUUID());await p.getByRole('button',{name:'创建账号',exact:true}).click();await p.getByRole('button',{name:'退出登录',exact:true}).waitFor();}
const id=()=>new URL(page.url()).searchParams.get('goal')!;
async function snapshot(identity=id()){return await context.request.get(`http://127.0.0.1:5174/api/career-goals/${identity}`).then(r=>r.json());}
async function worker(identity=id()){
 const result=await execute(python,['-X','utf8','scripts/career-web-support.py','--goal',identity,'--run-id',runId,...(live?['--live']:[])],{cwd:root,windowsHide:true,timeout:170000});
 return JSON.parse(result.stdout);
}
try{
 await page.goto('http://127.0.0.1:5174/?shell=web');await signup(page);await page.getByRole('button',{name:'求职目标',exact:true}).click();
 await page.getByRole('heading',{name:'从求职意向开始'}).waitFor();
 assert.equal(await page.locator('input[type=file]').count(),0);
 await page.getByLabel('求职意向或补充说明').fill('我是研二，想在上海做AI实习，应用和算法方向还不确定。');
 let posts=0;page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/api/career-goals/'))posts++;});
 await page.getByRole('button',{name:'保存求职目标',exact:true}).dblclick();
 await expect.poll(()=>id()).not.toBeNull();assert.equal(posts,1);const first=id();
 await worker();await expect.poll(async()=>(await snapshot()).status).toBe('WAITING_FOR_USER');
 await page.getByRole('status').filter({hasText:'等待你补充'}).waitFor();
 let value=await snapshot();assert.equal(value.steps.length,1);assert.equal(value.steps[0].origin,live?'real_model':'scripted_protocol');
 assert.equal(value.action.type,'ask_user');assert.equal(value.steps[0].tool_log[0].tool,'read_goal');cases.push('goal input -> real graph read -> clarification with provenance; double click creates once');
 const requests=value.model_requests;await page.reload();await page.getByRole('status').filter({hasText:'等待你补充'}).waitFor();
 await page.getByRole('button',{name:'刷新目标',exact:true}).click();assert.equal((await snapshot()).model_requests,requests);cases.push('refresh restores original input and clarification; WAITING_FOR_USER does not call model');
 await page.getByLabel('确认方向（可选）').fill('Java后端');await page.getByLabel('确认城市（可选，多个用顿号分隔）').fill('深圳');await page.getByLabel('城市要求').selectOption('required');
 await page.getByLabel('求职意向或补充说明').fill('我确认只考虑深圳的Java后端实习，2028年毕业，下月可到岗，每周4天，至少4个月。请先帮我查岗位。');
 await page.getByRole('button',{name:'保存补充与确认条件',exact:true}).click();
 await expect.poll(async()=>(await snapshot()).version).toBe(2);await worker();
 await expect.poll(async()=>(await snapshot()).status).toBe('WAITING_FOR_TOOL');
 await page.getByText('需求已保存。可在下方读取岗位；开启自动研究可由研究Agent继续，整场训练将在后续阶段接入。',{exact:true}).waitFor();
 value=await snapshot();assert.deepEqual(value.conditions.cities,['深圳']);assert.equal(value.conditions.city_policy,'required');assert.equal(value.action.type,'prepare_job_research');assert.equal(value.steps.length,2);
 await page.screenshot({path:join(output,'goal-research.png'),fullPage:true});cases.push('confirmed city update advances version and prepares research without faking search');
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await page.screenshot({path:join(output,'goal-mobile.png'),fullPage:true});await page.setViewportSize({width:1440,height:1100});cases.push('mobile goal card has no horizontal overflow');
 const other=await browser.newContext(),otherPage=await other.newPage();await otherPage.goto(`http://127.0.0.1:5174/?shell=web&goal=${first}`);await signup(otherPage);
 assert.equal((await other.request.get(`http://127.0.0.1:5174/api/career-goals/${first}`)).status(),404);await other.close();cases.push('cross-account goal access rejected');
 await page.getByRole('button',{name:'新建求职目标',exact:true}).click();await page.getByLabel('求职意向或补充说明').fill('我已经有JD，只想按这份JD准备面试，不用先找岗位。');
 await page.getByLabel('职位说明 JD',{exact:true}).fill('合成JD：Java后端实习，开发REST接口，理解数据库事务、Redis和幂等。');
 await page.getByRole('button',{name:'保存求职目标',exact:true}).click();await expect.poll(()=>Boolean(id()&&id()!==first)).toBe(true);await worker();
 await expect.poll(async()=>(await snapshot()).status).toBe('WAITING_FOR_TOOL');assert.equal((await snapshot()).action.type,'prepare_training');
 await page.getByRole('button',{name:'刷新目标',exact:true}).click();await page.screenshot({path:join(output,'goal-jd.png'),fullPage:true});cases.push('JD without resume skips job exploration and prepares training request');
 await page.getByRole('button',{name:'结束此目标',exact:true}).click();await page.getByRole('status').filter({hasText:'已结束'}).waitFor();cases.push('cancel persists and stops continuation');
 assert.deepEqual(errors,[]);
 writeFileSync(join(output,'receipt.json'),JSON.stringify({stage:'JA-01',live,status:'passed',cases,pageErrors:errors,realBrowser:true},null,2));
 console.log(JSON.stringify({status:'passed',live,cases:cases.length}));
}finally{await browser.close();}

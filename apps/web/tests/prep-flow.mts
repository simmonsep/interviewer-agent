import assert from 'node:assert/strict';
import {chromium,expect,type Page} from '@playwright/test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

// S01: the desktop-shell pipeline must show a frozen plan, wait for a human decision, and only
// then open a session. Rejecting leaves no session and offers the explicit "no resume" choice.
//
// Desktop-shell notes (learned the hard way, see the S01b report §5):
// - `?shell=desktop` signs in silently with local-<uuid>@desktop.example.com; there is no
//   signup form.
// - a full load of `?shell=desktop&goal=<id>` lands on the artifacts pane, so the test clicks
//   `agent-home` before expecting the pipeline.
// - writes through the vite proxy must carry Origin http://127.0.0.1:5174.
const runId=process.env.S01_RUN_ID!,output=process.env.S01_OUTPUT!,python=process.env.S01_PYTHON!;
assert.ok(runId&&output&&python,'use check-prep-web.py');
const root=resolve(import.meta.dirname,'../../..'),execute=promisify(execFile);
const base='http://127.0.0.1:5174';
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1100}}),page=await context.newPage();
const errors:string[]=[],cases:string[]=[];page.on('pageerror',e=>errors.push(e.message));
const headers=()=>({'Idempotency-Key':randomUUID(),Origin:base});
const goalId=()=>new URL(page.url()).searchParams.get('goal')!;
async function snapshot(identity:string){return await context.request.get(`${base}/api/career-goals/${identity}`).then(r=>r.json());}
async function interviews(identity:string){return await context.request.get(`${base}/api/career-goals/${identity}/interviews`).then(r=>r.json());}
async function careerWorker(identity:string){
 const result=await execute(python,['-X','utf8','scripts/career-web-support.py','--goal',identity,'--run-id',runId],{cwd:root,windowsHide:true,timeout:170000});
 return JSON.parse(result.stdout);
}
const prepProvider=process.env.S01_PREP_PROVIDER??'scripted';
const prepAuthorization=process.env.S01_PREP_AUTHORIZATION??'';
const prepCredentials=process.env.S01_PREP_CREDENTIALS??'.private/live-credentials.txt';
async function prepWorker(identity:string,mode:'plan'|'advance'='plan'){
 const extra=prepProvider==='live'&&mode==='plan'?['--provider','live','--authorization',prepAuthorization,'--credentials',prepCredentials]:[];
 const result=await execute(python,['-X','utf8','scripts/prep-web-support.py','--goal',identity,'--run-id',runId,'--watch','150','--mode',mode,...extra],{cwd:root,windowsHide:true,timeout:200000});
 return JSON.parse(result.stdout);
}
async function openWorkbench(identity:string){
 await page.goto(`${base}/?shell=desktop&goal=${identity}`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-ui-variant="B"]').waitFor({timeout:30000});
 const home=page.locator('[data-testid="agent-home"]');
 if(await home.count())await home.click();
 await expect(page.getByTestId('pipeline-run')).toBeVisible({timeout:30000});
}
try{
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-ui-variant="B"]').waitFor({timeout:30000});
 await expect.poll(async()=>{
  const session=await context.request.get(`${base}/api/auth/get-session`).then(r=>r.json());
  return Boolean(session?.user);
 },{timeout:20000}).toBe(true);
 cases.push('desktop shell signs in with the silent local identity (no signup form)');

 const created=await context.request.post(`${base}/api/career-goals/`,
  {headers:headers(),data:{message:'我是研二，想在上海做AI应用方向实习，先准备一场模拟面试。',conditions:{direction:'AI应用'}}});
 assert.ok([200,202].includes(created.status()),String(created.status()));
 const goal=(await created.json()).id;
 await careerWorker(goal);
 await expect.poll(async()=>['WAITING_FOR_USER','WAITING_FOR_TOOL'].includes((await snapshot(goal)).status),{timeout:60000}).toBe(true);
 cases.push('synthetic goal created and parked by the scoped coordinator worker');

 const materials=await context.request.get(`${base}/api/career-goals/${goal}/materials`).then(r=>r.json());
 const upload=await context.request.post(`${base}/api/career-goals/${goal}/materials/sources`,
  {headers:headers(),data:{expected_version:(await snapshot(goal)).version,expected_revision:materials.material_revision,
   name:'合成简历原文',format:'text',
   text:'合成材料：我在课程项目里用 Python 写过一个问答服务，负责接口和错误处理，并写了测试。'}});
 assert.equal(upload.status(),201);

 await openWorkbench(goal);
 cases.push('desktop workbench shows the one-click pipeline for the selected goal');

 // ---- scenario 1: plan -> confirm -> exactly one session -------------------
 await page.getByTestId('pipeline-run').click();
 const first=await prepWorker(goal);
 assert.equal(first.status,'WAITING_FOR_USER',JSON.stringify(first));
 await expect(page.getByTestId('pipeline-plan')).toBeVisible({timeout:60000});
 const planText=await page.getByTestId('pipeline-plan-text').innerText();
 assert.ok(planText.includes('题')&&planText.includes('AI 应用 / Agent'),planText);
 assert.equal((await page.getByTestId('pipeline-plan-sha').innerText()).length,12);
 assert.equal((await interviews(goal)).items.length,0);
 cases.push('plan shows role/questions/duration and a 12-char fingerprint; no session exists before confirmation');
 await page.screenshot({path:join(output,'plan.png'),fullPage:true});

 await page.getByTestId('pipeline-confirm').click();
 const second=await prepWorker(goal,'advance');
 assert.equal(second.status,'SUCCEEDED',JSON.stringify(second));
 // The summary is rendered only after the confirmed preparation succeeded and the review
 // step read real local records, so it is a stronger signal than the status copy.
 await expect(page.getByTestId('pipeline-summary')).toBeVisible({timeout:90000});
 await expect(page.getByTestId('pipeline-plan')).toHaveCount(0);
 const afterConfirm=await interviews(goal);
 assert.equal(afterConfirm.items.length,1);
 assert.equal(afterConfirm.items[0].status,'PENDING');
 cases.push('approving opens exactly one PENDING session and the review step reads real local records');
 await page.screenshot({path:join(output,'confirmed.png'),fullPage:true});

 // ---- scenario 2: reject -> nothing created, explicit choice offered --------
 await page.getByTestId('pipeline-run').click();
 const third=await prepWorker(goal);
 assert.equal(third.status,'WAITING_FOR_USER',JSON.stringify(third));
 await expect(page.getByTestId('pipeline-plan')).toBeVisible({timeout:60000});
 await page.getByTestId('pipeline-reject').click();
 await expect(page.getByTestId('pipeline-no-resume')).toBeVisible({timeout:60000});
 await expect(page.getByTestId('pipeline-plan')).toHaveCount(0);
 assert.equal((await interviews(goal)).items.length,1);
 cases.push('rejecting creates no session and offers the explicit "open without a resume" choice');
 await page.screenshot({path:join(output,'rejected.png'),fullPage:true});

 assert.deepEqual(errors,[]);
 writeFileSync(join(output,'receipt.json'),JSON.stringify({stage:'S01',status:'passed',cases,pageErrors:errors,realBrowser:true},null,2));
 console.log(JSON.stringify({status:'passed',cases:cases.length}));
}finally{await browser.close();}

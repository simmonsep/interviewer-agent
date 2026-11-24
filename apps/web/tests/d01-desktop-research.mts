// F02 acceptance: goal history, research runs and job selection on the desktop shell.
// Real stack (Vite 5174 -> FastAPI 3100 -> PG/Redis). The research worker is the
// repo's offline fixture harness (scripts/research-web-support.py), so provenance is
// "合成来源演示" and the run is never a real portal read.
//
//   D01_OUTPUT=<new dir> node apps/web/tests/d01-desktop-research.mts
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {openDetail, waitGoalReady} from './desktop-nav.mts';
import {execFileSync} from 'node:child_process';
import {writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/desktop-handoff/F02-research'));
const base=process.env.D01_BASE_URL??'http://127.0.0.1:5174';
const python=process.env.D01_PYTHON??'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
assert.ok(executable,'installed_browser_required');
mkdirSync(join(output,'shots'),{recursive:true});

const runId=randomUUID();
const cases:{name:string;type:'real';detail?:unknown}[]=[];
const mark=(name:string,detail?:unknown)=>cases.push({name,type:'real',detail});
const pageErrors:string[]=[];
const shots:string[]=[];
const evidence:Record<string,unknown>={runs:[] as unknown[]};
const account={email:`pyweb-${runId}-desktop@example.com`,password:'Synthetic-password-'+randomUUID(),name:'岗位验收学员'};

let browser:Browser;
const shot=async(page:Page,name:string)=>{await page.screenshot({path:join(output,'shots',name)});shots.push(name);};

function researchWorker(goalId:string,scenario:'ok'|'rate_limited'){
 const text=execFileSync(python,['-X','utf8','scripts/research-web-support.py','--goal',goalId,'--run-id',runId,'--scenario',scenario],
  {cwd:root,encoding:'utf-8',timeout:115000,windowsHide:true});
 const parsed=JSON.parse(text);
 (evidence.runs as unknown[]).push({scenario,...parsed});
 return parsed;
}

try{
 browser=await chromium.launch({executablePath:executable,headless:true});
 const context=await browser.newContext({viewport:{width:1280,height:860}});
 const page=await context.newPage();
 page.on('pageerror',error=>pageErrors.push(error.message));
 let researchPosts=0;
 page.on('request',request=>{if(request.method()==='POST'&&request.url().endsWith('/research'))researchPosts++;});

 // ---------------------------------------------------------------- 1. enter the desktop shell
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
 mark('桌面入口注册并进入工作台（合成账号前缀 pyweb-<run>，岗位研究脚本只接受本轮账号）',{email:account.email});

 // ---------------------------------------------------------------- 2. goal A
 async function createGoal(directionText:string,city:string,direction='Java 后端'){
  await openDetail(page,'工作台');
  await page.locator('.desktop-shell[data-pane="workbench"]').waitFor({timeout:15000});
  const details=page.locator('.d-goal-details');
  if(await details.count()&&!(await details.getAttribute('open')))await page.locator('.d-goal-details>summary').click();
  const form=page.locator('#desktop-goal-form');
  await form.locator('textarea[aria-label="求职意向或补充说明"]').fill(directionText);
  await form.locator('label:has-text("确认方向") input').fill(direction);
  await form.locator('label:has-text("确认城市") input').fill(city);
  await form.locator('label:has-text("城市要求") select').selectOption('required');
  await form.locator('label:has-text("职位说明 JD") textarea').fill('合成JD：Java 接口与 SQL。');
  // Without a goal the primary action outside the form submits it; with a goal the
  // form owns its own save button.
  const primary=page.locator('[data-testid="workbench-primary"]');
  if(await primary.count()&&(await primary.getAttribute('type'))==='submit')await primary.click();
  else await form.locator('button.primary').click();
  await waitGoalReady(page);
  await page.waitForTimeout(800);
  return await page.evaluate(()=>new URLSearchParams(location.search).get('goal'));
 }

 const goalA=await createGoal('合成验收 A：找 Java 后端实习。','上海');
 assert.ok(goalA,'goal_a_missing');
 mark('目标 A 保存成功，上下文栏与地址都指向它',{goal:goalA});

 // ---------------------------------------------------------------- 3. research pane, empty state
 await openDetail(page,'岗位研究');
 const pane=page.locator('[data-testid="research-pane"]');
 await pane.waitFor({timeout:15000});
 assert.equal(await page.locator('[data-testid="desktop-page"]').innerText(),'岗位研究');
 assert.equal(await pane.locator('.job-card').count(),0,'empty_goal_should_have_no_cards');
 assert.ok((await pane.innerText()).includes('岗位来源与列表'));
 await shot(page,'01-research-empty.png');
 mark('岗位研究成为正式入口：空目标下没有岗位卡，先给出能力范围与来源说明',{});

 // ---------------------------------------------------------------- 4. queue a run, then the worker commits it
 await pane.getByLabel('查询关键词',{exact:true}).fill('合成');
 await pane.getByRole('button',{name:'查询岗位',exact:true}).click();
 await pane.getByRole('status').filter({hasText:'等待读取'}).waitFor({timeout:20000});
 assert.equal(researchPosts,1,'double_submit');
 await shot(page,'02-research-waiting.png');
 mark('发起查询只产生一次 POST /research，状态进入等待读取',{posts:researchPosts});

 const ok=researchWorker(goalA!,'ok');
 assert.equal(ok.status,'SUCCEEDED');
 await pane.getByRole('status').filter({hasText:'读取完成'}).waitFor({timeout:30000});
 await pane.locator('.job-card').first().waitFor({timeout:20000});
 const cardCount=await pane.locator('.job-card').count();
 assert.ok(cardCount>0,'no_job_card');
 const resultText=await pane.innerText();
 assert.ok(resultText.includes('合成来源演示'),'provenance_missing');
 await shot(page,'03-research-result.png');
 mark('研究完成：岗位卡来自合成来源（offline_fixture），界面明确标注来源与在招状态待核验',
  {cards:cardCount,origin:ok.result.origin,readable:ok.result.readable,included:ok.result.included});

 // ---------------------------------------------------------------- 5. selection reaches the context bar
 await pane.locator('.check-label').first().click();
 await pane.getByRole('button',{name:'选择该岗位'}).first().click();
 await page.locator('[data-testid="desktop-selection"]').waitFor({timeout:20000});
 const picked=await page.locator('[data-testid="desktop-selection"]').innerText();
 assert.match(picked,/已选 /);
 await shot(page,'04-research-selected.png');
 mark('选岗写入服务端，上下文栏显示已选岗位（切页面不丢）',{selection:picked});

 // ---------------------------------------------------------------- 6. reload restores result and selection without a new run
 const beforeReload=researchPosts;
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 await openDetail(page,'岗位研究');
 await pane.locator('.job-card').first().waitFor({timeout:30000});
 assert.equal(await page.locator('[data-testid="desktop-selection"]').innerText(),picked,'selection_lost_on_reload');
 assert.equal(researchPosts,beforeReload,'reload_triggered_new_run');
 await shot(page,'05-research-reload.png');
 mark('刷新重开：岗位结果与已选岗位都从服务端回读，没有重新发起查询',{posts:researchPosts});

 // ---------------------------------------------------------------- 7. second goal, switch, come back
 await openDetail(page,'工作台');
 await page.locator('.desktop-shell[data-pane="workbench"]').waitFor({timeout:15000});
 // a new goal needs an explicit "new" action; the form edits the current goal otherwise
 await page.getByRole('button',{name:'新建求职目标'}).click();
 await page.locator('#desktop-goal-form textarea[aria-label="求职意向或补充说明"]').waitFor({timeout:15000});
 const goalB=await (async()=>{
  const form=page.locator('#desktop-goal-form');
  await form.locator('textarea[aria-label="求职意向或补充说明"]').fill('合成验收 B：找 AI 应用实习。');
  await form.locator('label:has-text("确认方向") input').fill('AI 应用');
  await form.locator('label:has-text("确认城市") input').fill('深圳');
  await form.locator('label:has-text("城市要求") select').selectOption('preferred');
  await form.locator('label:has-text("职位说明 JD") textarea').fill('合成JD B：RAG 与工具调用。');
  await page.locator('[data-testid="workbench-primary"]').click();
  await page.waitForTimeout(2500);
  return await page.evaluate(()=>new URLSearchParams(location.search).get('goal'));
 })();
 evidence.goalB=goalB;
 assert.ok(goalB&&goalB!==goalA,'second_goal_not_created');
 await page.locator('.d-goal-item').first().waitFor({timeout:30000});
 const otherGoals=await page.locator('.d-goal-item').count();
 assert.ok(otherGoals>=1,'goal_history_empty');
 await shot(page,'06-goal-history.png');
 mark('第二个目标保存后，侧栏「其他目标」列出可切换的历史目标',{count:otherGoals,goalB});

 // switch back to goal A through the history list
 const itemA=page.locator('.d-goal-item').filter({hasText:'Java 后端'}).first();
 await itemA.click();
 await page.locator('[data-testid="desktop-goal"]').filter({hasText:'Java 后端'}).waitFor({timeout:30000});
 await openDetail(page,'岗位研究');
 await pane.locator('.job-card').first().waitFor({timeout:30000});
 assert.equal(await page.locator('[data-testid="desktop-selection"]').innerText(),picked,'selection_not_restored_after_switch');
 await shot(page,'07-goal-switched-back.png');
 mark('从历史切回目标 A：研究结果与已选岗位都按目标回读，没有串到 B',{});

 // ---------------------------------------------------------------- 8. failure path (source rate limited)
 await openDetail(page,'工作台');
 await page.locator('.d-goal-item').filter({hasText:'AI 应用'}).first().click();
 await page.locator('[data-testid="desktop-goal"]').filter({hasText:'AI 应用'}).waitFor({timeout:30000});
 await openDetail(page,'岗位研究');
 await pane.getByLabel('查询关键词',{exact:true}).fill('合成失败');
 await pane.getByRole('button',{name:'查询岗位',exact:true}).click();
 await pane.getByRole('status').filter({hasText:'等待读取'}).waitFor({timeout:20000});
 const bad=researchWorker(goalB!,'rate_limited');
 await page.waitForTimeout(3500);
 const failText=await pane.locator('.research-status').innerText();
 assert.notEqual(bad.status,'SUCCEEDED','rate_limited_should_not_succeed');
 assert.ok(/未完成|未提交|限流|429|失败/.test(failText),`failure_cause_missing ${failText.slice(0,120)}`);
 await shot(page,'08-research-failed.png');
 mark('来源限流时走失败路径：面板给出状态与原因，不静默也不伪造岗位',
  {worker_status:bad.status,text:failText.replace(/\n/g,' / ').slice(0,180)});
 await context.close();
}catch(error){
 writeFileSync(join(output,'result.json'),JSON.stringify({run:runId,account:account.email,cases,page_errors:pageErrors,shots,evidence,error:(error as Error).message},null,2));
 await browser?.close();
 console.error('FAILED:',(error as Error).message);
 process.exit(1);
}

const result={run:runId,account:account.email,base,viewport:'1280x860',cases,page_errors:pageErrors,shots,evidence};
writeFileSync(join(output,'result.json'),JSON.stringify(result,null,2));
console.log(JSON.stringify({passed:cases.length,page_errors:pageErrors,shots:shots.length,runs:(evidence.runs as unknown[]).length},null,1));
await browser.close();

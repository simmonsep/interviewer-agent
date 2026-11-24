// F04 (part 3) acceptance: application tracking on the desktop shell.
// Creating an application requires a selected job and the current material revision,
// both enforced by the server; the panel shows the real blocker instead of a dead button.
//
//   D01_OUTPUT=<new dir> node apps/web/tests/d01-desktop-apps.mts
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {openDetail, waitGoalReady} from './desktop-nav.mts';
import {execFileSync} from 'node:child_process';
import {writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/desktop-handoff/F04-apps'));
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
const evidence:Record<string,unknown>={};
const account={email:`pyweb-${runId}-f04c@example.com`,password:'Synthetic-password-'+randomUUID(),name:'投递验收学员'};

let browser:Browser;
const shot=async(page:Page,name:string)=>{await page.screenshot({path:join(output,'shots',name)});shots.push(name);};

try{
 browser=await chromium.launch({executablePath:executable,headless:true});
 const context=await browser.newContext({viewport:{width:1280,height:860}});
 const page=await context.newPage();
 page.on('pageerror',error=>pageErrors.push(error.message));

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

 const pane=page.locator('[data-testid="apps-pane"]');

 // ---------------------------------------------------------------- 1. needs a goal
 await openDetail(page,'申请跟踪');
 await page.getByText('申请跟踪需要先有求职目标').waitFor({timeout:20000});
 mark('申请跟踪挂在目标下：无目标时给出空态，不显示一个空表单',{});

 await openDetail(page,'工作台');
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('合成验收：找 Java 后端实习。');
 await page.locator('label:has-text("确认方向") input').fill('Java 后端');
 await page.locator('label:has-text("确认城市") input').fill('上海');
 await page.locator('label:has-text("城市要求") select').selectOption('required');
 await page.locator('label:has-text("职位说明 JD") textarea').fill('合成JD：Java 接口与 SQL。');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);

 // ---------------------------------------------------------------- 2. blocked until a job is selected
 await openDetail(page,'申请跟踪');
 await pane.waitFor({timeout:15000});
 await page.getByText('还没有选定岗位：投递必须绑定一个岗位。').waitFor({timeout:20000});
 const createButton=pane.getByRole('button',{name:'记录为草稿'});
 assert.equal(await createButton.isDisabled(),true,'create_enabled_without_job');
 await shot(page,'01-needs-job.png');
 mark('没有选岗时创建按钮禁用并说明原因：投递必须绑定岗位（服务端 application_job_required）',{});

 // ---------------------------------------------------------------- 3. select a job through the research pane
 await openDetail(page,'岗位研究');
 const research=page.locator('[data-testid="research-pane"]');
 await research.waitFor({timeout:15000});
 await research.getByLabel('查询关键词',{exact:true}).fill('合成');
 await research.getByRole('button',{name:'查询岗位',exact:true}).click();
 await research.getByRole('status').filter({hasText:'等待读取'}).waitFor({timeout:20000});
 execFileSync(python,['-X','utf8','scripts/research-web-support.py','--goal',await page.evaluate(()=>new URLSearchParams(location.search).get('goal'))??'','--run-id',runId,'--scenario','ok'],
  {cwd:root,encoding:'utf-8',timeout:115000,windowsHide:true});
 await research.getByRole('status').filter({hasText:'读取完成'}).waitFor({timeout:30000});
 await research.locator('.job-card').first().waitFor({timeout:20000});
 await research.locator('.check-label').first().click();
 await research.getByRole('button',{name:'选择该岗位'}).first().click();
 await page.locator('[data-testid="desktop-selection"]').waitFor({timeout:20000});
 const selected=await page.locator('[data-testid="desktop-selection"]').innerText();
 mark('在岗位研究里选定岗位后，上下文栏显示已选岗位',{selected});

 // ---------------------------------------------------------------- 4. create the application
 await openDetail(page,'申请跟踪');
 await pane.waitFor({timeout:15000});
 await page.getByText('还没有选定岗位').waitFor({state:'detached',timeout:20000});
 assert.equal(await pane.getByRole('button',{name:'记录为草稿'}).isDisabled(),true,'create_enabled_before_input');
 await pane.locator('label:has-text("公司") input').fill('合成科技有限公司');
 await pane.locator('label:has-text("职位") input').fill('后端开发实习生');
 await pane.locator('label:has-text("投递链接") input').fill('https://example.com/jobs/1');
 await shot(page,'02-form.png');
 await pane.getByRole('button',{name:'记录为草稿'}).click();
 await pane.getByText('投递已记录为草稿').waitFor({timeout:30000});
 assert.equal(await pane.locator('.d-session-item').count(),1,'application_row_missing');
 assert.ok((await pane.innerText()).includes('合成科技有限公司'),'detail_card_missing_company');
 await shot(page,'03-created.png');
 mark('选岗后可以创建投递：列表出现一条记录，详情卡带公司与职位，状态为草稿',{});

 // ---------------------------------------------------------------- 5. progress
 await pane.locator('select[aria-label="新状态"]').selectOption('SUBMITTED');
 await pane.locator('textarea[aria-label="状态说明"]').fill('已在官网提交，等待回复。');
 await pane.getByRole('button',{name:'更新状态'}).click();
 await pane.getByText('状态已更新').waitFor({timeout:30000});
 await page.waitForTimeout(400);
 const detailText=await pane.locator('[aria-label="投递详情"]').innerText();
 assert.ok(detailText.includes('已投递'),'status_not_updated');
 assert.ok(detailText.includes('已在官网提交'),'event_note_missing');
 await shot(page,'04-progress.png');
 mark('推进状态写入事件流：详情状态变「已投递」，事件里保留了这次说明',{});

 // ---------------------------------------------------------------- 6. availability
 await pane.locator('select[aria-label="到岗意向"]').selectOption('open');
 await pane.locator('label:has-text("说明") input').fill('暑期可到岗，每周四天。');
 await pane.getByRole('button',{name:'保存意向'}).click();
 await pane.getByText('到岗意向已记录。').waitFor({timeout:30000});
 await page.waitForTimeout(400);
 assert.ok((await pane.innerText()).includes('暑期可到岗'),'availability_note_missing');
 await shot(page,'05-availability.png');
 mark('到岗意向单独记录，与状态变更分开',{});

 // ---------------------------------------------------------------- 7. reload restores status and events
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 await openDetail(page,'申请跟踪');
 await pane.locator('.d-session-item').first().waitFor({timeout:30000});
 await pane.locator('.d-session-item').first().click();
 await page.getByText('合成科技有限公司').first().waitFor({timeout:20000});
 const afterReload=await pane.locator('[aria-label="投递详情"]').innerText();
 assert.ok(afterReload.includes('已投递'),'status_lost_on_reload');
 assert.ok(afterReload.includes('已在官网提交'),'event_lost_on_reload');
 await shot(page,'06-reload.png');
 mark('刷新后从列表打开同一投递：状态与事件都回读，没有新建记录',{});

 // ---------------------------------------------------------------- 8. narrow window
 await page.setViewportSize({width:880,height:600});
 await page.waitForTimeout(400);
 const overflow=await page.evaluate(()=>({inner:innerWidth,doc:document.documentElement.scrollWidth}));
 assert.ok(overflow.doc<=overflow.inner+2,`narrow_overflow ${JSON.stringify(overflow)}`);
 await shot(page,'07-narrow.png');
 mark('880×600 窄窗下投递页无横向溢出',overflow);
 await context.close();
}catch(error){
 writeFileSync(join(output,'result.json'),JSON.stringify({run:runId,account:account.email,cases,page_errors:pageErrors,shots,evidence,error:(error as Error).message},null,2));
 await browser?.close();
 console.error('FAILED:',(error as Error).message);
 process.exit(1);
}

writeFileSync(join(output,'result.json'),JSON.stringify({run:runId,account:account.email,base,viewport:'1280x860',cases,page_errors:pageErrors,shots,evidence},null,2));
console.log(JSON.stringify({passed:cases.length,page_errors:pageErrors,shots:shots.length},null,1));
await browser.close();

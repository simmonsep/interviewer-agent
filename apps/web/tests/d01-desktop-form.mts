// F05B acceptance: the product form changes — top bar with a collapsible sidebar,
// guest direct entry (no login screen on first run), settings additions (account +
// model access), the small-type font swap, and the tracking surface after the
// F05C revision (local site progress sync entry + link copy; the paste-email
// parser has been removed). All on the real local stack, no injection, no model
// calls.
//
//   D05B_OUTPUT=<new dir> node apps/web/tests/d01-desktop-form.mts
//
// Requires: API (with --model-config), Vite on 5174, PG/Redis up.
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {openDetail, waitGoalReady} from './desktop-nav.mts';
import {execFileSync,spawn,type ChildProcess} from 'node:child_process';
import {readFileSync,writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D05B_OUTPUT??join(root,'docs/records/desktop-handoff/F05B-form'));
const base=process.env.D05B_BASE_URL??'http://127.0.0.1:5174';
const modelFile=resolve(process.env.D05B_MODEL_FILE??join(root,'.private/f05b/model.json'));
const pythonExe=process.env.D05B_PYTHON??'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
const fixture=JSON.parse(readFileSync(join(root,'fixtures/question-banks/agent-developer-trial.json'),'utf8'));
mkdirSync(join(root,'.private/f05b'),{recursive:true});
mkdirSync(output,{recursive:true});

const cases:{name:string;detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,detail?:unknown)=>{cases.push({name,detail});console.log('PASS',name);};
const evidence:Record<string,unknown>={run:new Date().toISOString(),cases,page_errors:pageErrors,screenshots:[],
 case_type_note:'真实本地服务联调；研究执行体为仓库自带离线夹具脚本；模型接入只验证保存与掩码，不调用任何模型。'};

function shot(page:Page,name:string){
 const file=join(output,'shots',name);
 mkdirSync(join(output,'shots'),{recursive:true});
 return page.screenshot({path:file}).then(()=>{(evidence.screenshots as string[]).push(file);});
}
function runSupport(goalId:string,scenario:'ok'|'rate_limited'='ok'){
 execFileSync(pythonExe,['-X','utf8',join(root,'scripts/research-web-support.py'),'--goal',goalId,
  '--run-id',randomUUID(),'--scenario',scenario],{encoding:'utf8',timeout:180000,windowsHide:true});
}
async function nums(page:Page){
 return page.evaluate(()=>{
  const nodes=[...document.querySelectorAll('.desktop-shell .d-num, .desktop-shell .d-num-sm, .desktop-shell .d-num-lg')];
  const families=new Set(nodes.map(node=>getComputedStyle(node).fontFamily.split(',')[0].trim().replace(/"/g,'')));
  const sizes=new Set(nodes.map(node=>getComputedStyle(node).fontSize));
  return {count:nodes.length,families:[...families],sizes:[...sizes]};
 });
}
async function setupGoal(page:Page){
 // The settings step leaves the shell on the settings pane; the goal form lives in the
 // (hidden but mounted) workbench pane.
 await openDetail(page,'工作台');
 await page.locator('.desktop-shell[data-pane="workbench"]').waitFor({timeout:15000});
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('研二，想在上海找 AI 应用 / Agent 开发实习。');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);
 return page.evaluate(async()=>{
  const response=await fetch('/api/career-goals/',{credentials:'same-origin',cache:'no-store'});
  const body=await response.json() as {items:string[]};
  return body.items[0];
 });
}

let browser:Browser|null=null;
let vite:ChildProcess|null=null;
let api:ChildProcess|null=null;
try{
 // ------------------------------------------------ 0. services (the suites normally start them; here we own them)
 const vitePath=join(root,'apps/web/node_modules/vite/bin/vite.js');
 vite=spawn(process.env.D05B_NODE??'node',[vitePath,'--host','127.0.0.1','--port','5174','--strictPort'],
  {cwd:join(root,'apps/web'),stdio:'ignore',env:{...process.env,PATH:`/e/codex/myagent/node24-shim:${process.env.PATH??''}`}});
 api=spawn(pythonExe,['-X','utf8','-m','interview_agent.api','--config',join(root,'.private/postgres/test.json'),
  '--model-config',modelFile],{cwd:join(root,'backend'),stdio:'ignore',env:{...process.env,PYTHONPATH:join(root,'backend/src')}});
 const started=Date.now();
 while(Date.now()-started<60000){
  try{
   const health=await fetch('http://127.0.0.1:3100/health',{signal:AbortSignal.timeout(1200)});
   const app=await fetch(base+'/',{signal:AbortSignal.timeout(1200)});
   if(health.ok&&app.ok)break;
  }catch{}
  await delay(500);
 }

 // ------------------------------------------------ 1. guest direct entry: no login screen
 const context=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true})
  .then(browser=>browser.newContext({viewport:{width:1280,height:860}}));
 browser=context.browser();
 const page=await context.newPage();
 page.on('pageerror',error=>pageErrors.push(error.message));
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane="workbench"]').waitFor({timeout:60000});
 assert.equal(await page.locator('.desktop-auth-card').count(),0,'login_screen_shown_on_first_run');
 const identityEmail=await page.evaluate(()=>{try{return (JSON.parse(localStorage.getItem('py-local-identity')??'null') as {email:string}|null)?.email??'';}catch{return '';}});
 assert.match(identityEmail,/^local-[0-9a-f-]{36}@desktop\.example\.com$/,'guest_identity_shape_unexpected');
 mark('免登直入：首次打开没有登录页，自动用本机身份进入工作台（身份是本机服务里的真实账号，历史照常积累）',
  {identity:identityEmail.replace(/[0-9a-f-]{36}/,'<uuid>')});

 // ------------------------------------------------ 2. top bar: collapse persists across reload
 await page.locator('[data-testid="rail-toggle"]').click();
 await page.locator('.desktop-shell[data-rail="collapsed"]').waitFor({timeout:10000});
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 assert.equal(await page.locator('.desktop-shell').getAttribute('data-rail'),'collapsed','collapse_not_kept_after_reload');
 await page.locator('[data-testid="rail-toggle"]').click();
 await page.locator('.desktop-shell[data-rail="open"]').waitFor({timeout:10000});
 mark('上边栏 ☰ 收起/展开侧栏：状态存本机，刷新后仍保持（参照 panes 的 sidebarPin 模式）',{});
 await shot(page,'01-topbar-collapsed.png');

 // ------------------------------------------------ 3. theme quick switch in the top bar
 await page.locator('.d-theme-quick button[title="深色"]').click();
 await page.locator('.desktop-shell[data-theme="dark"]').waitFor({timeout:10000});
 await page.locator('.d-theme-quick button[title="浅色"]').click();
 await page.locator('.desktop-shell[data-theme="light"]').waitFor({timeout:10000});
 mark('上边栏外观快切（浅/深/跟随系统）：与设置页共用同一偏好');

 // ------------------------------------------------ 4. settings: account + model access
 await page.locator('[data-testid="nav-settings"]').click();
 await page.locator('[data-testid="settings-pane"]').waitFor({timeout:15000});
 const accountRow=await page.locator('.d-set-row',{hasText:'当前身份'}).innerText();
 assert.match(accountRow,/本机用户/,'guest_name_missing_in_settings');
 assert.match(await page.locator('.d-set-row',{hasText:'标识'}).innerText(),/@desktop\.example\.com/);
 const secret=randomUUID();
 await page.locator('.d-model-form input').first().fill('self-hosted');
 await page.locator('.d-model-form input').nth(1).fill('https://api.example.com/v1');
 await page.locator('.d-model-form input').nth(2).fill('deepseek-chat');
 await page.locator('.d-model-form input[type="password"]').fill(secret);
 await page.getByRole('button',{name:'保存模型配置'}).click();
 // F07Z-138: wait for the panel's own save notice, which only exists after the PUT returns.
 // The old structural wait swallowed its timeout with .catch(()=>{}), so the row could be read
 // while the save result was still unrendered and the assertion failed on timing, not on the
 // product (same run: the row said 免费脚本模式, and a few milliseconds later 已保存，未启用).
 await page.locator('.d-set-group .notice').filter({hasText:'已保存'}).first().waitFor({timeout:20000});
 // F07Z-138: the settings pane renamed this row when the three model switches were split;
 // asserting the tab's new label keeps the original intent (saving must not enable live).
 const statusRow=await page.locator('.d-set-row',{hasText:'面试/教练模式'}).innerText();
 assert.match(statusRow,/未启用/,'saved_should_not_activate');
 const stored=JSON.parse(readFileSync(modelFile,'utf8')) as {provider:string;api_key:string;enabled:boolean;key_storage?:string};
 assert.equal(stored.provider,'self-hosted');
 const vault=stored.key_storage==='windows_credential_manager';
 assert.equal(stored.api_key,vault?'':secret,
  vault?'key_must_not_be_plaintext_in_model_json':'key_not_persisted_to_external_file');
 const session=await page.evaluate(async()=>{
  const response=await fetch('/api/runtime/model-settings',{credentials:'same-origin',cache:'no-store'});
  return response.json() as {key_masked:string;api_key?:string;active:boolean;assessment_mode:string;key_storage:string};
 });
 assert.ok(!('api_key' in session),'key_must_not_round_trip');
 assert.equal(session.key_storage,vault?'windows_credential_manager':'local_file','key_storage_not_reported');
 assert.match(session.key_masked,new RegExp(`${secret.slice(-4)}$`),'mask_wrong');
 assert.equal(session.active,false,'save_must_not_activate_live');
 mark('设置：账号区显示本机身份；模型 Key 优先存 Windows 凭据管理器（model.json 不留明文，不可用时回退本机文件），GET 只回掩码，保存不等于启用',
  {key_masked:session.key_masked,key_storage:session.key_storage,assessment_mode:session.assessment_mode});
 await shot(page,'02-settings-model.png');

 // ------------------------------------------------ 5. small type: system UI family, tabular
 const before=await nums(page);
 assert.equal(before.families.length,1,`mixed_numeric_families ${JSON.stringify(before.families)}`);
 assert.match(before.families[0],/Segoe UI|system-ui/,'small_digits_should_use_system_ui_stack');
 mark('小字修正：数字与元信息用系统 UI 字栈 + tabular-nums（等宽刻板感来自 Geist Mono，已移除）',
  {family:before.families[0],sizes:before.sizes});

 // ------------------------------------------------ 6. tracking: paste-email parse into a real event
 const goalId=await setupGoal(page);
 await openDetail(page,'岗位研究');
 const pane=page.locator('[data-testid="research-pane"]');
 await pane.getByLabel('查询关键词',{exact:true}).fill('合成');
 await pane.getByRole('button',{name:'查询岗位',exact:true}).click();
 await pane.getByRole('status').filter({hasText:'等待读取'}).waitFor({timeout:20000});
 runSupport(goalId!);
 await pane.locator('.job-match, .research-jobs, article, [class*="job"]').first().waitFor({timeout:60000}).catch(()=>{});
 await page.waitForTimeout(1200);
 const info=await page.evaluate(()=>{
  const label=document.querySelector('[data-testid="research-pane"] .check-label') as HTMLElement|null;
  const button=[...document.querySelectorAll('[data-testid="research-pane"] button')].find(b=>b.textContent?.includes('选择该岗位')) as HTMLElement|null;
  return {hasLabel:Boolean(label),hasButton:Boolean(button),status:(document.querySelector('[data-testid="research-pane"] .research-status') as HTMLElement|null)?.innerText?.slice(0,120)};
 });
 assert.ok(info.hasLabel&&info.hasButton,`research_result_missing ${JSON.stringify(info)}`);
 await pane.locator('.check-label').first().click();
 await pane.getByRole('button',{name:'选择该岗位'}).first().click();
 await page.locator('[data-testid="desktop-selection"]').waitFor({timeout:30000});
 await openDetail(page,'申请跟踪');
 const apps=page.locator('[data-testid="apps-pane"]');
 await apps.locator('input[placeholder="例如：某科技公司"]').fill('合成科技');
 await apps.locator('input[placeholder="例如：后端开发实习生"]').fill('Agent 开发实习生');
 await apps.locator('input[placeholder="https://…"]').fill('https://jobs.example.com/agent-intern');
 await apps.getByRole('button',{name:'记录为草稿'}).click();
 await apps.locator('.d-session-item').first().waitFor({timeout:30000});
 await apps.locator('.d-session-item').first().click();
 await apps.getByRole('button',{name:'复制链接'}).waitFor({timeout:15000});
 // F05C：粘贴邮件解析已下线；进度改为从用户已登录的站点页面做本机同步。
 const sync=apps.locator('.d-sync');
 await sync.waitFor({timeout:15000});
 assert.equal(await apps.locator('textarea[aria-label="邮件或回执正文"]').count(),0,'mail_parser_should_be_removed');
 assert.ok(await sync.getByRole('button',{name:'打开站点'}).count(),'site_sync_open_missing');
 assert.match(await sync.innerText(),/不保存 Cookie 或令牌/,'site_sync_boundary_missing');
 // Manual progress still goes through the original event stream and state machine.
 await apps.locator('select[aria-label="新状态"]').selectOption('SUBMITTED');
 await apps.locator('textarea[aria-label="状态说明"]').fill('合成验收：用户本人确认已投递。');
 await apps.getByRole('button',{name:'更新状态'}).click();
 await apps.locator('.d-app-events li').first().waitFor({timeout:30000});
 const events=await apps.locator('.d-app-events').innerText();
 assert.match(events,/用户本人确认已投递/,'manual_event_missing');
 assert.match(events,/包 v/,'event_missing_package_revision');
 mark('申请跟踪：邮件解析下线，改为本机站点进度同步入口；链接可复制，手动状态仍写入事件流',
  {status:'SUBMITTED',evidence:'user_confirmation',sync_entry:'local_site_sync'});
 await shot(page,'03-site-sync-entry.png');

 // ------------------------------------------------ 7. switch account reaches the login page and back
 await page.locator('.d-nav-item[title="设置"]').click();
 await page.getByRole('button',{name:'切换账号'}).click();
 await page.locator('.desktop-shell.is-login').waitFor({timeout:15000});
 await page.getByRole('button',{name:'使用本机身份进入工作台'}).click();
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:60000});
 mark('切换账号：登录页可到达；「使用本机身份进入工作台」一键回到免登状态',{});
 await context.close();

 // ------------------------------------------------ 8. web entry untouched: still shows its own login
 const webContext=await browser.newContext({viewport:{width:1280,height:860}});
 const webPage=await webContext.newPage();
 webPage.on('pageerror',error=>pageErrors.push(error.message));
 await webPage.goto(`${base}/?shell=web`,{waitUntil:'domcontentloaded'});
 await webPage.locator('.auth-layout').waitFor({timeout:30000});
 assert.equal(await webPage.locator('.desktop-shell').count(),0,'desktop_shell_leaked_into_web');
 await webContext.close();
 mark('浏览器入口不受影响：仍是站点页头 + 登录页，无桌面外壳', {});

 evidence.cases=cases;
 writeFileSync(join(output,'result.json'),JSON.stringify(evidence,null,2));
 console.log(`\n${cases.length}/${cases.length} passed`);
 process.exit(0);
}catch(error){
 evidence.failure=(error as Error).message;
 evidence.cases=cases;
 writeFileSync(join(output,'result.json'),JSON.stringify(evidence,null,2));
 console.error('FAILED:',(error as Error).message);
 process.exit(1);
}finally{
 if(browser)await browser.close().catch(()=>{});
 vite?.kill();api?.kill();
}

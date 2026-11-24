// F07Z-165 (arbitration THIRD_OPTION) UI acceptance: each explicit enable is its own
// authorization instance, a plain save keeps the bound one, a paused row says so and offers
// "新建授权". Real local stack, no model calls (scripted stays scripted; no key is used).
//
//   node --experimental-strip-types apps/web/tests/model-authorization-instances.mts
//
// Requires: PG/Redis up. The suite starts Vite 5174 and the API 3100 itself.
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {execFileSync,spawn,type ChildProcess} from 'node:child_process';
import {mkdirSync,writeFileSync,readFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.AUTHZ_OUTPUT??join(root,'.private/f07z165/authz-ui'));
const modelFile=resolve(process.env.AUTHZ_MODEL_FILE??join(root,'.private/f07z165/authz-ui/model.json'));
const pythonExe=process.env.AUTHZ_PYTHON??'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
const dbConfig=resolve(process.env.AUTHZ_DB_CONFIG??join(root,'.private/postgres/test-migration.json'));
mkdirSync(output,{recursive:true});
const cases:{name:string;detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,detail?:unknown)=>{cases.push({name,detail});console.log('PASS',name);};

// Owner-side SQL through the same psycopg the backend uses; the test only inspects and pauses
// the row it created itself.
function sql(statement:string,params:string[]=[]){
 const code=`import json,sys,psycopg\n`+
  `cfg=json.load(open(r'${dbConfig}',encoding='utf-8'))\n`+
  `conn=psycopg.connect(host=cfg['host'],port=cfg['port'],dbname=cfg['dbname'],user=cfg['user'],password=cfg['password'],autocommit=True)\n`+
  `rows=conn.execute(sys.argv[1],sys.argv[2:]).fetchall()\n`+
  `print(json.dumps(rows))\n`;
 return JSON.parse(execFileSync(pythonExe,['-X','utf8','-c',code,statement,...params],{encoding:'utf8'}).trim());
}

async function settings(page:Page){
 return page.evaluate(async()=>await (await fetch('/api/runtime/model-settings',{credentials:'same-origin',cache:'no-store'})).json() as {
  enabled:boolean;active:boolean;live_authorization:string;live_state:string;live_cap_cny:number;
  live_spent_cny:number|null;live_remaining_cny:number|null});
}

let browser:Browser|null=null;
let vite:ChildProcess|null=null;
let api:ChildProcess|null=null;
const created:string[]=[];
try{
 const vitePath=join(root,'apps/web/node_modules/vite/bin/vite.js');
 vite=spawn(process.env.AUTHZ_NODE??'node',[vitePath,'--host','127.0.0.1','--port','5174','--strictPort'],
  {cwd:join(root,'apps/web'),stdio:'ignore'});
 api=spawn(pythonExe,['-X','utf8','-m','interview_agent.api','--config',join(root,'.private/postgres/test.json'),
  '--model-config',modelFile],{cwd:join(root,'backend'),stdio:'ignore',
  env:{...process.env,PYTHONPATH:join(root,'backend/src'),INTERVIEW_KEY_VAULT:'off'}});
 const started=Date.now();
 while(Date.now()-started<60000){
  try{
   const health=await fetch('http://127.0.0.1:3100/health',{signal:AbortSignal.timeout(1200)});
   const app=await fetch('http://127.0.0.1:5174/',{signal:AbortSignal.timeout(1200)});
   if(health.ok&&app.ok)break;
  }catch{}
  await delay(500);
 }
 const context=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true})
  .then(value=>value.newContext({viewport:{width:1280,height:900}}));
 browser=context.browser();
 const page=await context.newPage();
 page.on('pageerror',error=>pageErrors.push(error.message));
 await page.goto('http://127.0.0.1:5174/?shell=desktop',{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:60000});
 await page.locator('[data-testid="nav-settings"]').click();
 await page.locator('[data-testid="settings-pane"]').waitFor({timeout:20000});

 const form=page.locator('.d-model-form');
 await form.locator('input').nth(0).fill('deepseek');
 await form.locator('input').nth(1).fill('https://api.deepseek.com');
 await form.locator('input').nth(2).fill('deepseek-chat');
 await form.locator('input[type="password"]').fill('sk-ui-instance-123456789');
 await form.locator('label.d-check-row',{hasText:'面试/教练'}).locator('input').check();
 await form.locator('input[type="number"]').first().fill('0.5');
 await page.getByRole('button',{name:'保存模型配置'}).click();
 await page.locator('.d-set-group .notice').filter({hasText:'已保存'}).first().waitFor({timeout:30000});

 const first=await settings(page);
 created.push(first.live_authorization);
 assert.match(first.live_authorization,/^desktop-live-[0-9a-f-]{36}$/,'instance_id_shape');
 assert.equal(first.live_state,'ACTIVE');
 assert.equal(first.live_spent_cny,0);
 assert.equal(first.live_remaining_cny,0.5);
 const spendRow=await page.locator('[data-testid="live-spend"]').innerText();
 assert.match(spendRow,/¥0\.000 已花/,'spend_row_missing');
 mark('首次启用生成独立授权实例（id 带客户端请求标识，不是 policy+cap 指纹）',{id:first.live_authorization});

 // Saving again without a new intent keeps the same row and never mints a second budget.
 await page.getByRole('button',{name:'保存模型配置'}).click();
 await page.locator('.d-set-group .notice').filter({hasText:'已保存'}).first().waitFor({timeout:30000});
 const kept=await settings(page);
 assert.equal(kept.live_authorization,first.live_authorization,'plain_save_must_rebind');
 assert.equal(kept.live_cap_cny,0.5);
 mark('普通保存（只改 Key/不动开关）沿用当前授权，不新建预算',{id:kept.live_authorization});

 // Changing the cap is a new intent: the UI asks for a new instance and the old one stays.
 await form.locator('input[type="number"]').first().fill('0.9');
 await page.getByRole('button',{name:'保存模型配置'}).click();
 await page.locator('.d-set-group .notice').filter({hasText:'已保存'}).first().waitFor({timeout:30000});
 const changed=await settings(page);
 created.push(changed.live_authorization);
 assert.notEqual(changed.live_authorization,first.live_authorization,'changed_cap_must_mint');
 assert.equal(changed.live_cap_cny,0.9);
 assert.equal(changed.live_state,'ACTIVE');
 mark('修改授权上限会新建一笔授权（旧花费不结转）',{old:first.live_authorization,next:changed.live_authorization});

 // A row paused after an uncertain call is visible and can be replaced, never auto-resumed.
 sql("UPDATE app.model_authorizations SET state='PAUSED' WHERE id=%s RETURNING id",[changed.live_authorization]);
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 // The pane choice is not in the URL; reopen settings after the reload.
 await page.locator('[data-testid="nav-settings"]').click();
 await page.locator('[data-testid="settings-pane"]').waitFor({timeout:30000});
 const paused=await settings(page);
 assert.equal(paused.live_state,'PAUSED');
 await page.locator('.d-set-group .error',{hasText:'这笔授权已停止'}).waitFor({timeout:15000});
 mark('授权暂停后在设置页可见：明确提示不会自动恢复、需要新建',{state:paused.live_state});

 await page.locator('[data-testid="renew-live"]').click();
 await page.locator('.d-set-group .notice').filter({hasText:'已保存'}).first().waitFor({timeout:30000});
 const renewed=await settings(page);
 created.push(renewed.live_authorization);
 assert.notEqual(renewed.live_authorization,changed.live_authorization,'renew_must_mint');
 assert.equal(renewed.live_state,'ACTIVE');
 assert.equal(renewed.live_cap_cny,0.9);
 const old=sql('SELECT state FROM app.model_authorizations WHERE id=%s',[changed.live_authorization]);
 assert.deepEqual(old,[['PAUSED']],'old_row_must_stay_paused');
 mark('「新建授权」得到新的 ACTIVE 实例，旧实例保持 PAUSED（历史保留）',
  {paused:changed.live_authorization,renewed:renewed.live_authorization});

 assert.deepEqual(pageErrors,[]);
 writeFileSync(join(output,'result.json'),JSON.stringify({run:new Date().toISOString(),cases,page_errors:pageErrors,
  stored:readFileSync(modelFile,'utf8')},null,2));
 await page.screenshot({path:join(output,'settings.png'),fullPage:true});
 console.log(JSON.stringify({cases:cases.length,output}));
}finally{
 if(browser)await browser.close().catch(()=>{});
 for(const identity of created.filter(Boolean))sql('DELETE FROM app.model_authorizations WHERE id=%s RETURNING id',[identity]);
 if(api&&api.exitCode===null)api.kill();
 if(vite&&vite.exitCode===null)vite.kill();
}

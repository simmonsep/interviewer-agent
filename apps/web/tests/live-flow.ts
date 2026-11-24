import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {existsSync,readFileSync,mkdirSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {createTrainingHarness} from '../../worker/tests/training-support.ts';
import {createPI09AssessmentExecutor,loadPI09Config} from '../../worker/src/assessment.ts';

if(!process.argv.includes('--run'))throw Error('explicit_live_run_required');
const root=process.cwd(),config=loadPI09Config(root),output=process.env.PI09_LIVE_OUTPUT??join(root,'.private/pi109-live',randomUUID());mkdirSync(output,{recursive:true});
const ledgerPath=join(root,'.private/pi09/spend-ledger.json');
const before=existsSync(ledgerPath)?JSON.parse(readFileSync(ledgerPath,'utf8')):{authorization_id:config.authorization.id,attempts:0,reserved_cny:0};
const h=await createTrainingHarness(root,{port:3339,serveWeb:true,autoDispatch:true,assess:createPI09AssessmentExecutor(root,true)});
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);if(!executable)throw Error('installed_browser_required');
const browser=await chromium.launch({executablePath:executable,headless:true}),context=await browser.newContext({viewport:{width:1440,height:1100}}),page=await context.newPage();
let passed=false,evidence:unknown=null,failure:string|null=null;
try{
 await page.goto(h.config.baseURL);await page.getByRole('button',{name:'第一次来？创建账号'}).click();await page.getByLabel('称呼').fill('真实模型联调学员');await page.getByLabel('邮箱').fill(randomUUID()+'@example.invalid');await page.getByLabel('密码').fill('Synthetic-live-'+randomUUID());await page.getByRole('button',{name:'创建账号',exact:true}).click();await page.getByRole('heading',{name:'今天，先练好一题。'}).waitFor();
 await page.getByRole('button',{name:'开始这一题 →'}).click();await page.getByLabel('你的回答',{exact:true}).fill('JSON Schema通过只能说明参数形状符合要求，不能证明有资源访问权限。\n服务端先校验输入，从认证会话获得当前用户，再检查文档归属与允许的操作，实际工具执行入口也要检查授权，不能信任模型传入的user_id。');
 await page.getByRole('button',{name:'提交并查看反馈 →'}).click();await page.getByRole('heading',{name:'回看这一题'}).waitFor({timeout:130000});
 const sessionId=new URL(page.url()).searchParams.get('session')!;
 const row=(await h.pool.query('SELECT ar.*,s.status FROM app.assessment_revisions ar JOIN app.practice_sessions s ON s.id=ar.session_id WHERE s.id=$1',[sessionId])).rows[0];
 assert.equal(row.status,'COMPLETED');assert.equal(row.metadata.origin,'live_model');assert.equal(row.semantic_scoring_eligible,false);assert.equal(row.metadata.usage.usage_complete,true);assert.ok(row.candidate.citations.some((c:any)=>c.kind==='answer'));assert.ok(row.candidate.citations.some((c:any)=>c.kind==='evidence'));
 const reports=join(root,'reports');mkdirSync(reports,{recursive:true});await page.screenshot({path:join(reports,'PI-09-live-feedback.png'),fullPage:true});
 await page.reload();await page.getByRole('heading',{name:'回看这一题'}).waitFor();
 evidence={session_id:sessionId,origin:row.metadata.origin,metadata:row.metadata,candidate:row.candidate,semantic_scoring_eligible:false,reload_restored:true};passed=true;
}catch(error){failure=error instanceof Error?error.message:'live_web_flow_failed';writeFileSync(join(output,'failure.txt'),error instanceof Error?error.stack??error.message:String(error));await page.screenshot({path:join(output,'failure.png'),fullPage:true}).catch(()=>{});}
finally{
 await browser.close();await h.close();
 const after=existsSync(ledgerPath)?JSON.parse(readFileSync(ledgerPath,'utf8')):before;
 const receipt={step:'PI-09-live-web',status:passed?'passed':'failed',checkedAt:new Date().toISOString(),authorization_id:config.authorization.id,max_cny:2,before,after,new_http_attempts:after.attempts-before.attempts,evidence,failure,scope:'real browser signup/answer -> HTTP API -> SQL answer/operation/outbox -> BullMQ -> real Pi/DeepSeek -> citation gate -> SQL feedback -> SSE/snapshot -> browser; synthetic answer; no independent semantic quality result',human_reviewed:false,semantic_accuracy:null};
 writeFileSync(join(output,'receipt.json'),JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify({status:receipt.status,receipt:join(output,'receipt.json'),http_attempts:receipt.new_http_attempts,reserved_cny_total:after.reserved_cny,max_cny:2,failure}));process.exitCode=passed?0:1;
}

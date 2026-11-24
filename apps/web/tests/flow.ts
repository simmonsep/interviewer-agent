import assert from 'node:assert/strict';
import {chromium,type Page} from '@playwright/test';
import {mkdirSync,writeFileSync,existsSync} from 'node:fs';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import {createTrainingHarness} from '../../worker/tests/training-support.ts';

const root=process.cwd(),output=process.env.PI09_WEB_OUTPUT??join(root,'.private/pi109-web',randomUUID());mkdirSync(output,{recursive:true});
const reports=join(root,'reports');mkdirSync(reports,{recursive:true});
const h=await createTrainingHarness(root,{port:3329,serveWeb:true,autoDispatch:true});h.controls.delayMs=1800;
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
if(!executable)throw Error('installed_browser_required');
const browser=await chromium.launch({executablePath:executable,headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1100}}),page=await context.newPage();
const errors:string[]=[],cases:{name:string;status:string}[]=[],streams:number[]=[];
page.on('pageerror',e=>errors.push(e.message));page.on('response',response=>{if(response.url().includes('/events?'))streams.push(response.status());});
const mark=(name:string)=>cases.push({name,status:'passed'});
const answer='参数格式通过不等于获得读取权限。\n\n服务端应从会话取得身份，校验资源所属关系，并在工具入口再次检查。';
async function signup(p:Page,name:string){
 await p.getByRole('button',{name:'第一次来？创建账号'}).click();await p.getByLabel('称呼').fill(name);await p.getByLabel('邮箱').fill(randomUUID()+'@example.invalid');await p.getByLabel('密码').fill('Synthetic-password-'+randomUUID());await p.getByRole('button',{name:'创建账号',exact:true}).click();await p.getByRole('heading',{name:'今天，先练好一题。'}).waitFor();
}
let passed=false;
try{
 await page.goto(h.config.baseURL);await signup(page,'本地测试学员');
 assert.ok(await page.getByText('无需上传材料',{exact:true}).isVisible());assert.equal(await page.locator('input[type=file]').count(),0);
 await page.screenshot({path:join(reports,'PI-09-home.png'),fullPage:true});mark('W01 actual browser signup and role-only entry require no resume or JD');
 await page.getByRole('button',{name:'开始这一题 →'}).click();await page.getByLabel('你的回答',{exact:true}).fill(answer);const originalURL=page.url(),originalId=new URL(originalURL).searchParams.get('session')!;
 await page.reload();await page.getByLabel('你的回答',{exact:true}).waitFor();assert.equal(await page.getByLabel('你的回答',{exact:true}).inputValue(),answer);mark('W02 browser refresh restores waiting session and user-scoped draft');
 let submissions=0;page.on('request',request=>{if(request.method()==='POST'&&request.url().endsWith('/answers'))submissions++;});
 await page.getByRole('button',{name:'提交并查看反馈 →'}).dblclick();await page.getByText('你的回答 · 已保存',{exact:true}).waitFor();await page.reload();await page.getByText('你的回答 · 已保存',{exact:true}).waitFor();
 assert.equal(submissions,1);assert.equal((await h.pool.query('SELECT count(*)::int n FROM app.answer_attempts WHERE session_id=$1',[originalId])).rows[0].n,1);mark('W03 double submission emits one command; reload retains original submitted answer');
 await page.getByRole('heading',{name:'回看这一题'}).waitFor({timeout:15000});assert.ok(await page.getByText('离线协议联调反馈',{exact:true}).isVisible());assert.ok(streams.includes(200));
 assert.ok(await page.getByText('参考资料摘要',{exact:false}).first().isVisible());assert.equal((await h.pool.query('SELECT count(*)::int n FROM app.assessment_revisions WHERE session_id=$1',[originalId])).rows[0].n,1);
 await page.screenshot({path:join(reports,'PI-09-feedback.png'),fullPage:true});mark('W04 real SSE status and SQL snapshot show quoted feedback; no formal score');
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);await page.screenshot({path:join(reports,'PI-09-mobile.png'),fullPage:true});await page.setViewportSize({width:1440,height:1100});mark('W05 mobile viewport renders without horizontal overflow');
 const other=await browser.newContext(),otherPage=await other.newPage();await otherPage.goto(originalURL);await signup(otherPage,'另一个测试学员');await otherPage.getByRole('alert').filter({hasText:'不可用'}).waitFor();assert.equal(await otherPage.getByRole('heading',{name:'回看这一题'}).count(),0);await other.close();mark('W06 another account cannot restore a copied session link or see its feedback');
 await page.getByRole('button',{name:'返回练习首页'}).click();await page.getByLabel('选择练习问题').selectOption({index:2});const selected=await page.locator('.question-preview').textContent();
 await page.route(`**/api/practice-sessions/${originalId}`,async route=>{await delay(600);await route.continue();});
 await page.locator('.history-item').first().click();await page.getByRole('button',{name:'开始这一题 →'}).click();await page.locator('.question-title').waitFor();await delay(800);
 assert.equal(await page.locator('.question-title').textContent(),selected);assert.notEqual(new URL(page.url()).searchParams.get('session'),originalId);await page.unroute(`**/api/practice-sessions/${originalId}`);mark('W07 delayed old-session response cannot overwrite a newly started session');
 h.controls.delayMs=2500;await page.getByLabel('你的回答',{exact:true}).fill('这是一条用于验证取消路径的合成回答。');await page.getByRole('button',{name:'提交并查看反馈 →'}).click();await page.getByRole('button',{name:'取消分析',exact:true}).click();await page.getByRole('heading',{name:'已取消分析，原回答仍保留'}).waitFor();assert.equal(await page.getByRole('heading',{name:'回看这一题'}).count(),0);assert.deepEqual(errors,[]);mark('W08 browser cancel rejects late feedback and the flow has no page JS errors');
 passed=true;
}catch(error){writeFileSync(join(output,'failure.txt'),error instanceof Error?error.stack??error.message:String(error));await page.screenshot({path:join(output,'failure.png'),fullPage:true}).catch(()=>{});console.error(error instanceof Error?error.message:'web_flow_failed');}
finally{await browser.close();await h.close();writeFileSync(join(output,'receipt.json'),JSON.stringify({step:'PI-09-web',status:passed?'passed':'failed',checkedAt:new Date().toISOString(),browser:executable,cases,pageErrors:errors,sseResponseStatuses:streams,modelCalls:0,scope:'real headless installed Chromium browser, React build, HTTP Fastify, PostgreSQL/Redis/BullMQ, real Pi with scripted provider; synthetic accounts; not live semantic feedback'},null,2)+'\n');console.log(JSON.stringify({status:passed?'passed':'failed',receipt:join(output,'receipt.json'),cases:cases.length}));process.exitCode=passed?0:1;}

import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {chromium} from '@playwright/test';

const run=process.env.JA02_RUN_ID!,output=process.env.JA02_OUTPUT!,python=process.env.JA02_PYTHON!,live=process.env.JA02_SOURCE_LIVE==='1';
const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
const cases:string[]=[],errors:string[]=[];
try{
 const context=await browser.newContext(),page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 async function register(p:typeof page,suffix:string){
  await p.goto('http://127.0.0.1:5174/?shell=web');await p.getByRole('button',{name:'第一次来？创建账号'}).click();
  await p.getByLabel('称呼').fill('Synthetic Student');await p.getByLabel('邮箱').fill(`pyweb-${run}-${suffix}@example.com`);await p.getByLabel('密码').fill('Synthetic-password-42!');
  await p.getByRole('button',{name:'创建账号',exact:true}).click();await p.getByRole('button',{name:'退出登录'}).waitFor();
 }
 await register(page,'research');await page.getByRole('button',{name:'求职目标',exact:true}).click();
 await page.getByLabel('求职意向或补充说明').fill('合成验收：我要找后端实习。');
 await page.getByLabel('确认方向（可选）').fill('Java');await page.getByLabel('确认城市（可选，多个用顿号分隔）').fill(live?'深圳':'上海');
 await page.getByLabel('城市要求').selectOption('required');await page.getByLabel('职位说明 JD').fill('合成JD：Java接口与SQL。');
 await page.getByRole('button',{name:'保存求职目标',exact:true}).click();
 await page.waitForURL(u=>u.searchParams.has('goal'));
 const goal=new URL(page.url()).searchParams.get('goal')!;
 let panel=page.getByRole('region',{name:'岗位来源与列表'});
 const worker=(scenario='ok')=>{const args=['-X','utf8','scripts/research-web-support.py','--goal',goal,'--run-id',run,'--scenario',scenario,...(live?['--source-live']:[])];const text=execFileSync(python,args,{cwd:join(import.meta.dirname,'../../..'),encoding:'utf-8',timeout:115000,windowsHide:true});writeFileSync(join(output,`worker-${cases.length}.json`),text);return JSON.parse(text);};
 await panel.getByLabel('查询关键词',{exact:true}).fill(live?'后台':'合成');
 let submits=0;page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/research'))submits++;});
 await panel.getByRole('button',{name:'查询岗位',exact:true}).dblclick();await panel.getByRole('status').filter({hasText:'等待读取'}).waitFor();assert.equal(submits,1);
 const result=worker();assert.equal(result.status,'SUCCEEDED');
 await panel.getByRole('status').filter({hasText:'读取完成'}).waitFor({timeout:10000});
 await panel.locator('.job-card').first().waitFor();assert.equal(result.result.origin,live?'source_live':'offline_fixture');
 assert.ok((await panel.locator('.job-card').count())>0);
 cases.push('double-click creates one durable query; real PG/Redis worker commits verified detail snapshots with explicit source provenance');
 if(!live){assert.equal(await panel.locator('.job-card').count(),1);await panel.getByText('查看与硬性城市要求冲突的岗位').click();assert.ok((await panel.innerText()).includes('北京市'));}
 cases.push('multi-city detail retained, hard-city conflicts explained and availability stays unknown');
 await page.reload();panel=page.getByRole('region',{name:'岗位来源与列表'});await panel.locator('.job-card').first().waitFor();assert.equal(submits,1);
 await panel.getByText(`本次关键词：${live?'后台':'合成'}`,{exact:true}).waitFor();
 cases.push('refresh restores committed source result without launching another query');
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 await page.screenshot({path:join(output,'jobs-mobile.png'),fullPage:true});await page.setViewportSize({width:1280,height:900});
 cases.push('390px source list has no horizontal overflow');
 if(!live){
  await panel.getByRole('button',{name:'导入已保存的JD'}).click();await panel.getByRole('status').filter({hasText:'等待读取'}).waitFor();assert.equal(worker().result.origin,'user_provided');
  await panel.getByText('用户提供，未官网核验').first().waitFor();await panel.getByText('查看岗位正文与要求').click();await panel.getByText('合成JD：Java接口与SQL。',{exact:true}).waitFor();
  cases.push('saved JD imports without external requests and remains user-provided rather than officially verified');
  const other=await browser.newContext(),p2=await other.newPage();await register(p2,'other');const response=await p2.request.get(`http://127.0.0.1:5174/api/career-goals/${goal}/jobs`);assert.equal(response.status(),404);await other.close();
  cases.push('another authenticated account cannot read goal job snapshots');
  await page.getByLabel('求职意向或补充说明').fill('改为北京。');await page.getByLabel('确认城市（可选，多个用顿号分隔）').fill('北京');await page.getByRole('button',{name:'保存补充与确认条件'}).click();
  await page.getByText('已确认方向：Java；城市：北京',{exact:false}).waitFor();panel=page.getByRole('region',{name:'岗位来源与列表'});
  await panel.getByText('查询历史').waitFor();assert.equal(await panel.locator('.job-card').count(),0);
  cases.push('new goal version hides previous job observations while retaining history metadata');
  await panel.getByLabel('查询关键词',{exact:true}).fill('合成');await panel.getByRole('button',{name:'查询岗位',exact:true}).click();await panel.getByRole('status').filter({hasText:'等待读取'}).waitFor();assert.equal(worker('rate_limited').status,'FAILED');
  await panel.getByRole('status').filter({hasText:'读取未完成'}).waitFor();await panel.getByText(/rate_limited/).waitFor();assert.equal(await panel.locator('.job-card').count(),0);
  cases.push('source 429 is a failed read with a JD fallback, not a successful empty list');
  await panel.getByRole('button',{name:'查询岗位',exact:true}).click();await panel.getByRole('status').filter({hasText:'等待读取'}).waitFor();await page.getByRole('button',{name:'结束此目标'}).click();assert.equal(worker().status,'CANCELLED');
  cases.push('cancelling a goal prevents a pending source read from committing');
 }
 await page.screenshot({path:join(output,'jobs-desktop.png'),fullPage:true});assert.deepEqual(errors,[]);
 writeFileSync(join(output,'receipt.json'),JSON.stringify({stage:'JA-02',status:'passed',sourceLive:live,cases,pageErrors:errors,modelCalls:0,realBrowser:true},null,2));
 console.log(JSON.stringify({status:'passed',cases:cases.length,sourceLive:live}));
}finally{await browser.close();}

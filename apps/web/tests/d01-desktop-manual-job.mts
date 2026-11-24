import {waitGoalReady} from './desktop-nav.mts';
// F07-E acceptance: a job the user types in themselves.
//
//   F07E_OUTPUT=<new dir> node --experimental-strip-types apps/web/tests/d01-desktop-manual-job.mts
//
// Why this suite exists: the two built-in sources cover two companies, so any
// other employer was unreachable and the track/apply chain was blocked behind
// them. This checks the escape hatch end to end against the real API + Postgres:
//   1. The form starts folded, and its save button stays disabled until the
//      required fields are filled - no empty job can be submitted.
//   2. Saving stores a real job row and renders it as a card, marked as
//      user-provided and not verified against any career site.
//   3. That job can be selected, which is what unblocks applications/tracking.
//   4. Saving the same job twice does not create a second row.
//   5. An http:// link is refused rather than silently stored.
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.F07E_OUTPUT??join(root,'docs/records/desktop-handoff/F07E-manual-job'));
const base=process.env.F07E_BASE??'http://127.0.0.1:5175';
const edge=process.env.F07E_EDGE??'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;type:'check';detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,detail?:unknown)=>{cases.push({name,type:'check',detail});console.log('CHECK',name);};
const shots:string[]=[];
const evidence:Record<string,unknown>={run:new Date().toISOString(),base,cases,page_errors:pageErrors,screenshots:shots,
 case_type_note:'真实浏览器 + 真实 API + PostgreSQL；岗位由脚本逐字输入，不调用任何模型，也不访问招聘网站。'};

let browser:Browser|null=null;
try{
 browser=await chromium.launch({executablePath:edge,headless:true});
 const ctx=await browser.newContext({viewport:{width:1440,height:1000}});
 const page:Page=await ctx.newPage();
 page.on('pageerror',error=>pageErrors.push(String(error)));

 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.evaluate(async()=>{
  await fetch('/api/auth/sign-up/email',{method:'POST',credentials:'include',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({email:`f07e-${Date.now()}@example.com`,name:'自建岗位验收',password:`Synthetic-${Date.now()}-local-only`})});
 });
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'networkidle'});
 await page.locator('textarea[aria-label="求职意向或补充说明"]').waitFor({timeout:60000});
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('研二，想在上海找 AI 应用开发实习。');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);

 // ---------------------------------------------------------- 1. folded, guarded
 await page.locator('.desktop-rail button[title="岗位研究"]').click();
 const form=page.locator('[data-testid="manual-job-form"]');
 await form.waitFor({timeout:20000});
 assert.equal(await form.evaluate(node=>(node as HTMLDetailsElement).open),false,'manual_form_should_start_folded');
 assert.equal(await page.locator('[data-testid="manual-job-save"]').isDisabled(),true,'save_enabled_with_empty_fields');
 mark('自建岗位表单默认折叠；公司/职位/JD 未填时保存按钮不可用');

 // ---------------------------------------------------------- 2. save a real job
 const openForm=async()=>{
  if(!(await form.evaluate(node=>(node as HTMLDetailsElement).open)))await form.locator('summary').click();
 };
 await openForm();
 await page.locator('[aria-label="自建岗位公司"]').fill('字节跳动');
 await page.locator('[aria-label="自建岗位职位"]').fill('AI 应用开发实习生');
 await page.locator('[aria-label="自建岗位城市"]').fill('上海');
 await page.locator('[aria-label="自建岗位链接"]').fill('https://jobs.example.com/123');
 const jd='岗位职责：负责大模型应用的产品化落地，搭建 RAG 与 Agent 流程。\n任职要求：2026 届本科及以上，每周到岗 4 天，熟悉 Python 与 SQL。';
 await page.locator('[aria-label="自建岗位JD"]').fill(jd);
 await page.locator('[data-testid="manual-job-save"]').click();
 const card=page.locator('.job-card').filter({hasText:'AI 应用开发实习生'});
 await card.waitFor({timeout:30000});
 const cardText=(await card.innerText()).replace(/\s+/g,' ');
 assert.match(cardText,/字节跳动/,'company_not_rendered');
 assert.match(cardText,/上海/,'city_not_rendered');
 assert.match(cardText,/用户提供，未官网核验/,'user_provided_provenance_missing');
 assert.match(cardText,/在招状态待核验/,'availability_not_flagged');
 assert.match(cardText,/2026 届/,'jd_text_not_stored');
 mark('保存后出现真实岗位卡片：公司/职位/城市来自输入，来源标注为用户提供且未官网核验',{card:cardText.slice(0,160)});
 await card.scrollIntoViewIfNeeded();
 await page.screenshot({path:join(output,'shots','01-manual-job-saved.png')});
 shots.push('01-manual-job-saved.png');

 // ---------------------------------------------------------- 3. it must be selectable
 const ack=card.locator('.check-label input[type="checkbox"]');
 if(await ack.count())await ack.check();
 await card.locator('button',{hasText:'选择该岗位'}).click();
 await page.locator('.notice',{hasText:'已选择岗位用于后续准备'}).first().waitFor({timeout:20000});
 mark('自建岗位可以被选中，后续投递/跟踪不再被"必须先有可抓取岗位"卡住');
 await page.screenshot({path:join(output,'shots','02-manual-job-selected.png')});
 shots.push('02-manual-job-selected.png');

 // ---------------------------------------------------------- 4. same job twice = one row
 const before=await page.locator('.job-card').count();
 await openForm();
 await page.locator('[aria-label="自建岗位公司"]').fill('字节跳动');
 await page.locator('[aria-label="自建岗位职位"]').fill('AI 应用开发实习生');
 await page.locator('[aria-label="自建岗位城市"]').fill('上海');
 await page.locator('[aria-label="自建岗位JD"]').fill(jd);
 await page.locator('[data-testid="manual-job-save"]').click();
 await page.waitForTimeout(2500);
 const after=await page.locator('.job-card').count();
 const duplicates=await page.locator('.job-card').filter({hasText:'AI 应用开发实习生'}).count();
 assert.equal(after,before,'saving_the_same_job_twice_created_another_row');
 assert.equal(duplicates,1,'duplicate_card_rendered');
 mark('同一岗位重复保存不会新增第二条记录（唯一键去重，不报错也不重复渲染）',{cards_before:before,cards_after:after});

 // ---------------------------------------------------------- 5. http:// refused
 await page.locator('[aria-label="自建岗位公司"]').fill('某公司');
 await page.locator('[aria-label="自建岗位职位"]').fill('后端实习');
 await page.locator('[aria-label="自建岗位链接"]').fill('http://insecure.example/job');
 await page.locator('[aria-label="自建岗位JD"]').fill('职责：写接口。要求：熟悉 Java。');
 await page.locator('[data-testid="manual-job-save"]').click();
 await page.locator('.error').first().waitFor({timeout:20000});
 const afterBad=await page.locator('.job-card').count();
 assert.equal(afterBad,after,'rejected_job_still_rendered');
 mark('非 https 链接被拒绝并给出错误，未写入任何岗位记录',{cards_after_reject:afterBad});

 const result={result:pageErrors.length===0?'passed':'failed',cases:cases.length,page_errors:pageErrors,...evidence};
 writeFileSync(join(output,'result.json'),JSON.stringify(result,null,2));
 console.log(`\n${cases.length} cases, ${pageErrors.length} page errors -> ${result.result}`);
}catch(error){
 const failed=error instanceof Error?error.message:String(error);
 (evidence as Record<string,unknown>).error=failed;
 writeFileSync(join(output,'result.json'),JSON.stringify({result:'failed',cases:cases.length,page_errors:pageErrors,...evidence},null,2));
 console.error('FAILED:',failed);
 process.exitCode=1;
}finally{
 try{await browser?.close();}catch{}
}

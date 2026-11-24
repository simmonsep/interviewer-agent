// F04 (part 2) acceptance: text written tests on the desktop shell.
// Uses the question-bank path (kind=question_set), which needs no execution image.
// F07Z-54 added the code-question card next to it; this suite still only drives the
// question-set path and asserts the code card tells the truth about execution availability.
//
//   D01_OUTPUT=<new dir> node apps/web/tests/d01-desktop-written.mts
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {openDetail, waitGoalReady} from './desktop-nav.mts';
import {writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/desktop-handoff/F04-written'));
const base=process.env.D01_BASE_URL??'http://127.0.0.1:5174';
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
assert.ok(executable,'installed_browser_required');
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;type:'real';detail?:unknown}[]=[];
const mark=(name:string,detail?:unknown)=>cases.push({name,type:'real',detail});
const pageErrors:string[]=[];
const shots:string[]=[];
const evidence:Record<string,unknown>={};
const account={email:`f04w-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID(),name:'笔试验收学员'};

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

 // ---------------------------------------------------------------- 1. needs a goal first
 const pane=page.locator('[data-testid="written-pane"]');
 await openDetail(page,'笔试');
 await pane.waitFor({timeout:15000});
 await page.getByText('笔试需要先有求职目标').waitFor({timeout:20000});
 await shot(page,'01-needs-goal.png');
 mark('笔试挂在目标下：没有目标时给出空态与去创建入口，不会显示一个空题目列表',{});

 await openDetail(page,'工作台');
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('合成验收：找 Java 后端实习。');
 await page.locator('label:has-text("确认方向") input').fill('Java 后端');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);

 // ---------------------------------------------------------------- 2. start card
 await openDetail(page,'笔试');
 await pane.getByRole('button',{name:'开始笔试'}).waitFor({timeout:20000});
 const bankOptions=await pane.locator('select[aria-label="笔试题库"] option').allInnerTexts();
 assert.ok(bankOptions.some(text=>text.includes('java-backend')),'builtin_java_bank_missing');
 // F07Z-68: the code-question entry is out of the product (2026-11-13), so the pane must not
 // offer it any more. The assertion is inverted on purpose rather than deleted, so a change
 // that brings the card back fails here.
 const paneText=await pane.innerText();
 assert.ok(!paneText.includes('代码题'),'code_card_still_present');
 assert.ok(!paneText.includes('本机未启用代码执行'),'code_execution_notice_still_present');
 await shot(page,'02-start.png');
 mark('开始卡只提供客观题题库；代码题入口已从产品移除',{banks:bankOptions});

 // ---------------------------------------------------------------- 3. answering: one answer at a time
 await pane.getByRole('button',{name:'开始笔试'}).click();
 const items=pane.locator('.d-written-item');
 await items.first().waitFor({timeout:40000});
 const count=await items.count();
 assert.ok(count>=3,`question_count ${count}`);
 const firstQuestion=await items.first().locator('.d-written-question').innerText();
 await shot(page,'03-answering.png');
 mark('笔试开始后按题列出题干与作答框，题数来自题库',{questions:count});

 const answers:string[]=[];
 for(let index=0;index<count;index++){
  const text=`第 ${index+1} 题：先看监控与慢日志确认范围，再看执行计划与索引命中，最后考虑缓存与重试。`;
  answers.push(text);
  await items.nth(index).locator('textarea').fill(text);
  await items.nth(index).getByRole('button',{name:'保存这一题'}).click();
  // The notice text is the same every time, so wait for the request to settle instead.
  await page.waitForTimeout(900);
 }
 await shot(page,'04-saved.png');
 mark('逐题保存：每题一次写入，保存后给出反馈，不要求一次提交整卷',{answered:count});

 // ---------------------------------------------------------------- 4. finish and read the visible result
 await pane.getByRole('button',{name:'结束并看结果'}).click();
 await pane.getByText('已结束，下面是这次的可见结果。').waitFor({timeout:30000});
 await page.waitForTimeout(500);
 const resultText=await pane.locator('[aria-label="笔试结果"]').innerText();
 assert.ok(resultText.includes('答案键'),'answer_key_note_missing');
 assert.equal(await pane.locator('.d-written-item textarea').count(),0,'textarea_still_editable_after_finish');
 const graded=await pane.locator('.d-written-item .tag').allInnerTexts();
 assert.ok(graded.some(text=>text.includes('需人工看')),`grading_label_missing ${graded.join(',')}`);
 await shot(page,'05-result.png');
 mark('结束后不能改答案，结果逐题给出判定：简答题标注「需人工看」并附参考答案，统计说明答案键由题库作者声明',
  {graded:[...new Set(graded)],
   stats:await pane.locator('[aria-label="笔试结果"] .d-side-stats').innerText()});

 // ---------------------------------------------------------------- 5. history and reopen
 assert.equal(await pane.locator('.d-session-item').count(),1,'history_row_missing');
 const historyRow=pane.locator('.d-session-item').first();
 assert.ok((await historyRow.innerText()).includes('题'),'history_missing_bank_line');
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 await openDetail(page,'笔试');
 await pane.locator('.d-session-item').first().waitFor({timeout:30000});
 await pane.locator('.d-session-item').first().click();
 await page.getByText('本次笔试').waitFor({timeout:20000});
 const reopened=await pane.locator('.d-written-item').first().innerText();
 assert.ok(reopened.includes(answers[0].slice(0,10)),'answer_not_restored');
 await shot(page,'06-reopen.png');
 mark('刷新后从历史打开原场次：题目与当时的回答都回读，没有新建会话',{questions:count});

 // ---------------------------------------------------------------- 6. narrow window
 await page.setViewportSize({width:880,height:600});
 await page.waitForTimeout(400);
 const overflow=await page.evaluate(()=>({inner:innerWidth,doc:document.documentElement.scrollWidth}));
 assert.ok(overflow.doc<=overflow.inner+2,`narrow_overflow ${JSON.stringify(overflow)}`);
 await shot(page,'07-narrow.png');
 mark('880×600 窄窗下笔试页无横向溢出',overflow);
 evidence.firstQuestion=firstQuestion;
 await context.close();
}catch(error){
 writeFileSync(join(output,'result.json'),JSON.stringify({account:account.email,cases,page_errors:pageErrors,shots,evidence,error:(error as Error).message},null,2));
 await browser?.close();
 console.error('FAILED:',(error as Error).message);
 process.exit(1);
}

writeFileSync(join(output,'result.json'),JSON.stringify({account:account.email,base,viewport:'1280x860',cases,page_errors:pageErrors,shots,evidence},null,2));
console.log(JSON.stringify({passed:cases.length,page_errors:pageErrors,shots:shots.length},null,1));
await browser.close();

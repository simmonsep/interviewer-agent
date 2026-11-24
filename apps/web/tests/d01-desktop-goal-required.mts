import {waitGoalReady} from './desktop-nav.mts';
// F07Z-20 acceptance: a pane that needs a goal must offer the goals that already exist.
//
// Before the fix, the desktop shell showed "需要先有求职目标 / 去创建工作目标" even when the
// account already had goals, because the only selection paths were `?goal=` in the URL and
// the shell's remembered last goal. A user who created a goal in the web shell (or simply
// cleared the remembered one) had no way in.
//
//   node apps/web/tests/d01-desktop-goal-required.mts
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'.private/f07x/e2e'));
const base=process.env.D01_BASE_URL??'http://127.0.0.1:5174';
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
assert.ok(executable,'installed_browser_required');
mkdirSync(output,{recursive:true});

const runId=randomUUID();
const account={email:`goal-required-${runId}@example.com`,password:'Synthetic-password-'+randomUUID(),name:'目标入口验收'};
const evidence:Record<string,unknown>={};
let browser:Browser;

try{
 browser=await chromium.launch({executablePath:executable,headless:true});
 const context=await browser.newContext({viewport:{width:1280,height:900}});
 const page=await context.newPage();
 const pageErrors:string[]=[];
 page.on('pageerror',error=>pageErrors.push(error.message));
 const shot=async(name:string)=>{await page.screenshot({path:join(output,name),fullPage:true});};

 await page.goto(base+'/?shell=desktop',{waitUntil:'domcontentloaded'});
 // The desktop shell signs itself in with a local identity (App.tsx desktop entry), so a fresh
 // context is already an account with zero goals - no sign-up form involved.
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:40000});
 evidence.autoLogin=true;

 // one real goal through the real workbench form
 await page.getByRole('button',{name:'工作台'}).click();
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('合成验收：找 Java 后端实习。');
 await page.locator('label:has-text("确认方向") input').fill('Java 后端');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);

 // forget which goal was open, exactly like a first visit on a machine that has goals
 await page.evaluate(()=>{for(const key of Object.keys(localStorage))if(key.startsWith('py-desktop-goal'))localStorage.removeItem(key);});
 await page.goto(base+'/?shell=desktop&fresh='+randomUUID(),{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 evidence.urlAfterReload=page.url();
 evidence.rememberedKeys=await page.evaluate(()=>Object.keys(localStorage).filter(k=>k.includes('goal')));

 await page.getByRole('button',{name:/材料与简历/}).click();
 const pane=page.locator('[data-testid="materials-pane"]');
 await pane.waitFor({timeout:15000});

 // the fix: the pane names the goals that exist and offers them
 const list=pane.getByRole('list',{name:'选择已有求职目标'});
 try{
  await list.waitFor({timeout:20000});
 }catch(error){
  evidence.paneText=(await pane.innerText()).slice(0,800);
  evidence.urlAtPane=page.url();
  evidence.goalsInStorage=await page.evaluate(()=>Object.keys(localStorage).filter(k=>k.includes('goal')));
  await shot('goal-required-missing-list.png');
  writeFileSync(join(output,'goal-required-failed.json'),JSON.stringify({account:account.email,cases:evidence},null,2));
  throw error;
 }
 const options=list.getByRole('listitem');
 const count=await options.count();
 assert.ok(count>=1,'no_existing_goal_offered');
 evidence.offeredGoals=count;
 evidence.emptyStateText=await pane.locator('h1').innerText();
 await shot('goal-required-offer.png');

 // picking one must open the real materials panel
 await options.first().click();
 await pane.getByRole('heading',{name:/材料与简历|分析与初稿|草稿/}).first().waitFor({timeout:30000});
 evidence.openedPane=true;
 evidence.urlAfterChoose=page.url();
 await shot('goal-required-opened.png');

 assert.equal(pageErrors.length,0,'page_errors:'+pageErrors.join('|'));
 evidence.pageErrors=pageErrors;
 writeFileSync(join(output,'goal-required.json'),JSON.stringify({account:account.email,cases:evidence},null,2));
 console.log(JSON.stringify({ok:true,evidence},null,2));
}finally{
 await browser?.close();
}

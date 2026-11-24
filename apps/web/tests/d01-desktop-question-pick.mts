// F07Z-89 acceptance: the question bank starts a session with exactly the questions you picked,
// and the written pane opens that session.
//
//   D01_PICK_BASE=http://127.0.0.1:5174 D01_PICK_GOAL=<uuid> \
//     node --experimental-strip-types apps/web/tests/d01-desktop-question-pick.mts
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {randomUUID} from 'node:crypto';

const out=resolve(process.env.D01_PICK_OUT??join(resolve(import.meta.dirname,'../../..'),'.private/f07z89'));
mkdirSync(out,{recursive:true});
const base=process.env.D01_PICK_BASE??'http://127.0.0.1:5174';
const api=process.env.D01_PICK_API??'http://127.0.0.1:3100';
const goal=process.env.D01_PICK_GOAL??'';
const email=process.env.D01_PICK_EMAIL??'budget-1794568957@example.com';
const password=process.env.D01_PICK_PASSWORD??'Synthetic-9f3a2b7c1d';
assert.ok(goal,'D01_PICK_GOAL is required');
const exe=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(p=>existsSync(p));
assert.ok(exe,'installed_browser_required');

const login=await fetch(`${api}/api/auth/sign-in/email`,{
 method:'POST',headers:{'Content-Type':'application/json',Origin:api},
 body:JSON.stringify({email,password})});
assert.ok(login.ok,`login ${login.status}`);
const session=login.headers.get('set-cookie')!;
const cookie=session.split(';')[0];

// An open session blocks a new one; end leftovers the way the product does.
async function cancelActive(){
 const listed=await (await fetch(`${api}/api/career-goals/${goal}/written`,{headers:{cookie}})).json() as any;
 for(const item of listed.items??[]){
  if(item.status!=='ACTIVE')continue;
  const current=await (await fetch(`${api}/api/career-goals/${goal}/written/${item.id}`,{headers:{cookie}})).json() as any;
  await fetch(`${api}/api/career-goals/${goal}/written/${item.id}/cancel`,{method:'POST',
   headers:{cookie,Origin:api,'Content-Type':'application/json','Idempotency-Key':randomUUID()},
   body:JSON.stringify({expected_version:current.version})});
 }
}
await cancelActive();

const browser=await chromium.launch({executablePath:exe,headless:true});
const context=await browser.newContext({viewport:{width:1280,height:1000}});
await context.addCookies([{name:cookie.slice(0,cookie.indexOf('=')),
 value:cookie.slice(cookie.indexOf('=')+1),domain:'127.0.0.1',path:'/',httpOnly:true,sameSite:'Lax'}]);
const page=await context.newPage();
const errors:string[]=[];
page.on('pageerror',e=>errors.push(String(e)));
await page.goto(`${base}/?shell=desktop&goal=${goal}`,{waitUntil:'domcontentloaded'});
await page.locator('.desktop-shell[data-pane]').waitFor({timeout:40000});
await page.locator('.d-nav-item').filter({hasText:'题库'}).first().click();
await page.locator('[data-testid="learning-resources"]').waitFor({timeout:30000});

// 1. Open the picker on the first bank and list its questions.
const card=page.locator('.d-bank').first();
const bankName=(await card.locator('strong').first().innerText()).trim();
await card.getByRole('button',{name:'选题'}).click();
await page.locator('[data-testid="bank-picker"]').waitFor({timeout:20000});
const items=page.locator('.d-pick-item');
await items.first().waitFor({timeout:20000});
const available=await items.count();
assert.ok(available>=2,`bank ${bankName} exposes ${available} items`);

// 2. Pick two. The row's second line ends with the item id, which is what the created session can
//    be checked against - the visible question text is longer prose and is not repeated verbatim.
const meta=await Promise.all([0,1].map(async index=>{
 const text=(await items.nth(index).locator('small').innerText()).trim();
 return {id:text.split('·').pop()!.trim(),title:(await items.nth(index).locator('strong').innerText()).trim()};
}));
const pickedIds=meta.map(item=>item.id);
await items.nth(0).locator('input').check();
await items.nth(1).locator('input').check();
await page.getByRole('button',{name:'用选中的题开始笔试'}).click();

// 3. The written pane opens that session.
await page.locator('.desktop-shell[data-pane="written"]').waitFor({timeout:40000});
const shown=page.locator('.d-written-item .d-written-question');
await shown.first().waitFor({timeout:40000});
const shownCount=await shown.count();
await page.screenshot({path:join(out,'picked-session.png')});

// 4. The session holds exactly those items, in the order they were ticked.
const listed=await (await fetch(`${api}/api/career-goals/${goal}/written`,{headers:{cookie}})).json() as any;
const active=(listed.items??[]).find((item:any)=>item.status==='ACTIVE');
assert.ok(active,'no session was created');
const detail=await (await fetch(`${api}/api/career-goals/${goal}/written/${active.id}`,{headers:{cookie}})).json() as any;
const sessionIds=(detail.questions??[]).map((q:any)=>q.bank_item.item_id);

const result={bankName,available,picked:meta.map(item=>item.title),pickedIds,sessionIds,shownCount,
 selectionPolicy:detail.selection_basis?.policy,pageErrors:errors.slice(0,3)};
writeFileSync(join(out,'picker.json'),JSON.stringify(result,null,1));
console.log(JSON.stringify(result,null,1));

assert.equal(shownCount,2,'the session must show exactly the two picked questions');
assert.deepEqual(sessionIds,pickedIds,'the session holds exactly the picked items, in order');
assert.equal(detail.selection_basis?.policy,'explicit_items_in_user_order');
assert.equal(errors.length,0,`page errors: ${errors.slice(0,2).join(' | ')}`);

// Leave the account as it was: no session stuck in progress.
await cancelActive();
console.log('RESULT passed: picked questions start a session and open in the written pane');
await context.close();
await browser.close();

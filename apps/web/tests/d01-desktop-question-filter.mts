// F07Z-91 acceptance: the bank picker can build a set from a question kind, at random, and start
// that session in the written pane.
//
//   D01_FILTER_BASE=http://127.0.0.1:3100 D01_FILTER_API=http://127.0.0.1:3100 \
//     D01_FILTER_GOAL=<uuid> node --experimental-strip-types apps/web/tests/d01-desktop-question-filter.mts
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {randomUUID} from 'node:crypto';

const out=resolve(process.env.D01_FILTER_OUT??join(resolve(import.meta.dirname,'../../..'),'.private/f07z91'));
mkdirSync(out,{recursive:true});
const base=process.env.D01_FILTER_BASE??'http://127.0.0.1:3100';
const api=process.env.D01_FILTER_API??'http://127.0.0.1:3100';
const goal=process.env.D01_FILTER_GOAL??'';
const email=process.env.D01_FILTER_EMAIL??'budget-1794568957@example.com';
const password=process.env.D01_FILTER_PASSWORD??'Synthetic-9f3a2b7c1d';
assert.ok(goal,'D01_FILTER_GOAL is required');
const exe=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(p=>existsSync(p));
assert.ok(exe,'installed_browser_required');

const login=await fetch(`${api}/api/auth/sign-in/email`,{
 method:'POST',headers:{'Content-Type':'application/json',Origin:api},
 body:JSON.stringify({email,password})});
assert.ok(login.ok,`login ${login.status}`);
const raw=login.headers.get('set-cookie')!;
const cookie=raw.split(';')[0];

// The account has a per-goal session cap; report it instead of failing halfway through.
const before=await (await fetch(`${api}/api/career-goals/${goal}/written`,{headers:{cookie}})).json() as any;
assert.ok((before.items??[]).length<20,`goal already has ${(before.items??[]).length} written sessions`);

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

// 1. Find a bank with enough items to draw from (the aptitude bank has 20 single-choice items).
const cards=page.locator('.d-bank');
const cardCount=await cards.count();
let bankName='',available=0;
for(let index=0;index<cardCount;index++){
 const card=cards.nth(index);
 await card.getByRole('button',{name:'选题'}).click();
 await page.waitForTimeout(900);
 const items=page.locator('.d-pick-item');
 if(await items.count()>=10){
  bankName=(await card.locator('strong').first().innerText()).trim();
  available=await items.count();
  break;
 }
 const open=card.getByRole('button',{name:'选题'});
 if(await open.getAttribute('aria-expanded')==='true')await open.click();
 await page.waitForTimeout(300);
}
assert.ok(available>=10,`no bank with at least 10 items (checked ${cardCount})`);

// 2. Filter to single choice, ask for three, draw.
const chips=page.locator('.d-kind');
assert.ok(await chips.count()>=1,'the picker shows no kind filter');
const single=page.locator('.d-kind').filter({hasText:'单选'}).first();
await single.locator('input').check();
const count=page.locator('input[aria-label="抽取题数"]');
await count.fill('3');
const poolText=(await page.locator('.d-pick-auto p.fine-print').innerText()).trim();
await page.getByTestId('auto-pick').click();

// 3. The written pane opens the new session.
await page.locator('.desktop-shell[data-pane="written"]').waitFor({timeout:40000});
const shown=page.locator('.d-written-item .d-written-question');
await shown.first().waitFor({timeout:40000});
const shownCount=await shown.count();
await page.screenshot({path:join(out,'auto-picked-session.png')});

// 4. The session holds three distinct single-choice items and records how they were drawn.
const listed=await (await fetch(`${api}/api/career-goals/${goal}/written`,{headers:{cookie}})).json() as any;
const active=(listed.items??[]).find((item:any)=>item.status==='ACTIVE');
assert.ok(active,'no session was created');
const detail=await (await fetch(`${api}/api/career-goals/${goal}/written/${active.id}`,{headers:{cookie}})).json() as any;
const sessionIds=(detail.questions??[]).map((q:any)=>q.bank_item.item_id);
const sessionKinds=(detail.questions??[]).map((q:any)=>q.kind);
const basis=detail.selection_basis??{};

const result={bankName,available,poolText,shownCount,sessionIds,sessionKinds,
 policy:basis.policy,itemKinds:basis.item_kinds,seed:basis.seed,availableQuestions:basis.available_questions,
 pageErrors:errors.slice(0,3)};
writeFileSync(join(out,'filter.json'),JSON.stringify(result,null,1));
console.log(JSON.stringify(result,null,1));

assert.equal(shownCount,3,'the session shows the three drawn questions');
assert.equal(sessionIds.length,3);
assert.equal(new Set(sessionIds).size,3,'a random draw must not repeat a question');
assert.deepEqual(sessionKinds,['single_choice','single_choice','single_choice']);
assert.equal(basis.policy,'filtered_random_items_v1');
assert.deepEqual(basis.item_kinds,['single_choice']);
assert.match(String(basis.seed),/^[0-9a-f]{32}$/,'the draw seed is recorded');
assert.equal(basis.available_questions,available);
assert.equal(errors.length,0,`page errors: ${errors.slice(0,2).join(' | ')}`);

await cancelActive();
console.log('RESULT passed: kind filter + random draw start a session with the picked kind');
await context.close();
await browser.close();

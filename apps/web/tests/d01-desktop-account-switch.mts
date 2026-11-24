// F07Z-77 acceptance: on one machine, switching accounts must not leave the previous account's
// work in the window, and the server must refuse the previous account's goal.
//
//   D01_SWITCH_BASE=http://127.0.0.1:5174 D01_SWITCH_API=http://127.0.0.1:3100 \
//     node --experimental-strip-types apps/web/tests/d01-desktop-account-switch.mts
//
// What it pins:
//   1. "切换账号" clears the previous account's account-scoped sessionStorage keys (the pending
//      command and the interview draft used to survive the switch).
//   2. The rail summary names the account that is open, so the two data sets cannot be confused.
//   3. The server boundary is unchanged: another account reading the first account's goal is 404.
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..');
const out=resolve(process.env.D01_SWITCH_OUT??join(root,'.private/f07z77'));
mkdirSync(out,{recursive:true});
const ui=process.env.D01_SWITCH_BASE??'http://127.0.0.1:5174';
const api=process.env.D01_SWITCH_API??'http://127.0.0.1:3100';
const exe=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(p=>existsSync(p));
assert.ok(exe,'installed_browser_required');

async function signIn(email:string,password:string){
 const r=await fetch(`${api}/api/auth/sign-in/email`,{method:'POST',
  headers:{'Content-Type':'application/json','Origin':api},body:JSON.stringify({email,password})});
 assert.ok(r.ok,`sign-in ${email} ${r.status}`);
 return r.headers.get('set-cookie').split(';')[0];
}

const browser=await chromium.launch({executablePath:exe,headless:true});
const context=await browser.newContext({viewport:{width:1280,height:900}});
const page=await context.newPage();
const errors:string[]=[];
page.on('pageerror',e=>errors.push(String(e)));

await page.goto(`${ui}/?shell=desktop`,{waitUntil:'domcontentloaded'});
await page.locator('.desktop-shell[data-pane]').waitFor({timeout:40000});

const identity=await page.evaluate(()=>{
 const raw=localStorage.getItem('py-local-identity');
 return raw?JSON.parse(raw):null;
});
assert.ok(identity?.email,'local identity missing');

const cookieA=await signIn(identity.email,identity.password);
const userA=(await (await fetch(`${api}/api/auth/get-session`,{headers:{cookie:cookieA}})).json()).user.id;

const goalA=await (await fetch(`${api}/api/career-goals/`,{method:'POST',
 headers:{'Content-Type':'application/json','Origin':api,cookie:cookieA,'Idempotency-Key':randomUUID()},
 body:JSON.stringify({message:`账号隔离验收 ${randomUUID().slice(0,6)}`,jd_text:'',
  conditions:{direction:'Java后端',cities:[],city_policy:'unknown',auto_research:false,
   recruitment:'unknown',graduation_year:null,days_per_week:null,duration_months:null,skills:[]}})})).json();
assert.ok(goalA.id,`goal not created: ${JSON.stringify(goalA)}`);

// Leave the traces the UI leaves for account A.
await page.evaluate(({goalId,userId})=>{
 localStorage.setItem(`py-desktop-goal:${userId}`,goalId);
 sessionStorage.setItem(`py-career:${userId}:command`,JSON.stringify({kind:'create',key:'pending',url:'/x',body:{}}));
 sessionStorage.setItem(`py-interview-draft:${userId}:s:q`,'这是账号 A 的草稿正文');
},{goalId:goalA.id,userId:userA});

// A second real account, then switch through the product's own menu and login form.
const emailB=`switch-${randomUUID().slice(0,8)}@example.com`;
const passwordB='Synthetic-9f3a2b7c1d';
assert.ok((await fetch(`${api}/api/auth/sign-up/email`,{method:'POST',
 headers:{'Content-Type':'application/json','Origin':api},
 body:JSON.stringify({name:'账号 B',email:emailB,password:passwordB})})).ok,'sign-up failed');

await page.locator('.d-account summary').click();
await page.getByRole('button',{name:'切换账号'}).click();
await page.locator('.desktop-login').waitFor({timeout:20000});
await page.locator('input[type="email"]').fill(emailB);
await page.locator('input[type="password"]').fill(passwordB);
await page.getByRole('button',{name:'登录',exact:true}).click();
await page.locator('.desktop-login').waitFor({state:'detached',timeout:30000});
await page.waitForTimeout(1200);

const after=await page.evaluate(()=>({
 session:Object.keys(sessionStorage),
 local:Object.keys(localStorage),
 label:(document.querySelector('[data-testid="account-summary"]')?.textContent??'').trim(),
}));
const userB=await page.evaluate(async()=>{
 const r=await fetch('/api/auth/get-session',{credentials:'same-origin'});
 return (await r.json())?.user?.id??null;
});
const crossRead=await page.evaluate(async(goalId)=>{
 const r=await fetch(`/api/career-goals/${goalId}`,{credentials:'same-origin'});
 return r.status;
},goalA.id);

await page.screenshot({path:join(out,'after-switch.png')});
writeFileSync(join(out,'switch.json'),JSON.stringify({
 identity_email:identity.email,goalA:goalA.id,userA,userB,before_and_after:after,crossRead,page_errors:errors},null,1));
console.log(JSON.stringify({
 userA,userB,label:after.label,
 left_over_for_a:after.session.filter(k=>k.includes(userA)),
 cross_account_goal_status:crossRead,page_errors:errors.length},null,1));

assert.notEqual(userB,userA,'account did not change');
assert.deepEqual(after.session.filter(k=>k.includes(userA)),[],
 'the previous account\'s window state survived the switch');
assert.ok(after.label.includes(emailB.split('@')[0].slice(0,12)),
 `the rail does not name the open account: ${after.label}`);
assert.equal(crossRead,404,'another account could read the goal');
assert.equal(errors.length,0,`page_errors ${JSON.stringify(errors.slice(0,3))}`);
console.log('RESULT passed account switch isolation');
await browser.close();

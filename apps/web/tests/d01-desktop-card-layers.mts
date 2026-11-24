// F07Z-80 acceptance (P3.5 item 2): a section gets one surface, and the items inside it are rows
// separated by a hairline - not a card inside a card inside a card.
//
//   D01_LAYERS_BASE=http://127.0.0.1:5174 D01_LAYERS_GOAL=<uuid> \
//     node --experimental-strip-types apps/web/tests/d01-desktop-card-layers.mts
//
// It measures computed borders/backgrounds on the real pages, because that is where the previous
// "changed the CSS" mistake lived: a rule can be present and still be overridden.
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';

const out=resolve(process.env.D01_LAYERS_OUT??join(resolve(import.meta.dirname,'../../..'),'.private/f07z80'));
mkdirSync(out,{recursive:true});
const base=process.env.D01_LAYERS_BASE??'http://127.0.0.1:5174';
const api=process.env.D01_LAYERS_API??'http://127.0.0.1:3100';
const goal=process.env.D01_LAYERS_GOAL??'';
assert.ok(goal,'D01_LAYERS_GOAL is required');
const exe=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(p=>existsSync(p));
assert.ok(exe,'installed_browser_required');

const login=await fetch(`${api}/api/auth/sign-in/email`,{
 method:'POST',headers:{'Content-Type':'application/json',Origin:api},
 body:JSON.stringify({email:'budget-1794568957@example.com',password:'Synthetic-9f3a2b7c1d'})});
assert.ok(login.ok,`login ${login.status}`);
const raw=login.headers.get('set-cookie')!;
const [ck,vk]=[raw.slice(0,raw.indexOf('=')),raw.slice(raw.indexOf('=')+1).split(';')[0]];

const browser=await chromium.launch({executablePath:exe,headless:true});
const context=await browser.newContext({viewport:{width:1280,height:900}});
await context.addCookies([{name:ck,value:vk,domain:'127.0.0.1',path:'/',httpOnly:true,sameSite:'Lax'}]);
const page=await context.newPage();
const errors:string[]=[];
page.on('pageerror',e=>errors.push(String(e)));
await page.goto(`${base}/?shell=desktop&goal=${goal}`,{waitUntil:'domcontentloaded'});
await page.locator('.desktop-shell[data-pane]').waitFor({timeout:40000});

type Box={sides:number[];bg:string;top:number;cls:string};
const style=(selector:string)=>page.evaluate((s:string)=>{
 return [...document.querySelectorAll(s)].map(el=>{
  const cs=getComputedStyle(el);
  const w=(side:string)=>parseFloat(cs.getPropertyValue(`border-${side}-width`))||0;
  return {sides:[w('top'),w('right'),w('bottom'),w('left')],
   bg:cs.backgroundColor,top:w('top'),cls:((el as HTMLElement).className||'').toString()};
 });
},selector);
const boxed=(b:Box)=>b.sides.every(v=>v>0);

// 1. Pipeline steps: three rows inside one tinted card, separated by a hairline, phase shown by
//    a left bar rather than by giving each step its own frame.
const steps=await style('.d-pipeline-steps li') as Box[];
assert.ok(steps.length>=3,`expected pipeline steps, got ${steps.length}`);
for(const step of steps)assert.equal(boxed(step),false,`pipeline step still boxed: ${step.sides}`);
assert.ok(steps[1].top>0,'consecutive pipeline steps must be separated by a hairline');

// 2. History rows: ten boxed rows inside a boxed card was the worst offender on the written page.
await page.locator('.d-nav-item').filter({hasText:'笔试'}).first().click();
await page.locator('[aria-label="笔试历史"]').first().waitFor({timeout:40000});
await page.locator('[aria-label="笔试历史"] .d-session-item').first().waitFor({timeout:30000});
const rows=await style('[aria-label="笔试历史"] .d-session-item') as Box[];
assert.ok(rows.length>=2,`expected written history rows, got ${rows.length}`);
for(const row of rows)assert.equal(boxed(row),false,`history row still boxed: ${row.sides}`);
assert.ok(rows[1].top>0,'history rows must be separated by a hairline');

// 3. Written question items: same treatment inside the session card.
const history=page.locator('[aria-label="笔试历史"] .d-session-item');
for(let i=0;i<await history.count();i+=1){
 await history.nth(i).click();
 await page.waitForTimeout(800);
 if(await page.locator('.d-written-item').count())break;
}
const items=await style('.d-written-item') as Box[];
assert.ok(items.length>=1,'expected at least one written item');
for(const item of items)assert.equal(boxed(item),false,`written item still boxed: ${item.sides}`);

// 4. The "本机已有 N 场会话" line inside an agent card used to be a third border layer.
const agentState=await style('.d-agent-state') as Box[];
assert.ok(agentState.length>=1,'expected the agent state line');
for(const state of agentState){
 assert.equal(boxed(state),false,`agent state still boxed: ${state.sides}`);
 assert.equal(state.bg,'rgba(0, 0, 0, 0)','agent state must not paint its own background');
}

// 5. Structural guard: a card never contains another card. This is the invariant behind "one
//    surface per section" and the thing that creeped back in four different places.
const nested=await page.evaluate(()=>{
 const isCard=(el:Element)=>el.classList.contains('card')||el.classList.contains('d-card');
 return [...document.querySelectorAll('.desktop-body *')].filter(e=>isCard(e))
  .filter(e=>{for(let p=e.parentElement;p;p=p.parentElement)if(isCard(p))return true;return false;})
  .map(e=>`${e.tagName}.${((e as HTMLElement).className||'').toString().trim().replace(/\s+/g,'.')}`);
});

const report={steps:steps.length,rows:rows.length,items:items.length,
 agentState:agentState.length,nestedCards:nested,pageErrors:errors.slice(0,3)};
writeFileSync(join(out,'card-layers.json'),JSON.stringify(report,null,1));
console.log(JSON.stringify(report,null,1));
assert.deepEqual(nested,[],`card inside card: ${nested.join(' | ')}`);
assert.equal(errors.length,0,`page errors: ${errors.slice(0,2).join(' | ')}`);
console.log('RESULT passed: one surface per section, rows separated by hairlines');
await browser.close();

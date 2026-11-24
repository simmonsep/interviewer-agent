// F07Z-76 acceptance: a long, number-heavy question must stay readable.
// Checks the rendered text column rather than the source: measure (characters per line), preserved
// line breaks, line height and horizontal overflow. Runs against a real written session built from
// the built-in aptitude bank, whose data questions are the longest text in the product.
//
//   D01_LAYOUT_BASE=http://127.0.0.1:5174 D01_LAYOUT_GOAL=<uuid> \
//     node --experimental-strip-types apps/web/tests/d01-desktop-question-layout.mts
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {randomUUID} from 'node:crypto';

const out=resolve(process.env.D01_LAYOUT_OUT??join(resolve(import.meta.dirname,'../../..'),'.private/f07z76'));
mkdirSync(out,{recursive:true});
const base=process.env.D01_LAYOUT_BASE??'http://127.0.0.1:5174';
const api=process.env.D01_LAYOUT_API??'http://127.0.0.1:3100';
const goal=process.env.D01_LAYOUT_GOAL??'';
const APTI='00000000-0000-4000-8000-000000000004';
assert.ok(goal,'D01_LAYOUT_GOAL is required');
const exe=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(p=>existsSync(p));
assert.ok(exe,'installed_browser_required');

const login=await fetch(`${api}/api/auth/sign-in/email`,{
 method:'POST',headers:{'Content-Type':'application/json','Origin':api},
 body:JSON.stringify({email:'budget-1794568957@example.com',password:'Synthetic-9f3a2b7c1d'})});
assert.ok(login.ok,`login ${login.status}`);
const cookie=login.headers.get('set-cookie').split(';')[0];
const [ck,vk]=[cookie.slice(0,cookie.indexOf('=')),cookie.slice(cookie.indexOf('=')+1)];

// A leftover open session blocks a new one; end it the way the product does.
const listed=await (await fetch(`${api}/api/career-goals/${goal}/written`,{headers:{cookie}})).json();
for(const item of listed.items??[]){
 if(item.status!=='ACTIVE')continue;
 const current=await (await fetch(`${api}/api/career-goals/${goal}/written/${item.id}`,{headers:{cookie}})).json();
 await fetch(`${api}/api/career-goals/${goal}/written/${item.id}/cancel`,{method:'POST',
  headers:{cookie,'Content-Type':'application/json','Idempotency-Key':randomUUID()},
  body:JSON.stringify({expected_version:current.version})});
}

const browser=await chromium.launch({executablePath:exe,headless:true});
const context=await browser.newContext({viewport:{width:1280,height:900}});
await context.addCookies([{name:ck,value:vk,domain:'127.0.0.1',path:'/',httpOnly:true,sameSite:'Lax'}]);
const page=await context.newPage();
const errors:string[]=[];
page.on('pageerror',e=>errors.push(String(e)));

await page.goto(`${base}/?shell=desktop&goal=${goal}`,{waitUntil:'domcontentloaded'});
await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
await page.locator('.d-nav-item').filter({hasText:'笔试'}).first().click();
await page.waitForTimeout(900);
await page.locator('select[aria-label="笔试题库"]').selectOption(`${APTI}|1`);
await page.locator('input[aria-label="笔试题数"]').fill('12');
await page.getByRole('button',{name:'开始笔试'}).click();
const items=page.locator('.d-written-item');
try{
 await items.first().waitFor({timeout:15000});
}catch{
 // A goal can be at its session cap; fall back to an already finished session so the check
 // still measures real content instead of failing for an unrelated reason.
 const rows=page.locator('[aria-label="笔试历史"] .d-session-item');
 await rows.first().waitFor({timeout:15000});
 let opened=false;
 for(let i=0;i<await rows.count();i+=1){
  await rows.nth(i).click();
  await page.waitForTimeout(900);
  if(await items.count()){opened=true;break;}
 }
 assert.ok(opened,'no written session with questions to measure');
}

// Pick the longest question on screen - that is the case this node is about.
const longest=await page.evaluate(()=>{
 const all=[...document.querySelectorAll('.d-written-item')];
 let best=0,bestLen=0;
 all.forEach((el,i)=>{const text=el.querySelector('.d-written-question')?.textContent??'';
  if(text.length>bestLen){bestLen=text.length;best=i;}});
 return {index:best,length:bestLen};
});
assert.ok(longest.length>=80,`no long question found (${longest.length} chars)`);

const target=items.nth(longest.index);
await target.scrollIntoViewIfNeeded();
const metrics=await target.evaluate((el)=>{
 const q=el.querySelector('.d-written-question') as HTMLElement;
 const cs=getComputedStyle(q);
 const range=document.createRange();range.selectNodeContents(q);
 const rects=[...range.getClientRects()].filter(r=>r.width>1);
 const lineWidth=rects.length?Math.max(...rects.map(r=>r.width)):0;
 const font=parseFloat(cs.fontSize);
 const lineHeight=parseFloat(cs.lineHeight);
 return {
  chars:(q.textContent??'').length,
  font_size:font,line_height:lineHeight,line_ratio:Math.round(lineHeight/font*100)/100,
  white_space:cs.whiteSpace,
  block_width:Math.round(q.getBoundingClientRect().width),
  column_width:Math.round((q.parentElement as HTMLElement).getBoundingClientRect().width),
  chars_per_line:font>0?Math.round((lineWidth/font)*10)/10:null,
  lines:rects.length,
  paragraphs:(q.textContent??'').split(/\n\s*\n/).filter(Boolean).length,
  item_overflows_x:el.scrollWidth>el.clientWidth,
  question_overflows_x:q.scrollWidth>q.clientWidth,
  option_overflows:[...el.querySelectorAll('.d-written-option')].some(o=>o.scrollWidth>o.clientWidth+1),
 };
});
await target.screenshot({path:join(out,'layout-long-question.png')});
writeFileSync(join(out,'layout.json'),JSON.stringify({longest,metrics,page_errors:errors},null,1));
console.log(JSON.stringify({longest,metrics,page_errors:errors.length},null,1));

assert.equal(metrics.white_space,'pre-wrap','line breaks must be preserved');
assert.ok(metrics.chars_per_line<=50,`too many characters per line: ${metrics.chars_per_line}`);
assert.ok(metrics.block_width<metrics.column_width,`text column not capped: ${metrics.block_width}/${metrics.column_width}`);
assert.ok(metrics.line_ratio>=1.6,`line height too tight: ${metrics.line_ratio}`);
assert.ok(metrics.lines>=2,'the long question should wrap onto more than one line');
assert.equal(metrics.item_overflows_x,false,'card scrolls horizontally');
assert.equal(metrics.question_overflows_x,false,'question text overflows its box');
assert.equal(metrics.option_overflows,false,'an option overflows its row');
assert.equal(errors.length,0,`page_errors ${JSON.stringify(errors.slice(0,3))}`);
console.log(`RESULT passed ${metrics.chars} chars, ${metrics.chars_per_line} per line, ${metrics.lines} lines`);
await browser.close();

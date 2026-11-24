// F07Z-82 acceptance (P3.5 item 6): the artifacts page is results, not a second copy of the rail.
// Two zones with headings (运行结果 / 记录与配置), one action, and zero navigation entries - the
// seven detail links that used to live here were the rail, printed twice.
//
//   D01_ARTIFACTS_BASE=http://127.0.0.1:5174 D01_ARTIFACTS_GOAL=<uuid> \
//     node --experimental-strip-types apps/web/tests/d01-desktop-artifacts.mts
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';

const out=resolve(process.env.D01_ARTIFACTS_OUT??join(resolve(import.meta.dirname,'../../..'),'.private/f07z82'));
mkdirSync(out,{recursive:true});
const base=process.env.D01_ARTIFACTS_BASE??'http://127.0.0.1:5174';
const api=process.env.D01_ARTIFACTS_API??'http://127.0.0.1:3100';
const goal=process.env.D01_ARTIFACTS_GOAL??'';
assert.ok(goal,'D01_ARTIFACTS_GOAL is required');
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
const ctx=await browser.newContext({viewport:{width:1280,height:900}});
await ctx.addCookies([{name:ck,value:vk,domain:'127.0.0.1',path:'/',httpOnly:true,sameSite:'Lax'}]);
const page=await ctx.newPage();
const errors:string[]=[];
page.on('pageerror',e=>errors.push(String(e)));
await page.goto(`${base}/?shell=desktop&goal=${goal}`,{waitUntil:'domcontentloaded'});
await page.locator('.desktop-shell[data-pane]').waitFor({timeout:40000});
await page.locator('.desktop-rail [data-testid="nav-artifacts"]').click();
await page.locator('[data-testid="artifacts-pane"]').waitFor({timeout:20000});
await page.waitForTimeout(800);

const info=await page.evaluate(()=>{
 const pane=document.querySelector('[data-testid="artifacts-pane"]') as HTMLElement;
 const zones=[...pane.querySelectorAll('.d-artifact-zone')].map(z=>({
  label:(z.querySelector('.d-artifact-zone-label') as HTMLElement|null)?.innerText.trim()??'',
  rows:z.querySelectorAll('.d-agent-artifact').length,
 }));
 return {
  zones,
  navItems:pane.querySelectorAll('.d-nav-item').length,
  rows:pane.querySelectorAll('.d-agent-artifact').length,
  firstLabel:(pane.querySelector('.d-agent-artifact-label') as HTMLElement|null)?.innerText.trim()??'',
  action:[...pane.querySelectorAll('button')].some(b=>b.innerText.includes('新建求职目标')),
  text:pane.innerText,
  cards:pane.querySelectorAll('.card').length,
 };
});

// 1. Zones exist, are labelled, and split results from records.
const labelled=info.zones.filter(z=>z.label);
assert.deepEqual(labelled.map(z=>z.label),['运行结果','记录与配置'],
 `zones: ${JSON.stringify(info.zones)}`);
assert.equal(labelled[0].rows,4,'运行结果 must hold the four produced things');
assert.equal(labelled[1].rows,2,'记录与配置 must hold the record and the bank');

// 2. The rail is not copied into this page. This is the whole point of the node.
assert.equal(info.navItems,0,'the artifacts page must not re-print the rail');
assert.equal(info.cards,1,'results should be one surface, not a stack of cards');

// 3. Rows keep their contract: six of them, each opening its pane, the first being the job.
assert.equal(info.rows,6,'six artifact rows');
assert.equal(info.firstLabel,'选定岗位');
assert.equal(info.action,true,'the create-goal action stays on the page');

// 4. Wording: evidence, not "facts" (F06F's rule), and the empty states say what is missing.
assert.match(info.text,/材料来源 \/ 已确认依据/);
assert.doesNotMatch(info.text,/已确认事实/);
assert.doesNotMatch(info.text,/0 份来源/,'an empty material list must not render as "0 份来源"');

// 5. Clicking the first row still opens the research pane.
// The workbench aside carries the same card as context, so scope to this pane.
await page.locator('[data-testid="artifacts-pane"] .d-agent-artifact').first().click();
await page.waitForTimeout(600);
assert.equal(await page.locator('.desktop-shell').getAttribute('data-pane'),'research',
 'the job row must open the research pane');
await page.screenshot({path:join(out,'artifacts-page.png')});

writeFileSync(join(out,'artifacts.json'),JSON.stringify({...info,text:undefined,pageErrors:errors},null,1));
console.log(JSON.stringify({zones:info.zones,navItems:info.navItems,rows:info.rows,cards:info.cards},null,1));
assert.equal(errors.length,0,`page errors: ${errors.slice(0,2).join(' | ')}`);
console.log('RESULT passed: artifacts page is zoned results, not a copied rail');
await ctx.close();
await browser.close();

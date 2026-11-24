// F07Z-81 acceptance (P3.5 item 4): the top bar keeps only what is global to the window -
// drag region, window controls, one-sentence input, appearance, account. Navigation belongs to
// the rail, page-level jumps belong to the page, and the "本地 · 数据保存在本机" line belongs to
// the status bar (it was printed twice).
//
//   D01_TOPBAR_BASE=http://127.0.0.1:5174 D01_TOPBAR_GOAL=<uuid> \
//     node --experimental-strip-types apps/web/tests/d01-desktop-topbar.mts
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';

const out=resolve(process.env.D01_TOPBAR_OUT??join(resolve(import.meta.dirname,'../../..'),'.private/f07z81'));
mkdirSync(out,{recursive:true});
const base=process.env.D01_TOPBAR_BASE??'http://127.0.0.1:5174';
const api=process.env.D01_TOPBAR_API??'http://127.0.0.1:3100';
const goal=process.env.D01_TOPBAR_GOAL??'';
assert.ok(goal,'D01_TOPBAR_GOAL is required');
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
const report:any={};
const errors:string[]=[];

async function open(width:number,height:number){
 const ctx=await browser.newContext({viewport:{width,height}});
 await ctx.addCookies([{name:ck,value:vk,domain:'127.0.0.1',path:'/',httpOnly:true,sameSite:'Lax'}]);
 const page=await ctx.newPage();
 page.on('pageerror',e=>errors.push(String(e)));
 await page.goto(`${base}/?shell=desktop&goal=${goal}`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:40000});
 await page.waitForTimeout(800);
 return {ctx,page};
}

const {ctx,page}=await open(1280,900);

// 1. No navigation lives in the bar.
const barNav=await page.evaluate(()=>{
 const bar=document.querySelector('.desktop-topbar') as HTMLElement;
 return {
  navItems:bar.querySelectorAll('.d-nav-item,.d-top-rail,.d-top-artifacts').length,
  railJump:bar.querySelectorAll('[data-testid="agent-rail-jump"]').length,
  text:bar.innerText.replace(/\s+/g,''),
 };
});
assert.equal(barNav.navItems,0,'the top bar must not host navigation');
assert.equal(barNav.railJump,0,'the run-rail jump must not live in the top bar');
assert.ok(!barNav.text.includes('本地·数据保存在本机')&&!barNav.text.includes('本地·数据保存在本机'),
 'the duplicated "本地 · 数据保存在本机" line must be gone from the bar');

// 2. 产物 is a rail entry and actually switches panes.
const railEntry=page.locator('.desktop-rail [data-testid="nav-artifacts"]');
assert.equal(await railEntry.count(),1,'产物 must be reachable from the rail');
await railEntry.click();
await page.waitForTimeout(600);
assert.equal(await page.locator('.desktop-shell').getAttribute('data-pane'),'artifacts',
 'clicking the rail entry must open the artifacts pane');
assert.equal(await railEntry.getAttribute('aria-current'),'page','the open pane must be marked');
await page.locator('.d-nav-item').filter({hasText:'工作台'}).first().click();
await page.waitForTimeout(500);

// 3. The run-rail jump lives on the workbench page and opens the section if it was collapsed.
const jump=page.locator('.desktop-body [data-testid="agent-rail-jump"]');
assert.equal(await jump.count(),1,'the jump belongs to the workbench pane');
assert.equal(await page.locator('.desktop-topbar [data-testid="agent-rail-jump"]').count(),0);
await page.evaluate(()=>{
 const el=document.getElementById('agent-rail') as HTMLDetailsElement|null;
 if(el)el.open=false;
});
await jump.click();
await page.waitForTimeout(700);
const railState=await page.evaluate(()=>{
 const el=document.getElementById('agent-rail') as HTMLDetailsElement|null;
 return el?{open:el.open,scrollY:Math.round(window.scrollY||document.querySelector('.desktop-body')!.scrollTop)}:null;
});
assert.equal(railState?.open,true,'the jump must open the collapsed section');

// 4. The freed width went to the input, and the bar still fits.
for(const [w,h] of [[1280,900],[900,700],[760,600]] as [number,number][]){
 const {ctx:c,page:p}=await open(w,h);
 const m=await p.evaluate(()=>{
  const bar=document.querySelector('.desktop-topbar') as HTMLElement;
  const input=bar.querySelector('.d-topbar-omni input') as HTMLInputElement|null;
  return {input:input?Math.round(input.getBoundingClientRect().width):0,
   overflow:document.documentElement.scrollWidth>window.innerWidth,
   barHeight:Math.round(bar.getBoundingClientRect().height)};
 });
 assert.equal(m.overflow,false,`${w}px: horizontal overflow`);
 // Before this node the input was 188px wide at every width under 1180px, and 260px at 1280.
 const floor=w>=1200?300:w>=880?260:200;
 assert.ok(m.input>=floor,`${w}px: input only ${m.input}px (floor ${floor})`);
 report[`${w}x${h}`]=m;
 await c.close();
}

report.barNav=barNav;
report.railState=railState;
report.pageErrors=errors.slice(0,3);
writeFileSync(join(out,'topbar.json'),JSON.stringify(report,null,1));
console.log(JSON.stringify(report,null,1));
assert.equal(errors.length,0,`page errors: ${errors.slice(0,2).join(' | ')}`);
console.log('RESULT passed: bar = window + input + appearance + account; navigation is in the rail');
await ctx.close();
await browser.close();

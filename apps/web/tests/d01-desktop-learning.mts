// F07Z-86 acceptance: the 题库 page carries the learning list, and a link the user adds can be
// added and removed without touching the shipped entries.
//
//   D01_LEARN_BASE=http://127.0.0.1:5174 D01_LEARN_GOAL=<uuid> \
//     node --experimental-strip-types apps/web/tests/d01-desktop-learning.mts
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';

const out=resolve(process.env.D01_LEARN_OUT??join(resolve(import.meta.dirname,'../../..'),'.private/f07z86'));
mkdirSync(out,{recursive:true});
const base=process.env.D01_LEARN_BASE??'http://127.0.0.1:5174';
const api=process.env.D01_LEARN_API??'http://127.0.0.1:3100';
const goal=process.env.D01_LEARN_GOAL??'';
assert.ok(goal,'D01_LEARN_GOAL is required');
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
const context=await browser.newContext({viewport:{width:1280,height:1000}});
await context.addCookies([{name:ck,value:vk,domain:'127.0.0.1',path:'/',httpOnly:true,sameSite:'Lax'}]);
const page=await context.newPage();
const errors:string[]=[];
page.on('pageerror',e=>errors.push(String(e)));
await page.goto(`${base}/?shell=desktop&goal=${goal}`,{waitUntil:'domcontentloaded'});
await page.locator('.desktop-shell[data-pane]').waitFor({timeout:40000});
await page.locator('.d-nav-item').filter({hasText:'题库'}).first().click();
await page.locator('[data-testid="learning-resources"]').waitFor({timeout:30000});

// 1. The shipped list is grouped into topics and carries blurbs and a check date.
const before=await page.evaluate(()=>{
 const zone=document.querySelector('[data-testid="learning-resources"]') as HTMLElement;
 const topics=[...zone.querySelectorAll('.d-artifact-zone-label')].map(el=>el.textContent?.trim());
 return {topics,rows:zone.querySelectorAll('.d-resource').length,
  blurbs:[...zone.querySelectorAll('.d-resource-open small')].filter(el=>el.textContent?.trim()).length,
  checked:document.body.innerText.includes('核对')};
});
assert.ok(before.topics.length>=4,`topics: ${before.topics}`);
assert.ok(before.rows>=12,`rows: ${before.rows}`);
assert.equal(before.blurbs,before.rows,'every row explains what the site is for');
assert.equal(before.checked,true,'the page must say what was and was not checked');

// 2. Add a user link: it appears under 我添加的 and can be removed again.
const marker=`workbuddy-${Date.now()}`;
const host=`https://example.com/${marker}`;
await page.getByLabel('学习资源名称').fill(`我的课程 ${marker}`);
await page.getByLabel('学习资源地址').fill(host);
await page.getByRole('button',{name:'添加'}).click();
const added=page.locator('.d-resource').filter({hasText:`我的课程 ${marker}`});
await added.waitFor({timeout:20000});
const mineZone=await page.evaluate(()=>{
 const labels=[...document.querySelectorAll('.d-artifact-zone-label')].map(el=>el.textContent?.trim());
 return labels.includes('我添加的');
});
assert.equal(mineZone,true,'user links get their own group');
assert.ok((await page.locator('.d-resource').count())===before.rows+1);

await added.getByRole('button',{name:/删除/}).click();
await page.waitForFunction(sel=>document.querySelectorAll(sel).length>0,
  '.d-resource', {timeout:20000});
let gone=false;
for(let i=0;i<40;i+=1){
 if(await page.locator('.d-resource').filter({hasText:`我的课程 ${marker}`}).count()===0){gone=true;break;}
 await page.waitForTimeout(250);
}
assert.equal(gone,true,'the removed link disappears');
assert.equal(await page.locator('.d-resource').count(),before.rows,'shipped entries are untouched');

await page.screenshot({path:join(out,'learning-zone.png')});
writeFileSync(join(out,'learning.json'),JSON.stringify({before,pageErrors:errors.slice(0,3)},null,1));
console.log(JSON.stringify(before,null,1));
assert.equal(errors.length,0,`page errors: ${errors.slice(0,2).join(' | ')}`);
console.log('RESULT passed: learning list is on the question-bank page, add/remove works');
await context.close();
await browser.close();

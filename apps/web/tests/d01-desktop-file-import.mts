// F07Z-94 acceptance: a PDF/DOCX resume can be imported from the materials card, previewed
// before it enters the ledger, and then analysed like a pasted source.
//
//   D01_FILE_BASE=http://127.0.0.1:3100 D01_FILE_API=http://127.0.0.1:3100 \
//     D01_FILE_GOAL=<uuid> node --experimental-strip-types apps/web/tests/d01-desktop-file-import.mts
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';

const out=resolve(process.env.D01_FILE_OUT??join(resolve(import.meta.dirname,'../../..'),'.private/f07z94'));
mkdirSync(out,{recursive:true});
const base=process.env.D01_FILE_BASE??'http://127.0.0.1:3100';
const api=process.env.D01_FILE_API??'http://127.0.0.1:3100';
const goal=process.env.D01_FILE_GOAL??'';
const email=process.env.D01_FILE_EMAIL??'budget-1794568957@example.com';
const password=process.env.D01_FILE_PASSWORD??'Synthetic-9f3a2b7c1d';
const docx=process.env.D01_FILE_DOCX??join(resolve(import.meta.dirname,'../../..'),'.private/f07z94/resume.docx');
assert.ok(goal,'D01_FILE_GOAL is required');
assert.ok(existsSync(docx),`missing docx fixture: ${docx}`);
const exe=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(p=>existsSync(p));
assert.ok(exe,'installed_browser_required');

const login=await fetch(`${api}/api/auth/sign-in/email`,{
 method:'POST',headers:{'Content-Type':'application/json',Origin:api},
 body:JSON.stringify({email,password})});
assert.ok(login.ok,`login ${login.status}`);
const raw=login.headers.get('set-cookie')!;
const cookie=raw.split(';')[0];

const browser=await chromium.launch({executablePath:exe,headless:true});
const context=await browser.newContext({viewport:{width:1280,height:1000}});
await context.addCookies([{name:cookie.slice(0,cookie.indexOf('=')),
 value:cookie.slice(cookie.indexOf('=')+1),domain:'127.0.0.1',path:'/',httpOnly:true,sameSite:'Lax'}]);
const page=await context.newPage();
const errors:string[]=[];
page.on('pageerror',e=>errors.push(String(e)));
await page.goto(`${base}/?shell=desktop&goal=${goal}`,{waitUntil:'domcontentloaded'});
await page.locator('.desktop-shell[data-pane]').waitFor({timeout:40000});
await page.locator('.d-nav-item').filter({hasText:'材料'}).first().click();
await page.locator('.desktop-shell[data-pane="materials"]').waitFor({timeout:20000});
await page.getByRole('button',{name:'从 PDF / DOCX 导入'}).waitFor({timeout:20000});

// 1. Pick the file; the backend parses it and the page shows the extracted text first.
await page.locator('input[aria-label="选择简历文件"]').setInputFiles(docx);
const preview=page.getByTestId('file-preview');
await preview.waitFor({timeout:40000});
const previewText=(await preview.locator('.d-file-preview-text').innerText()).trim();
const head=(await preview.locator('.d-file-preview-head').innerText()).trim();
await page.screenshot({path:join(out,'file-preview.png')});

// 2. Import it; the source list must show a file-backed source.
await preview.getByRole('button',{name:'导入为材料'}).click();
const source=page.locator('.d-source').filter({hasText:'订单查询接口'}).first();
await source.waitFor({timeout:40000});
const sourceText=(await source.innerText()).trim();

// 3. The ledger records it as a file source (not a pasted text source).
const listing=await (await fetch(`${api}/api/career-goals/${goal}/materials`,{headers:{cookie}})).json() as any;
const imported=(listing.sources??[]).filter((item:any)=>item.file_reference).at(-1);

const result={head,previewChars:previewText.length,previewHasExperience:previewText.includes('订单查询接口'),
 sourceTag:sourceText.includes('文件导入'),imported:imported?{name:imported.name,format:imported.format,
 bytes:imported.file_reference.byte_length}:null,pageErrors:errors.slice(0,3)};
writeFileSync(join(out,'file-import.json'),JSON.stringify(result,null,1));
console.log(JSON.stringify(result,null,1));

assert.ok(previewText.includes('订单查询接口'),'preview must show the extracted Chinese text');
assert.ok(head.includes('resume.docx'),`preview head: ${head}`);
assert.ok(sourceText.includes('文件导入'),'imported source should be marked as file-backed');
assert.ok(imported,'the ledger has no file-backed source');
assert.equal(imported.format,'docx');
assert.ok(imported.file_reference.byte_length>0);
assert.equal(errors.length,0,`page errors: ${errors.slice(0,2).join(' | ')}`);
console.log('RESULT passed: DOCX preview then import, with a file-backed source');
await context.close();
await browser.close();

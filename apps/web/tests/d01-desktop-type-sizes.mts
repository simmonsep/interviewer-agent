// F07Z-90: read the computed text sizes that the user can actually see on the frozen product.
//
// All panes stay in the DOM (hidden with `hidden`), so a selector alone can hit an element from a
// pane that is not on screen. Every probe therefore filters to elements with a real box, and
// reports the min/max across them. A separate census lists every visible text element below or at
// a threshold, so nothing is missed just because it was not in the probe list.
//
//   D01_SIZE_BASE=http://127.0.0.1:3100 D01_SIZE_GOAL=<uuid> D01_SIZE_OUT=<dir> \
//     node --experimental-strip-types apps/web/tests/d01-desktop-type-sizes.mts
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';

const out=resolve(process.env.D01_SIZE_OUT??join(resolve(import.meta.dirname,'../../..'),'.private/f07z90'));
mkdirSync(out,{recursive:true});
const base=process.env.D01_SIZE_BASE??'http://127.0.0.1:3100';
const goal=process.env.D01_SIZE_GOAL??'';
assert.ok(goal,'D01_SIZE_GOAL is required');
const exe=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(p=>existsSync(p));
assert.ok(exe,'installed_browser_required');

const login=await fetch(`${base}/api/auth/sign-in/email`,{
 method:'POST',headers:{'Content-Type':'application/json',Origin:base},
 body:JSON.stringify({email:'budget-1794568957@example.com',password:'Synthetic-9f3a2b7c1d'})});
assert.ok(login.ok,`login ${login.status}`);
const raw=login.headers.get('set-cookie')!;
const cookie=raw.split(';')[0];

const browser=await chromium.launch({executablePath:exe,headless:true});
const context=await browser.newContext({viewport:{width:1280,height:960}});
await context.addCookies([{name:cookie.slice(0,cookie.indexOf('=')),
 value:cookie.slice(cookie.indexOf('=')+1),domain:'127.0.0.1',path:'/',httpOnly:true,sameSite:'Lax'}]);
const page=await context.newPage();
const errors:string[]=[];
page.on('pageerror',e=>errors.push(String(e)));
await page.goto(`${base}/?shell=desktop&goal=${goal}`,{waitUntil:'domcontentloaded'});
await page.locator('.desktop-shell[data-pane]').waitFor({timeout:40000});

const probes:[string,string][]=[
 ['rail entry','.d-nav-item'],
 ['rail entry text','.d-nav-item .d-nav-label'],
 ['rail group heading','.d-nav-section-title, .d-nav-group-title'],
 ['brand text','.d-brand-text'],
 ['topbar title','.d-topbar-title'],
 ['topbar page','.d-context-page'],
 ['topbar goal','.d-context-goal strong'],
 ['topbar goal meta','.d-context-goal small'],
 ['account summary','.d-account>summary'],
 ['rail footnote','.d-rail-foot'],
 ['pane heading','.d-pane-head h1'],
 ['pane description','.d-pane-head p'],
 ['card heading','.d-card h2'],
 ['form label','.desktop-shell label'],
 ['text input','.desktop-shell input:not([type="checkbox"]):not([type="radio"])'],
 ['select','.desktop-shell select'],
 ['textarea','.desktop-shell textarea'],
 ['checkbox','.desktop-shell input[type="checkbox"]'],
 ['primary button','.desktop-shell button.primary'],
 ['secondary button','.desktop-shell button.secondary'],
 ['plain button','.desktop-shell button:not(.primary):not(.secondary):not(.text-button):not(.d-nav-item)'],
 ['text button','.desktop-shell .text-button'],
 ['tag','.desktop-shell .tag'],
 ['eyebrow','.desktop-shell .eyebrow'],
 ['fine print','.desktop-shell .fine-print'],
 ['muted','.desktop-shell .muted'],
 ['written question','.d-written-question'],
 ['written option','.d-written-option'],
 ['session meta','.d-session-meta'],
 ['mode switch note','.d-mode-switch button small'],
 ['citation label','.desktop-shell .citation-label'],
];

// Every visible element that directly owns visible text, with its computed size. This is the
// census: it catches controls that the probe list happens to miss.
const CENSUS=`(()=>{
 const out=[];
 const walk=(root)=>{
  for(const el of root.querySelectorAll('*')){
   let own='';
   for(const node of el.childNodes) if(node.nodeType===3) own+=node.textContent;
   own=own.trim();
   if(!own) continue;
   const rect=el.getBoundingClientRect();
   if(rect.width<2||rect.height<2) continue;
   const cs=getComputedStyle(el);
   if(cs.visibility==='hidden'||cs.display==='none') continue;
   out.push({tag:el.tagName.toLowerCase(),cls:String(el.className||'').split(/\\s+/)[0]||'',
    text:own.slice(0,18),size:Number.parseFloat(cs.fontSize)});
  }
 };
 walk(document.querySelector('.desktop-shell'));
 return out;
})()`;

async function measurePane(navText:string|null){
 if(navText){
  await page.locator('.d-nav-item').filter({hasText:navText}).first().click();
  await page.waitForTimeout(1200);
 }
 return page.evaluate(([list,census])=>{
  const rows=[];
  for(const [role,selector] of list as [string,string][]){
   const sizes=[];
   for(const el of document.querySelectorAll(selector)){
    const rect=el.getBoundingClientRect();
    if(rect.width<2||rect.height<2) continue;
    sizes.push(Number.parseFloat(getComputedStyle(el).fontSize));
   }
   rows.push({role,sizes:[...new Set(sizes)].sort((a,b)=>a-b)});
  }
  const shell=document.querySelector('.desktop-shell') as HTMLElement;
  return {pane:shell?.getAttribute('data-pane'),
   overflowX:document.documentElement.scrollWidth>document.documentElement.clientWidth,
   rows,census:eval(census) as any[]};
 },[probes,CENSUS] as const);
}

const panes:any[]=[];
panes.push(await measurePane(null));
for(const [name,navText] of [['written','笔试'],['banks','题库'],['artifacts','产物'],
 ['settings','设置'],['materials','材料'],['interview','面试']] as const){
 const result=await measurePane(navText);
 panes.push({requested:name,...result});
}

// The whole-app picture: smallest sizes across all panes, and everything at or below 12px.
const all=panes.flatMap(p=>p.census);
const belowOrAt12=all.filter((row:any)=>row.size<=12);
const summary={smallest:Math.min(...all.map((row:any)=>row.size)),
 counts:Object.fromEntries([11,11.5,12,12.5,13,13.5,14].map(size=>
  [String(size),all.filter((row:any)=>row.size===size).length])),
 atOrBelow12:[...new Map(belowOrAt12.map((row:any)=>[row.size+'|'+row.tag+'.'+row.cls,
  {tag:row.tag,cls:row.cls,size:row.size,sample:row.text}])).values()]};

// Narrow window: is the smallest control still readable when the rail and panes are tight?
await page.setViewportSize({width:760,height:600});
await page.waitForTimeout(900);
const narrow=await page.evaluate(()=>{
 const visible=[...document.querySelectorAll('.desktop-shell button,.desktop-shell select,.desktop-shell input')]
  .filter(el=>{const r=el.getBoundingClientRect();return r.width>2&&r.height>2;})
  .map(el=>Number.parseFloat(getComputedStyle(el).fontSize));
 return {overflowX:document.documentElement.scrollWidth>document.documentElement.clientWidth,
  smallestControl:visible.length?Math.min(...visible):null};
});

const report={base,viewport:'1280x960',panes,summary,narrow,pageErrors:errors.slice(0,3)};
writeFileSync(join(out,'type-sizes.json'),JSON.stringify(report,null,1));
console.log(JSON.stringify({panes:panes.map(p=>({pane:p.pane??p.requested,overflowX:p.overflowX,
 rows:p.rows.filter((r:any)=>r.sizes.length)})),summary,narrow},null,1));
assert.equal(errors.length,0,`page errors: ${errors.slice(0,2).join(' | ')}`);

// The contract this node introduces: navigation reads like body text, a control is never
// smaller than 12.5px, and nothing the user can read drops below 10px.
const sizeOf=(role:string)=>{
 const found=panes.flatMap(p=>p.rows).filter((r:any)=>r.role===role&&r.sizes.length);
 return found.length?Math.min(...found.flatMap((r:any)=>r.sizes)):null;
};
const rail=sizeOf('rail entry');
const controls=['text input','select','textarea','primary button','secondary button',
 'plain button','text button'].map(sizeOf).filter((size):size is number=>size!==null);
const label=sizeOf('form label');
const fine=sizeOf('fine print');
assert.ok(rail!==null&&rail>=14,`rail_entry_too_small ${rail}px`);
assert.ok(controls.length>0&&Math.min(...controls)>=12.5,`control_too_small ${controls.join('/')}`);
assert.ok(label!==null&&label>=13,`form_label_too_small ${label}px`);
assert.ok(fine!==null&&fine>=12,`fine_print_too_small ${fine}px`);
assert.ok(summary.smallest>=10,`text_too_small ${summary.smallest}px`);
assert.equal(narrow.overflowX,false,'narrow_window_overflows');
assert.ok((narrow.smallestControl??0)>=12.5,`narrow_control_too_small ${narrow.smallestControl}px`);
console.log(`CHECK rail=${rail} controls>=${Math.min(...controls)} label=${label} fine=${fine} smallest=${summary.smallest}`);
console.log('RESULT measured type sizes');
await context.close();
await browser.close();

// F07Z-75 acceptance: WCAG AA contrast for the desktop shell, in both themes, across the roles
// that carry meaning (nav, tags, notices, record rows) plus two states the earlier sampling
// suite could not see: disabled controls and placeholder text.
//
//   D01_CONTRAST_BASE=http://127.0.0.1:5174 D01_CONTRAST_OUT=.private/contrast \
//     node --experimental-strip-types apps/web/tests/d01-desktop-contrast.mts
//
// It only measures what is on screen; a missing selector is skipped rather than failed, so the
// check stays usable as panes change. Disabled controls are reported but exempt from AA, exactly
// as WCAG 1.4.3 excludes inactive components - the report says so instead of silently passing.
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {mkdirSync,writeFileSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';

const root=resolve(import.meta.dirname,'../../..');
const out=resolve(process.env.D01_CONTRAST_OUT??join(root,'.private/f07z75/contrast'));
mkdirSync(out,{recursive:true});
const base=process.env.D01_CONTRAST_BASE??'http://127.0.0.1:5174';
const goal=process.env.D01_CONTRAST_GOAL??'';
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(p=>existsSync(p));
assert.ok(executable,'installed_browser_required');

const SELECTORS:[string,string][]=[
 ['.d-topbar-title','顶栏标题'],['.d-topbar-sub','顶栏副标题'],['.d-nav-label','侧栏分组'],
 ['.d-nav-item','侧栏入口'],['.d-topbar-btn','顶栏按钮'],['.d-nav-badge','侧栏角标'],
 ['.tag','标签'],['.d-pane-head h1','页面标题'],['.d-pane-head p','页面说明'],
 ['.fine-print','小字说明'],['.muted','次要文字'],['.d-num','数字'],
 ['.d-agent-card h3','角色卡标题'],['.d-agent-intro','角色卡说明'],['.d-agent-state','角色卡状态'],
 ['.d-empty-cta p','空态说明'],['.d-pipeline-status','流水线状态'],
 ['.d-session-item strong','场次标题'],['.d-session-item small','场次副标题'],
 ['.d-written-question','题目正文'],['.d-written-option','选项'],['.notice','提示条'],
 ['.card','卡片正文'],['.d-written-points li','参考要点'],['.history-item small','历史行副标题'],
 ['button.primary:enabled','主按钮'],['button.secondary:enabled','次按钮'],
 ['.d-nav-item[aria-current="page"]','当前入口'],
];

const AUDIT=(selectors:[string,string][])=>{
 const parse=(c:string)=>{
  const m=c.match(/rgba?\(([^)]+)\)/);if(!m)return null;
  const p=m[1].split(',').map(v=>parseFloat(v.trim()));
  return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1};
 };
 const lum=(c:any)=>{
  const f=(v:number)=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);};
  return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b);
 };
 const ratio=(a:any,b:any)=>{
  const l1=lum(a),l2=lum(b);
  return (Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05);
 };
 const blend=(fg:any,bg:any,alpha:number)=>({
  r:fg.r*alpha+bg.r*(1-alpha),g:fg.g*alpha+bg.g*(1-alpha),b:fg.b*alpha+bg.b*(1-alpha),a:1});
 const opacityOf=(el:Element)=>{
  let o=1,node:Element|null=el;
  while(node){o*=parseFloat(getComputedStyle(node).opacity||'1');node=node.parentElement;}
  return o;
 };
 const backgroundOf=(el:Element)=>{
  let node:Element|null=el;
  while(node){
   const bg=parse(getComputedStyle(node).backgroundColor);
   if(bg&&bg.a>=0.99)return bg;
   node=node.parentElement;
  }
  return parse(getComputedStyle(document.body).backgroundColor)??{r:255,g:255,b:255,a:1};
 };
 const push=(selector:string,label:string,cs:CSSStyleDeclaration,bg:any,op:number,rows:any[])=>{
  const fg=parse(cs.color);if(!fg)return;
  const visible=blend({...fg,a:1},bg,Math.min(1,fg.a*op));
  const size=parseFloat(cs.fontSize),weight=parseInt(cs.fontWeight,10)||400;
  const large=size>=18||(size>=14&&weight>=700);
  const r=ratio(visible,bg);
  rows.push({selector,label,ratio:Math.round(r*100)/100,size,weight,opacity:Math.round(op*100)/100,
   required:large?3:4.5,ok:r>=(large?3:4.5),disabled:false,
   fg:cs.color,bg:`rgb(${Math.round(bg.r)}, ${Math.round(bg.g)}, ${Math.round(bg.b)})`});
 };
 const rows:any[]=[];
 for(const [selector,label] of selectors){
  const el=document.querySelector(selector);if(!el)continue;
  push(selector,label,getComputedStyle(el as HTMLElement),backgroundOf(el),opacityOf(el),rows);
 }
 // Placeholder text carries meaning here and is invisible to a plain colour probe.
 for(const selector of ['textarea','input[type="password"]']){
  const el=document.querySelector(selector) as HTMLElement|null;if(!el)continue;
  const pseudo=getComputedStyle(el,'::placeholder');
  const fg=parse(pseudo.color);if(!fg)continue;
  const bg=backgroundOf(el);
  const r=ratio(blend({...fg,a:1},bg,fg.a),bg);
  rows.push({selector:selector+'::placeholder',label:'输入框占位',ratio:Math.round(r*100)/100,
   size:parseFloat(getComputedStyle(el).fontSize),weight:400,opacity:1,required:4.5,ok:r>=4.5,
   disabled:false,fg:pseudo.color,bg:`rgb(${Math.round(bg.r)}, ${Math.round(bg.g)}, ${Math.round(bg.b)})`});
 }
 // Inactive components are exempt from AA, but the measurement is still reported.
 const disabledBtn=document.querySelector('button.primary:disabled,button.secondary:disabled,button.text-button:disabled') as HTMLElement|null;
 let disabled=null;
 if(disabledBtn){
  const cs=getComputedStyle(disabledBtn);
  const fg=parse(cs.color),bg=parse(cs.backgroundColor);
  if(fg&&bg&&bg.a>=0.99){
   const r=ratio(blend({...fg,a:1},bg,Math.min(1,fg.a*parseFloat(cs.opacity||'1'))),bg);
   disabled={selector:'button:disabled',label:'禁用按钮（AA 豁免）',ratio:Math.round(r*100)/100,
    size:parseFloat(cs.fontSize),weight:parseInt(cs.fontWeight,10)||400,
    opacity:parseFloat(cs.opacity||'1'),required:null,ok:true,disabled:true,
    fg:cs.color,bg:cs.backgroundColor};
  }
 }
 return {rows,disabled};
};

const browser=await chromium.launch({executablePath:executable,headless:true});
const context=await browser.newContext({viewport:{width:1280,height:900}});
const page=await context.newPage();
const errors:string[]=[];
page.on('pageerror',e=>errors.push(String(e)));
const query=goal?`?shell=desktop&goal=${goal}`:'?shell=desktop';
await page.goto(`${base}/${query}`,{waitUntil:'domcontentloaded'});
await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});

async function run(theme:'light'|'dark'){
 await page.evaluate((t:string)=>localStorage.setItem('py-desktop-theme',t),theme);
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 const rows:any[]=[];let disabled:any=null;
 for(const pane of ['工作台','面试','笔试','设置']){
  if(pane==='笔试')await page.locator('.d-nav-item').filter({hasText:'笔试'}).first().click();
  else if(pane==='设置')await page.getByTestId('nav-settings').click();
  else if(pane==='面试')await page.locator('.d-nav-item').filter({hasText:'面试'}).first().click();
  else await page.getByTestId('agent-home').click();
  await page.waitForTimeout(700);
  const result=await page.evaluate(AUDIT,SELECTORS);
  rows.push(...result.rows);
  disabled=disabled??result.disabled;
 }
 return {theme,rows,disabled};
}

const light=await run('light');
const dark=await run('dark');
await page.screenshot({path:join(out,'dark.png')});
const failures=(rows:any[])=>rows.filter((r:any)=>!r.ok);
writeFileSync(join(out,'contrast.json'),JSON.stringify({light,dark,page_errors:errors},null,1));

const bad=[...failures(light.rows),...failures(dark.rows)];
console.log(JSON.stringify({
 checked:{light:light.rows.length,dark:dark.rows.length},
 disabled_sample:light.disabled,
 failures:bad.map((r:any)=>({theme:r.selector,ratio:r.ratio,need:r.required,size:r.size})),
 page_errors:errors,
},null,1));
assert.equal(bad.length,0,`contrast_below_aa: ${JSON.stringify(bad.slice(0,5))}`);
assert.equal(errors.length,0,`page_errors ${JSON.stringify(errors.slice(0,3))}`);
console.log(`RESULT passed ${light.rows.length + dark.rows.length} measurements`);
await browser.close();

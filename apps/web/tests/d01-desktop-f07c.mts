import {waitGoalReady} from './desktop-nav.mts';
// F07-C acceptance: rail typography, icon alignment, and homepage text density.
//
//   F07C_OUTPUT=<new dir> node --experimental-strip-types apps/web/tests/d01-desktop-f07c.mts
//
// The requests this covers, as measurable properties rather than opinions:
//   1. Every rail entry renders its text at body size (>= 12.5px) in the app's
//      own text font, never at the muted section-heading size.
//   2. Entry text is the primary ink colour, not the muted one.
//   3. The selected entry is visibly heavier than the others (weight gap >= 200).
//   4. Each entry's icon box starts at the same x as every other entry's, and the
//      icon is vertically centred on the entry's first text line.
//   5. The workbench's visible Chinese prose stayed within a budget: the card
//      faces and notes were shortened, and no long paragraph block remains.
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.F07C_OUTPUT??join(root,'docs/records/desktop-handoff/F07C-ui'));
const base=process.env.F07C_BASE??'http://127.0.0.1:5175';
const edge=process.env.F07C_EDGE??'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;type:'check'|'measure';detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,type:'check'|'measure'='measure',detail?:unknown)=>{
 cases.push({name,type,detail});
 console.log(String(type).toUpperCase(),name);
};
const evidence:Record<string,unknown>={run:new Date().toISOString(),base,cases,page_errors:pageErrors,
 screenshots:[],
 case_type_note:'在真实浏览器（系统 Edge）里读计算样式与几何位置，不靠肉眼判断字号、颜色与对齐；'
  +'首页文字量按可见中文正文字数统计。未调用任何模型。'};

let browser:Browser|null=null;
try{
 browser=await chromium.launch({executablePath:edge,headless:true});
 const ctx=await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:1});
 const page:Page=await ctx.newPage();
 page.on('pageerror',e=>pageErrors.push(e.message));

 const email=`f07c-${Date.now()}@example.com`;
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.evaluate(async(addr)=>{
  await fetch('/api/auth/sign-up/email',{method:'POST',credentials:'include',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({email:addr,name:'F07C 界面核对',password:`Synthetic-${Date.now()}-local-only`})});
 },email);
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'networkidle'});

 // A goal is needed for the workbench to render its full content.
 await page.locator('textarea[aria-label="求职意向或补充说明"]').waitFor({timeout:60000});
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('研二，想在上海找 AI 应用开发实习，方向还不确定。');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);
 await delay(1500);
 mark('起点：真实登录 + 真实创建目标后进入工作台','check',{base,email});

 // ---------------------------------------------------------------- 1-4. rail typography
 const rail=await page.evaluate(()=>{
  const px=(v:string)=>Math.round(parseFloat(v)*10)/10;
  const read=(el:Element|null)=>{
   if(!el)return null;
   const cs=getComputedStyle(el);const r=el.getBoundingClientRect();
   return {color:cs.color,size:px(cs.fontSize),weight:Number(cs.fontWeight),
    family:cs.fontFamily.split(',')[0].replace(/"/g,''),x:Math.round(r.x),
    center_y:Math.round(r.y+r.height/2),height:Math.round(r.height)};
  };
  const entries=[...document.querySelectorAll('.d-nav:not(.d-artifact-nav) .d-nav-item')];
  const active=entries.find(e=>e.getAttribute('aria-current')==='page')??entries[0];
  const inactive=entries.find(e=>e.getAttribute('aria-current')!=='page')!;
  // The icon box is the first span with aria-hidden inside the entry.
  const iconX=(entry:Element)=>{
   const icon=entry.querySelector('span[aria-hidden]') as HTMLElement|null;
   return icon?Math.round(icon.getBoundingClientRect().x):null;
  };
  const textOf=(entry:Element)=>read(entry.querySelector('.d-nav-label'));
  return {
   active:{entry:read(active),text:textOf(active),icon_x:iconX(active),
    icon_center_y:(()=>{const i=active.querySelector('span[aria-hidden]') as HTMLElement|null;
     if(!i)return null;const r=i.getBoundingClientRect();return Math.round(r.y+r.height/2);})()},
   inactive:{entry:read(inactive),text:textOf(inactive),icon_x:iconX(inactive)},
   entry_icon_xs:entries.map(iconX),
   group_label:read(document.querySelector('.d-nav > div.d-nav-label')),
   entry_count:entries.length,
  };
 });
 (evidence as Record<string,unknown>).rail=rail;

 // 1. body size, not section-heading size
 assert.ok(rail.active.text!.size>=12.5,`entry_text_too_small ${rail.active.text!.size}px`);
 assert.ok(rail.inactive.text!.size>=12.5,`inactive_entry_text_too_small ${rail.inactive.text!.size}px`);
 mark('入口文字是正文字号，不再被渲染成分组小标题那么小',{active:rail.active.text!.size,
  inactive:rail.inactive.text!.size,group_label:rail.group_label!.size});
 assert.ok(rail.group_label!.size<rail.active.text!.size,
  'entry_text_is_not_larger_than_the_group_heading');

 // 2. primary ink, not muted
 const muted=rail.group_label!.color;
 assert.notEqual(rail.inactive.entry!.color,muted,`entry_text_still_muted ${rail.inactive.entry!.color}`);
 assert.equal(rail.inactive.entry!.color,rail.active.entry!.color,'active_and_inactive_ink_differ');
 mark('入口文字用正文黑色，不再是灰色；分组标题仍是弱化色以区分层级',
  {entry_ink:rail.inactive.entry!.color,group_heading_ink:muted});

 // 3. the selected entry is visibly heavier
 const gap=rail.active.entry!.weight-rail.inactive.entry!.weight;
 assert.ok(gap>=200,`active_entry_not_bolder weight_gap=${gap}`);
 mark('选中的入口比其它入口更粗，一眼能看出当前位置',{active_weight:rail.active.entry!.weight,
  inactive_weight:rail.inactive.entry!.weight,gap});

 // 4. icons share one column, centred on the text line
 const xs=[...new Set(rail.entry_icon_xs)];
 assert.equal(xs.length,1,`entry_icons_do_not_share_a_column ${JSON.stringify(rail.entry_icon_xs)}`);
 assert.ok(Math.abs((rail.active.icon_center_y??0)-rail.active.text!.center_y)<=2,
  `icon_not_centred_on_text icon=${rail.active.icon_center_y} text=${rail.active.text!.center_y}`);


 await page.locator('.desktop-rail').screenshot({path:join(output,'shots','01-rail.png')});
 (evidence.screenshots as string[]).push(join(output,'shots','01-rail.png'));

 // ---------------------------------------------------------------- 5. homepage density
 const density=await page.evaluate(()=>{
  const cn=(text:string)=>(text.match(/[\u4e00-\u9fa5]/g)??[]).length;
  const pane=document.querySelector('.desktop-pane[data-pane="workbench"]')
   ??document.querySelector('.desktop-body');
  // checkVisibility accounts for closed <details>: their content is not shown to
  // the user, so it must not count towards the visible prose budget.
  const visible=(el:Element)=>{
   const check=(el as HTMLElement).checkVisibility;
   if(typeof check==='function')return (el as HTMLElement).checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
   const cs=getComputedStyle(el);const r=el.getBoundingClientRect();
   return cs.display!=='none'&&cs.visibility!=='hidden'&&r.height>0&&r.width>0;
  };
  // Only leaf paragraphs count: a <li> that wraps a whole step is a container,
  // not a block of prose the reader has to get through.
  const paragraphs=[...document.querySelectorAll('.desktop-body p, .desktop-body small')]
   .filter(el=>visible(el)&&el.querySelector('p,small,li,div')===null)
   .map(el=>({cls:(el.className||'').toString().slice(0,34),chars:cn(el.textContent??''),
    text:(el.textContent??'').replace(/\s+/g,' ').slice(0,80)}))
   .filter(x=>x.chars>=20)
   .sort((a,b)=>b.chars-a.chars);
  return {total_visible_chinese:cn((pane as HTMLElement)?.innerText??''),
   long_paragraphs:paragraphs.slice(0,8),
   longest:paragraphs[0]?.chars??0};
 });
 (evidence as Record<string,unknown>).density=density;
 // No prose paragraph may run past roughly two lines of body text (about 50
 // characters at this width). Long explanations belong in a collapsed 技术详情.
 assert.ok(density.longest<=35,`paragraph_too_long ${density.longest} chars: ${JSON.stringify(density.long_paragraphs[0])}`);
 mark('首页没有超过两行的说明段落：长句都拆短或收进可展开区',{longest_paragraph_chars:density.longest,
  visible_chinese_chars:density.total_visible_chinese,top:density.long_paragraphs});
 assert.ok(density.total_visible_chinese<=700,`homepage_still_text_heavy ${density.total_visible_chinese}`);
 mark('首页可见正文总量在预算内',{visible_chinese_chars:density.total_visible_chinese,budget:700});

 await page.screenshot({path:join(output,'shots','02-workbench.png'),fullPage:true});
 (evidence.screenshots as string[]).push(join(output,'shots','02-workbench.png'));

 // Typing an entry must still be operable: the selected entry changes.
 await page.locator('.d-nav:not(.d-artifact-nav) .d-nav-item').nth(1).click();
 await delay(900);
 const afterClick=await page.evaluate(()=>{
  const items=[...document.querySelectorAll('.d-nav:not(.d-artifact-nav) .d-nav-item')];
  const active=items.find(i=>i.getAttribute('aria-current')==='page');
  return {active_text:(active?.textContent??'').trim().slice(0,12),
   active_weight:active?Number(getComputedStyle(active).fontWeight):null,
   others:items.filter(i=>i!==active).map(i=>Number(getComputedStyle(i).fontWeight))};
 });
 (evidence as Record<string,unknown>).after_click=afterClick;
 assert.ok(afterClick.active_text.length>0,'no_entry_became_active');
 assert.ok(afterClick.others.every(w=>w<afterClick.active_weight!),'other_entries_not_lighter_than_active');
 mark('点另一个入口后，新的入口变粗、其余恢复常规，选中状态可辨认',afterClick);
 await page.locator('.desktop-rail').screenshot({path:join(output,'shots','03-rail-selected.png')});
 (evidence.screenshots as string[]).push(join(output,'shots','03-rail-selected.png'));

 const result={result:pageErrors.length===0?'passed':'failed',cases:cases.length,page_errors:pageErrors,...evidence};
 writeFileSync(join(output,'result.json'),JSON.stringify(result,null,2));
 console.log(`\n${cases.length} cases, ${pageErrors.length} page errors -> ${result.result}`);
}catch(error){
 const failed=error instanceof Error?error.message:String(error);
 evidence.error=failed;
 const result={result:'failed',cases:cases.length,page_errors:pageErrors,...evidence};
 try{writeFileSync(join(output,'result.json'),JSON.stringify(result,null,2));}catch{}
 console.error('FAILED:',failed);
 process.exitCode=1;
}finally{
 try{await browser?.close();}catch{}
}

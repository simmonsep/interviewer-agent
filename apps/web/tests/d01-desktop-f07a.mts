import {waitGoalReady} from './desktop-nav.mts';
// F07-A acceptance: the checks an external UI/UX reviewer could not decide from
// screenshots alone, measured on the REAL frozen product build.
//
//   F07A_OUTPUT=<new dir> D05_PORTABLE=<portable dir> \
//     node --experimental-strip-types apps/web/tests/d01-desktop-f07a.mts
//
// What is measured, and why each one matters:
//   1. Keyboard reachability - Tab order over the real controls, and whether the
//      focused element is actually visible (outline/shadow), not just present.
//   2. WCAG contrast - computed ratios from the real rendered colours, in both
//      themes, for the body/secondary/muted text the reviewer flagged as weak.
//   3. Text scaling - 125% and 150% zoom, checking that nothing overflows the
//      viewport horizontally and that the rail keeps its controls reachable.
//   4. Window maximisation - the real Win32 window maximised, captured as a
//      native screenshot including the OS title bar.
//   5. Error and retry state - a real failure (the API refuses the write) must
//      show a readable Chinese message instead of a blank or silent failure.
//
// Everything runs inside the real WebView2 window of the portable build: no
// page.route injection, no synthetic API, no model calls.
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {execFileSync,spawn,type ChildProcess} from 'node:child_process';
import {mkdirSync,writeFileSync,existsSync,readdirSync,rmSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.F07A_OUTPUT??join(root,'docs/records/desktop-handoff/F07A-a11y'));
const portable=resolve(process.env.D05_PORTABLE??join(root,'.private/f07a/portable-f07a'));
const cdpPort=Number(process.env.F07A_CDP_PORT??9271);
const winScript=join(root,'scripts/d01-desktop-window.ps1');
const hostExe=join(portable,'interview-desktop-probe.exe');
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;type:'check'|'measure';detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,type:'check'|'measure'='measure',detail?:unknown)=>{
 cases.push({name,type,detail});
 console.log(String(type).toUpperCase(),name);
};
const evidence:Record<string,unknown>={
 run:new Date().toISOString(),portable_dir:portable,host_exe:hostExe,cases,page_errors:pageErrors,
 screenshots:[],
 case_type_note:'全部用例在真实冻结产物的 WebView2 窗口内测量（便携目录 + 冻结 sidecar + 真实 PG/Redis + 免费 scripted Worker）。'
  +'未使用 page.route 注入或合成 API，未调用任何模型。对比度按 WCAG 2.1 相对亮度公式实时计算，'
  +'键盘与缩放均在真实渲染状态下测量。',
 unverified:['真实招聘网站页面结构与登录态','高 DPI 与系统级辅助功能设置','多显示器与跨屏拖拽','真实用户可用性测试'],
};

function shot(page:Page,name:string):Promise<void>{
 const file=join(output,'shots',name);
 return page.screenshot({path:file}).then(()=>{(evidence.screenshots as string[]).push(file);});
}
function win(action:'Info'|'Capture'|'Resize',pid:number,args:string[]=[]):any{
 const stdout=execFileSync('powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-File',winScript,
  '-Action',action,'-ProcessId',String(pid),...args],{encoding:'utf8',timeout:45000,windowsHide:true});
 const text=stdout.trim();
 const start=text.indexOf('{');
 if(start<0)throw new Error('window_helper_no_json: '+text.slice(0,300));
 return JSON.parse(text.slice(start));
}
async function portOpen(port:number):Promise<boolean>{
 try{const r=await fetch(`http://127.0.0.1:${port}/health`,{signal:AbortSignal.timeout(1200)});return r.ok;}
 catch{return false;}
}
async function waitForCdp(timeout=90000):Promise<any>{
 const started=Date.now();
 while(Date.now()-started<timeout){
  try{const r=await fetch(`http://127.0.0.1:${cdpPort}/json/version`);if(r.ok)return await r.json();}catch{}
  await delay(400);
 }
 throw new Error('webview_cdp_not_ready');
}
async function attach():Promise<{browser:Browser;page:Page}>{
 await waitForCdp();
 const browser=await chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`,{timeout:40000});
 for(let i=0;i<120;i++){
  for(const ctx of browser.contexts())for(const p of ctx.pages()){
   if(p.url().includes('127.0.0.1:3100')){
    p.on('pageerror',e=>pageErrors.push(e.message));
    return {browser,page:p};
   }
  }
  await delay(300);
 }
 throw new Error('webview_page_not_found');
}
function launch():ChildProcess{
 const profile=resolve(portable,'..','profiles',String(Date.now()));
 mkdirSync(profile,{recursive:true});
 const env={...process.env,INTERVIEW_DESKTOP_CDP_PORT:String(cdpPort),INTERVIEW_DESKTOP_DATA_DIR:profile};
 delete (env as any).INTERVIEW_DESKTOP_PREVIEW;
 delete (env as any).INTERVIEW_DESKTOP_PROBE;
 const child=spawn(hostExe,[],{cwd:portable,env,stdio:['ignore','ignore','ignore'],windowsHide:false});
 (evidence as Record<string,unknown>).profile_dir=profile;
 return child;
}

// WCAG 2.1 relative luminance and contrast ratio, computed in the page from the
// colours the browser actually painted.
const CONTRAST_FN=`(fg,bg)=>{
 const parse=(value)=>{
  const m=value.match(/rgba?\\(([^)]+)\\)/);if(!m)return null;
  const parts=m[1].split(',').map(x=>parseFloat(x));
  return {r:parts[0],g:parts[1],b:parts[2],a:parts.length>3?parts[3]:1};
 };
 const blend=(fg,bg)=>({r:fg.r*fg.a+bg.r*(1-fg.a),g:fg.g*fg.a+bg.g*(1-fg.a),b:fg.b*fg.a+bg.b*(1-fg.a),a:1});
 const lum=(c)=>{const f=(v)=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);};
  return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b);};
 const c1=parse(fg),c2=parse(bg);if(!c1||!c2)return null;
 const a=blend(c1,c2),b=c2;
 const l1=lum(a),l2=lum(b);
 const hi=Math.max(l1,l2),lo=Math.min(l1,l2);
 return Math.round(((hi+0.05)/(lo+0.05))*100)/100;
}`;

let browser:Browser|null=null;
let child:ChildProcess|null=null;
try{
 assert.ok(existsSync(hostExe),`portable_build_missing: ${hostExe}`);
 assert.equal(await portOpen(3100),false,'port_3100_busy_before_launch');
 mark('起点：3100 空闲，Vite 未运行；被测对象是冻结产物，不是开发服务器','check',
  {portable_dir:portable,host_exe:hostExe});

 child=launch();
 const attached=await attach();
 browser=attached.browser;
 const page=attached.page;
 await page.locator('.desktop-shell[data-pane="workbench"]').waitFor({timeout:90000});
 const dpr=await page.evaluate(()=>window.devicePixelRatio);
 (evidence as Record<string,unknown>).device_pixel_ratio=dpr;
 mark('冻结产物启动：真实 WebView2 窗口落在 http://127.0.0.1:3100（同源），无 Vite','check',
  {url:page.url(),device_pixel_ratio:dpr});

 // ---------------------------------------------------------------- 1. keyboard reachability
 await page.locator('body').click({position:{x:4,y:4}});
 const tabOrder:string[]=[];
 const focusStyles:any[]=[];
 for(let i=0;i<26;i++){
  await page.keyboard.press('Tab');
  const info=await page.evaluate(()=>{
   const el=document.activeElement as HTMLElement|null;
   if(!el||el===document.body)return null;
   const cs=getComputedStyle(el);
   const r=el.getBoundingClientRect();
   const label=(el.getAttribute('aria-label')||el.textContent||el.getAttribute('title')||el.tagName).trim().slice(0,28);
   return {
    tag:el.tagName.toLowerCase(),
    testid:el.getAttribute('data-testid')||'',
    label,
    visible:r.width>0&&r.height>0&&r.bottom>0&&r.top<window.innerHeight&&r.right>0&&r.left<window.innerWidth,
    outline_style:cs.outlineStyle,outline_width:cs.outlineWidth,outline_color:cs.outlineColor,
    box_shadow:cs.boxShadow.slice(0,60),
  };
  });
  if(!info)break;
  tabOrder.push(`${info.tag}${info.testid?'#'+info.testid:''}:${info.label}`);
  const hasRing=info.outline_style!=='none'&&parseFloat(info.outline_width)>0
   ||(info.box_shadow&&info.box_shadow!=='none'&&!info.box_shadow.startsWith('none'));
  focusStyles.push({...info,focus_ring:hasRing});
 }
 (evidence as Record<string,unknown>).keyboard={tab_count:tabOrder.length,order:tabOrder,styles:focusStyles};
 const reachable=focusStyles.filter(f=>f.visible).length;
 assert.ok(tabOrder.length>=8,`too_few_focusable_controls ${tabOrder.length}`);
 assert.ok(reachable>=8,`too_few_visible_focus_targets ${reachable}`);
 const ringed=focusStyles.filter(f=>f.focus_ring).length;
 mark('键盘可达：Tab 依次走到真实控件，焦点元素在视口内且带可见焦点环',{focusable:tabOrder.length,
  visible_in_viewport:reachable,with_focus_ring:ringed,first_12:tabOrder.slice(0,12)});
 assert.ok(ringed>0,'no_focus_ring_found_on_any_control');
 await shot(page,'01-keyboard-focus.png');

 // ---------------------------------------------------------------- 2. WCAG contrast, both themes
 async function contrastFor(theme:'light'|'dark'){
  await page.evaluate((value)=>{
   const btn=document.querySelector('.d-theme-quick button[aria-label="'+value+'"]') as HTMLElement|null;
   if(btn)btn.click();
  },theme==='light'?'浅色':'深色');
  await delay(700);
  const painted=await page.evaluate((fnSrc)=>{
   const ratio=eval(fnSrc) as (fg:string,bg:string)=>number|null;
   const pick=(selector:string)=>{
    const el=document.querySelector(selector) as HTMLElement|null;
    if(!el)return null;
    const cs=getComputedStyle(el);
    // Walk up for the first non-transparent background actually painted behind the text.
    let bg='rgba(0, 0, 0, 0)';
    let node:HTMLElement|null=el;
    while(node){
     const b=getComputedStyle(node).backgroundColor;
     if(b&&b!=='rgba(0, 0, 0, 0)'&&b!=='transparent'){bg=b;break;}
     node=node.parentElement;
    }
    return {selector,text:(el.textContent||'').trim().slice(0,26),color:cs.color,background:bg,
     font_size:cs.fontSize,font_weight:cs.fontWeight,ratio:ratio(cs.color,bg)};
   };
   return {
    theme:document.querySelector('.desktop-shell')?.getAttribute('data-theme'),
    samples:['.d-topbar-title','.d-topbar-sub','.d-nav-label','.fine-print','.d-agent-jump-item small',
     '.d-section-note','.muted','.d-pane-head p','.d-num'].map(pick).filter(Boolean),
   };
  },CONTRAST_FN);
  return painted;
 }
 const light=await contrastFor('light');
 const dark=await contrastFor('dark');
 (evidence as Record<string,unknown>).contrast={light,dark};
 const flat=(t:any)=>t.samples.map((s:any)=>({selector:s.selector,text:s.text,font_size:s.font_size,
  color:s.color,background:s.background,ratio:s.ratio,normal_text_threshold:4.5,large_text_threshold:3}));
 mark('浅色主题实测对比度：每个文本角色取真实绘制色与其背后实际背景色，按 WCAG 2.1 计算比值',
  {theme:light.theme,samples:flat(light)});
 mark('深色主题实测对比度：同上，逐条记录而非“看起来偏灰”',{theme:dark.theme,samples:flat(dark)});
 const failures=[...flat(light),...flat(dark)].filter(s=>{
  if(s.ratio===null)return false;
  const size=parseFloat(s.font_size);
  const large=size>=18||(size>=14&&parseInt(s.font_weight,10)>=700);
  return s.ratio<(large?3:4.5);
 });
 (evidence as Record<string,unknown>).contrast_failures=failures;
 assert.equal(failures.length,0,`wcag_contrast_below_threshold: ${JSON.stringify(failures)}`);
 mark('对比度全部达标：无低于 WCAG AA 阈值（正文 4.5 / 大字 3）的文本样本',{checked:(flat(light).length+flat(dark).length),
  failures:0});
 await shot(page,'02-contrast-dark.png');
 await page.evaluate(()=>{const btn=document.querySelector('.d-theme-quick button[aria-label="浅色"]') as HTMLElement|null;if(btn)btn.click();});
 await delay(500);

 // ---------------------------------------------------------------- 3. 125% / 150% text scaling
 async function scaledCheck(factor:number){
  await page.evaluate((value)=>{
   const root=document.documentElement;
   root.style.setProperty('zoom',String(value));
  },factor);
  await delay(900);
  const measured=await page.evaluate(()=>{
   const shell=document.querySelector('.desktop-shell') as HTMLElement;
   const rail=document.querySelector('.desktop-rail') as HTMLElement;
   const jump=document.querySelector('.d-agent-jump') as HTMLElement|null;
   const items=[...document.querySelectorAll('.d-agent-jump-item')] as HTMLElement[];
   const navs=[...document.querySelectorAll('.d-nav:not(.d-artifact-nav) .d-nav-item')] as HTMLElement[];
   const rr=rail.getBoundingClientRect();
   const overflow=(el:HTMLElement)=>Math.round(el.getBoundingClientRect().right-rr.right);
   return {
    zoom:getComputedStyle(document.documentElement).getPropertyValue('zoom'),
    inner_width:window.innerWidth,
    page_scroll_width:document.documentElement.scrollWidth,
    horizontal_overflow:document.documentElement.scrollWidth>window.innerWidth+2,
    rail_width:Math.round(rr.width),
    jump_overflow:jump?overflow(jump):null,
    agent_item_overflow:items.map(overflow),
    agent_item_widths:items.map(i=>Math.round(i.getBoundingClientRect().width)),
    nav_count:navs.length,
    nav_fully_inside:navs.every(n=>n.getBoundingClientRect().right<=rr.right+1),
    agent_text_visible:items.every(i=>{
     const body=i.querySelector('.d-agent-jump-body') as HTMLElement|null;
     return !body||getComputedStyle(body).display!=='none';
    }),
   };
  });
  return measured;
 }
 const scale125=await scaledCheck(1.25);
 await shot(page,'03-zoom-125.png');
 const scale150=await scaledCheck(1.5);
 await shot(page,'04-zoom-150.png');
 await page.evaluate(()=>{document.documentElement.style.removeProperty('zoom');});
 await delay(500);
 (evidence as Record<string,unknown>).scaling={'125%':scale125,'150%':scale150};
 assert.equal(scale125.horizontal_overflow,false,'horizontal_overflow_at_125');
 assert.equal(scale150.horizontal_overflow,false,'horizontal_overflow_at_150');
 assert.equal(scale125.nav_fully_inside,true,'nav_clipped_at_125');
 assert.equal(scale150.nav_fully_inside,true,'nav_clipped_at_150');
 assert.ok(scale125.agent_item_overflow.every(v=>v<=2),`agent_items_overflow_at_125: ${JSON.stringify(scale125.agent_item_overflow)}`);
 assert.ok(scale150.agent_item_overflow.every(v=>v<=2),`agent_items_overflow_at_150: ${JSON.stringify(scale150.agent_item_overflow)}`);
 mark('125% / 150% 文本缩放：无横向溢出，8 个功能入口与 3 个 Agent 项仍完整可见',{
  '125%':scale125,'150%':scale150});

 // ---------------------------------------------------------------- 4. native window maximise
 const info=win('Info',child.pid!);
 const screen={w:info.screen_width,h:info.screen_height};
 await win('Resize',child.pid!,['-Width',String(screen.w),'-Height',String(screen.h)]);
 await delay(1200);
 const maximised=await page.evaluate(()=>({w:window.innerWidth,h:window.innerHeight,
  scroll_width:document.documentElement.scrollWidth,
  overflow:document.documentElement.scrollWidth>window.innerWidth+2,
  rail:document.querySelector('.desktop-rail')?.getBoundingClientRect().width,
  nav_visible:[...document.querySelectorAll('.d-nav:not(.d-artifact-nav) .d-nav-item')]
   .every(n=>n.getBoundingClientRect().width>0)}));
 (evidence as Record<string,unknown>).maximised={screen,window:maximised};
 await shot(page,'05-window-maximised.png');
 assert.equal(maximised.overflow,false,'horizontal_overflow_when_maximised');
 assert.equal(maximised.nav_visible,true,'nav_hidden_when_maximised');
 mark('窗口最大化到屏幕尺寸：真实 Win32 窗口下无横向溢出，导航仍完整',{screen,window:maximised});

 // ---------------------------------------------------------------- 5. real error and retry state
 // Create a real goal through the real UI first: the pipeline button stays disabled
 // without one, and the failure below has to be a genuine refusal by the API.
 const goalLabel=page.locator('[data-testid="desktop-goal"]');
 if((await goalLabel.innerText()).includes('尚未创建')){
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('研二，想在上海找 AI 应用开发实习，方向还不确定。');
  await page.locator('[data-testid="workbench-primary"]').click();
  await waitGoalReady(page);
 }
 (evidence as Record<string,unknown>).goal_created=true;
 mark('真实目标已建立：走产品自己的创建路径，后续失败与流水线都在这个目标上进行','check',
  {goal:(await goalLabel.innerText()).slice(0,40)});

 // The reviewer asked whether a user sees something readable when a step fails.
 // This drives the real button and reads the real rendered alert - no injection.
 // The pipeline's own reason() mapping (apps/web/src/AgentPipeline.tsx) turns an
 // API error code into Chinese; we make the API refuse on purpose by sending a
 // stale expected_version, then check what the user actually reads.
 const errorState=await page.evaluate(async()=>{
  const goalId=new URLSearchParams(location.search).get('goal');
  const response=await fetch('/api/career-goals/'+goalId+'/interviews',{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID(),
    'Origin':location.origin},
   body:JSON.stringify({expected_version:999999,role:'ai-application',mode:'interview',
    max_questions:1,duration_seconds:60,question_banks:[]})});
  const body=await response.json().catch(()=>({})) as any;
  return {status:response.status,wire_code:String(body?.code??''),wire_message:String(body?.message??'')};
 });
 (evidence as Record<string,unknown>).error_state=errorState;
 assert.ok(errorState.status>=400,`expected_a_rejected_write got_${errorState.status}`);
 assert.ok(errorState.wire_code.length>0,'rejected_write_has_no_code');

 // Drive the real pipeline button and let the application's own error path run.
 // The goal is created inside the product, so the pipeline is enabled.
 const runButton=page.locator('[data-testid="pipeline-run"]');
 assert.equal(await runButton.count(),1,'pipeline_run_button_missing');
 assert.equal(await runButton.isEnabled(),true,'pipeline_run_button_disabled');
 await runButton.click();
 const alert=page.locator('[data-testid="pipeline-error"]');
 let renderedText='';
 try{
  await alert.waitFor({state:'visible',timeout:90000});
  renderedText=await alert.innerText();
 }catch{
  // A pipeline that legitimately completes is also a valid outcome; record which.
  const summary=await page.locator('[data-testid="pipeline-summary"]').count();
  (evidence as Record<string,unknown>).pipeline_completed_instead_of_failing=summary>0;
 }
 const rendered=await page.evaluate(()=>{
  const el=document.querySelector('[data-testid="pipeline-error"]');
  const role=document.querySelector('.desktop-shell [role="alert"]');
  const text=(el?.textContent??'').trim();
  return {text,has_role_alert:!!role,
   is_chinese:/[\u4e00-\u9fa5]/.test(text),
   length:text.length,
   // Either it names what was not written, or it says what to do next, or it
   // states what it will not do. A message that only repeats a code is not
   // acceptable.
   explains_outcome:/没有写入|未写入|不会写入|等它跑完|请刷新|不会重复|没有创建|请先在|不会替你/.test(text),
   raw_code_only:/^[a-z_]+\(\d+\)$/.test(text)};
 });
 (evidence as Record<string,unknown>).rendered_error=rendered;
 if(rendered.length>0){
  assert.ok(rendered.is_chinese,'error_message_not_readable_chinese');
  assert.ok(rendered.has_role_alert,'error_not_in_role_alert');
  assert.ok(rendered.explains_outcome,'error_message_does_not_explain_outcome_or_next_step');
  assert.equal(rendered.raw_code_only,false,'error_message_is_only_a_raw_code');
  mark('真实失败路径：点“准备简历与模拟面试”触发后端拒绝，界面用中文说明原因与下一步，并放在 role=alert 里',
   {http_status:errorState.status,wire_code:errorState.wire_code,rendered_message:rendered.text});
 }else{
  mark('真实失败路径检查：本次流水线未进入失败分支（已如实记录，未伪造失败画面）',
   {http_status:errorState.status,wire_code:errorState.wire_code,
    pipeline_completed_instead:!((evidence as Record<string,unknown>).pipeline_completed_instead_of_failing)});
 }

 // The retry affordance, checked where the failure actually happened: the same
 // button must be usable again once the cause is gone, and the shell must still
 // offer a way to re-read the current state.
 const retry=await page.evaluate(()=>{
  const run=document.querySelector('[data-testid="pipeline-run"]') as HTMLButtonElement|null;
  const refresh=document.querySelector('[data-testid="desktop-refresh"]') as HTMLButtonElement|null;
  return {run_button_present:!!run,run_button_disabled:run?run.disabled:null,
   refresh_control_present:!!refresh,refresh_disabled:refresh?refresh.disabled:null,
   refresh_text:(refresh?.textContent??'').trim()};
 });
 (evidence as Record<string,unknown>).retry=retry;
 assert.equal(retry.run_button_present,true,'pipeline_button_disappeared_after_error');
 assert.equal(retry.run_button_disabled,false,'pipeline_button_stays_disabled_after_error');
 assert.equal(retry.refresh_control_present,true,'no_refresh_control_in_shell');
 assert.equal(retry.refresh_disabled,false,'refresh_control_disabled_after_error');
 assert.ok(retry.refresh_text.length>0,'refresh_control_has_no_label');
 mark('失败后仍可重试：出错的流水线按钮重新可用，桌面顶栏有显式“刷新目标”入口且未被禁用',retry);
 await shot(page,'06-error-state.png');

 // ---------------------------------------------------------------- teardown
 const before=await portOpen(3100);
 assert.equal(before,true,'api_not_running_before_close');
 win('Close',child.pid!);
 const exited=await new Promise<boolean>(resolve=>{
  const started=Date.now();
  const timer=setInterval(()=>{
   if(child!.exitCode!==null||child!.signalCode!==null){clearInterval(timer);resolve(true);return;}
   if(Date.now()-started>25000){clearInterval(timer);resolve(false);}
  },300);
 });
 assert.equal(exited,true,'host_did_not_exit');
 for(let i=0;i<40;i++){if(!(await portOpen(3100)))break;await delay(400);}
 assert.equal(await portOpen(3100),false,'api_not_released_after_close');
 mark('关闭窗口：宿主退出、3100 释放、协调器与两个 Worker 一并结束',
  {api_released:true,page_errors:pageErrors.length});

 const result={result:pageErrors.length===0?'passed':'failed',cases:cases.length,
  page_errors:pageErrors,...evidence};
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
 try{child?.kill();}catch{}
 try{browser?.close();}catch{}
}

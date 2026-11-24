// F07-B acceptance: the undecorated desktop window and the app's own window
// controls.
//
//   F07B_OUTPUT=<new dir> D05_PORTABLE=<portable dir> \
//     node --experimental-strip-types apps/web/tests/d01-desktop-f07b.mts
//
// What this proves, on the REAL frozen build:
//   1. The window has no native Windows title bar. The app draws its own
//      chrome, so the window frame must not add a second one around it.
//   2. The client area reaches the top of the window (no reserved caption row).
//   3. The app's own minimise / maximise / close buttons exist and work:
//      minimise really minimizes, maximise toggles, close really ends the
//      process and releases the local stack.
//   4. The rail is the app's outermost edge: it carries no brand block and no
//      framed panel around the three Agent entries.
//
// Everything runs inside the real WebView2 window of the portable build. No
// page.route injection, no synthetic API, no model calls.
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {spawn,execFileSync,type ChildProcess} from 'node:child_process';
import {mkdirSync,rmSync,writeFileSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.F07B_OUTPUT??join(root,'docs/records/desktop-handoff/F07B-window'));
const portable=resolve(process.env.D05_PORTABLE??join(root,'.private/f07a/portable-f07b'));
const cdpPort=Number(process.env.F07B_CDP_PORT??9319);
const winScript=join(root,'scripts/d01-desktop-window.ps1');
const hostExe=join(portable,'interview-desktop-probe.exe');
const profile=join(portable,'.f07b-profile');
mkdirSync(join(output,'shots'),{recursive:true});
rmSync(profile,{recursive:true,force:true});mkdirSync(profile,{recursive:true});

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
  +'窗口状态通过 Win32 读取（矩形、可见性、最大化态），不是靠截图判断。未调用任何模型。',
};

/** Read the real window rectangle and state for a process. */
function winInfo(pid:number):any{
 const stdout=execFileSync('powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-File',winScript,
  '-Action','Info','-ProcessId',String(pid)],{encoding:'utf8',timeout:45000,windowsHide:true});
 const text=stdout.trim();
 const start=text.indexOf('{');
 if(start<0)throw new Error('window_helper_no_json: '+text.slice(0,200));
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

let browser:Browser|null=null;
let child:ChildProcess|null=null;
try{
 assert.ok(existsSync(hostExe),`portable_build_missing: ${hostExe}`);
 assert.equal(await portOpen(3100),false,'port_3100_busy_before_launch');
 mark('起点：3100 空闲，未运行 Vite；被测对象是冻结产物的真实窗口','check',{portable_dir:portable});

 child=spawn(hostExe,[],{cwd:portable,windowsHide:false,
  env:{...process.env,INTERVIEW_DESKTOP_CDP_PORT:String(cdpPort),INTERVIEW_DESKTOP_DATA_DIR:profile}});
 const attached=await attach();
 browser=attached.browser;
 const page=attached.page;
 await page.locator('.desktop-shell').waitFor({timeout:90000});

 // ---------------------------------------------------------------- 1. no native title bar
 const chrome=await page.evaluate(()=>({
  outer_h:window.outerHeight,inner_h:window.innerHeight,
  outer_w:window.outerWidth,inner_w:window.innerWidth,
  chrome_h:window.outerHeight-window.innerHeight,
  chrome_w:window.outerWidth-window.innerWidth}));
 const native=winInfo(child.pid);
 (evidence as Record<string,unknown>).window={chrome,native};
 // The client area must reach the window edge: a native caption would reserve
 // a row above the content.
 assert.ok(chrome.chrome_h<=2,`native_title_bar_still_present chrome_h=${chrome.chrome_h}`);
 mark('窗口没有原生标题栏：客户区一直顶到窗口上沿，窗口外不再多套一层',
  {outer_h:chrome.outer_h,inner_h:chrome.inner_h,chrome_h:chrome.chrome_h,
   native_window:{w:native.width,h:native.height}});
 await page.screenshot({path:join(output,'shots','01-undecorated.png')});

 // ---------------------------------------------------------------- 2. the app's own controls
 await delay(1200);
 const controls=await page.locator('.d-window-controls button').evaluateAll(bs=>bs.map(b=>({
  testid:b.getAttribute('data-testid'),label:b.getAttribute('aria-label'),title:b.getAttribute('title')})));
 (evidence as Record<string,unknown>).controls=controls;
 assert.equal(controls.length,3,'window_controls_missing');
 assert.deepEqual(controls.map(c=>c.testid),['win-minimize','win-maximize','win-close'],'window_control_ids_changed');
 assert.ok(controls.every(c=>!!c.label&&!!c.title),'window_control_has_no_accessible_name');
 mark('应用自带三个窗口按钮，各有可访问名称与悬停说明',{controls});

 // ---------------------------------------------------------------- 3. minimise / maximise
 const beforeMin=winInfo(child.pid);
 await page.locator('[data-testid="win-minimize"]').click();
 await delay(1500);
 const afterMin=winInfo(child.pid);
 const apiStillUp=await portOpen(3100);
 assert.equal(apiStillUp,true,'api_stopped_when_window_minimized');
 assert.equal(afterMin.iconic,true,'window_not_actually_minimized');
 (evidence as Record<string,unknown>).minimize={before:beforeMin.width+'x'+beforeMin.height,
  after:afterMin.width+'x'+afterMin.height,iconic:afterMin.iconic};
 mark('最小化按钮真实生效：窗口进入最小化态，而本机服务继续运行',{before:beforeMin.width+'x'+beforeMin.height,
  after:afterMin.width+'x'+afterMin.height,iconic:afterMin.iconic,api_still_running:apiStillUp});

 // Pressing maximise while the window sits minimised must restore it, not just
 // set a flag. `zoomed` is the OS's own answer, so it cannot be fooled by a
 // window that merely looks large. (A maximised window covers the work area, not
 // the whole screen, so its height is legitimately smaller than the screen.)
 await page.locator('[data-testid="win-maximize"]').click();
 await delay(1800);
 const afterMax=winInfo(child.pid);
 const maxButton=await page.locator('[data-testid="win-maximize"]').evaluate(el=>({
  maximized:el.getAttribute('data-maximized'),label:el.getAttribute('aria-label'),
  text:(el.textContent??'').trim()}));
 (evidence as Record<string,unknown>).maximize={width:afterMax.width,height:afterMax.height,
  zoomed:afterMax.zoomed,button:maxButton};
 assert.equal(afterMax.zoomed,true,'window_did_not_maximize_after_minimize');
 assert.equal(afterMax.iconic,false,'window_still_minimized_after_maximize');
 mark('最小化后再点最大化：窗口真的回到前台并最大化（系统 IsZoomed 为真）',
  {width:afterMax.width,height:afterMax.height,zoomed:afterMax.zoomed});
 assert.equal(maxButton.maximized,'true','maximize_button_does_not_show_maximized_state');
 assert.equal(maxButton.label,'还原窗口','maximize_button_does_not_offer_restore');
 mark('按钮自身反映窗口状态：最大化后图标与提示变为「还原窗口」，点了必有可见变化',maxButton);

 await page.locator('[data-testid="win-maximize"]').click();
 await delay(1800);
 const afterRestore=winInfo(child.pid);
 const restoreButton=await page.locator('[data-testid="win-maximize"]').evaluate(el=>({
  maximized:el.getAttribute('data-maximized'),label:el.getAttribute('aria-label')}));
 (evidence as Record<string,unknown>).restore={width:afterRestore.width,height:afterRestore.height,
  zoomed:afterRestore.zoomed,button:restoreButton};
 assert.equal(afterRestore.zoomed,false,'window_did_not_restore');
 assert.equal(afterRestore.width,beforeMin.width,'restore_did_not_return_to_the_previous_width');
 assert.equal(restoreButton.maximized,'false','restore_button_state_not_updated');
 mark('再点一次还原：窗口回到原来的尺寸，按钮回到「最大化」形态',
  {width:afterRestore.width,height:afterRestore.height,previous:beforeMin.width+'x'+beforeMin.height});

 // ---------------------------------------------------------------- 3b. double-click the sidebar
 // F07Z-122: a user double-clicks the sidebar expecting the window to maximise, the way a
 // title bar behaves, and every rail entry is a navigation button - exactly the part that a
 // "skip interactive descendants" rule would leave dead. Measured through the OS's own
 // IsZoomed, so a window that merely looks large cannot pass. The double-click is a toggle:
 // the second one has to bring the window back to the size it had before.
 const railEntry=page.locator('.desktop-rail .d-nav-item').first();
 await railEntry.dblclick();
 await delay(1800);
 const afterRailMax=winInfo(child.pid);
 assert.equal(afterRailMax.zoomed,true,'rail_double_click_did_not_maximize');
 mark('双击侧栏条目（最密集的部分是导航按钮）：窗口真的最大化，系统 IsZoomed 为真',
  {zoom:afterRailMax.zoomed,width:afterRailMax.width,height:afterRailMax.height});
 await railEntry.dblclick();
 await delay(1800);
 const afterRailRestore=winInfo(child.pid);
 assert.equal(afterRailRestore.zoomed,false,'rail_double_click_did_not_restore');
 assert.equal(afterRailRestore.width,beforeMin.width,'rail_restore_did_not_return_to_the_previous_width');
 mark('再双击一次回到原尺寸：侧栏双击是开关，不是只能放大',
  {width:afterRailRestore.width,height:afterRailRestore.height,previous:beforeMin.width+'x'+beforeMin.height});

 // ---------------------------------------------------------------- 3c. double-click the top bar over the omnibox
 // F07Z-133: the omnibox is the largest part of the bar, and the old rule skipped every form and
 // input, so most of the bar did not maximise. The narrower exception (buttons only) must cover
 // it without stealing the buttons' own double-click.
 const omniBox=await page.locator('.desktop-topbar input').boundingBox();
 const omniX=omniBox!.x+omniBox!.width/2, omniY=omniBox!.y+omniBox!.height/2;
 await page.mouse.dblclick(omniX,omniY);
 await delay(1800);
 const afterOmniMax=winInfo(child.pid);
 assert.equal(afterOmniMax.zoomed,true,'topbar_omnibox_double_click_did_not_maximize');
 mark('双击顶栏输入框（栏内最大区域）：窗口最大化，系统 IsZoomed 为真',
  {zoom:afterOmniMax.zoomed,width:afterOmniMax.width,height:afterOmniMax.height});
 await page.mouse.dblclick(omniX,omniY);
 await delay(1800);
 const afterOmniRestore=winInfo(child.pid);
 assert.equal(afterOmniRestore.zoomed,false,'topbar_omnibox_double_click_did_not_restore');
 mark('再双击一次恢复原尺寸',{width:afterOmniRestore.width,height:afterOmniRestore.height});
 // A button keeps its own double-click: the rail toggle must not maximise the window.
 const toggleBox=await page.locator('[data-testid="rail-toggle"]').boundingBox();
 await page.mouse.dblclick(toggleBox!.x+toggleBox!.width/2,toggleBox!.y+toggleBox!.height/2);
 await delay(1200);
 const afterToggle=winInfo(child.pid);
 assert.equal(afterToggle.zoomed,false,'topbar_button_double_click_must_not_maximize');
 mark('双击顶栏按钮（侧栏开关）：不最大化，按钮保留自己的双击语义',{zoomed:afterToggle.zoomed});


 // ---------------------------------------------------------------- 4. the rail is the outermost edge
 const rail=await page.evaluate(()=>{
  const shell=document.querySelector('.desktop-shell') as HTMLElement;
  const aside=document.querySelector('.desktop-rail') as HTMLElement;
  const jump=document.querySelector('.d-agent-jump') as HTMLElement;
  const cs=(el:HTMLElement|null)=>el?getComputedStyle(el):null;
  const jumpStyle=cs(jump);
  return {
   brand_blocks:document.querySelectorAll('.desktop-rail .d-brand').length,
   jump_border:jumpStyle?jumpStyle.borderTopWidth+' '+jumpStyle.borderTopStyle:'absent',
   jump_background:jumpStyle?jumpStyle.backgroundColor:'absent',
   rail_border_right:cs(aside)?cs(aside)!.borderRightWidth+' '+cs(aside)!.borderRightStyle:'absent',
   rail_left_offset:Math.round(aside.getBoundingClientRect().left),
   shell_left_offset:Math.round(shell.getBoundingClientRect().left),
   first_rail_label:document.querySelector('.desktop-rail .d-nav-label')?.textContent?.trim()??'',
   agent_entries:document.querySelectorAll('.d-agent-jump-item').length,
   functional_entries:document.querySelectorAll('.d-nav:not(.d-artifact-nav) .d-nav-item').length,
  };
 });
 (evidence as Record<string,unknown>).rail=rail;
 assert.equal(rail.brand_blocks,0,'rail_still_has_a_brand_block');
 // F07Z-72 removed the block itself, so there is no frame left to measure. The old
 // assertion demanded "0px none" from an element that no longer exists, which made this
 // section red from F07Z-72 onward; asserting absence is the intent that survived.
 assert.equal(rail.jump_border,'absent','agent_group_block_came_back');
 assert.equal(rail.rail_left_offset,rail.shell_left_offset,'rail_is_not_at_the_window_edge');
 assert.equal(rail.agent_entries,0,'agent_entry_block_removed');
 // F07Z-81: 产物 moved out of the top bar and became a rail entry at the end of 工作,
 // which takes the functional entries from eight to nine. This count was not updated then,
 // so it has been failing on the current rail ever since.
 assert.equal(rail.functional_entries,9,'nine_functional_entries_missing');
  // F07Z-72: the rail's Agent block was removed, so the rail now starts with the 工作 group.
  assert.equal(rail.first_rail_label,'工作','rail_does_not_start_with_the_work_group');
 mark('边栏就是最外层：没有品牌块，没有 Agent 入口块（F07Z-72），侧栏直接顶到窗口左缘',
  {rail_left:rail.rail_left_offset,agent_group_border:rail.jump_border,first_label:rail.first_rail_label});

 // ---------------------------------------------------------------- 5. close really closes
 await page.locator('[data-testid="win-close"]').click({timeout:20000});
 let exited=false;
 for(let i=0;i<60;i++){if(child.exitCode!==null||child.signalCode!==null){exited=true;break;}await delay(300);}
 assert.equal(exited,true,'window_did_not_close_through_the_app_button');
 for(let i=0;i<40;i++){if(!(await portOpen(3100)))break;await delay(400);}
 assert.equal(await portOpen(3100),false,'api_not_released_after_close');
 mark('关闭按钮真实生效：宿主退出，3100 释放，本机服务一并结束',{api_released:true});

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
 try{child?.kill();}catch{}
 try{browser?.close();}catch{}
 try{rmSync(profile,{recursive:true,force:true});}catch{}
}

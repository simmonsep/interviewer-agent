import {waitGoalReady} from './desktop-nav.mts';
// F01 appearance acceptance: the desktop shell has one light/dark pair driven by the
// settings pane, defaults to light, survives a reload, and follows the OS on `auto`.
// The web entry must stay untouched. Real stack, no injection.
//
//   D01_OUTPUT=<new dir> node apps/web/tests/d01-desktop-theme.mts
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/desktop-handoff/F01-theme'));
const base=process.env.D01_BASE_URL??'http://127.0.0.1:5174';
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
assert.ok(executable,'installed_browser_required');
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;type:'real';detail?:unknown}[]=[];
const mark=(name:string,detail?:unknown)=>cases.push({name,type:'real',detail});
const pageErrors:string[]=[];
const evidence:{shots:string[];failures:string[]}={shots:[],failures:[]};
const account={email:`f01-theme-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID(),name:'外观验收学员'};

let browser:Browser;
let active:Page|null=null;
const shot=async(page:Page,name:string)=>{await page.screenshot({path:join(output,'shots',name)});evidence.shots.push(name);};

async function appearance(page:Page){
 return page.evaluate(()=>{
  const shell=document.querySelector('.desktop-shell') as HTMLElement|null;
  if(!shell)return null;
  const style=getComputedStyle(shell);
  return {
   theme:shell.dataset.theme??null,
   background:style.backgroundColor,
   color:style.color,
   stored:localStorage.getItem('py-desktop-theme'),
  };
 });
}
async function nums(page:Page){
 return page.evaluate(()=>{
  const nodes=[...document.querySelectorAll('.d-num')].map(el=>{
   const style=getComputedStyle(el);
   return {family:style.fontFamily.split(',')[0].replace(/"/g,''),size:style.fontSize,weight:style.fontWeight};
  });
  return {
   count:nodes.length,
   families:[...new Set(nodes.map(node=>node.family))],
   weights:[...new Set(nodes.map(node=>node.weight))].sort(),
   fonts:{
    geist:document.fonts.check('13px Geist'),
    monoLg:document.fonts.check('600 21px "Geist Mono"'),
    monoSm:document.fonts.check('500 11.5px "Geist Mono"'),
    loaded:[...document.fonts].filter(face=>face.status==='loaded').map(face=>`${face.family} ${face.weight}`),
   },
  };
 });
}

 try{
 const browser2=await chromium.launch({executablePath:executable,headless:true});
 browser=browser2;
 const context=await browser.newContext({viewport:{width:1280,height:860}});
 const page=await context.newPage();active=page;
 page.on('pageerror',error=>pageErrors.push(error.message));

 // ---------------------------------------------------------------- 1. web entry untouched
 await page.goto(base,{waitUntil:'domcontentloaded'});
 await page.locator('.auth-layout').waitFor({timeout:30000});
 assert.equal(await page.locator('.desktop-shell').count(),0,'web_entry_has_desktop_shell');
 assert.equal(await page.evaluate(()=>document.documentElement.getAttribute('data-theme')),null,'theme_leaked_to_document_root');
 await shot(page,'01-web-entry.png');
 mark('Web 入口：不出现桌面外壳，主题不写到 documentElement（配色只作用于桌面）',{});

 // ---------------------------------------------------------------- 2. desktop defaults to light
 // F05B 免登直入：需要登录页的套件先放一次性标志（App 读取后清除）。
 await page.goto(`${base}`,{waitUntil:'domcontentloaded'});
 await page.evaluate(()=>{try{localStorage.setItem('py-desktop-login','1');}catch{}});
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-auth-card form').waitFor({timeout:30000});
 const loginAppearance=await appearance(page);
 assert.equal(loginAppearance?.theme,'light','login_page_not_light_by_default');
 mark('桌面登录页也带主题（默认浅色），不会先闪一下错的主题',loginAppearance);

 await page.getByRole('button',{name:'第一次来？创建账号'}).click();
 await page.locator('input[name="name"]').fill(account.name);
 await page.locator('input[name="email"]').fill(account.email);
 await page.locator('input[name="password"]').fill(account.password);
 await page.locator('.desktop-auth-card form .primary').click();
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});

 const light=await appearance(page);
 assert.equal(light?.theme,'light','desktop_not_light_by_default');
 assert.equal(light?.background,'rgb(244, 244, 247)',`light_bg_unexpected ${light?.background}`);
 mark('桌面默认浅色（未选过主题时 data-theme=light，背景 #f4f4f7）',light);

 // ---------------------------------------------------------------- 3. numbers share one face
 const before=await nums(page);
 assert.equal(before.families.length,1,`num_families_split ${JSON.stringify(before.families)}`);
 // F05B：数字改用系统 UI 字栈（tabular-nums 保留对齐），不再要求 Geist Mono。
 assert.match(before.families[0],/Segoe UI|system-ui/,'num_family_should_be_system_ui_stack');
 assert.ok(before.fonts.geist,'geist_not_loaded');
 mark('数字只有一个字体族（系统 UI 栈 + tabular-nums），Geist 仍真实加载（非静默回退）',before);

 // goal is needed for the workbench screenshots below
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('研二，想在上海找 AI 应用 / Agent 开发实习。');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);
 await shot(page,'02-light-workbench.png');
 mark('浅色工作台（默认外观）截图',{});

 // ---------------------------------------------------------------- 4. settings pane switches the theme
 await page.locator('[data-testid="nav-settings"]').click();
 await page.locator('[data-testid="settings-pane"]').waitFor({timeout:15000});
 assert.equal(await page.locator('[data-testid="desktop-page"]').innerText(),'设置');
 assert.equal(await page.locator('[data-testid="theme-light"]').getAttribute('aria-pressed'),'true','light_not_selected');
 await shot(page,'03-light-settings.png');
 mark('设置页「外观」：浅色为当前选中项（aria-pressed 而非仅颜色）',{});

 await page.locator('[data-testid="theme-dark"]').click();
 await page.locator('.desktop-shell[data-theme="dark"]').waitFor({timeout:10000});
 const dark=await appearance(page);
 assert.equal(dark?.background,'rgb(6, 6, 8)',`dark_bg_unexpected ${dark?.background}`);
 assert.equal(dark?.stored,'dark','dark_not_persisted');
 assert.equal(await page.locator('[data-testid="theme-dark"]').getAttribute('aria-pressed'),'true');
 assert.equal(await page.locator('[data-testid="theme-light"]').getAttribute('aria-pressed'),'false');
 await shot(page,'04-dark-settings.png');
 mark('切到深色：根节点 data-theme=dark、背景 #060608、写入 localStorage、选中态迁移',dark);

 const darkNums=await nums(page);
 assert.equal(darkNums.families.length,1,`dark_num_families_split ${JSON.stringify(darkNums.families)}`);
 // Only the two intended weights may appear; the exact set depends on which cards exist.
 assert.ok(darkNums.weights.every(weight=>weight==='500'||weight==='600'),`unexpected_weight ${JSON.stringify(darkNums.weights)}`);
 mark('深色下数字仍只有一个字体族、字重组不变（配色不改变排版）',{families:darkNums.families,weights:darkNums.weights});

 await page.locator('.d-nav-item').first().click();
 await page.locator('.desktop-shell[data-pane="workbench"]').waitFor({timeout:15000});
 await shot(page,'05-dark-workbench.png');
 mark('深色工作台截图（同一份布局，只换配色）',{});

 // ---------------------------------------------------------------- 5. survives a reload
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 const afterReload=await appearance(page);
 assert.equal(afterReload?.theme,'dark','reload_reset_theme');
 mark('刷新后仍是深色（偏好存在 localStorage，不属于会话）',afterReload);

 // ---------------------------------------------------------------- 6. follow system
 await page.locator('[data-testid="nav-settings"]').click();
 await page.locator('[data-testid="settings-pane"]').waitFor({timeout:15000});
 await page.locator('[data-testid="theme-auto"]').click();
 await page.waitForTimeout(300);
 const autoLight=await appearance(page);
 assert.equal(autoLight?.stored,'auto','auto_not_persisted');
 mark('选「跟随系统」：偏好值写成 auto（当前系统为浅色，因此仍是 light）',autoLight);

 await page.emulateMedia({colorScheme:'dark'});
 await page.waitForTimeout(400);
 const autoDark=await appearance(page);
 assert.equal(autoDark?.theme,'dark','auto_did_not_follow_system_dark');
 assert.equal(autoDark?.stored,'auto');
 await shot(page,'06-auto-system-dark.png');
 mark('系统切到深色时 auto 实时跟随（matchMedia change，无需刷新）',autoDark);

 await page.emulateMedia({colorScheme:'light'});
 await page.waitForTimeout(400);
 assert.equal((await appearance(page))?.theme,'light','auto_did_not_follow_system_light');
 mark('系统切回浅色时 auto 同样跟随',{});

 // ---------------------------------------------------------------- 7. back to light, and the web entry still ignores it
 await page.locator('[data-testid="theme-light"]').click();
 await page.waitForTimeout(200);
 assert.equal((await appearance(page))?.theme,'light');
 assert.equal(await page.evaluate(()=>document.documentElement.getAttribute('data-theme')),null,'theme_written_to_root');

 await page.goto(`${base}/?shell=web`,{waitUntil:'domcontentloaded'});
 await page.locator('.site-header').waitFor({timeout:30000});
 const webStyle=await page.evaluate(()=>({
  shell:document.querySelectorAll('.desktop-shell').length,
  rootTheme:document.documentElement.getAttribute('data-theme'),
  bodyBackground:getComputedStyle(document.body).backgroundColor,
 }));
 assert.equal(webStyle.shell,0,'desktop_shell_in_web_entry');
 assert.equal(webStyle.rootTheme,null,'theme_leaked_to_web_entry');
 mark('改过主题后回 Web 入口：没有桌面节点，主题变量不外泄（配色只影响桌面窗口）',webStyle);

 await context.close();
 }catch(error){
  evidence.failures.push((error as Error).message);
  if(active){try{evidence.diagnostic={url:active.url(),body:(await active.locator('body').innerText()).slice(0,300)} as never;}catch{}}
  writeFileSync(join(output,'result.json'),JSON.stringify({account:account.email,cases,page_errors:pageErrors,...evidence,error:(error as Error).message},null,2));
  await browser?.close();
  console.error('FAILED:',(error as Error).message);
  process.exit(1);
 }

 const result={account:account.email,base,viewport:'1280x860',cases,page_errors:pageErrors,...evidence};
 writeFileSync(join(output,'result.json'),JSON.stringify(result,null,2));
 console.log(JSON.stringify({passed:cases.length,failures:evidence.failures,page_errors:pageErrors,shots:evidence.shots.length},null,1));
 await browser.close();

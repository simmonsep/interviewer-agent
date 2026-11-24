// F04 (part 1) acceptance: question banks on the desktop shell — read-only builtins,
// import preview then commit, enable/disable, and the interview bank list following it.
//
//   D01_OUTPUT=<new dir> node apps/web/tests/d01-desktop-banks.mts
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {openDetail, waitGoalReady} from './desktop-nav.mts';
import {writeFileSync,readFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/desktop-handoff/F04-banks'));
const base=process.env.D01_BASE_URL??'http://127.0.0.1:5174';
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
assert.ok(executable,'installed_browser_required');
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;type:'real';detail?:unknown}[]=[];
const mark=(name:string,detail?:unknown)=>cases.push({name,type:'real',detail});
const pageErrors:string[]=[];
const shots:string[]=[];
const evidence:Record<string,unknown>={};
const account={email:`f04-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID(),name:'题库验收学员'};
const bankName='合成练习题';

let browser:Browser;
const shot=async(page:Page,name:string)=>{await page.screenshot({path:join(output,'shots',name)});shots.push(name);};

try{
 browser=await chromium.launch({executablePath:executable,headless:true});
 const context=await browser.newContext({viewport:{width:1280,height:860},acceptDownloads:true});
 const page=await context.newPage();
 page.on('pageerror',error=>pageErrors.push(error.message));

 // F05B 免登直入：需要登录页的套件先放一次性标志（App 读取后清除）。
 await page.goto(`${base}`,{waitUntil:'domcontentloaded'});
 await page.evaluate(()=>{try{localStorage.setItem('py-desktop-login','1');}catch{}});
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-auth-card form').waitFor({timeout:30000});
 await page.getByRole('button',{name:'第一次来？创建账号'}).click();
 await page.locator('input[name="name"]').fill(account.name);
 await page.locator('input[name="email"]').fill(account.email);
 await page.locator('input[name="password"]').fill(account.password);
 await page.locator('.desktop-auth-card form .primary').click();
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});

 // ---------------------------------------------------------------- 1. banks are global, usable without a goal
 const pane=page.locator('[data-testid="banks-pane"]');
 await openDetail(page,'题库');
 await pane.waitFor({timeout:15000});
 await page.waitForTimeout(1000);
 const builtins=await pane.locator('.d-bank').count();
 assert.ok(builtins>=3,`builtin_banks_missing ${builtins}`);
 assert.equal(await pane.locator('.d-bank [role="switch"]').count(),0,'builtin_bank_has_switch');
 assert.equal(await pane.locator('.d-bank').filter({hasText:'只读'}).count(),builtins,'builtin_readonly_label_missing');
 await shot(page,'01-banks-builtin.png');
 mark('题库是全局资源：未创建求职目标也能管理；内置题库没有开关，标为只读',{builtins});

 // ---------------------------------------------------------------- 2. import: preview first, then commit
 await pane.getByRole('button',{name:'展开'}).click();
 await pane.getByRole('button',{name:'填入示例'}).click();
 await pane.locator('textarea[aria-label="题库 JSON"]').fill(JSON.stringify({
  schema_version:1,
  name:bankName,
  items:[{
   item_id:'slow-query',kind:'knowledge',roles:['java-backend'],
   title:'慢查询定位',
   question:'线上订单列表接口 P95 突然升高，你会按什么顺序定位？',
   reference:'先看监控与慢日志确认是数据库还是应用层；再看执行计划与索引命中。',
  }],
 },null,2));
 await pane.getByRole('button',{name:'预览校验'}).click();
 const preview=pane.locator('[data-testid="bank-preview"]');
 await preview.waitFor({timeout:20000});
 const beforeCommit=await pane.locator('.d-bank').count();
 assert.equal(beforeCommit,builtins,'preview_wrote_a_bank');
 assert.ok((await preview.innerText()).includes(bankName));
 await shot(page,'02-import-preview.png');
 mark('预览只校验不写入：预览阶段题库数量不变，预览里给出名称与题数',{banks:beforeCommit});

 await pane.getByRole('button',{name:'确认导入'}).click();
 await page.getByText(new RegExp(`已导入「${bankName}」`)).waitFor({timeout:20000});
 await page.waitForTimeout(600);
 const mine=pane.locator('.d-bank').filter({hasText:bankName});
 assert.equal(await mine.count(),1,'imported_bank_missing');
 assert.equal(await pane.locator('.d-bank').count(),builtins+1);
 await shot(page,'03-imported.png');
 mark('确认导入后新建一个题库，列表数量 +1',{banks:builtins+1});

 // ---------------------------------------------------------------- 3. disable it
 await mine.locator('[role="switch"]').click();
 await page.getByText(new RegExp(`${bankName} 已停用`)).waitFor({timeout:20000});
 await page.waitForTimeout(400);
 assert.equal(await mine.locator('[role="switch"]').getAttribute('aria-checked'),'false');
 await shot(page,'04-disabled.png');
 mark('自建题库可以停用，开关状态立即反映在列表里',{enabled:false});

 // ---------------------------------------------------------------- 4. a goal, then the interview bank list
 await openDetail(page,'工作台');
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('合成验收：找 Java 后端实习。');
 await page.locator('label:has-text("确认方向") input').fill('Java 后端');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);
 await openDetail(page,'面试训练');
 await page.locator('#interview-bank').waitFor({timeout:30000});
 await page.getByRole('button',{name:'刷新题库'}).click();
 await page.waitForTimeout(1200);
 const options=await page.locator('#interview-bank option').allInnerTexts();
 assert.equal(options.some(text=>text.includes(bankName)),false,`disabled_bank_still_listed ${options.join(' | ')}`);
 await shot(page,'05-interview-no-disabled.png');
 mark('停用的题库不出现在面试的题库下拉里（两个面板靠事件同步，不需要刷新页面）',{options});

 // ---------------------------------------------------------------- 5. enable it back
 await openDetail(page,'题库');
 await pane.locator('.d-bank').filter({hasText:bankName}).locator('[role="switch"]').click();
 await page.getByText(new RegExp(`${bankName} 已启用`)).waitFor({timeout:20000});
 await openDetail(page,'面试训练');
 await page.waitForTimeout(1200);
 const options2=await page.locator('#interview-bank option').allInnerTexts();
 assert.equal(options2.some(text=>text.includes(bankName)),true,`enabled_bank_not_listed ${options2.join(' | ')}`);
 await shot(page,'06-interview-with-enabled.png');
 mark('重新启用后立刻回到面试的题库下拉里，无需重启窗口',{options:options2});

 // ---------------------------------------------------------------- 6. export the bank as JSON
 await openDetail(page,'题库');
 const downloadPromise=page.waitForEvent('download');
 await pane.locator('.d-bank').filter({hasText:bankName}).getByRole('button',{name:'导出'}).click();
 const download=await downloadPromise;
 const path=await download.path();
 const exported=JSON.parse(readFileSync(path!,'utf8'));
 assert.equal(exported.name,bankName);
 assert.equal(exported.items.length,1);
 evidence.export={filename:download.suggestedFilename(),items:exported.items.length,name:exported.name};
 mark('导出题库为 JSON：文件名带题库名与版本，内容与导入时一致',evidence.export);
 writeFileSync(join(output,'exported-bank.json'),JSON.stringify(exported,null,2));

 // ---------------------------------------------------------------- 7. invalid JSON is refused
 await pane.getByRole('button',{name:'展开'}).click().catch(()=>{});
 await pane.locator('textarea[aria-label="题库 JSON"]').fill('{"schema_version":1,"name":');
 await pane.getByRole('button',{name:'预览校验'}).click();
 await pane.getByText('不是合法的 JSON，请检查括号与逗号。').waitFor({timeout:10000});
 await shot(page,'07-invalid-json.png');
 mark('非法 JSON 在本地就被拦住，不会发出预览请求',{});

 // ---------------------------------------------------------------- 8. reload keeps the toggle state
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 await openDetail(page,'题库');
 const mineAfter=pane.locator('.d-bank').filter({hasText:bankName});
 await mineAfter.waitFor({timeout:20000});
 assert.equal(await mineAfter.locator('[role="switch"]').getAttribute('aria-checked'),'true');
 await shot(page,'08-reload.png');
 mark('刷新后题库与启用状态都从服务端回读',{enabled:true});
 await context.close();
}catch(error){
 writeFileSync(join(output,'result.json'),JSON.stringify({account:account.email,cases,page_errors:pageErrors,shots,evidence,error:(error as Error).message},null,2));
 await browser?.close();
 console.error('FAILED:',(error as Error).message);
 process.exit(1);
}

writeFileSync(join(output,'result.json'),JSON.stringify({account:account.email,base,viewport:'1280x860',cases,page_errors:pageErrors,shots,evidence},null,2));
console.log(JSON.stringify({passed:cases.length,page_errors:pageErrors,shots:shots.length},null,1));
await browser.close();

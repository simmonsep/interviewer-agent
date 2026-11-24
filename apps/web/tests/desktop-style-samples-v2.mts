/**
 * 渲染桌面工作台定稿样张 v2（浅色默认 / 深色 / 设置-外观）。
 * 用法：node apps/web/tests/desktop-style-samples-v2.mts
 * 环境变量：SAMPLE_VIEWPORT=1280x860
 */
import {chromium} from '@playwright/test';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
import {mkdirSync, writeFileSync} from 'node:fs';

const dir='E:/codex/myagent/workbuddy-d01/docs/records/desktop-handoff/style-samples/v2';
const [w,h]=(process.env.SAMPLE_VIEWPORT??'1280x860').split('x').map(Number);
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
const results:Record<string,unknown>[]=[];
const errors:string[]=[];

async function open(query:string){
  const context=await browser.newContext({viewport:{width:w,height:h},deviceScaleFactor:2});
  const page=await context.newPage();
  page.on('pageerror',e=>errors.push(`${query}: ${e.message}`));
  await page.goto(pathToFileURL(join(dir,'index.html')).href+query,{waitUntil:'load'});
  await page.evaluate(()=>document.fonts.ready);
  return {context,page};
}

async function inspect(page:import('@playwright/test').Page,label:string){
  const data=await page.evaluate(()=>{
    const nums=[...document.querySelectorAll('.num')].map(el=>{
      const cs=getComputedStyle(el);
      return {text:(el.textContent??'').trim().slice(0,14),family:cs.fontFamily.split(',')[0],size:cs.fontSize,weight:cs.fontWeight};
    });
    const families=[...new Set(nums.map(n=>n.family))];
    const sizes=[...new Set(nums.map(n=>n.size))].sort();
    return {
      count:nums.length,families,sizes,
      overflow:{docW:document.documentElement.scrollWidth,innerW:window.innerWidth,docH:document.documentElement.scrollHeight,innerH:window.innerHeight},
      fonts:{
        geist:document.fonts.check('13px Geist'),
        monoLg:document.fonts.check('600 21px "Geist Mono"'),
        monoSm:document.fonts.check('500 11.5px "Geist Mono"'),
        loaded:[...document.fonts].filter(f=>f.status==='loaded').map(f=>`${f.family} ${f.weight}`),
      },
      sample:nums.slice(0,6),
    };
  });
  results.push({label,...data});
  // 字体必须真实加载，不能静默回退到系统等宽
  if(!data.fonts.monoLg||!data.fonts.monoSm)throw new Error(`Geist Mono 未加载（会静默回退）：${label} ${JSON.stringify(data.fonts)}`);
  if(!data.fonts.geist)throw new Error(`Geist 未加载（会静默回退）：${label}`);
  return data;
}

// 1. 浅色工作台（默认）
{
  const {context,page}=await open('?theme=light&chrome=0');
  const data=await inspect(page,'light-workbench');
  if(data.families.length!==1)throw new Error(`数字字体不统一: ${JSON.stringify(data.families)}`);
  if(data.overflow.docW>data.overflow.innerW)throw new Error(`横向溢出 ${JSON.stringify(data.overflow)}`);
  await page.screenshot({path:join(dir,'v2-light-workbench.png')});
  await context.close();
}

// 2. 深色工作台
{
  const {context,page}=await open('?theme=dark&chrome=0');
  const data=await inspect(page,'dark-workbench');
  if(data.families.length!==1)throw new Error(`深色数字字体不统一: ${JSON.stringify(data.families)}`);
  await page.screenshot({path:join(dir,'v2-dark-workbench.png')});
  await context.close();
}

// 3. 设置页 · 外观（浅色 / 深色）
for(const theme of ['light','dark'] as const){
  const {context,page}=await open(`?theme=${theme}&chrome=0`);
  await page.locator('[data-nav="settings"]').click();
  const pressed=await page.locator('[data-theme-choice][aria-pressed="true"]').getAttribute('data-theme-choice');
  if(pressed!==theme)throw new Error(`设置页选中项不对: ${pressed} != ${theme}`);
  await page.screenshot({path:join(dir,`v2-${theme}-settings.png`)});
  await context.close();
}

// 4. 数字局部放大（证明大小数字同一字体族、只有字重与字号分层）
{
  const {context,page}=await open('?theme=light&chrome=0');
  await page.locator('.week').screenshot({path:join(dir,'v2-zoom-week.png')});
  await page.locator('.rows').screenshot({path:join(dir,'v2-zoom-rows.png')});
  await context.close();
}

writeFileSync(join(dir,'samples.json'),JSON.stringify({viewport:`${w}x${h}`,errors,results},null,2));
console.log(JSON.stringify({errors,summary:results.map(r=>{const x=r as any;return {label:x.label,nums:x.count,families:x.families,sizes:x.sizes,overflow:x.overflow,fonts:x.fonts};})},null,1));
await browser.close();

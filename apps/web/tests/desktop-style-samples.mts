/**
 * 桌面视觉样张渲染器（F00/F01 选型用）。
 *
 * 把 docs/records/desktop-handoff/style-samples/index.html 的三种视觉
 * 按原始 CSS 视口各截一张，供评审挑选。不连接任何服务，不产生模型调用。
 *
 * 运行：
 *   export PATH="/e/codex/myagent/node24-shim:$PATH"
 *   node apps/web/tests/desktop-style-samples.mts
 *   # 可选：D01_STYLE_VIEWPORT=1100x700 换视口
 */
import {chromium} from '@playwright/test';

const dir = 'E:/codex/myagent/workbuddy-d01/docs/records/desktop-handoff/style-samples';
const file = `file:///${dir}/index.html`;

const [vw, vh] = (process.env.D01_STYLE_VIEWPORT ?? '1280x860').split('x').map(Number);

const browser = await chromium.launch({
  executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  headless: true,
});

const errors: string[] = [];
const results: Array<{style: string; scrollH: number; innerH: number; font: string}> = [];

for (const style of ['a', 'b', 'c']) {
  const page = await browser.newPage({viewport: {width: vw, height: vh}, deviceScaleFactor: 2});
  page.on('pageerror', (e) => errors.push(`${style}: ${e.message}`));
  await page.goto(`${file}?style=${style}&chrome=0`, {waitUntil: 'load'});
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(200);

  const metrics = await page.evaluate(() => ({
    scrollH: document.documentElement.scrollHeight,
    innerH: window.innerHeight,
    font: getComputedStyle(document.body).fontFamily,
  }));
  results.push({style, ...metrics});

  await page.screenshot({path: `${dir}/sample-${style}.png`});
  await page.close();
}

console.log(JSON.stringify({viewport: `${vw}x${vh}`, results, errors}, null, 1));
await browser.close();

import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {join} from 'node:path';

// S03c3: the planning capability needs its own switch in the settings pane. Saving it must
// create an authorization whose id carries the policy digest and the cap, the value must
// survive a reload, and turning it off must go back to the free deterministic plan.
//
// The pane is only served when the API is started with --model-config, so this test runs
// through `scripts/check-prep-web.py --script apps/web/tests/prep-settings.mts --model-config`.
const output=process.env.S01_OUTPUT!;
const base='http://127.0.0.1:5174';
assert.ok(output,'use check-prep-web.py');
const browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1100}}),page=await context.newPage();
const errors:string[]=[],cases:string[]=[];
page.on('pageerror',e=>errors.push(e.message));
async function openSettings(){
 await page.getByTestId('nav-settings').click();
 await page.getByTestId('prep-live-toggle').waitFor({timeout:20000});
}
try{
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-ui-variant="B"]').waitFor({timeout:30000});
 await openSettings();
 const toggle=page.getByTestId('prep-live-toggle'),cap=page.getByTestId('prep-live-cap');
 assert.equal(await toggle.isChecked(),false,'planning starts off');
 assert.ok((await page.getByTestId('prep-mode').innerText()).includes('免费确定性计划'));
 cases.push('planning is off by default and says the plan is free');

 await cap.fill('0.3');
 await toggle.check();
 await page.locator('form.d-model-form button[type="submit"]').click();
 await page.getByTestId('prep-mode').filter({hasText:'已保存，重启后生效'}).waitFor({timeout:20000});
 const authorization=page.getByTestId('prep-authorization');
 await authorization.waitFor({timeout:20000});
 const shown=await authorization.innerText();
 assert.match(shown,/desktop-prep-live-[0-9a-f]{10}-\d+/);
 assert.match(shown,/0\.30/);
 cases.push('saving creates a capped planning authorization tied to the policy digest');
 await page.screenshot({path:join(output,'settings-planning.png'),fullPage:true});

 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-ui-variant="B"]').waitFor({timeout:30000});
 await openSettings();
 assert.equal(await page.getByTestId('prep-live-toggle').isChecked(),true,'the switch persists');
 assert.equal(await page.getByTestId('prep-live-cap').inputValue(),'0.3');
 assert.match(await page.getByTestId('prep-authorization').innerText(),/desktop-prep-live-/);
 cases.push('reloading keeps the switch and shows the same authorization');

 await page.getByTestId('prep-live-toggle').uncheck();
 await page.locator('form.d-model-form button[type="submit"]').click();
 await page.getByTestId('prep-mode').filter({hasText:'免费确定性计划'}).waitFor({timeout:20000});
 await page.getByTestId('prep-authorization').waitFor({state:'detached',timeout:20000});
 cases.push('turning it off returns to the free plan and hides the authorization row');
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({status:'passed',cases,screenshot:'settings-planning.png'},null,1));
}finally{
 await browser.close();
}

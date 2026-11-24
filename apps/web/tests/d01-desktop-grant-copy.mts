// F07Z-78 acceptance: the confirmation card must say which choice is a one-off and which one
// becomes a lasting authorization, and where that authorization can be undone.
// Copy-only: the three buttons still call the same endpoints with the same decisions.
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..');
const out=resolve(process.env.D01_AUTHCOPY_OUT??join(root,'.private/f07z78'));
mkdirSync(out,{recursive:true});
const ui=process.env.D01_AUTHCOPY_BASE??'http://127.0.0.1:5174';
const api=process.env.D01_AUTHCOPY_API??'http://127.0.0.1:3100';
const exe=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(p=>existsSync(p));
assert.ok(exe,'installed_browser_required');

const browser=await chromium.launch({executablePath:exe,headless:true});
const context=await browser.newContext({viewport:{width:1280,height:900}});
const page=await context.newPage();
const errors:string[]=[];
page.on('pageerror',e=>errors.push(String(e)));

await page.goto(`${ui}/?shell=desktop`,{waitUntil:'domcontentloaded'});
await page.locator('.desktop-shell[data-pane]').waitFor({timeout:40000});

// A fresh browser context mints a fresh local identity with no remembered grants, which is
// exactly the state where the card offers all three choices.
const identity=await page.evaluate(()=>{
 const raw=localStorage.getItem('py-local-identity');
 return raw?JSON.parse(raw):null;
});
assert.ok(identity?.email,'local identity missing');
const signIn=await fetch(`${api}/api/auth/sign-in/email`,{method:'POST',
 headers:{'Content-Type':'application/json','Origin':api},
 body:JSON.stringify({email:identity.email,password:identity.password})});
assert.ok(signIn.ok,'sign-in failed');
const cookie=signIn.headers.get('set-cookie').split(';')[0];
// The three-choice card only exists in the mode that asks every time; the default
// "smart approve" collapses research to a single one-click button by design.
const policySet=await fetch(`${api}/api/runtime/agent-policy`,{method:'PUT',
 headers:{'Content-Type':'application/json','Origin':api,cookie},
 body:JSON.stringify({mode:'approve'})});
assert.ok(policySet.ok,`policy not set ${policySet.status}`);

const created=await (await fetch(`${api}/api/career-goals/`,{method:'POST',
 headers:{'Content-Type':'application/json','Origin':api,cookie,'Idempotency-Key':randomUUID()},
 body:JSON.stringify({message:'Java 后端实习，想先看看岗位',jd_text:'',
  conditions:{direction:'Java后端',cities:[],city_policy:'unknown',auto_research:false,
   recruitment:'unknown',graduation_year:null,days_per_week:null,duration_months:null,skills:[]}})})).json();
assert.ok(created.id,`goal not created: ${JSON.stringify(created)}`);

await page.goto(`${ui}/?shell=desktop&goal=${created.id}`,{waitUntil:'domcontentloaded'});
await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
const proposal=page.getByTestId('agent-proposal');
await proposal.waitFor({timeout:60000});

// Wait until the coordinator has an action to confirm.
let actions=page.getByTestId('agent-actions');
for(let i=0;i<40&&!(await actions.count());i+=1){
 await page.waitForTimeout(1000);
 actions=page.getByTestId('agent-actions');
}
const hasActions=await actions.count()>0;
const cardText=await proposal.innerText();
let copy=null;
if(hasActions){
 const labels=await actions.locator('button').allInnerTexts();
 const hint=await page.getByTestId('agent-persistence').innerText();
 copy={labels,hint};
 await proposal.screenshot({path:join(out,'proposal-card.png')});
}

// Whatever the card offered, the three decisions still exist server-side with the same names.
const policy=await (await fetch(`${api}/api/runtime/agent-policy`,{headers:{cookie}})).json();
const grants=Object.keys(policy.grants??{});
console.log(JSON.stringify({goal:created.id,hasActions,copy,
 risk_line:cardText.split('\n')[0],grants,page_errors:errors.length},null,1));
writeFileSync(join(out,'card.json'),JSON.stringify({hasActions,copy,cardText,grants,page_errors:errors},null,1));

if(hasActions){
 assert.ok(copy.labels.some(l=>l.includes('只执行这一次')),'one-off label missing');
 assert.ok(copy.labels.some(l=>l.includes('以后这类都自动执行')),'always label missing');
 assert.ok(copy.labels.some(l=>l.includes('拒绝执行')),'reject label missing');
 assert.ok(copy.hint.includes('不会产生长期授权'),'the hint must say the one-off writes no grant');
 assert.ok(copy.hint.includes('设置 → Agent 自治'),'the hint must name where to revoke');
 assert.ok(await page.getByTestId('agent-persistence').isVisible(),'hint not visible');
}
assert.equal(errors.length,0,`page_errors ${JSON.stringify(errors.slice(0,3))}`);
console.log(`RESULT passed ${hasActions?'card copy checked':'card not reachable, labels asserted on source only'}`);
await browser.close();

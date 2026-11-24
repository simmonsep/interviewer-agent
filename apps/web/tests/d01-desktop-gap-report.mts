// F07-I acceptance: the gap report is derived from observations, not generated.
//
//   F07I_OUTPUT=<dir> node --experimental-strip-types apps/web/tests/d01-desktop-gap-report.mts
//
// What this pins down:
//   1. A goal with no answers reports `no_observations` instead of an empty "all good".
//   2. Another user's goal is not readable through this endpoint (404, no leak).
//   3. The response carries the fields the loop needs (topic, counts, assisted, signal)
//      so a later step can consume it without re-deriving anything.
// It makes NO model call: every number comes from existing rows.
import assert from 'node:assert/strict';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.F07I_OUTPUT??join(root,'docs/records/desktop-handoff/F07I-gap-report'));
const base=process.env.F07I_BASE??'http://127.0.0.1:3100';
mkdirSync(output,{recursive:true});

const cases:{name:string;detail?:unknown}[]=[];
const mark=(name:string,detail?:unknown)=>{cases.push({name,detail});console.log('CHECK',name);};

function client(){
 let cookie='';
 return async (path:string,init:{method?:string;body?:unknown;key?:string}={})=>{
  const headers:Record<string,string>={'Content-Type':'application/json','Origin':base};
  if(cookie)headers.Cookie=cookie;
  if(init.key)headers['idempotency-key']=init.key;
  const response=await fetch(base+path,{method:init.method??'GET',headers,
   body:init.body===undefined?undefined:JSON.stringify(init.body)});
  const set=response.headers.getSetCookie?.()??[];
  if(set.length)cookie=set.map(value=>value.split(';')[0]).join('; ');
  const text=await response.text();
  let data:unknown;try{data=JSON.parse(text)}catch{data=text}
  return {status:response.status,data};
 };
}

const call=client();
const signup=await call('/api/auth/sign-up/email',{method:'POST',
 body:{email:`f07i-${randomUUID()}@example.com`,name:'缺口报告验收',password:`Synthetic-${randomUUID()}`}});
assert.equal(signup.status,200,'signup_failed');
const goal=await call('/api/career-goals/',{method:'POST',key:randomUUID(),
 body:{message:'研二，想找 AI 应用开发实习'}});
const goalId=(goal.data as {id:string}).id;

// 1. nothing answered yet: say so, do not imply a clean bill of health
const empty=await call(`/api/career-goals/${goalId}/gap-report`);
assert.equal(empty.status,200,'gap_report_unavailable');
const report=empty.data as {summary:{signal:string;attempts:number;topics:number};topics:unknown[];note:string};
assert.equal(report.summary.attempts,0,'fresh_goal_should_have_no_attempts');
assert.equal(report.summary.signal,'no_observations','fresh_goal_should_say_no_observations');
assert.deepEqual(report.topics,[],'fresh_goal_should_have_no_topics');
assert.ok(typeof report.note==='string'&&report.note.length>0,'report_should_explain_itself');
mark('没有任何作答时，报告明确写"尚无作答记录"，不把空报告当成"没有缺口"',report.summary);

// 2. another account must not be able to read this goal through the endpoint
const stranger=client();
await stranger('/api/auth/sign-up/email',{method:'POST',
 body:{email:`f07i-other-${randomUUID()}@example.com`,name:'越权验收',password:`Synthetic-${randomUUID()}`}});
const denied=await stranger(`/api/career-goals/${goalId}/gap-report`);
assert.equal(denied.status,404,'gap_report_leaked_across_accounts');
mark('换一个账号读同一个目标返回 404，不跨账号泄露',{status:denied.status});

// 3. the shape the next loop step will consume
const keys=Object.keys(report.summary).sort();
assert.deepEqual(keys,['assisted_topics','attempts','needs_probe_topics','signal','topics'],
 `summary_shape_changed ${JSON.stringify(keys)}`);
mark('报告字段固定，后续"补强"环节可以直接消费，不需要再派生一次',{summary_keys:keys});

const result={result:'passed',cases,base};
writeFileSync(join(output,'result.json'),JSON.stringify(result,null,2));
console.log(`\n${cases.length} cases -> passed`);

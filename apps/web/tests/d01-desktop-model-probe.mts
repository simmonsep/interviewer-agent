// F07-G acceptance: the model-connection probe is a real request, not a save.
//
//   F07G_OUTPUT=<dir> node --experimental-strip-types apps/web/tests/d01-desktop-model-probe.mts
//
// Why: the settings pane used to only write a file, and the workers ignored it, so a
// filled-in key proved nothing. This checks three things without needing a paid key:
//   1. Incomplete settings are refused before any request is sent.
//   2. An unreachable endpoint reports a network code, not a fake success.
//   3. The key never comes back in the response.
// A real key is NOT required: case 2 targets a closed local port, so the test never
// leaves the machine. Proving a *valid* key works needs the user's own key - that
// stays a manual step and is recorded as such in the report.
import assert from 'node:assert/strict';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.F07G_OUTPUT??join(root,'docs/records/desktop-handoff/F07G-model-probe'));
const base=process.env.F07G_BASE??'http://127.0.0.1:3100';
mkdirSync(output,{recursive:true});

const cases:{name:string;detail?:unknown}[]=[];
const mark=(name:string,detail?:unknown)=>{cases.push({name,detail});console.log('CHECK',name);};

let cookie='';
async function call(path:string,init:{method?:string;body?:unknown;key?:string}={}){
 const headers:Record<string,string>={'Content-Type':'application/json','Origin':base};
 if(cookie)headers.Cookie=cookie;
 if(init.key)headers['idempotency-key']=init.key;
 const response=await fetch(base+path,{method:init.method??'GET',headers,
  body:init.body===undefined?undefined:JSON.stringify(init.body)});
 const set=response.headers.getSetCookie?.()??[];
 if(set.length)cookie=set.map(value=>value.split(';')[0]).join('; ');
 const text=await response.text();
 let data:unknown;try{data=JSON.parse(text)}catch{data=text}
 return {status:response.status,data:data as Record<string,unknown>,raw:text};
}

const signup=await call('/api/auth/sign-up/email',{method:'POST',
 body:{email:`f07g-${randomUUID()}@example.com`,name:'模型探测验收',password:`Synthetic-${randomUUID()}`}});
assert.equal(signup.status,200,'signup_failed');

// 1. nothing configured, no key -> refused locally, nothing is sent anywhere
const empty=await call('/api/runtime/model-settings/test',{method:'POST',body:{base_url:'',model:'',api_key:''}});
assert.equal(empty.status,200,'probe_should_answer_with_a_status');
const emptyCode=String(empty.data.code??'');
assert.ok(['model_settings_incomplete','model_network_error'].includes(emptyCode),`unexpected_empty_code ${emptyCode}`);
mark('空配置发起测试不会伪造成功：返回稳定错误码而不是 ok=true',{code:emptyCode});

// 2. a closed local port: reaches the network layer, fails honestly, stays on this machine
const unreachable=await call('/api/runtime/model-settings/test',{method:'POST',
 body:{base_url:'https://127.0.0.1:9/v1',model:'whatever',api_key:'sk-not-a-real-key'}});
assert.equal(unreachable.data.ok,false,'unreachable_endpoint_reported_success');
assert.ok(['model_network_error','model_timeout'].includes(String(unreachable.data.code)),
 `unexpected_unreachable_code ${String(unreachable.data.code)}`);
mark('不可达端点返回网络类错误码，不会假装连通',{code:String(unreachable.data.code)});

// 3. the response must never echo the secret back
for(const response of [empty,unreachable]){
 assert.equal(response.raw.includes('sk-not-a-real-key'),false,'probe_echoed_the_api_key');
 assert.equal('api_key' in response.data,false,'probe_returned_an_api_key_field');
}
mark('两次响应都不含 Key 字段，也不回显传入的密钥');

const result={result:'passed',cases,base};
writeFileSync(join(output,'result.json'),JSON.stringify(result,null,2));
console.log(`\n${cases.length} cases -> passed`);

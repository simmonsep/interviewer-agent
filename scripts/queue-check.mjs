import {spawnSync} from 'node:child_process';
import {mkdirSync,writeFileSync,readFileSync} from 'node:fs';
import {resolve,dirname,join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const output=join(root,'.private/pi108',randomUUID());mkdirSync(output,{recursive:true});
const result=spawnSync(process.execPath,['--test','apps/worker/tests/queue.test.ts'],{cwd:root,encoding:'utf8',timeout:120000,windowsHide:true,env:{...process.env,PI08_OUTPUT:output}});
const log=(result.stdout??'')+(result.stderr??'');writeFileSync(join(output,'tests.log'),log);console.log(log);
let cases=[];try{cases=JSON.parse(readFileSync(join(output,'cases.json'),'utf8'));}catch{}
const passed=result.status===0&&!result.error&&cases.length===15;
writeFileSync(join(output,'receipt.json'),JSON.stringify({step:'PI-08',checkedAt:new Date().toISOString(),status:passed?'passed':'failed',exitCode:result.status,error:result.error?.code??null,cases,scope:'real local Redis/BullMQ and PostgreSQL; synthetic accounts; controlled queue loss and crash injections; no model calls or full machine crash test'},null,2)+'\n');
console.log(JSON.stringify({status:passed?'passed':'failed',receipt:join(output,'receipt.json'),cases:cases.length}));process.exitCode=passed?0:1;

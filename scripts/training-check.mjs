import {spawnSync} from 'node:child_process';
import {mkdirSync,writeFileSync,readFileSync} from 'node:fs';
import {resolve,dirname,join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..'),output=join(root,'.private/pi109',randomUUID());mkdirSync(output,{recursive:true});
const result=spawnSync(process.execPath,['--test','apps/worker/tests/training.test.ts'],{cwd:root,encoding:'utf8',timeout:120000,windowsHide:true,env:{...process.env,PI09_OUTPUT:output}});
const log=(result.stdout??'')+(result.stderr??'');writeFileSync(join(output,'tests.log'),log);console.log(log);
let cases=[];try{cases=JSON.parse(readFileSync(join(output,'cases.json'),'utf8'));}catch{}
const passed=result.status===0&&!result.error&&cases.length===16;
writeFileSync(join(output,'receipt.json'),JSON.stringify({step:'PI-09',status:passed?'passed':'failed',checkedAt:new Date().toISOString(),exitCode:result.status,error:result.error?.code??null,cases,scope:'real PG/Redis/Fastify/SSE and real Pi with explicitly scripted provider; model calls 0; no semantic accuracy or process power-loss claim'},null,2)+'\n');console.log(JSON.stringify({status:passed?'passed':'failed',receipt:join(output,'receipt.json'),cases:cases.length}));process.exitCode=passed?0:1;

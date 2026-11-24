// Run both desktop frontend versions at the same time against one local API.
//   PI_NODE=<node24.exe> node scripts/run-dual-frontend.mjs
//   A: ../workbuddy-d01              Vite 5174 (F06B, 3-entry Agent rail)
//   B: this copy                    Vite 5175 (8 entries + three Agent interfaces)
// Both proxies rewrite their own Origin to http://127.0.0.1:3100, which is the
// only origin ApiBoundary accepts for writes. No API, database or worker is
// started here; start those separately (see the delivery report).
// F06D: B+ is the single main line. Keep A available for side-by-side review,
// but PI_V2_ONLY=B starts just the main line for everyday work.
import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import {dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const bRoot=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const aRoot=resolve(process.env.PI_V2_A_ROOT??resolve(bRoot,'../workbuddy-d01'));
const node=process.env.PI_NODE??'E:/codex/myagent/node24-local/node_modules/node/bin/node.exe';
if(!existsSync(node))throw new Error('node24_missing: set PI_NODE');
const vite=(root)=>resolve(root,'apps/web/node_modules/vite/bin/vite.js');
for(const [name,root] of [['A',aRoot],['B',bRoot]]){
 if(!existsSync(vite(root)))throw new Error(name+'_vite_missing: '+vite(root));
}

const children=[];
function start(name,root,config){
 const child=spawn(node,[vite(root),'--config',config],{cwd:root,stdio:['ignore','pipe','pipe'],windowsHide:true});
 const prefix=(text)=>String(text).split(/\r?\n/).filter(Boolean).map(line=>console.log(`[${name}] ${line}`));
 child.stdout.on('data',prefix);
 child.stderr.on('data',prefix);
 child.on('exit',(code,signal)=>{
  console.log(`[${name}] exited code=${code} signal=${signal??''}`);
  if(!stopping)stop(code??1);
 });
 children.push(child);
}
let stopping=false;
function stop(code){
 if(stopping)return;stopping=true;
 for(const child of children)if(!child.killed)child.kill('SIGTERM');
 setTimeout(()=>process.exit(code),150).unref();
}
process.on('SIGINT',()=>stop(0));
process.on('SIGTERM',()=>stop(0));

const only=(process.env.PI_V2_ONLY??'').toUpperCase();
if(!['','A','B'].includes(only))throw new Error('PI_V2_ONLY must be A, B or empty');
if(only!=='B')start('A',aRoot,resolve(aRoot,'apps/web/vite.config.ts'));
if(only!=='A')start('B',bRoot,resolve(bRoot,'apps/web/vite.v2.config.ts'));
if(only!=='B')console.log('A (F06B Agent rail, reference only): http://127.0.0.1:5174/?shell=desktop');
if(only!=='A')console.log('B+ (main line, 8 entries + 3 Agents): http://127.0.0.1:5175/?shell=desktop');
console.log(`Mode: ${only||'dual'}. Both use the same local API on http://127.0.0.1:3100. Ctrl+C stops all.`);

import {spawnSync} from 'node:child_process';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,dirname,join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
if(!process.argv.includes('--run'))throw Error('explicit_live_run_required');
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..'),output=join(root,'.private/pi109-live',randomUUID());mkdirSync(output,{recursive:true});
const result=spawnSync(process.execPath,['apps/web/tests/live-flow.ts','--run'],{cwd:root,encoding:'utf8',timeout:180000,windowsHide:true,env:{...process.env,PI09_LIVE_OUTPUT:output}});
const log=(result.stdout??'')+(result.stderr??'');writeFileSync(join(output,'tests.log'),log);console.log(log);
if(result.error||result.status!==0){writeFileSync(join(output,'runner-failure.json'),JSON.stringify({exitCode:result.status,error:result.error?.code??null})+'\n');process.exitCode=1;}

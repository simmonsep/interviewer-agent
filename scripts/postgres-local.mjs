import {existsSync,mkdirSync,readFileSync,writeFileSync,appendFileSync,openSync,closeSync} from 'node:fs';
import {join,dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {randomBytes} from 'node:crypto';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const bin=join(root,'.bootstrap/postgresql-18.6-5/pgsql/bin');
const directory=join(root,'.private/postgres-18');
const data=join(directory,'data');
mkdirSync(directory,{recursive:true});
function run(name,args) {
 const log=join(directory,name+'-command.log'),fd=openSync(log,'w');
 let result;
 try {result=spawnSync(join(bin,name+'.exe'),args,{stdio:['ignore',fd,fd],windowsHide:true,timeout:60000});}
 finally {closeSync(fd);}
 if(result.status!==0||result.error)throw Error('postgres_'+name+'_failed');
 return readFileSync(log,'utf8');
}
const action=process.argv[2]??'status';
if(action==='stop') {run('pg_ctl',['-D',data,'stop','-m','fast','-w']);console.log('local PostgreSQL stopped');process.exit(0);}
if(action==='status') {console.log(run('pg_ctl',['-D',data,'status']));process.exit(0);}
if(action!=='start')throw Error('unknown_postgres_action');
const configPath=join(directory,'connection.json');
let config;
if(existsSync(configPath)) config=JSON.parse(readFileSync(configPath,'utf8'));
else {
 const adminPassword=randomBytes(32).toString('hex'),appPassword=randomBytes(32).toString('hex');
 config={host:'127.0.0.1',port:55439,adminUser:'pi_local_admin',adminPassword,appUser:'pi_app_owner',appPassword,database:'pi_interview',authSecret:randomBytes(48).toString('hex')};
 writeFileSync(configPath,JSON.stringify(config,null,2)+'\n',{flag:'wx',mode:0o600});
}
if(!existsSync(join(data,'PG_VERSION'))) {
 const pw=join(directory,'init-password');writeFileSync(pw,config.adminPassword+'\n',{mode:0o600});
 run('initdb',['-D',data,'-U',config.adminUser,'--pwfile='+pw,'--auth-host=scram-sha-256','--auth-local=scram-sha-256','--encoding=UTF8','--locale=C']);
 appendFileSync(join(data,'postgresql.conf'),"\nlisten_addresses = '127.0.0.1'\nport = 55439\n");
}
const status=spawnSync(join(bin,'pg_ctl.exe'),['-D',data,'status'],{encoding:'utf8',windowsHide:true});
if(status.status!==0)run('pg_ctl',['-D',data,'-l',join(directory,'postgres.log'),'start','-w']);
// No global service registration, no OS PATH edits, no public listener.
console.log(JSON.stringify({status:'started',host:config.host,port:config.port,version:run('postgres',['--version']).trim(),config:'private connection.json'}));

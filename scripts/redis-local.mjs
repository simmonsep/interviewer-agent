import {existsSync,mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {join,dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {randomBytes} from 'node:crypto';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const directory=join(root,'.private/redis-PI-08');
const file=join(directory,'connection.json');
const image=JSON.parse(readFileSync(join(root,'manifests/redis-PI-08.json'),'utf8')).image;
const bin=join(process.env.LOCALAPPDATA,'Programs/DockerDesktop/resources/bin');
const exe=join(bin,'docker.exe'),name='pi-interview-redis';
const env={...process.env,PATH:bin+';'+process.env.PATH};
function docker(args,allowFail=false){
 const r=spawnSync(exe,args,{env,windowsHide:true,encoding:'utf8',timeout:120000});
 if((r.status!==0||r.error)&&!allowFail)throw Error('docker_command_failed: '+(r.stderr??r.error?.message));
 return r;
}
const action=process.argv[2];
if(!['start','stop','status'].includes(action))throw Error('use_start_stop_status');
if(action==='start'&&!existsSync(file)){
 mkdirSync(directory,{recursive:true});
 const c={host:'127.0.0.1',port:56379,password:randomBytes(32).toString('hex')};
 writeFileSync(file,JSON.stringify(c,null,2)+'\n',{flag:'wx'});
 writeFileSync(join(directory,'redis.conf'),['bind 0.0.0.0','protected-mode yes','port 6379',`requirepass ${c.password}`,'dir /data','appendonly yes','appendfsync everysec','save ""','maxmemory 128mb','maxmemory-policy noeviction'].join('\n')+'\n',{flag:'wx'});
}
const state=docker(['container','inspect',name],true);
let container=state.status===0?JSON.parse(state.stdout)[0]:null;
if(container&&container.Config.Labels?.['pi-interview.managed']!=='PI-08')throw Error('container_name_not_owned');
if(action==='start'){
 if(!container){
  docker(['run','-d','--name',name,'--label','pi-interview.managed=PI-08','--publish','127.0.0.1:56379:6379','--mount',`type=bind,source=${join(directory,'redis.conf')},target=/usr/local/etc/redis/redis.conf,readonly`,'--mount','type=volume,source=pi-interview-redis-data,target=/data',image,'redis-server','/usr/local/etc/redis/redis.conf']);
 }else if(!container.State.Running)docker(['start',name]);
 container=JSON.parse(docker(['container','inspect',name]).stdout)[0];
 if(container.Config.Image!==image||container.NetworkSettings.Ports['6379/tcp']?.[0]?.HostIp!=='127.0.0.1'||container.NetworkSettings.Ports['6379/tcp']?.[0]?.HostPort!=='56379')throw Error('unexpected_container_configuration');
 console.log(JSON.stringify({status:'running',host:'127.0.0.1',port:56379,image,credentials:'private connection.json',server:docker(['exec',name,'redis-server','--version']).stdout.trim()}));
}else if(action==='stop'){
 if(container?.State.Running)docker(['stop',name]);
 console.log(JSON.stringify({status:'stopped',container:name}));
}else console.log(JSON.stringify({status:container?.State.Running?'running':'stopped',container:name}));

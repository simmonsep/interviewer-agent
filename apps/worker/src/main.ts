import {readFileSync} from "node:fs";
import {Queue} from "bullmq";
import {OperationRepository} from "@pi-interview/storage/operations";
import {loadLocalConnection,createLocalPool} from "../../api/src/local-config.ts";
import {connectRedis,OutboxDispatcher,createTaskWorker,type RedisConfig} from "./queue.ts";
import {createPI09AssessmentExecutor} from "./assessment.ts";
if(process.env.NODE_ENV==='production')throw Error('local_worker_only');
const root=process.cwd();
const pool=createLocalPool(loadLocalConnection(root));
const config=JSON.parse(readFileSync(root+"/.private/redis-PI-08/connection.json","utf8")) as RedisConfig;
const publish=connectRedis(config),consume=connectRedis(config,true);
const queue=new Queue("pi-interview-local-v1",{connection:publish});
const repository=new OperationRepository(pool),dispatcher=new OutboxDispatcher(repository,queue);
await publish.ping();
const liveEnabled=process.argv.includes('--live-pi09');
const worker=createTaskWorker(repository,consume,queue.name,{concurrency:1,assess:createPI09AssessmentExecutor(root,liveEnabled)});
await worker.waitUntilReady();
let dispatching:Promise<unknown>|null=null,closing=false;
function dispatch(){if(closing||dispatching)return;dispatching=dispatcher.dispatch().catch(()=>console.error("outbox_dispatch_failed_sql_retained")).finally(()=>{dispatching=null;});}
const timer=setInterval(dispatch,1000);dispatch();
console.log(JSON.stringify({status:"ready",mode:"local-development",queue:queue.name,pi09ModelEnabled:liveEnabled,maxAuthorizedCny:liveEnabled?2:0}));
async function close(){
 if(closing)return;closing=true;clearInterval(timer);await dispatching;
 await worker.close();await queue.close();await publish.quit();await consume.quit();await pool.end();
}
process.once("SIGINT",()=>void close());process.once("SIGTERM",()=>void close());

import {loadLocalConnection,createLocalPool} from '../../api/src/local-config.ts';
import {buildDevServer} from '../../api/src/server.ts';
const root=process.cwd(),database=process.argv[2];
if(!database||!/^pi09_[a-f0-9]{32}$/.test(database)||!process.send)throw Error('test_parent_and_database_required');
const c=loadLocalConnection(root),pool=createLocalPool(c,database),authPool=createLocalPool(c,database,true);
const app=buildDevServer(pool,authPool,{mode:'test',host:'127.0.0.1',port:3379,baseURL:'http://127.0.0.1:3379',secret:c.authSecret},{root});
await app.listen({host:'127.0.0.1',port:3379});process.send({ready:true});
process.once('message',async message=>{if(message!=='stop')return;await app.close();await authPool.end();await pool.end();process.disconnect();});

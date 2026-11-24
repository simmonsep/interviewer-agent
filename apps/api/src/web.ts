import {existsSync,readFileSync,readdirSync} from "node:fs";
import {join} from "node:path";
import type {FastifyInstance} from "fastify";
export function registerBuiltWeb(app:FastifyInstance,root:string){
 const directory=join(root,'apps/web/dist'),index=join(directory,'index.html');
 if(!existsSync(index))throw Error('web_build_required');
 const assets=new Map<string,{body:Buffer;type:string}>();
 for(const name of readdirSync(join(directory,'assets'))){
  if(!/^[a-zA-Z0-9_.-]+\.(js|css)$/.test(name))continue;
  assets.set(name,{body:readFileSync(join(directory,'assets',name)),type:name.endsWith('.css')?'text/css; charset=utf-8':'text/javascript; charset=utf-8'});
 }
 app.get('/',async(_req,reply)=>reply.type('text/html; charset=utf-8').header('Cache-Control','no-store').send(readFileSync(index)));
 app.get<{Params:{name:string}}>('/assets/:name',async(req,reply)=>{
  const asset=assets.get(req.params.name);if(!asset)return reply.code(404).send({code:'asset_unavailable'});
  return reply.type(asset.type).header('Cache-Control','public, max-age=31536000, immutable').send(asset.body);
 });
}

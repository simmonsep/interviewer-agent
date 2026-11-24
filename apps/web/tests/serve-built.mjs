// Test server: built assets + the exact development /api proxy. No old TS API.
import {createServer,loadConfigFromFile} from 'vite';
import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
const root=resolve('apps/web'),dist=resolve(root,'dist');
const loaded=await loadConfigFromFile({command:'serve',mode:'test'},resolve(root,'vite.config.ts'));
const activeStreams=new Set();
const proxy=loaded.config.server.proxy['/api'],configure=proxy.configure;
proxy.configure=(instance,options)=>{
 configure?.(instance,options);
 instance.on('proxyRes',(_upstream,incoming,response)=>{
  if(incoming.url?.includes('/events?')){activeStreams.add(response);response.once('close',()=>activeStreams.delete(response));}
 });
};
const server=await createServer({...loaded.config,configFile:false,plugins:[{
 name:'serve-built-test',configureServer(server){
  server.middlewares.use(async(req,res,next)=>{
   // Fault injection exists only in this test server, never in vite.config or the product API.
   if(req.url==='/__test/drop-sse'&&req.method==='POST'&&req.headers.origin==='http://127.0.0.1:5174'){
    const dropped=activeStreams.size;for(const stream of activeStreams)stream.destroy();
    res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({dropped}));
   }
   if(req.url?.startsWith('/api/'))return next();
   const pathname=new URL(req.url??'/','http://127.0.0.1').pathname;
   const path=pathname.startsWith('/assets/')?resolve(dist,'.'+pathname):resolve(dist,'index.html');
   if(!path.startsWith(dist+'/')&&!path.startsWith(dist+'\\')){res.statusCode=404;return res.end();}
   try{const data=await readFile(path);res.setHeader('Content-Type',path.endsWith('.js')?'text/javascript':path.endsWith('.css')?'text/css':'text/html');res.end(data);}catch{res.statusCode=404;res.end();}
  });
 }
}]});
await server.listen();

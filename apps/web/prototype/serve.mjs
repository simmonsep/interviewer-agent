// D-01-03 node-A synthetic prototype server. Static files only: no API, no
// database, no model calls. Everything is served from this directory.
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve,extname} from 'node:path';
import {fileURLToPath} from 'node:url';

const dir=fileURLToPath(new URL('.',import.meta.url));
const port=Number(process.env.D01_PROTOTYPE_PORT??5176);
const types={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8',
 '.js':'text/javascript; charset=utf-8','.mjs':'text/javascript; charset=utf-8',
 '.json':'application/json; charset=utf-8','.svg':'image/svg+xml','.png':'image/png'};

createServer(async(request,response)=>{
 try{
  const url=new URL(request.url,'http://127.0.0.1');
  const pathname=decodeURIComponent(url.pathname==='/'?'/index.html':url.pathname);
  const target=resolve(dir,'.'+pathname);
  if(!target.startsWith(resolve(dir))){response.writeHead(403);response.end('forbidden');return;}
  const data=await readFile(target);
  response.writeHead(200,{'Content-Type':types[extname(target)]??'application/octet-stream',
   'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});
  response.end(data);
 }catch{
  response.writeHead(404,{'Content-Type':'text/plain; charset=utf-8'});
  response.end('not found');
 }
}).listen(port,'127.0.0.1',()=>{
 console.log(`D-01-03 synthetic prototype ready: http://127.0.0.1:${port}/`);
 console.log('State is synthetic; no backend, no model calls, no network requests from the page.');
});

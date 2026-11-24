import {defineConfig} from 'vite';
import {fileURLToPath} from 'node:url';
import {readFileSync} from 'node:fs';
// Version B of the desktop frontend. It is a full copy of the repository, not a
// build flag: version A stays on 5174 with its own config, version B listens on
// 5175 so both windows can be open against the same local API (3100) at once.
// ApiBoundary only accepts Origin http://127.0.0.1:3100 on writes, so the proxy
// translates exactly this origin; foreign origins are still rejected by the API.
const pkg=JSON.parse(readFileSync(fileURLToPath(new URL('./package.json',import.meta.url)),'utf8')) as {version:string};
export default defineConfig({
 define:{__APP_VERSION__:JSON.stringify(pkg.version)},
 root:fileURLToPath(new URL('.',import.meta.url)),
 server:{host:'127.0.0.1',port:5175,strictPort:true,cors:false,proxy:{
  '/api':{target:'http://127.0.0.1:3100',configure(proxy){
   proxy.on('proxyReq',(request,incoming)=>{if(incoming.headers.origin==='http://127.0.0.1:5175')request.setHeader('Origin','http://127.0.0.1:3100');});
  }}
 }},build:{outDir:'dist-v2',emptyOutDir:true}
});

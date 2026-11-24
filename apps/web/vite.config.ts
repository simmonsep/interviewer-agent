import {defineConfig} from 'vite';
import {fileURLToPath} from 'node:url';
import {readFileSync} from 'node:fs';
// The version shown in the desktop status bar comes from package.json, not a literal,
// so a release cannot drift from what the window claims to be.
const pkg=JSON.parse(readFileSync(fileURLToPath(new URL('./package.json',import.meta.url)),'utf8')) as {version:string};
export default defineConfig({
 define:{__APP_VERSION__:JSON.stringify(pkg.version)},
 root:fileURLToPath(new URL('.',import.meta.url)),
 server:{host:'127.0.0.1',port:5174,strictPort:true,cors:false,proxy:{
  '/api':{target:'http://127.0.0.1:3100',configure(proxy){
   // Only our exact browser origin is translated. Foreign origins remain rejected by FastAPI.
   proxy.on('proxyReq',(request,incoming)=>{if(incoming.headers.origin==='http://127.0.0.1:5174')request.setHeader('Origin','http://127.0.0.1:3100');});
  }}
 }},build:{outDir:'dist',emptyOutDir:true}
});

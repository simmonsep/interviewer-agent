import {defineConfig} from 'vite';
import {fileURLToPath} from 'node:url';
// Two host pages are built here: the D-00 verification page and the F05 product
// startup page. Everything lands in apps/desktop/dist, which is what the Tauri
// build embeds, so both must come from this build - never from files dropped into
// dist by hand.
export default defineConfig({
  root:fileURLToPath(new URL('.',import.meta.url)),
  build:{outDir:'../desktop/dist',emptyOutDir:true,rollupOptions:{input:[
    fileURLToPath(new URL('desktop.html',import.meta.url)),
    fileURLToPath(new URL('startup.html',import.meta.url)),
  ]}},
});

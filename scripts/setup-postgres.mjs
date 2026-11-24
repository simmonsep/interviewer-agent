import {existsSync,mkdirSync,createWriteStream,readFileSync} from 'node:fs';
import {pipeline} from 'node:stream/promises';
import {Readable} from 'node:stream';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {dirname,resolve,join} from 'node:path';
import {fileURLToPath} from 'node:url';
if(process.platform!=='win32'||process.arch!=='x64')throw Error('windows_x64_bootstrap_only');
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const manifest=JSON.parse(readFileSync(join(root,'manifests/postgres-PI-07.json'),'utf8'));
const archive=join(root,'.bootstrap',manifest.filename),target=join(root,'.bootstrap/postgresql-18.6-5');
mkdirSync(dirname(archive),{recursive:true});
if(!existsSync(archive)) {
 const r=await fetch(manifest.url);if(!r.ok||!r.body)throw Error('postgres_download_failed');
 await pipeline(Readable.fromWeb(r.body),createWriteStream(archive,{flags:'wx'}));
}
if(createHash('sha256').update(readFileSync(archive)).digest('hex')!==manifest.sha256)throw Error('postgres_archive_hash_mismatch');
if(!existsSync(join(target,'pgsql/bin/postgres.exe'))) {
 mkdirSync(target,{recursive:true});
 const unpack=spawnSync('tar.exe',['-xf',archive,'-C',target,'pgsql/bin','pgsql/lib','pgsql/share'],{encoding:'utf8',windowsHide:true,timeout:120000});
 if(unpack.status!==0)throw Error('postgres_extract_failed');
}
const result=spawnSync(join(target,'pgsql/bin/postgres.exe'),['--version'],{encoding:'utf8',windowsHide:true});
if(result.status!==0||!result.stdout.includes('18.6'))throw Error('postgres_version_failed');
console.log(JSON.stringify({status:'ready',version:result.stdout.trim(),archive_sha256:manifest.sha256,note:'existing verified archive reused; no service installed'}));

import assert from "node:assert/strict";
import {test,after} from "node:test";
import {readFileSync,writeFileSync,mkdirSync,copyFileSync} from "node:fs";
import {join,resolve} from "node:path";
import {randomUUID} from "node:crypto";
import {Pool} from "pg";
import {migrate} from "../../../packages/storage/src/migrate.ts";
import {buildDevServer} from "../src/server.ts";
import {assertDevelopmentConfig} from "../src/auth.ts";
import {loadLocalConnection,createLocalPool} from "../src/local-config.ts";
const root=process.cwd(),c=loadLocalConnection(root);
const output=resolve(process.env.PI_PM107_OUTPUT??join(root,".private/pm107",randomUUID()));mkdirSync(output,{recursive:true});
const database="pi07_"+randomUUID().replaceAll("-","");
const admin=new Pool({host:c.host,port:c.port,user:c.adminUser,password:c.adminPassword,database:"postgres",max:1});
await admin.query(`CREATE DATABASE ${database} OWNER pi_app_owner`);
const pool=createLocalPool(c,database),authPool=createLocalPool(c,database,true);
const config={mode:"test",host:"127.0.0.1",port:3000,baseURL:"http://127.0.0.1:3000",secret:c.authSecret};
const migrations=join(root,"packages/storage/migrations");
await migrate(pool,migrations);
let app=buildDevServer(pool,authPool,config);
await app.ready();
const cases:{name:string;status:string}[]=[];
function check(name:string,fn:()=>Promise<unknown>|unknown){test(name,{timeout:15000},async()=>{await fn();cases.push({name,status:"passed"});});}
let cookieA="",cookieB="",idA="",idB="",userA="",userB="";
const password="Synthetic-test-password-"+randomUUID();
function headers(cookie:string,key?:string){return {cookie,origin:config.baseURL,...(key?{"idempotency-key":key}:{})};}
const payload={role_package_id:"agent-intern",level:"intern",focus:"tool-use"};
function cookie(response:{headers:Record<string,unknown>}){const set=response.headers["set-cookie"];const rows=Array.isArray(set)?set:[set];return rows.filter((x):x is string=>typeof x==="string").map(x=>x.split(";")[0]).join("; ");}
after(async()=>{
  writeFileSync(join(output,"cases.json"),JSON.stringify(cases,null,2)+"\n");
  await app.close();await authPool.end();await pool.end();
  // This exact database was created above exclusively for this test run.
  if(!/^pi07_[a-f0-9]{32}$/.test(database))throw Error("invalid_test_database");
  await admin.query(`DROP DATABASE ${database}`);await admin.end();
});
check("B01 real PostgreSQL18 loopback and non-superuser application role",async()=>{
  const r=await pool.query("SELECT version(),host(inet_server_addr()) AS host,(SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS superuser");
  assert.match(r.rows[0].version,/PostgreSQL 18\.6/);assert.equal(r.rows[0].host,"127.0.0.1");assert.equal(r.rows[0].superuser,false);
});
check("B02 migrations replay and concurrent runners do not reapply",async()=>{
  const results=await Promise.all([migrate(pool,migrations),migrate(pool,migrations)]);
  assert.ok(results.every(r=>r.applied.length===0&&r.skipped.length===5));
});
check("B03 changed migration rejected and failed DDL transaction rolls back",async()=>{
  const drift=join(output,"drift"),bad=join(output,"bad-migration");mkdirSync(drift);mkdirSync(bad);
  for(const path of [drift,bad])for(const name of ["001_better_auth.sql","002_training_targets.sql","003_target_deletion_state.sql","004_operations_outbox.sql","005_practice_sessions.sql"])copyFileSync(join(migrations,name),join(path,name));
  writeFileSync(join(drift,"001_better_auth.sql"),readFileSync(join(drift,"001_better_auth.sql"),"utf8")+"\n-- changed after apply\n");
  await assert.rejects(migrate(pool,drift),/migration_checksum_mismatch/);
  writeFileSync(join(bad,"006_bad.sql"),"CREATE TABLE app.rollback_probe(id integer); SELECT missing_column FROM app.rollback_probe;");
  await assert.rejects(migrate(pool,bad));
  assert.equal((await pool.query("SELECT to_regclass('app.rollback_probe') AS value")).rows[0].value,null);
  assert.equal((await pool.query("SELECT count(*)::int AS n FROM public.pi_schema_migrations")).rows[0].n,5);
});
check("B04 anonymous and forged identity header cannot authenticate",async()=>{
  const r=await app.inject({method:"GET",url:"/api/training-targets/",headers:{"x-user-id":"candidate-a"}});assert.equal(r.statusCode,401);
});
check("B05 Better Auth signup signin cookies and password hashing use real SQL",async()=>{
  for(const which of ["a","b"]){
    const response=await app.inject({method:"POST",url:"/api/auth/sign-up/email",headers:{origin:config.baseURL},payload:{name:`Synthetic ${which}`,email:`${which}@example.invalid`,password}});
    assert.equal(response.statusCode,200,response.body);
    const value=cookie(response);assert.ok(value);assert.match(String(response.headers["set-cookie"]),/HttpOnly/i);
    if(which==="a"){cookieA=value;userA=response.json().user.id;}else{cookieB=value;userB=response.json().user.id;}
  }
  const rows=await pool.query('SELECT password FROM auth.account');assert.equal(rows.rowCount,2);assert.ok(rows.rows.every(r=>r.password!==password&&r.password.length>32));
  const wrong=await app.inject({method:"POST",url:"/api/auth/sign-in/email",headers:{origin:config.baseURL},payload:{email:"a@example.invalid",password:"incorrect-password"}});assert.equal(wrong.statusCode,401);
});
check("B06 owner-scoped create and list support role_only without resume or JD",async()=>{
  const a=await app.inject({method:"POST",url:"/api/training-targets/",headers:headers(cookieA,"create-a-0001"),payload});
  const b=await app.inject({method:"POST",url:"/api/training-targets/",headers:headers(cookieB,"create-b-0001"),payload});
  assert.equal(a.statusCode,201,a.body);assert.equal(b.statusCode,201,b.body);idA=a.json().id;idB=b.json().id;
  assert.equal(a.json().user_id,userA);assert.equal(a.json().context_mode,"role_only");assert.equal(b.json().user_id,userB);
  const list=await app.inject({method:"GET",url:"/api/training-targets/",headers:headers(cookieA)});
  assert.deepEqual(list.json().items.map((x:{id:string})=>x.id),[idA]);assert.ok(!list.body.includes("request_hash"));
});
check("B07 cross-user reads and writes match missing-resource 404 and leave owner data unchanged",async()=>{
  const read=await app.inject({method:"GET",url:`/api/training-targets/${idA}`,headers:headers(cookieB)});
  const missing=await app.inject({method:"GET",url:`/api/training-targets/${randomUUID()}`,headers:headers(cookieB)});
  assert.equal(read.statusCode,404);assert.equal(read.body,missing.body);
  const write=await app.inject({method:"PATCH",url:`/api/training-targets/${idA}`,headers:headers(cookieB),payload:{expected_version:1,focus:"stolen"}});assert.equal(write.statusCode,404);
  const own=await app.inject({method:"GET",url:`/api/training-targets/${idA}`,headers:headers(cookieA)});assert.equal(own.json().focus,"tool-use");
  const temporary=await app.inject({method:"POST",url:"/api/training-targets/",headers:headers(cookieA,"revoked-target-001"),payload});
  const revoked=temporary.json().id;
  await pool.query("UPDATE app.training_targets SET deleted_at=now(),version=version+1 WHERE id=$1",[revoked]);
  assert.equal((await app.inject({method:"GET",url:`/api/training-targets/${revoked}`,headers:headers(cookieA)})).statusCode,404);
  assert.equal((await app.inject({method:"PATCH",url:`/api/training-targets/${revoked}`,headers:headers(cookieA),payload:{expected_version:2,focus:"revive"}})).statusCode,404);
  const retry=await app.inject({method:"POST",url:"/api/training-targets/",headers:headers(cookieA,"revoked-target-001"),payload});assert.equal(retry.statusCode,404);
  const list=await app.inject({method:"GET",url:"/api/training-targets/",headers:headers(cookieA)});assert.ok(!list.json().items.some((x:{id:string})=>x.id===revoked));
});
check("B08 extra owner/material fields and unsupported role rejected rather than silently stripped",async()=>{
  for(const extra of [{user_id:userB},{resume_id:"resume-b"},{role_package_id:"cpp-engineer"}]){
    const r=await app.inject({method:"POST",url:"/api/training-targets/",headers:headers(cookieA,"invalid-request-001"),payload:{...payload,...extra}});assert.equal(r.statusCode,400);
  }
});
check("B09 concurrent retries create one row; payload conflicts and user namespaces differ",async()=>{
  const requests=await Promise.all(Array.from({length:5},()=>app.inject({method:"POST",url:"/api/training-targets/",headers:headers(cookieA,"concurrent-key-01"),payload})));
  assert.equal(requests.filter(r=>r.statusCode===201).length,1);assert.ok(requests.every(r=>[200,201].includes(r.statusCode)));
  assert.equal(new Set(requests.map(r=>r.json().id)).size,1);
  const conflict=await app.inject({method:"POST",url:"/api/training-targets/",headers:headers(cookieA,"concurrent-key-01"),payload:{...payload,focus:"changed"}});assert.equal(conflict.statusCode,409);
  const other=await app.inject({method:"POST",url:"/api/training-targets/",headers:headers(cookieB,"concurrent-key-01"),payload});assert.equal(other.statusCode,201);
});
check("B10 expected version makes concurrent focus updates single-winner",async()=>{
  const responses=await Promise.all(["rag","memory"].map(focus=>app.inject({method:"PATCH",url:`/api/training-targets/${idA}`,headers:headers(cookieA),payload:{expected_version:1,focus}})));
  assert.deepEqual(responses.map(r=>r.statusCode).sort(),[200,409]);
  const own=await app.inject({method:"GET",url:`/api/training-targets/${idA}`,headers:headers(cookieA)});assert.equal(own.json().version,2);
});
check("B11 untrusted or missing Origin cannot mutate with an otherwise valid cookie",async()=>{
  for(const origin of ["https://evil.example",""]){const r=await app.inject({method:"POST",url:"/api/training-targets/",headers:{...headers(cookieA,"bad-origin-key-01"),origin},payload});assert.equal(r.statusCode,403);}
});
check("B12 logout and database session expiry invalidate original cookies",async()=>{
  const out=await app.inject({method:"POST",url:"/api/auth/sign-out",headers:headers(cookieA),payload:{}});assert.equal(out.statusCode,200);
  assert.equal((await app.inject({method:"GET",url:"/api/training-targets/",headers:headers(cookieA)})).statusCode,401);
  await pool.query('UPDATE auth.session SET "expiresAt"=now()-interval \'1 second\' WHERE "userId"=$1',[userB]);
  assert.equal((await app.inject({method:"GET",url:"/api/training-targets/",headers:headers(cookieB)})).statusCode,401);
});
check("B13 a new API instance reads persisted sessions and targets",async()=>{
  const login=await app.inject({method:"POST",url:"/api/auth/sign-in/email",headers:{origin:config.baseURL},payload:{email:"b@example.invalid",password}});assert.equal(login.statusCode,200);cookieB=cookie(login);
  await app.close();app=buildDevServer(pool,authPool,config);await app.ready();
  const r=await app.inject({method:"GET",url:`/api/training-targets/${idB}`,headers:headers(cookieB)});assert.equal(r.statusCode,200);assert.equal(r.json().user_id,userB);
});
check("B14 development identity configuration rejects production and public listeners",()=>{
  for(const changed of [{mode:"production"},{host:"0.0.0.0"},{baseURL:"http://example.com:3000"}])assert.throws(()=>assertDevelopmentConfig({...config,...changed}),/development_loopback_only/);
});
check("B15 real HTTP loopback health works without exposing a public listener",async()=>{
  await app.listen({host:"127.0.0.1",port:0});
  const address=app.server.address();assert.ok(address&&typeof address==="object");assert.equal(address.address,"127.0.0.1");
  const response=await fetch(`http://127.0.0.1:${address.port}/health`);assert.equal(response.status,200);assert.equal((await response.json()).mode,"local-development");
});

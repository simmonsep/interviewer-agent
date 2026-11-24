import {readFileSync} from "node:fs";
import {Pool} from "pg";
import {join} from "node:path";
export interface LocalConnection {host:string;port:number;adminUser:string;adminPassword:string;appUser:string;appPassword:string;database:string;authSecret:string}
export function loadLocalConnection(root:string):LocalConnection {
  const c=JSON.parse(readFileSync(join(root,".private/postgres-18/connection.json"),"utf8")) as LocalConnection;
  if(c.host!=="127.0.0.1"||c.port!==55439||c.database!=="pi_interview")throw Error("unsupported_local_database");return c;
}
export function createLocalPool(c:LocalConnection,database=c.database,auth=false) {
  return new Pool({host:c.host,port:c.port,user:c.appUser,password:c.appPassword,database,max:4,connectionTimeoutMillis:3000,options:auth?"-c search_path=auth,public":undefined});
}

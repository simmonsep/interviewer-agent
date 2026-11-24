import {dirname,resolve,join} from "node:path";
import {fileURLToPath} from "node:url";
import {migrate} from "../packages/storage/src/migrate.ts";
import {loadLocalConnection,createLocalPool} from "../apps/api/src/local-config.ts";
const root=resolve(dirname(fileURLToPath(import.meta.url)),"..");
const pool=createLocalPool(loadLocalConnection(root));
try {console.log(JSON.stringify({status:"passed",...await migrate(pool,join(root,"packages/storage/migrations"))}));}
finally {await pool.end();}

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";
import type { Pool } from "pg";

// One transaction + advisory lock serializes concurrent migration runners.
export async function migrate(pool: Pool, directory: string) {
  const client = await pool.connect();
  const applied: string[] = [], skipped: string[] = [];
  try {
    await client.query("BEGIN");
    await client.query("SELECT pg_advisory_xact_lock(607007)");
    await client.query("CREATE TABLE IF NOT EXISTS public.pi_schema_migrations (name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())");
    const files = readdirSync(directory).filter(n => /^\d{3}_[a-z0-9_]+\.sql$/.test(n)).sort();
    const old = await client.query<{name:string;sha256:string}>("SELECT name, sha256 FROM public.pi_schema_migrations ORDER BY name");
    if (old.rows.some(r => !files.includes(r.name))) throw new Error("applied_migration_missing");
    for (const name of files) {
      const sql = readFileSync(join(directory, name), "utf8");
      const hash = createHash("sha256").update(sql).digest("hex");
      const previous = old.rows.find(r => r.name === name);
      if (previous) {
        if (previous.sha256 !== hash) throw new Error("migration_checksum_mismatch");
        skipped.push(name); continue;
      }
      await client.query(sql);
      await client.query("INSERT INTO public.pi_schema_migrations(name, sha256) VALUES ($1,$2)", [name,hash]);
      applied.push(name);
    }
    await client.query("COMMIT"); return { applied, skipped };
  } catch(error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
}

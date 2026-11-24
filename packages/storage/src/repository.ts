import { createHash } from "node:crypto";
import type { Pool } from "pg";

export interface Principal { userId: string }
export interface TargetInput { role_package_id: "agent-intern"; level: "intern"; focus: string }
export interface TrainingTarget {
  id: string; user_id: string; role_package_id: string; role_package_version: string;
  level: string; focus: string; context_mode: "role_only"; version: number; created_at: Date;
}
export class ResourceError extends Error {
  status: number;
  constructor(code: string, status: number) { super(code); this.status=status; }
}
const columns = "id,user_id,role_package_id,role_package_version,level,focus,context_mode,version,created_at";
export class TargetRepository {
  private pool: Pool;
  constructor(pool: Pool) {this.pool=pool;}
  async create(principal: Principal, requestId: string, input: TargetInput) {
    const hash=createHash("sha256").update(JSON.stringify([input.role_package_id,input.level,input.focus])).digest("hex");
    const client=await this.pool.connect();
    try {
      await client.query("BEGIN");
      const inserted=await client.query<TrainingTarget>(`INSERT INTO app.training_targets(user_id,request_id,request_hash,role_package_id,role_package_version,level,focus) VALUES ($1,$2,$3,$4,'candidate-v1',$5,$6) ON CONFLICT(user_id,request_id) DO NOTHING RETURNING ${columns}`, [principal.userId,requestId,hash,input.role_package_id,input.level,input.focus]);
      let target=inserted.rows[0];
      if(!target) {
        const existing=await client.query<TrainingTarget & {request_hash:string;deleted_at:Date|null}>(`SELECT ${columns},request_hash,deleted_at FROM app.training_targets WHERE user_id=$1 AND request_id=$2`,[principal.userId,requestId]);
        if(existing.rows[0]?.deleted_at)throw new ResourceError("resource_unavailable",404);
        if(existing.rows[0]?.request_hash!==hash) throw new ResourceError("idempotency_conflict",409);
        const {request_hash: _hash,deleted_at:_deleted,...value}=existing.rows[0]; target=value;
      }
      await client.query("COMMIT"); return {target,created:inserted.rowCount===1};
    } catch(error) {await client.query("ROLLBACK");throw error;}
    finally {client.release();}
  }
  async list(principal: Principal) {
    return (await this.pool.query<TrainingTarget>(`SELECT ${columns} FROM app.training_targets WHERE user_id=$1 AND deleted_at IS NULL ORDER BY created_at,id LIMIT 100`,[principal.userId])).rows;
  }
  async get(principal: Principal, id: string) {
    const result=await this.pool.query<TrainingTarget>(`SELECT ${columns} FROM app.training_targets WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL`,[id,principal.userId]);
    if(!result.rows[0])throw new ResourceError("resource_unavailable",404); return result.rows[0];
  }
  async updateFocus(principal: Principal, id: string, expectedVersion: number, focus: string) {
    // owner is part of both the lookup and the conditional write; version prevents lost updates.
    await this.get(principal,id);
    const result=await this.pool.query<TrainingTarget>(`UPDATE app.training_targets SET focus=$4,version=version+1 WHERE id=$1 AND user_id=$2 AND version=$3 AND deleted_at IS NULL RETURNING ${columns}`,[id,principal.userId,expectedVersion,focus]);
    if(!result.rows[0])throw new ResourceError("version_conflict",409);return result.rows[0];
  }
}

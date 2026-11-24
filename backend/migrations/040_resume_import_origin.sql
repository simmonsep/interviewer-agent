-- F07Z-152 phase 1: a resume version can be the user's own text, with no model run.
--
-- `career_snapshot_immutable` is shared by eight immutable tables, so the position allowance in
-- 038 must live in a dedicated function. Restore the generic one first: the backfill below updates
-- career_material_drafts, whose trigger calls the generic function.
CREATE OR REPLACE FUNCTION app.career_snapshot_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'career_snapshot_immutable' USING ERRCODE='23514';
END $$;

CREATE OR REPLACE FUNCTION app.career_job_snapshot_position_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.position_id IS NOT NULL AND NEW.position_id IS DISTINCT FROM OLD.position_id THEN
  RAISE EXCEPTION 'career_snapshot_immutable' USING ERRCODE='23514';
 END IF;
 IF (to_jsonb(NEW) - 'position_id') IS DISTINCT FROM (to_jsonb(OLD) - 'position_id') THEN
  RAISE EXCEPTION 'career_snapshot_immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;

DROP TRIGGER career_snapshot_immutable ON app.career_job_snapshots;
CREATE TRIGGER career_snapshot_immutable BEFORE UPDATE ON app.career_job_snapshots
 FOR EACH ROW EXECUTE FUNCTION app.career_job_snapshot_position_immutable();

ALTER TABLE app.career_material_drafts
 ADD COLUMN origin text NOT NULL DEFAULT 'agent_rewrite'
 CHECK(origin IN ('agent_rewrite','user_edit','restore','uploaded'));

-- Drafts are immutable, so historical rows keep the column default; new revisions write
-- 'user_edit' explicitly. The shape check therefore accepts the default for old revision rows.
ALTER TABLE app.career_material_drafts DROP CONSTRAINT material_revision_shape;
ALTER TABLE app.career_material_drafts ADD CONSTRAINT material_revision_shape CHECK (
 (run_id IS NOT NULL AND parent_draft_id IS NULL AND request_id IS NULL AND request_hash IS NULL
   AND origin = 'agent_rewrite')
 OR (run_id IS NULL AND parent_draft_id IS NOT NULL AND request_id IS NOT NULL
   AND request_hash IS NOT NULL AND origin IN ('agent_rewrite','user_edit','restore'))
 OR (run_id IS NULL AND parent_draft_id IS NULL AND request_id IS NOT NULL
   AND request_hash IS NOT NULL AND origin = 'uploaded')
);

-- Human revisions append immutable drafts without inventing model runs.
ALTER TABLE app.career_material_drafts ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE app.career_material_drafts
 ADD COLUMN parent_draft_id uuid REFERENCES app.career_material_drafts(id) ON DELETE CASCADE,
 ADD COLUMN request_id text,
 ADD COLUMN request_hash text,
 ADD CONSTRAINT material_revision_shape CHECK (
  (run_id IS NOT NULL AND parent_draft_id IS NULL AND request_id IS NULL AND request_hash IS NULL)
  OR (run_id IS NULL AND parent_draft_id IS NOT NULL AND request_id IS NOT NULL AND request_hash IS NOT NULL AND length(request_hash)=64)
 ),
 ADD CONSTRAINT material_revision_request_unique UNIQUE(goal_id,request_id);

-- F07Z-152 phase 2.3: which resume version each position uses.
-- History is append-only; one active row per position. A new resume version never changes an
-- existing binding automatically.
CREATE TABLE app.career_position_bindings (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 position_id uuid NOT NULL REFERENCES app.career_positions(id) ON DELETE CASCADE,
 resume_draft_id uuid NOT NULL REFERENCES app.career_material_drafts(id) ON DELETE CASCADE,
 resume_version integer NOT NULL CHECK(resume_version >= 1),
 content_sha256 text NOT NULL CHECK(length(content_sha256)=64),
 active boolean NOT NULL DEFAULT true,
 request_id text,
 request_hash text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX career_position_bindings_active_idx
 ON app.career_position_bindings(position_id) WHERE active;
CREATE UNIQUE INDEX career_position_bindings_request_idx
 ON app.career_position_bindings(goal_id, request_id) WHERE request_id IS NOT NULL;
CREATE INDEX career_position_bindings_history_idx
 ON app.career_position_bindings(position_id, created_at DESC);
GRANT SELECT, INSERT, UPDATE, DELETE ON app.career_position_bindings TO py_interview_app;

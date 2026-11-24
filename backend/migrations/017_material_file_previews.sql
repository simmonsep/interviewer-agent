CREATE TABLE app.career_material_file_previews (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 goal_version integer NOT NULL, material_revision integer NOT NULL,
 request_id text NOT NULL, request_hash text NOT NULL, filename text NOT NULL,
 format text NOT NULL CHECK(format IN ('pdf','docx')), file_sha256 text NOT NULL,
 byte_length integer NOT NULL CHECK(byte_length BETWEEN 1 AND 2097152),
 body text NOT NULL CHECK(length(body) BETWEEN 1 AND 4000), content_sha256 text NOT NULL,
 metadata jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(goal_id,request_id)
);
CREATE TRIGGER career_material_file_preview_immutable BEFORE UPDATE ON app.career_material_file_previews
 FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
GRANT SELECT,INSERT,DELETE ON app.career_material_file_previews TO py_interview_app;
ALTER TABLE app.career_material_sources DROP CONSTRAINT career_material_sources_format_check;
ALTER TABLE app.career_material_sources ADD CHECK(format IN ('text','markdown','pdf','docx'));
ALTER TABLE app.career_material_sources ADD COLUMN file_preview_id uuid UNIQUE REFERENCES app.career_material_file_previews(id);

-- F07Z-57: manual practice records for external algorithm sites (P3.3 / F07H §4).
-- The product never judges algorithm questions; it records what the user did on a site
-- they opened in their own logged-in profile. Append-only: a record is a statement about
-- the past, so the same immutability trigger used by observations applies here.
CREATE TABLE app.career_practice_records (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
  request_id text NOT NULL,
  site_id text NOT NULL,
  site_label text NOT NULL,
  url text NOT NULL,
  title text NOT NULL,
  practiced_on date NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('SOLVED','PARTIAL','UNSOLVED','REVIEWED')),
  note text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (goal_id, request_id)
);
CREATE INDEX career_practice_records_goal_idx ON app.career_practice_records (goal_id, practiced_on DESC);
CREATE TRIGGER practice_record_immutable BEFORE UPDATE ON app.career_practice_records
 FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
GRANT SELECT,INSERT,DELETE ON app.career_practice_records TO py_interview_app;

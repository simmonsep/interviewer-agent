-- F07Z-152 phase 2.1: a stable position identity independent of the JD text.
-- A self-authored job's old identity was sha256(company+title+cities+JD), so fixing a typo made a
-- new job. Positions give the binding a stable key; snapshots keep their immutable content.
CREATE TABLE app.career_positions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 source_id text NOT NULL,
 source_job_id text NOT NULL,
 version integer NOT NULL DEFAULT 1 CHECK(version >= 1),
 current_snapshot_id uuid,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(goal_id, source_id, source_job_id)
);
CREATE INDEX career_positions_goal_idx ON app.career_positions(goal_id, created_at DESC);

-- Historical snapshots already carry the exact source triple, so the backfill is unambiguous.
INSERT INTO app.career_positions(goal_id, source_id, source_job_id)
 SELECT DISTINCT goal_id, source_id, source_job_id FROM app.career_job_snapshots;

ALTER TABLE app.career_job_snapshots
 ADD COLUMN position_id uuid REFERENCES app.career_positions(id) ON DELETE SET NULL;

-- The snapshot trigger froze every column. Allow only the one-time position assignment; the rest
-- of the row stays immutable.
CREATE OR REPLACE FUNCTION app.career_snapshot_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.position_id IS NOT NULL AND NEW.position_id IS DISTINCT FROM OLD.position_id THEN
  RAISE EXCEPTION 'career_snapshot_immutable' USING ERRCODE='23514';
 END IF;
 IF (to_jsonb(NEW) - 'position_id') IS DISTINCT FROM (to_jsonb(OLD) - 'position_id') THEN
  RAISE EXCEPTION 'career_snapshot_immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;

UPDATE app.career_job_snapshots s SET position_id = p.id
 FROM app.career_positions p
 WHERE p.goal_id = s.goal_id AND p.source_id = s.source_id
   AND p.source_job_id = s.source_job_id;

-- The pointer starts at the newest snapshot that belonged to each position at migration time.
UPDATE app.career_positions p SET current_snapshot_id = latest.id
 FROM (SELECT DISTINCT ON (position_id) position_id, id
         FROM app.career_job_snapshots
        WHERE position_id IS NOT NULL
        ORDER BY position_id, created_at DESC, id DESC) latest
 WHERE latest.position_id = p.id;

GRANT SELECT, INSERT, UPDATE, DELETE ON app.career_positions TO py_interview_app;

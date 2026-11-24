CREATE TABLE app.career_written_observations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 session_id uuid NOT NULL REFERENCES app.career_written_sessions(id) ON DELETE CASCADE,
 source_id text NOT NULL, request_id text NOT NULL, request_hash text NOT NULL,
 payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(goal_id,request_id), UNIQUE(session_id,source_id)
);
CREATE TABLE app.career_written_retractions (
 observation_id uuid PRIMARY KEY REFERENCES app.career_written_observations(id) ON DELETE CASCADE,
 reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER written_observation_immutable BEFORE UPDATE ON app.career_written_observations
 FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
CREATE TRIGGER written_retraction_immutable BEFORE UPDATE ON app.career_written_retractions
 FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
GRANT SELECT,INSERT,DELETE ON app.career_written_observations,app.career_written_retractions TO py_interview_app;

ALTER TABLE app.career_goals ADD COLUMN source_requests integer NOT NULL DEFAULT 0 CHECK(source_requests BETWEEN 0 AND 12);
ALTER TABLE app.career_research_runs
 ADD COLUMN mode text NOT NULL DEFAULT 'manual' CHECK(mode IN ('manual','agent')),
 ADD COLUMN parent_step_id uuid UNIQUE REFERENCES app.career_steps(id) ON DELETE CASCADE,
 ADD COLUMN model_requests integer NOT NULL DEFAULT 0 CHECK(model_requests BETWEEN 0 AND 6),
 ADD COLUMN source_requests integer NOT NULL DEFAULT 0 CHECK(source_requests BETWEEN 0 AND 12),
 ADD COLUMN agent_origin text CHECK(agent_origin IN ('scripted_protocol','real_model')),
 ADD CHECK((mode='agent')=(parent_step_id IS NOT NULL));
ALTER TABLE app.career_research_runs DROP CONSTRAINT career_research_runs_status_check;
ALTER TABLE app.career_research_runs ADD CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED','WAITING_CREDENTIALS'));
CREATE OR REPLACE FUNCTION app.career_research_input_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.goal_id,NEW.goal_version,NEW.request_id,NEW.request_hash,NEW.input_payload,NEW.mode,NEW.parent_step_id)
 IS DISTINCT FROM (OLD.goal_id,OLD.goal_version,OLD.request_id,OLD.request_hash,OLD.input_payload,OLD.mode,OLD.parent_step_id) THEN
  RAISE EXCEPTION 'career_research_input_immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
ALTER TABLE app.model_http_calls DROP CONSTRAINT model_http_calls_purpose_check;
ALTER TABLE app.model_http_calls ADD CHECK(purpose IN ('assessment','career_coordination','career_research'));
CREATE TABLE app.career_job_selections (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 goal_version integer NOT NULL, run_id uuid NOT NULL REFERENCES app.career_research_runs(id) ON DELETE CASCADE,
 snapshot_id uuid NOT NULL REFERENCES app.career_job_snapshots(id) ON DELETE CASCADE,
 acknowledged_unknown boolean NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(goal_id,goal_version)
);
CREATE TRIGGER career_selection_immutable BEFORE UPDATE ON app.career_job_selections
 FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
GRANT SELECT,INSERT,DELETE ON app.career_job_selections TO py_interview_app;

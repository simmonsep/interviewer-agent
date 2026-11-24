ALTER TABLE app.career_goals DROP CONSTRAINT career_goals_model_requests_check;
ALTER TABLE app.career_goals ADD CHECK(model_requests BETWEEN 0 AND 24);
ALTER TABLE app.career_goals ADD COLUMN material_model_requests integer NOT NULL DEFAULT 0 CHECK(material_model_requests BETWEEN 0 AND 12);
ALTER TABLE app.model_http_calls DROP CONSTRAINT model_http_calls_purpose_check;
ALTER TABLE app.model_http_calls ADD CHECK(purpose IN ('assessment','career_coordination','career_research','career_materials'));
CREATE TABLE app.career_material_state (
 goal_id uuid PRIMARY KEY REFERENCES app.career_goals(id) ON DELETE CASCADE,
 revision integer NOT NULL DEFAULT 0 CHECK(revision BETWEEN 0 AND 30)
);
CREATE TABLE app.career_material_sources (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 material_revision integer NOT NULL, request_id text NOT NULL, request_hash text NOT NULL,
 name text NOT NULL, format text NOT NULL CHECK(format IN ('text','markdown')), body text NOT NULL CHECK(length(body) BETWEEN 1 AND 4000),
 content_sha256 text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(goal_id,request_id)
);
CREATE TABLE app.career_material_runs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), job_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
 goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE, goal_version integer NOT NULL, material_revision integer NOT NULL,
 request_id text NOT NULL, request_hash text NOT NULL, input_payload jsonb NOT NULL,
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED','WAITING_CREDENTIALS')),
 revision bigint NOT NULL DEFAULT 1, attempt integer NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 3), fencing_token bigint NOT NULL DEFAULT 0,
 lease_until timestamptz, dispatched_at timestamptz, dispatch_count integer NOT NULL DEFAULT 0,
 model_requests integer NOT NULL DEFAULT 0 CHECK(model_requests BETWEEN 0 AND 6),
 candidate jsonb, result jsonb, error_code text, origin text CHECK(origin IN ('scripted_protocol','real_model')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(goal_id,request_id), CHECK((status='RUNNING')=(lease_until IS NOT NULL))
);
CREATE INDEX career_material_dispatch_idx ON app.career_material_runs(status,dispatched_at,lease_until);
CREATE TABLE app.career_material_confirmations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 run_id uuid NOT NULL REFERENCES app.career_material_runs(id) ON DELETE CASCADE, material_revision integer NOT NULL,
 request_id text NOT NULL, request_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(goal_id,request_id)
);
CREATE TABLE app.career_material_facts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 confirmation_id uuid NOT NULL REFERENCES app.career_material_confirmations(id) ON DELETE CASCADE,
 source_id uuid NOT NULL REFERENCES app.career_material_sources(id) ON DELETE CASCADE,
 kind text NOT NULL, quote text NOT NULL, start_char integer NOT NULL, end_char integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(goal_id,source_id,start_char,end_char,kind), CHECK(start_char>=0 AND end_char>start_char)
);
CREATE TABLE app.career_material_drafts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 run_id uuid NOT NULL UNIQUE REFERENCES app.career_material_runs(id) ON DELETE CASCADE,
 version integer NOT NULL, goal_version integer NOT NULL, material_revision integer NOT NULL,
 payload jsonb NOT NULL, markdown text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(goal_id,version)
);
CREATE FUNCTION app.career_material_input_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.goal_id,NEW.goal_version,NEW.material_revision,NEW.request_id,NEW.request_hash,NEW.input_payload)
 IS DISTINCT FROM (OLD.goal_id,OLD.goal_version,OLD.material_revision,OLD.request_id,OLD.request_hash,OLD.input_payload) THEN
  RAISE EXCEPTION 'career_material_input_immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER career_material_input_immutable BEFORE UPDATE ON app.career_material_runs FOR EACH ROW EXECUTE FUNCTION app.career_material_input_immutable();
CREATE TRIGGER career_material_source_immutable BEFORE UPDATE ON app.career_material_sources FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
CREATE TRIGGER career_material_fact_immutable BEFORE UPDATE ON app.career_material_facts FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
CREATE TRIGGER career_material_confirmation_immutable BEFORE UPDATE ON app.career_material_confirmations FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
CREATE TRIGGER career_material_draft_immutable BEFORE UPDATE ON app.career_material_drafts FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
GRANT SELECT,INSERT,UPDATE,DELETE ON app.career_material_state,app.career_material_runs TO py_interview_app;
GRANT SELECT,INSERT,DELETE ON app.career_material_sources,app.career_material_facts,app.career_material_confirmations,app.career_material_drafts TO py_interview_app;

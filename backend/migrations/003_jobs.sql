CREATE TABLE app.operations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id uuid NOT NULL REFERENCES auth.users(id),
 resource_id uuid NOT NULL REFERENCES app.training_targets(id),
 kind text NOT NULL CHECK(kind='update_target_focus'),
 request_id text NOT NULL CHECK(length(request_id) BETWEEN 8 AND 128),
 request_hash text NOT NULL CHECK(length(request_hash)=64),
 expected_version integer NOT NULL CHECK(expected_version>=1),
 input_focus text NOT NULL CHECK(length(input_focus)<=120),
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
 result jsonb, error_code text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), completed_at timestamptz,
 UNIQUE(user_id,kind,request_id)
);
CREATE TABLE app.background_jobs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 operation_id uuid NOT NULL UNIQUE REFERENCES app.operations(id),
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
 attempt integer NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 3),
 fencing_token bigint NOT NULL DEFAULT 0 CHECK(fencing_token>=0),
 lease_until timestamptz,
 ready_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK ((status='RUNNING') = (lease_until IS NOT NULL))
);
CREATE TABLE app.outbox_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 job_id uuid NOT NULL UNIQUE REFERENCES app.background_jobs(id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 dispatched_at timestamptz,
 dispatch_count integer NOT NULL DEFAULT 0 CHECK(dispatch_count>=0)
);
CREATE INDEX jobs_recovery_idx ON app.background_jobs(status,ready_at,lease_until);
CREATE INDEX operations_owner_created_idx ON app.operations(user_id,created_at,id);
GRANT SELECT,INSERT,UPDATE,DELETE ON app.operations,app.background_jobs,app.outbox_events TO py_interview_app;

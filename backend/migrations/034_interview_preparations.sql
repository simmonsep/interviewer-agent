-- S01: interview preparation workflow (plan -> human confirmation -> open one session).
-- One row per preparation. Unlike a whole interview this task is short-lived, so the attempt
-- columns (attempt/lease/fencing) live on the same row instead of a second *runs table.
CREATE TABLE app.career_interview_preparations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
 goal_version integer NOT NULL,
 material_revision integer NOT NULL,
 role text NOT NULL CHECK(role IN ('java-backend','ai-application')),
 mode text NOT NULL DEFAULT 'interview' CHECK(mode IN ('interview','coaching')),
 max_questions integer NOT NULL CHECK(max_questions BETWEEN 1 AND 6),
 duration_seconds integer NOT NULL CHECK(duration_seconds BETWEEN 60 AND 3600),
 draft_id uuid,
 resume_item_indexes jsonb NOT NULL DEFAULT '[]'::jsonb,
 request_id text NOT NULL,
 request_hash text NOT NULL,
 status text NOT NULL DEFAULT 'PENDING'
   CHECK(status IN ('PENDING','RUNNING','WAITING_FOR_USER','SUCCEEDED','FAILED','REJECTED','STALE','CANCELLED')),
 frozen jsonb,
 plan jsonb,
 plan_sha256 text,
 decision jsonb,
 session_id uuid REFERENCES app.career_interviews(id) ON DELETE SET NULL,
 error_code text,
 error_detail jsonb,
 attempt integer NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 3),
 fencing_token bigint NOT NULL DEFAULT 0,
 lease_until timestamptz,
 dispatched_at timestamptz,
 dispatch_count integer NOT NULL DEFAULT 0,
 version integer NOT NULL DEFAULT 1,
 revision bigint NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(goal_id, request_id)
);
CREATE INDEX career_interview_preparations_goal_idx
 ON app.career_interview_preparations(goal_id, created_at DESC);
CREATE INDEX career_interview_preparations_dispatch_idx
 ON app.career_interview_preparations(status, dispatched_at) WHERE status IN ('PENDING','RUNNING');
GRANT SELECT,INSERT,UPDATE,DELETE ON app.career_interview_preparations TO py_interview_app;

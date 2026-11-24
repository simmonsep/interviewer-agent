ALTER TABLE app.training_targets ADD CONSTRAINT target_id_owner_unique UNIQUE(id,user_id);
CREATE TABLE app.practice_sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id text NOT NULL REFERENCES auth."user"(id),
 target_id uuid NOT NULL,
 target_version integer NOT NULL CHECK(target_version>=1),
 request_id text NOT NULL,request_hash text NOT NULL,
 mode text NOT NULL DEFAULT 'practice' CHECK(mode='practice'),
 context_mode text NOT NULL DEFAULT 'role_only' CHECK(context_mode='role_only'),
 question_snapshot jsonb NOT NULL,
 version integer NOT NULL DEFAULT 1 CHECK(version>=1),
 status text NOT NULL DEFAULT 'WAITING_FOR_ANSWER' CHECK(status IN ('WAITING_FOR_ANSWER','EVALUATING','COMPLETED','CANCELLED','FAILED')),
 deleted_at timestamptz,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(target_id,user_id) REFERENCES app.training_targets(id,user_id),
 UNIQUE(user_id,request_id),UNIQUE(id,user_id)
);
CREATE INDEX practice_sessions_owner_created ON app.practice_sessions(user_id,created_at DESC);
CREATE TABLE app.answer_attempts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id text NOT NULL,
 session_id uuid NOT NULL UNIQUE,
 request_id text NOT NULL,request_hash text NOT NULL,
 original_text text NOT NULL CHECK(length(original_text) BETWEEN 1 AND 2000),
 origin text NOT NULL DEFAULT 'user_submitted' CHECK(origin='user_submitted'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(session_id,user_id) REFERENCES app.practice_sessions(id,user_id)
);
ALTER TABLE app.operations DROP CONSTRAINT operations_kind_check;
ALTER TABLE app.operations ADD CONSTRAINT operations_kind_check CHECK(kind IN ('update_target_focus','assess_answer'));
ALTER TABLE app.operations ALTER COLUMN input_focus SET DEFAULT '';
ALTER TABLE app.operations ADD COLUMN session_id uuid REFERENCES app.practice_sessions(id);
ALTER TABLE app.operations ADD COLUMN session_version integer;
ALTER TABLE app.operations ADD CONSTRAINT operation_session_kind CHECK(
 (kind='update_target_focus' AND session_id IS NULL AND session_version IS NULL) OR
 (kind='assess_answer' AND session_id IS NOT NULL AND session_version>=1));
CREATE UNIQUE INDEX assessment_operation_per_session ON app.operations(session_id) WHERE kind='assess_answer';
CREATE TABLE app.assessment_candidates (
 operation_id uuid PRIMARY KEY REFERENCES app.operations(id),
 input_hash text NOT NULL,candidate jsonb NOT NULL,metadata jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE app.assessment_revisions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id text NOT NULL,
 session_id uuid NOT NULL UNIQUE,
 operation_id uuid NOT NULL UNIQUE REFERENCES app.operations(id),
 answer_id uuid NOT NULL UNIQUE REFERENCES app.answer_attempts(id),
 candidate jsonb NOT NULL,metadata jsonb NOT NULL,
 semantic_scoring_eligible boolean NOT NULL DEFAULT false CHECK(semantic_scoring_eligible=false),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(session_id,user_id) REFERENCES app.practice_sessions(id,user_id)
);
CREATE TABLE app.business_events (
 event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 operation_id uuid NOT NULL REFERENCES app.operations(id),
 user_id text NOT NULL REFERENCES auth."user"(id),
 status text NOT NULL,error_code text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX business_events_operation_cursor ON app.business_events(operation_id,event_id);

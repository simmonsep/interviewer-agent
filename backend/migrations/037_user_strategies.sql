-- F07Z-164: user-imported project follow-up strategy cards.
-- Cards describe *how to probe*, never a question with an answer. Versions are append-only and a
-- session freezes the cards it started with; enabling only affects future sessions.
CREATE TABLE app.user_strategy_imports (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
 request_id text NOT NULL,
 request_hash text NOT NULL,
 document jsonb NOT NULL,
 content_sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(user_id, request_id)
);
CREATE INDEX user_strategy_imports_owner_idx ON app.user_strategy_imports(user_id, created_at DESC);

CREATE TABLE app.user_strategies (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
 strategy_set_id text NOT NULL CHECK(strategy_set_id ~ '^[a-z0-9][a-z0-9-]{0,59}$'),
 role text NOT NULL CHECK(role IN ('java-backend','ai-application')),
 version integer NOT NULL CHECK(version >= 1),
 revision bigint NOT NULL DEFAULT 1,
 enabled boolean NOT NULL DEFAULT true,
 author text NOT NULL DEFAULT '',
 content_sha256 text NOT NULL,
 document jsonb NOT NULL,
 request_id text NOT NULL,
 request_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(user_id, strategy_set_id, version),
 UNIQUE(user_id, request_id)
);
CREATE INDEX user_strategies_owner_idx ON app.user_strategies(user_id, role, strategy_set_id, version DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON app.user_strategies TO py_interview_app;
GRANT SELECT, INSERT, DELETE ON app.user_strategy_imports TO py_interview_app;

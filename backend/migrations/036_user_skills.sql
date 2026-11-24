-- F07Z-161: user-installed declarative skills (Agent Skills text subset).
-- A skill is user content: it never grants tools or changes state. Versions are append-only, and a
-- session freezes the body it started with, so disabling or upgrading never rewrites history.
CREATE TABLE app.user_skills (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
 skill_id text NOT NULL CHECK(skill_id ~ '^[a-z0-9][a-z0-9-]{0,59}$'),
 version integer NOT NULL CHECK(version >= 1),
 revision bigint NOT NULL DEFAULT 1,
 enabled boolean NOT NULL DEFAULT true,
 origin text NOT NULL DEFAULT 'user_install' CHECK(origin IN ('user_install')),
 author text NOT NULL DEFAULT '',
 content_sha256 text NOT NULL,
 document jsonb NOT NULL,
 request_id text NOT NULL,
 request_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(user_id, skill_id, version),
 UNIQUE(user_id, request_id)
);
CREATE INDEX user_skills_owner_idx ON app.user_skills(user_id, skill_id, version DESC);

-- One row per preview so the user can inspect name/description/files before committing.
CREATE TABLE app.user_skill_imports (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
 request_id text NOT NULL,
 request_hash text NOT NULL,
 document jsonb NOT NULL,
 content_sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(user_id, request_id)
);
CREATE INDEX user_skill_imports_owner_idx ON app.user_skill_imports(user_id, created_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON app.user_skills TO py_interview_app;
GRANT SELECT, INSERT, DELETE ON app.user_skill_imports TO py_interview_app;

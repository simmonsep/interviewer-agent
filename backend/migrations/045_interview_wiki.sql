CREATE TABLE app.interview_wiki_versions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
 source_event_id uuid NOT NULL REFERENCES app.career_interview_events(id) ON DELETE CASCADE,
 topic_key text NOT NULL,
 version integer NOT NULL CHECK(version > 0),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','active','archived','withdrawn')),
 content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[a-f0-9]{64}$'),
 document jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(user_id,topic_key,version),
 UNIQUE(user_id,topic_key,content_sha256)
);
CREATE UNIQUE INDEX interview_wiki_active_topic ON app.interview_wiki_versions(user_id,topic_key)
 WHERE status='active';
GRANT SELECT,INSERT ON app.interview_wiki_versions TO py_interview_app;
GRANT UPDATE(status) ON app.interview_wiki_versions TO py_interview_app;

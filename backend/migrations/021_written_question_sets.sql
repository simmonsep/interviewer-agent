CREATE TABLE app.career_written_answers (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), session_id uuid NOT NULL REFERENCES app.career_written_sessions(id) ON DELETE CASCADE,
 answer_seq integer NOT NULL CHECK(answer_seq>0), question_id text NOT NULL,
 request_id text NOT NULL, request_hash text NOT NULL, answer jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(session_id,answer_seq), UNIQUE(session_id,request_id)
);
CREATE TABLE app.career_written_reports (
 session_id uuid PRIMARY KEY REFERENCES app.career_written_sessions(id) ON DELETE CASCADE,
 payload jsonb NOT NULL, content_sha256 text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER written_answer_immutable BEFORE UPDATE ON app.career_written_answers
 FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
CREATE TRIGGER written_report_immutable BEFORE UPDATE ON app.career_written_reports
 FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
GRANT SELECT,INSERT,DELETE ON app.career_written_answers,app.career_written_reports TO py_interview_app;

-- First real HTTP 200 had no parseable usage. Preserve only safe wire diagnostics,
-- never Authorization/Cookie headers, full response bodies, answers or thinking.
ALTER TABLE app.model_http_calls ADD COLUMN response_meta jsonb;

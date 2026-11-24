-- F07Z-62: explicit desktop live mode. When the user turns real model calls on, the desktop
-- API (application role) creates its own capped authorization. The table already constrains
-- max_nano to 2 CNY; the code adds the "only when explicitly enabled" and cap checks.
GRANT INSERT ON app.model_authorizations TO py_interview_app;

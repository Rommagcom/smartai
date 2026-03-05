-- Enable RLS and policies for strict user-level isolation.
-- Use SET app.user_id = '<uuid>' per request in DB middleware.

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE long_term_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE cron_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE code_snippets ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_integrations ENABLE ROW LEVEL SECURITY;

CREATE POLICY sessions_isolation ON sessions
USING (user_id::text = current_setting('app.user_id', true));

CREATE POLICY messages_isolation ON messages
USING (user_id::text = current_setting('app.user_id', true));

CREATE POLICY ltm_isolation ON long_term_memory
USING (user_id::text = current_setting('app.user_id', true));

CREATE POLICY cron_isolation ON cron_jobs
USING (user_id::text = current_setting('app.user_id', true));

CREATE POLICY snippets_isolation ON code_snippets
USING (user_id::text = current_setting('app.user_id', true));

CREATE POLICY integration_isolation ON api_integrations
USING (user_id::text = current_setting('app.user_id', true));

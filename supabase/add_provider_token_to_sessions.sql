-- Add provider_token to nectar_sessions for fetching Discord guilds.
-- Idempotent: safe to run twice.

alter table public.nectar_sessions
  add column if not exists provider_token text;

-- Rollback:
-- alter table public.nectar_sessions drop column if exists provider_token;

-- Companion presence (v258): which script build a player runs, when they
-- were last seen with captures flowing, and AFK flag for /afk + /presence.
-- last_online is touched on every /api/capture batch (incl. heartbeats);
-- is-online is computed from its recency (no stale boolean to sweep).

alter table public.bngc_users
  add column if not exists companion_version text;
alter table public.bngc_users
  add column if not exists last_online timestamptz;
alter table public.bngc_users
  add column if not exists is_afk boolean not null default false;
alter table public.bngc_users
  add column if not exists afk_message text;

create index if not exists bngc_users_last_online_idx
  on public.bngc_users(last_online desc);

-- Rollback (run only to revert v258):
-- drop index if exists public.bngc_users_last_online_idx;
-- alter table public.bngc_users drop column if exists afk_message;
-- alter table public.bngc_users drop column if exists is_afk;
-- alter table public.bngc_users drop column if exists last_online;
-- alter table public.bngc_users drop column if exists companion_version;

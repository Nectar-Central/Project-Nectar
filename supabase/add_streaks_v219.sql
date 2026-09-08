-- v2.19.0 streak rewards (idempotent). Apply in Supabase SQL editor.
-- Rollback: DROP the 5 columns (see bottom).

alter table public.bngc_users add column if not exists daily_streak integer not null default 0;
alter table public.bngc_users add column if not exists last_daily_claim date;
alter table public.bngc_users add column if not exists win_streak integer not null default 0;
alter table public.bngc_users add column if not exists best_win_streak integer not null default 0;
alter table public.bngc_users add column if not exists best_match_kills integer not null default 0;

-- Rollback (run only to revert v2.19.0):
-- alter table public.bngc_users drop column if exists daily_streak;
-- alter table public.bngc_users drop column if exists last_daily_claim;
-- alter table public.bngc_users drop column if exists win_streak;
-- alter table public.bngc_users drop column if exists best_win_streak;
-- alter table public.bngc_users drop column if exists best_match_kills;

-- Play points ledger (idempotent). Apply in Supabase SQL editor.
-- play_points: activity ledger, only ever goes up (kill/win/knock/hourly).
-- play_points_hours_paid: hourly checkpoint (floor(playtime_global/3600)
-- already paid for), so replays and batch splits can never double-pay.

alter table public.bngc_users add column if not exists play_points integer not null default 0;
alter table public.bngc_users add column if not exists play_points_hours_paid integer not null default 0;

-- Rollback (run only to revert):
-- alter table public.bngc_users drop column if exists play_points;
-- alter table public.bngc_users drop column if exists play_points_hours_paid;

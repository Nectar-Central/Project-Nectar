-- v2.57 play-to-earn watermarks (idempotent). Apply in Supabase SQL editor.
-- The bot's earn loop credits 10 coins per kill / 50 per win (capped daily)
-- for new bngc_game_events rows. This table remembers the last credited
-- event id per user so each kill/win pays exactly once, restarts included.

create table if not exists public.bngc_earn_watermarks (
  discord_id text primary key references public.bngc_users(discord_id) on delete cascade,
  last_event_id bigint not null default 0,
  updated_at timestamptz not null default now()
);

alter table public.bngc_earn_watermarks enable row level security;
-- The bot uses the service-role key. It bypasses RLS.

-- Rollback (run only to revert v2.57):
-- drop table if exists public.bngc_earn_watermarks;

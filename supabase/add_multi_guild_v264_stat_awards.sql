-- Multi-guild follow-up (v264) — per-tournament result receipts.
-- Apply in the Supabase SQL editor. Idempotent: safe to run twice.
--
-- Run this only if you already applied add_multi_guild_v263.sql *before*
-- the "bank results" feature existed. A fresh install that runs v263 gets
-- both of these already; re-running v263 would also work, because every
-- statement in it is guarded.
--
-- What this adds:
--   1. nectar_guild_stat_awards — one row per (tournament, player) that has
--      been folded into a server's leaderboard. The primary key is what makes
--      banking idempotent: a retry can only insert rows that do not exist yet,
--      so a tournament's points can never be counted twice.
--   2. bngc_comp_tourneys.stats_applied_at — a cheap "this tournament is
--      banked" marker the dashboard uses to decide whether to show the
--      "Bank results" button.

create table if not exists public.nectar_guild_stat_awards (
  guild_id bigint not null references public.nectar_guilds(id) on delete cascade,
  tourney_id integer not null references public.bngc_comp_tourneys(id) on delete cascade,
  discord_id text not null,
  kills integer not null default 0,
  wins integer not null default 0,
  matches integer not null default 0,
  points integer not null default 0,
  pr numeric,
  applied_at timestamptz not null default now(),
  primary key (tourney_id, discord_id)
);

create index if not exists nectar_guild_stat_awards_guild_idx
  on public.nectar_guild_stat_awards(guild_id, applied_at desc);

alter table public.bngc_comp_tourneys
  add column if not exists stats_applied_at timestamptz;

-- Rollback (run only to revert):
-- alter table public.bngc_comp_tourneys drop column if exists stats_applied_at;
-- drop index if exists public.nectar_guild_stat_awards_guild_idx;
-- drop table if exists public.nectar_guild_stat_awards;

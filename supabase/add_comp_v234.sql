-- Comp Tourney PR (idempotent). Apply AFTER v233.
-- tourney_pr: latest computed PR per user (one row per discord_id in bngc_users).
-- bngc_comp_pr_history: one row per (tourney, player) per computation; computed
-- once at tourney end (server skips recompute when rows exist).

alter table public.bngc_users add column if not exists tourney_pr numeric;

create table if not exists public.bngc_comp_pr_history (
  tourney_id integer not null references public.bngc_comp_tourneys(id) on delete cascade,
  discord_id text not null,
  pr numeric not null,
  kills integer not null default 0,
  wins integer not null default 0,
  matches integer not null default 0,
  avg_placement numeric,
  players numeric not null default 0,
  computed_at timestamptz not null default now(),
  primary key (tourney_id, discord_id)
);

create index if not exists bngc_comp_pr_history_discord_idx
  on public.bngc_comp_pr_history (discord_id);

-- Rollback (run only to revert):
-- drop table if exists public.bngc_comp_pr_history;
-- alter table public.bngc_users drop column if exists tourney_pr;

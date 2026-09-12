-- Comp points adjustments ledger (idempotent). Apply AFTER v234.
-- Subtract (-pts) and manual point awards write here against GLOBAL tourney
-- standings (not per-match kills), so reconcile/calculate never snaps back.
-- bngc_comp_adjustments: one row per adjustment; standings sum deltas per
-- player on top of computed match points.

create table if not exists public.bngc_comp_adjustments (
  id bigserial primary key,
  tourney_id integer not null references public.bngc_comp_tourneys(id) on delete cascade,
  match_id integer references public.bngc_comp_matches(id) on delete set null,
  discord_id text not null,
  delta_points integer not null,
  reason text not null default '',
  created_by text not null default '',
  created_at timestamptz not null default now()
);

create index if not exists bngc_comp_adjustments_tourney_idx
  on public.bngc_comp_adjustments (tourney_id);
create index if not exists bngc_comp_adjustments_player_idx
  on public.bngc_comp_adjustments (tourney_id, discord_id);

-- Rollback (run only to revert):
-- drop table if exists public.bngc_comp_adjustments;

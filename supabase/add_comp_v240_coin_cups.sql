-- Comp coin cups (v240). Coin-prize tourneys: hosts flag the cup at
-- creation, the announcement shows the prize lines, and /tourney-pay-coins
-- pays standings top-3 once (coins_paid guards double-pay).

alter table public.bngc_comp_tourneys
  add column if not exists coin_give boolean not null default false;
alter table public.bngc_comp_tourneys
  add column if not exists coins_first integer not null default 0;
alter table public.bngc_comp_tourneys
  add column if not exists coins_second integer not null default 0;
alter table public.bngc_comp_tourneys
  add column if not exists coins_third integer not null default 0;
alter table public.bngc_comp_tourneys
  add column if not exists coins_paid boolean not null default false;

-- Rollback (run only to revert v240):
-- alter table public.bngc_comp_tourneys drop column if exists coins_paid;
-- alter table public.bngc_comp_tourneys drop column if exists coins_third;
-- alter table public.bngc_comp_tourneys drop column if exists coins_second;
-- alter table public.bngc_comp_tourneys drop column if exists coins_first;
-- alter table public.bngc_comp_tourneys drop column if exists coin_give;

-- v2.55 side markets (idempotent). Apply in Supabase SQL editor.
-- Bet types: 'win' (parimutuel pool, as before), 'out' (fixed odds:
-- picked player gets eliminated — live rooms only), 'kills' (fixed odds:
-- picked player reaches `line` kills). Fixed-odds multipliers lock at
-- placement in `odds` (payout = floor(stake * odds + 0.5)).
-- Requires add_match_betting_v253.sql first (v254 recommended too).

alter table public.bngc_match_bets add column if not exists bet_type text not null default 'win';
alter table public.bngc_match_bets add column if not exists line integer;
alter table public.bngc_match_bets add column if not exists odds numeric;

alter table public.bngc_match_bets drop constraint if exists bngc_match_bets_bet_type;
alter table public.bngc_match_bets
  add constraint bngc_match_bets_bet_type check (bet_type in ('win', 'out', 'kills'));

-- Rollback (run only to revert v2.55):
-- alter table public.bngc_match_bets drop constraint if exists bngc_match_bets_bet_type;
-- alter table public.bngc_match_bets drop column if exists odds;
-- alter table public.bngc_match_bets drop column if exists line;
-- alter table public.bngc_match_bets drop column if exists bet_type;

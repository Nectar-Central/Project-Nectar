-- Wager bans (v260). Owner-only /wager ban + /wager unban block a user from
-- creating, joining or accepting Nectar Coin duel wagers. Kept separate from
-- bngc_users.is_locked on purpose: a wager ban must not lock the rest of the
-- account, and the account lock stays self-service (/unlock).

create table if not exists public.bngc_wager_bans (
  discord_id text primary key,
  reason text not null default '',
  banned_by text not null default '',
  created_at timestamptz not null default now()
);

alter table public.bngc_wager_bans enable row level security;
-- The bot uses the service-role key. It bypasses RLS.

-- Rollback (run only to revert v260):
-- drop table if exists public.bngc_wager_bans;

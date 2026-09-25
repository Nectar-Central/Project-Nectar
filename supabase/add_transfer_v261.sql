-- Account transfer marker (admin /admin transfer).
-- Records that an account's profile + lifetime counters were moved to another
-- account, so the same data can never be transferred - or farmed by
-- re-registering the old account - twice.
-- Idempotent. Apply in the Supabase SQL editor before using the command.
--
-- Deliberately no foreign key: a transfer record is audit history and should
-- survive the target account being deleted.

alter table public.bngc_users add column if not exists transferred_to text;
alter table public.bngc_users add column if not exists transferred_at timestamptz;

create index if not exists bngc_users_transferred_to_idx
  on public.bngc_users (transferred_to);

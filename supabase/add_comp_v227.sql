-- Comp events lifecycle (idempotent). Apply in Supabase SQL editor AFTER v226.
-- Statuses: scheduled (announced, no matches yet) | open (running) |
-- closed (ended). Running/ended are DERIVED server-side (open + matches =
-- running, closed = ended), never stored as separate values.

alter table public.bngc_comp_tourneys drop constraint if exists bngc_comp_tourneys_status;
alter table public.bngc_comp_tourneys add constraint bngc_comp_tourneys_status
  check (status in ('scheduled', 'open', 'closed'));

alter table public.bngc_comp_tourneys add column if not exists scheduled_at timestamptz;
alter table public.bngc_comp_tourneys add column if not exists started_at timestamptz;
alter table public.bngc_comp_tourneys add column if not exists ended_at timestamptz;

-- Manual winner override per match (dashboard sets it when alv coverage is
-- thin; otherwise placement derives from alv death order, last alive wins).
alter table public.bngc_comp_matches add column if not exists winner_discord_id text;

-- Rollback (run only to revert):
-- alter table public.bngc_comp_matches drop column if exists winner_discord_id;
-- alter table public.bngc_comp_tourneys drop constraint bngc_comp_tourneys_status;
-- alter table public.bngc_comp_tourneys add constraint bngc_comp_tourneys_status check (status in ('open', 'closed'));
-- alter table public.bngc_comp_tourneys drop column if exists scheduled_at;
-- alter table public.bngc_comp_tourneys drop column if exists started_at;
-- alter table public.bngc_comp_tourneys drop column if exists ended_at;

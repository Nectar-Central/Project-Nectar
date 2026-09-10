-- Comp announcement fields (idempotent). Apply AFTER v227.
-- Feeds /tourney-post (announcement embed matching the host template) and
-- /tourney-schedule. Re-post edits the stored message (code filled later).

alter table public.bngc_comp_tourneys add column if not exists game_mode text;
alter table public.bngc_comp_tourneys add column if not exists region text not null default 'EU';
alter table public.bngc_comp_tourneys add column if not exists rules_channel_id text;
alter table public.bngc_comp_tourneys add column if not exists shoot_timer text not null default '4:30';
alter table public.bngc_comp_tourneys add column if not exists announce_channel_id text;
alter table public.bngc_comp_tourneys add column if not exists announce_message_id text;

-- Rollback (run only to revert):
-- alter table public.bngc_comp_tourneys drop column if exists game_mode;
-- alter table public.bngc_comp_tourneys drop column if exists region;
-- alter table public.bngc_comp_tourneys drop column if exists rules_channel_id;
-- alter table public.bngc_comp_tourneys drop column if exists shoot_timer;
-- alter table public.bngc_comp_tourneys drop column if exists announce_channel_id;
-- alter table public.bngc_comp_tourneys drop column if exists announce_message_id;

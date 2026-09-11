-- Comp manual scoring + host flag (idempotent). Apply AFTER v230.
-- placements: manual {discord_id: place} override per match (beats alv order).
-- manual: marks hand-added event rows (dashboard +/- kills).
-- host_playing: when false, the bridge ignores the host's own kills/alv.

alter table public.bngc_comp_matches add column if not exists placements jsonb not null default '{}'::jsonb;
alter table public.bngc_comp_events add column if not exists manual boolean not null default false;
alter table public.bngc_comp_tourneys add column if not exists host_playing boolean not null default true;

-- Rollback (run only to revert):
-- alter table public.bngc_comp_tourneys drop column if exists host_playing;
-- alter table public.bngc_comp_events drop column if exists manual;
-- alter table public.bngc_comp_matches drop column if exists placements;

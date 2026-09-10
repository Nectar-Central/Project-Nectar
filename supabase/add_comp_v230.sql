-- Comp registration UX (idempotent). Apply AFTER v228.
-- ping_role_id: role mentioned in the open/closed messages.
-- max_players: auto-close intake at this many ACCEPTED players (0 = unlimited).
-- team_color: per-team emoji (duos) assigned at intake.
-- mode/max_players enforced by bot + server convention (no new constraints
-- beyond sane ranges, to keep legacy rows valid).

alter table public.bngc_comp_tourneys add column if not exists ping_role_id text;
alter table public.bngc_comp_tourneys add column if not exists max_players integer not null default 0;
alter table public.bngc_comp_registrations add column if not exists team_color text;

-- Rollback (run only to revert):
-- alter table public.bngc_comp_tourneys drop column if exists ping_role_id;
-- alter table public.bngc_comp_tourneys drop column if exists max_players;
-- alter table public.bngc_comp_registrations drop column if exists team_color;

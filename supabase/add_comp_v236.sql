-- Comp registration display names (idempotent). Apply AFTER v235.
-- member_names: {discord_id: {username, display_name}} captured at @mention
-- intake. Dashboards list/options use these (no raw IDs); linking auto-maps
-- registered players and offers a display-name picker for the rest.

alter table public.bngc_comp_registrations
  add column if not exists member_names jsonb not null default '{}';

-- Rollback (run only to revert):
-- alter table public.bngc_comp_registrations drop column if exists member_names;

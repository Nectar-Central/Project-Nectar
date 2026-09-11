-- Comp per-tourney hosts (idempotent). Apply AFTER v232.
-- hosts: discord IDs co-managing this tourney (besides admins / the host
-- role). created_by is always treated as a host even if not listed.
-- host-status/add-host/remove-host (bot + dashboard) read and write it.

alter table public.bngc_comp_tourneys add column if not exists hosts text[] not null default '{}';

update public.bngc_comp_tourneys set hosts = ARRAY[created_by]
  where created_by is not null and created_by <> ''
  and (hosts is null or hosts = '{}');

-- Rollback (run only to revert):
-- alter table public.bngc_comp_tourneys drop column if exists hosts;

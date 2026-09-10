-- Comp tourney mode backfill (idempotent). Apply in Supabase SQL editor.
-- v226 gained its `mode` column in an amendment AFTER some deployments had
-- already applied it, so ensure it here instead of editing history.

alter table public.bngc_comp_tourneys add column if not exists mode text not null default 'solo';

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'bngc_comp_tourneys_mode') then
    alter table public.bngc_comp_tourneys add constraint bngc_comp_tourneys_mode
      check (mode in ('solo', 'duo'));
  end if;
end $$;

-- Rollback (run only to revert):
-- alter table public.bngc_comp_tourneys drop constraint if exists bngc_comp_tourneys_mode;
-- alter table public.bngc_comp_tourneys drop column if exists mode;

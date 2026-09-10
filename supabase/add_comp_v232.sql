-- Comp tourney lifecycle (idempotent). Apply AFTER v231.
-- created (fresh row) -> scheduled (datetime set) -> started (/tourney-start)
-- -> ended (/tourney-stop). Running/ended for display stay derived.
-- Data migration for pre-existing rows (open/closed era).

alter table public.bngc_comp_tourneys drop constraint if exists bngc_comp_tourneys_status;

update public.bngc_comp_tourneys set status = 'ended' where status = 'closed';
update public.bngc_comp_tourneys set status = 'started'
  where status = 'open'
  and (started_at is not null
    or id in (select distinct tourney_id from public.bngc_comp_matches));
update public.bngc_comp_tourneys set status = 'created' where status = 'open';

alter table public.bngc_comp_tourneys add constraint bngc_comp_tourneys_status
  check (status in ('created', 'scheduled', 'started', 'ended'));

-- Rollback (run only to revert; folds states back to the open/closed era):
-- alter table public.bngc_comp_tourneys drop constraint bngc_comp_tourneys_status;
-- update public.bngc_comp_tourneys set status = 'open' where status in ('created', 'scheduled', 'started');
-- update public.bngc_comp_tourneys set status = 'closed' where status = 'ended';
-- alter table public.bngc_comp_tourneys add constraint bngc_comp_tourneys_status
--   check (status in ('scheduled', 'open', 'closed'));

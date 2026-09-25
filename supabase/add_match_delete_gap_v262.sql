-- Deleted match versions (a gap that must stay a gap).
--
-- The series gate counts sealed/manual matches and the next version number is
-- max(surviving)+1, so deleting a match used to un-finish the series and
-- re-issue the deleted version number - the bridge then re-created the match
-- an admin had just deleted. Recording the version here keeps the consumed
-- slot consumed and the number retired.
--
-- Idempotent. Apply in the Supabase SQL editor.
--
-- Pre-migration the code reads this column defensively: a missing column
-- means no tombstones, i.e. the old behaviour, so applying this is what
-- actually enables the fix.

alter table public.bngc_comp_tourneys
  add column if not exists deleted_versions integer[] not null default '{}';

alter table public.bngc_ranked_tourneys
  add column if not exists deleted_versions integer[] not null default '{}';

-- Ranked tourneys v248: one-time ranked-points payout flag.
-- /rtourney stop pays each player's tourney-standings total into
-- ranked_points exactly once; this flag marks it done so retries and
-- double-stops never double-pay. Idempotent.
alter table public.bngc_ranked_tourneys
  add column if not exists points_paid boolean not null default false;

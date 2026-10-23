# Playing in a Nectar tournament

This is the player's guide. It covers signing up, playing, and finding your
results. Nothing here needs technical knowledge.

If you run a Discord server and want to host tournaments, read the
[admin guide](admin-guide.md) instead.

---

## What Nectar is

Nectar is a helper that runs tournaments inside a Discord server.

It does three things:

1. **Signups.** You press a button and you are in the tournament.
2. **Recording.** While you play, the host's game client quietly writes down
   kills, knocks and placements. Nobody has to screenshot anything.
3. **Results.** When the tournament ends, a scoreboard appears — and the
   points are added to that server's own leaderboard.

Each Discord server has its own tournaments and its own leaderboard. Playing
in one server does not change your standing in another. Your Discord account
is the same everywhere, but your score is per server.

---

## Signing up

1. Find the tournament post in the server. It has a **Join** button.
2. Press it.
3. If it is a **duo** tournament, mention your teammate in the same message —
   type `@` and pick their name from the list that appears. Typing a name by
   hand does not work; the mention has to be a real one.
4. Wait for the host to accept you. Some tournaments accept everyone
   automatically, some are checked by hand.

You will know it worked when your name shows up on the signup list.

**If the button does nothing:** the tournament is probably closed or full.
Ask the host.

---

## During the tournament

The host gives you a **room code** — five letters or numbers. Join that private
room in the game like you normally would.

Two rules that matter:

- **Do not leave the room early.** Placements are worked out from who was still
  alive at the end. Leaving counts as dying.
- **Play the matches the host announces.** The recorder follows the host's game,
  so a match starts and stops when theirs does.

You do not need to install anything, take screenshots, or send results to
anyone. If the host's recorder is running, your kills are already counted.

---

## Your results

Results live in two places:

**In Discord.** The host can post the scoreboard, and the bot answers
`/tourney-board` with the current standings.

**On the dashboard.** Open the link the host shares, or ask for it with
`/dashboard`. Sign in with Discord — the same account, no new password.

The dashboard shows:

- **Tournaments** — every tournament on that server, past and present.
- **Leaderboard** — everyone's totals for that server, all time.
- **Players** — the roster.

You can look at anything. You cannot change anything unless a server admin has
given you a staff role.

---

## What "PR" means

PR is a **rating** — one number that tries to say how strong a player is,
instead of just how much they have played.

- It starts at 1000.
- It goes up when you do well against other players and down when you do
  poorly.
- It is recalculated when a tournament ends, not during it.
- Not every tournament uses it. The host chooses. If a tournament has PR turned
  off, your rating does not move.

Like any rating, it is an estimate. A bad night is not a verdict on you.

---

## Points, kills and wins

The scoreboard tracks four things:

| Column | What it means |
| --- | --- |
| **Kills** | Eliminations credited to you |
| **Wins** | Matches you were the last one standing in |
| **Games** | How many matches you played |
| **Points** | Kills × the kill value, plus wins × the win value, plus placement points |

Every tournament can use different values for those — a host might make a win
worth more than three kills. The table is shown on the tournament page, so you
can always see how a result was scored.

---

## If something looks wrong

Results are recorded automatically, so occasionally a kill is missed or
credited to the wrong name. That is normal and fixable.

Tell the host:

- which tournament
- which match
- what should have happened

Hosts can correct a match by hand. Nothing is locked until the tournament ends.

Please do not argue with other players about it in the channel — take it to the
host.

---

## Fair play

Servers can ban a player from their tournaments. A ban applies to that server
only; it does not follow you elsewhere.

A ban usually means the host believes a result was faked, or that someone was
treated badly. If you think a ban was a mistake, message the server's admins
directly rather than arguing in public.

---

## Questions people actually ask

**Do I need to install anything?**
No. The host runs the recorder. You just play.

**Do I need to be in voice chat?**
That is the server's rule, not Nectar's.

**Can I play in two servers?**
Yes. Your points and rating are separate in each.

**Why did my rating go down after I won a match?**
Ratings compare you to the people you beat. Beating lower-rated players moves
you less than beating higher-rated ones — and a bad placement can outweigh a
win in the same tournament.

**Can I see tournaments from other servers?**
Only if you are staff on those servers. Otherwise you see the servers you
belong to.

**My name shows as a number.**
That means Nectar has not seen your in-game name yet. It fills in the first time
you play a recorded match.

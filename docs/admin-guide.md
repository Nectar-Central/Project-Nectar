# Running tournaments on your server

A guide for someone who has just been handed the keys to a Discord server on
Nectar. No technical background needed.

Read this once from top to bottom before your first tournament. It takes about
ten minutes.

---

## The short version

1. Invite the bot to your server.
2. Run `/setup` in the server. One click.
3. Sign in to the dashboard with Discord.
4. Create a tournament on the dashboard — it is a form, not a command.
5. Start it, let people sign up, play.
6. Close it. Results are saved to your server's leaderboard.

Everything that *sets up* a tournament happens on the dashboard. The slash
commands that remain are short ones for use during a live event.

---

## Step 1 — Invite the bot

Use the invite link the Nectar team gave you. You need the **Manage Server**
permission on your Discord server to add a bot.

Once it joins, it will sit there doing nothing until you run `/setup`. That is
deliberate: a bot that starts creating things the moment it is invited is a bot
that creates messes.

---

## Step 2 — Run `/setup`

Type `/setup` in any channel. You can also give the bot a name for your server:

```
/setup bot_name: Nectar EU
```

That name is only used in your server. Other servers can call it something
completely different.

`/setup` does three things:

- registers your server
- makes **you** the owner in Nectar
- remembers your server's name and icon

You only ever do this once. Running it again just refreshes the details.

> If you get "This server isn't connected to Nectar yet", `/setup` has not been
> run, or it was run by someone whose permissions have since changed. Run it
> again.

---

## Step 3 — Sign in to the dashboard

`/setup` replies with a link. Open it, press **Continue with Discord**, and
approve.

Nectar asks Discord for your username, avatar and server list. It never reads
your messages and never stores your Discord password.

You will land on a page listing your servers. Press one to open it.

**Being a Discord administrator is not enough on its own.** You appear in
Nectar because `/setup` made you the owner. If a colleague cannot get in, add
them from the **Staff** page — see step 5.

---

## Step 4 — Check your settings

Open **Settings**. Two things are worth doing before your first event.

**Bot nickname.** What the bot calls itself in your server. It applies within a
few minutes — the bot checks for changes on a timer, so you do not need to
restart anything.

**Default points table.** This is the scoring every new tournament starts with:

- **Per kill** — points for an elimination. Default 1.
- **Per win** — points for being last alive. Default 3.
- **Placement points** — points for finishing 1st, 2nd, 3rd… in order.
  Default `10,7,5,3,1`.

Changing this does **not** touch tournaments that already exist. It only sets
the starting values for the next one, and you can still change them per
tournament.

---

## Step 5 — Add your team

Open **Staff**. Paste a Discord user ID and pick a role.

To find someone's ID, right-click their name in Discord and choose **Copy User
ID**. (If that option is missing, turn on Developer Mode in Discord's advanced
settings.)

There are three roles:

| Role | Can do |
| --- | --- |
| **Owner** | Everything. There is exactly one, set by `/setup`, and it cannot be removed. |
| **Admin** | Create, edit, start and close tournaments. Manage staff. Ban players. |
| **Moderator** | Look at everything, ban players. Cannot change tournaments. |

Give **Moderator** to people who help during events but should not be able to
rewrite results. Give **Admin** only to people you would trust to run the whole
event alone.

The owner can never remove themselves, so a server can never lock itself out.

---

## Step 6 — Create your first tournament

Open **Tournaments** and press **New tournament**. It is a form. Nothing is
permanent until the tournament ends.

Here is what each field means.

**Name** — what players see. Keep it short: "Friday Solo Cup" beats
"FRIDAY NIGHT SOLO CUP #47 (EU ONLY)".

**Description** — optional. Rules, prize, anything players should know. This is
the right place for "no teaming" and "be in voice by 8".

**Format** — Solo or Duo. Solo means one player per signup. Duo means teams of
two, and players mention their teammate when they sign up.

**Game mode** — which mode you are playing. This is a label for players; it does
not change how anything is recorded.

**Room code** — the five-letter private room code. **Fill this in.** With a code,
the host's recorder knows exactly which tournament is live and attaches matches
automatically. Without one, you have to open matches by hand.

**Player limit** — the maximum number of players. `0` means no limit. For duos
this must be an even number, because a half-team cannot play.

**Games planned** — how many matches you intend to run. `0` means open-ended.
This is a target for the display; it does not stop you early.

**Scheduled start** — optional. Leave it empty to start manually. Setting it
shows the tournament in Discord's Events list.

**Region** and **Shoot timer** — both are labels for players.

**Points per match** — the scoring for this tournament only. It starts from your
server default and you can change it here.

**Host plays too** — if you are playing in your own tournament, leave this on.
If you are only hosting, turn it off so your own kills are not scored.

**Calculate PR** — whether closing this tournament updates everyone's rating.
Turn it off for casual events where you do not want ratings to move.

Press **Create tournament** when you are done. You land on its page.

---

## Step 7 — Run it

A tournament moves through four states: **Draft → Live → Ended**, with
**Scheduled** in between if you set a start time.

**Post it.** In Discord, run `/tourney-post <id>` with the number shown on the
tournament page. That posts the announcement with the **Join** button.

**Watch signups.** The **Signups** tab shows who pressed Join. You can:

- **Accept** or **Ban** a signup
- **Remove** a signup entirely
- **Add** someone yourself by pasting their Discord ID

Adding someone by hand is useful when a player cannot find the button.

**Start it.** Press **Start**. This closes signups and marks the tournament
live. In Discord, `/tourney-start <id>` also gives accepted players their
player role.

**Play.** Matches appear on the **Matches** tab as they are recorded. You do not
create them: the host's recorder opens a match when the game's lobby timer
starts and seals it when the match ends. If something is wrong, a match can be
corrected on the classic board — the **Matches** tab links to it.

**Close it.** Press **Close**. This ends the tournament, calculates ratings if
you left PR on, and saves the results.

That last part — saving the results — is worth understanding.

---

## What "bank results" means

Closing a tournament writes the final numbers to your server's leaderboard.
This is called **banking**.

It is safe to do twice. Banking is designed so the same tournament can never
add its points to somebody's total more than once. If you close a tournament,
then press **Bank results** again a week later, nothing doubles.

You would use the button when:

- the tournament was closed from Discord rather than the dashboard
- the close was interrupted — a dropped connection, a closed laptop
- you corrected a match after closing and want the fix to count

If a tournament has ended but its results were never banked, its page shows a
**Bank results** button. Press it. That is all it does.

> Rule of thumb: if a tournament is **Ended** and there is no **Bank results**
> button, it is banked. Nothing to do.

---

## The leaderboard

**Leaderboard** shows every player who has ever played a tournament on your
server, sorted by points.

- It is **your server's** leaderboard. Another server running the same
  tournaments has its own, with its own totals, for the same people.
- Points are added when a tournament is banked, not per match.
- Ratings (PR) are the player's most recent value, not a sum.

**Players** is the roster behind it: search by name or Discord ID, and ban
someone if you need to.

A ban is per server. It does not follow the player anywhere else, and it does
not delete their history.

---

## The slash commands you still need

You no longer create tournaments with a command — that was a fourteen-option
form pretending to be a chat message, and it was easy to get wrong.

These are the commands worth knowing during an event:

| Command | What it does |
| --- | --- |
| `/setup` | Connect a server. Once, by an administrator. |
| `/dashboard` | Get the link to your server's dashboard. |
| `/tourney-create` | Quick-create with just a name. Finish the setup on the dashboard. |
| `/tourney-post <id>` | Post the announcement with the Join button. |
| `/tourney-start <id>` | Start it and hand out the player role. |
| `/tourney-stop <id>` | Close it from Discord. Results are banked automatically. |
| `/tourney-board <id>` | Show the current standings. |
| `/tourney-pr <id>` | Show the rating board. |
| `/tourney-list` | List tournaments. |

Everything else — points tables, player limits, schedules, ping roles — lives
on the dashboard, where you can see what you are changing.

---

## Before your first event: a checklist

- [ ] `/setup` has been run in the server
- [ ] You can open the dashboard and see your server
- [ ] The bot nickname is what you want
- [ ] The default points table matches your rules
- [ ] At least one other person has Admin or Moderator
- [ ] Your first tournament has a **room code** filled in
- [ ] You know where the **Close** button is

---

## When something goes wrong

**"This server isn't connected to Nectar yet"**
`/setup` has not been run in this server. An administrator needs to run it.

**The dashboard shows no servers**
You are not staff on any server in Nectar. Either `/setup` has not been run, or
the owner has not added you from the **Staff** page.

**A player cannot join**
The tournament may be full, signups may be closed, or they may be banned from
that server. Check the **Signups** tab.

**The scoreboard is empty but matches were played**
Matches are only counted once they are sealed. Open the **Matches** tab — if
matches show as open rather than sealed, the recording stopped early. A host can
seal or correct them on the classic board.

**A kill is credited to the wrong player**
Correct the match on the classic board, then press **Bank results** if the
tournament has already ended.

**Someone's rating looks wrong**
Ratings are recalculated from the tournament's own results. If a match was
corrected after closing, re-bank the tournament.

**A tournament ended by accident**
There is no un-end button. Create a new tournament and, if you need the numbers
to be right, correct the old one before banking. This is why **Delete** only
works while a tournament is still a draft.

**The bot's name did not change**
Nickname changes are picked up on a timer — up to five minutes. If it still has
not changed after that, check that the bot has permission to change its own
nickname in your server settings.

---

## Housekeeping, or: how not to annoy your players

- **Fill in the room code.** It is the difference between results that appear
  by themselves and results you fix by hand afterwards.
- **Turn off "Host plays too" if you are not playing.** Otherwise your own
  kills land on the board and players notice.
- **Do not delete tournaments that have been played.** Delete only works on
  drafts, on purpose. Ending is the honest way to retire one.
- **Close before you announce the winner.** Points are only final once the
  tournament is closed and banked.
- **Tell people the rules in the description**, not in a channel message that
  scrolls away by the time the tournament starts.

That is everything. Run one small tournament before you run a big one — the
first one is where you find out what your own rules were missing.

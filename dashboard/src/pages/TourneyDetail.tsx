import { useState } from "react";
import { Link, useOutletContext, useParams } from "react-router-dom";
import { ArrowLeft, Database, Play, Square, Trash2, UserPlus } from "lucide-react";
import { api, type Registration } from "@/lib/api";
import { useAsync } from "@/lib/auth";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  EmptyState,
  Field,
  Input,
  Select,
  Skeleton,
  Switch,
} from "@/components/ui";
import { cn, formatDate, statusLabel, statusTone } from "@/lib/utils";

type Ctx = { guild: { id: number; name: string }; canManage: boolean };

const TABS = ["Standings", "Signups", "Matches", "Settings"] as const;
type Tab = (typeof TABS)[number];

export function TourneyDetailPage() {
  const { guildId, tourneyId } = useParams();
  const { canManage } = useOutletContext<Ctx>();
  const gid = Number(guildId);
  const tid = Number(tourneyId);

  const { data, loading, error, reload } = useAsync(() => api.tourney(gid, tid), [gid, tid]);
  const [tab, setTab] = useState<Tab>("Standings");
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (loading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-20" />
        <Skeleton className="h-40" />
      </div>
    );
  }
  if (error || !data) {
    return (
      <EmptyState
        title="Tournament not found"
        hint={error || "It may have been deleted."}
        action={
          <Link to={`/dashboard/${gid}/tourneys`}>
            <Button variant="secondary" size="sm">
              Back to tournaments
            </Button>
          </Link>
        }
      />
    );
  }

  const t = data.tourney;
  const live = t.status === "started";
  const ended = t.status === "ended";

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setNotice(null);
    try {
      await fn();
      reload();
      setNotice(`${label} done.`);
    } catch (e) {
      setNotice(e instanceof Error ? e.message : `${label} failed`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-5">
      <Link
        to={`/dashboard/${gid}/tourneys`}
        className="inline-flex items-center gap-1.5 text-xs text-muted hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" /> All tournaments
      </Link>

      <Card>
        <CardBody className="pt-5">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-xl font-semibold tracking-tight">{t.name}</h1>
                <Badge className={statusTone(t.status)}>{statusLabel(t.status)}</Badge>
                <Badge className="border-border bg-white/5 capitalize text-muted">{t.mode}</Badge>
                {t.pr_enabled ? (
                  <Badge className="border-border bg-white/5 text-muted">PR</Badge>
                ) : null}
              </div>
              <p className="mt-1.5 text-xs text-muted">
                #{t.id} · {t.code ? `room ${t.code}` : "no room code"} ·{" "}
                {t.game_mode || "any mode"} · {t.region || "EU"} ·{" "}
                {formatDate(t.scheduled_at || t.created_at)}
              </p>
              {t.description ? (
                <p className="mt-2 max-w-2xl text-sm text-muted">{t.description}</p>
              ) : null}
            </div>

            {canManage ? (
              <div className="flex flex-wrap items-center gap-2">
                {!live && !ended ? (
                  <Button
                    variant="success"
                    size="sm"
                    disabled={busy !== null}
                    onClick={() => act("Start", () => api.startTourney(gid, tid))}
                  >
                    <Play className="h-3.5 w-3.5" /> Start
                  </Button>
                ) : null}
                {!ended ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={busy !== null}
                    onClick={() => act("Close", () => api.stopTourney(gid, tid))}
                  >
                    <Square className="h-3.5 w-3.5" /> Close
                  </Button>
                ) : null}
                {t.status === "created" ? (
                  <Button
                    variant="danger"
                    size="sm"
                    disabled={busy !== null}
                    onClick={() => {
                      if (!confirm("Delete this tournament and all of its signups?")) return;
                      void act("Delete", async () => {
                        await api.deleteTourney(gid, tid);
                        history.back();
                      });
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5" /> Delete
                  </Button>
                ) : null}
                {ended && !t.stats_applied_at ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={busy !== null}
                    onClick={() => act("Bank results", () => api.bankTourney(gid, tid))}
                    title="Add this tournament's kills, wins and points to the server leaderboard"
                  >
                    <Database className="h-3.5 w-3.5" /> Bank results
                  </Button>
                ) : null}
              </div>
            ) : null}
          </div>

          {notice ? (
            <div className="mt-4">
              <Alert tone={notice.includes("failed") || notice.includes("could not") ? "error" : "info"}>
                {notice}
              </Alert>
            </div>
          ) : null}
        </CardBody>
      </Card>

      <div className="flex gap-1 overflow-x-auto border-b border-border">
        {TABS.map((name) => (
          <button
            key={name}
            onClick={() => setTab(name)}
            className={cn(
              "-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm transition-colors",
              tab === name
                ? "border-primary text-foreground"
                : "border-transparent text-muted hover:text-foreground"
            )}
          >
            {name}
          </button>
        ))}
      </div>

      {tab === "Standings" ? (
        <StandingsTab rows={data.standings} pr={data.pr} />
      ) : tab === "Signups" ? (
        <SignupsTab
          gid={gid}
          tid={tid}
          mode={t.mode}
          maxPlayers={t.max_players}
          regs={data.registrations}
          canManage={canManage}
          onChanged={reload}
        />
      ) : tab === "Matches" ? (
        <MatchesTab matches={data.matches} />
      ) : (
        <SettingsTab gid={gid} tid={tid} tourney={t} canManage={canManage} onSaved={reload} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Standings
// ---------------------------------------------------------------------------

function StandingsTab({
  rows,
  pr,
}: {
  rows: Array<{
    discord_id: string;
    name: string;
    kills: number;
    wins: number;
    points: number;
    matches: number;
    avg_placement?: number | null;
  }>;
  pr: Array<{ discord_id: string; pr: number }>;
}) {
  if (!rows.length) {
    return (
      <EmptyState
        title="No standings yet"
        hint="Standings appear once a match is sealed — either automatically from the host capture or manually from the old dashboard."
      />
    );
  }
  const prBy = new Map(pr.map((p) => [String(p.discord_id), p.pr]));
  return (
    <Card>
      <CardBody className="pt-4">
        <div className="overflow-x-auto scroll-thin">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-muted">
                <th className="pb-2 pr-2 font-medium">#</th>
                <th className="pb-2 pr-2 font-medium">Player</th>
                <th className="pb-2 pr-2 text-right font-medium">Kills</th>
                <th className="pb-2 pr-2 text-right font-medium">Wins</th>
                <th className="pb-2 pr-2 text-right font-medium">Games</th>
                <th className="pb-2 pr-2 text-right font-medium">Avg place</th>
                <th className="pb-2 pr-2 text-right font-medium">Points</th>
                <th className="pb-2 text-right font-medium">PR</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((r, i) => (
                <tr key={r.discord_id}>
                  <td className="py-2 pr-2 tabular-nums text-muted">{i + 1}</td>
                  <td className="py-2 pr-2">{r.name}</td>
                  <td className="py-2 pr-2 text-right tabular-nums">{r.kills}</td>
                  <td className="py-2 pr-2 text-right tabular-nums">{r.wins}</td>
                  <td className="py-2 pr-2 text-right tabular-nums text-muted">{r.matches}</td>
                  <td className="py-2 pr-2 text-right tabular-nums text-muted">
                    {r.avg_placement != null ? Number(r.avg_placement).toFixed(1) : "—"}
                  </td>
                  <td className="py-2 pr-2 text-right font-semibold tabular-nums">{r.points}</td>
                  <td className="py-2 text-right tabular-nums text-violet-300">
                    {prBy.has(String(r.discord_id))
                      ? Math.round(Number(prBy.get(String(r.discord_id))))
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardBody>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Signups
// ---------------------------------------------------------------------------

function SignupsTab({
  gid,
  tid,
  mode,
  maxPlayers,
  regs,
  canManage,
  onChanged,
}: {
  gid: number;
  tid: number;
  mode: string;
  maxPlayers: number;
  regs: Registration[];
  canManage: boolean;
  onChanged: () => void;
}) {
  const [discordId, setDiscordId] = useState("");
  const [partner, setPartner] = useState("");
  const [teamLabel, setTeamLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const accepted = regs.filter((r) => r.status === "accepted");
  const slots = accepted.reduce((n, r) => n + (r.member_ids?.length || 0), 0);

  const add = async () => {
    setError(null);
    setBusy(true);
    try {
      const member_ids = [discordId.trim(), mode === "duo" ? partner.trim() : ""].filter(Boolean);
      await api.addRegistration(gid, tid, { member_ids, team_label: teamLabel || undefined });
      setDiscordId("");
      setPartner("");
      setTeamLabel("");
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not add player");
    } finally {
      setBusy(false);
    }
  };

  const setStatus = async (regId: number, status: string) => {
    setBusy(true);
    try {
      await api.setRegistrationStatus(gid, tid, regId, status);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not update signup");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (regId: number) => {
    setBusy(true);
    try {
      await api.deleteRegistration(gid, tid, regId);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not remove signup");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      {canManage ? (
        <Card>
          <CardBody className="pt-5">
            <h2 className="text-sm font-semibold">Add a signup</h2>
            <p className="mb-3 text-xs text-muted">
              Paste Discord IDs — the same thing the signup button does in Discord, without the
              button.
            </p>
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label={mode === "duo" ? "Captain Discord ID" : "Discord ID"}>
                <Input
                  value={discordId}
                  onChange={(e) => setDiscordId(e.target.value)}
                  placeholder="123456789012345678"
                  inputMode="numeric"
                />
              </Field>
              {mode === "duo" ? (
                <Field label="Partner Discord ID">
                  <Input
                    value={partner}
                    onChange={(e) => setPartner(e.target.value)}
                    placeholder="123456789012345678"
                    inputMode="numeric"
                  />
                </Field>
              ) : null}
              <Field label="Team name" hint="Optional label for the board.">
                <Input
                  value={teamLabel}
                  onChange={(e) => setTeamLabel(e.target.value)}
                  placeholder="Team Nectar"
                  maxLength={40}
                />
              </Field>
            </div>
            {error ? (
              <div className="mt-3">
                <Alert tone="error">{error}</Alert>
              </div>
            ) : null}
            <div className="mt-4 flex items-center justify-between">
              <p className="text-xs text-muted">
                {maxPlayers
                  ? `${slots} / ${maxPlayers} slots taken`
                  : `${slots} player${slots === 1 ? "" : "s"} signed up`}
              </p>
              <Button onClick={add} disabled={busy || !discordId.trim()}>
                <UserPlus className="h-3.5 w-3.5" /> Add
              </Button>
            </div>
          </CardBody>
        </Card>
      ) : null}

      {regs.length === 0 ? (
        <EmptyState
          title="Nobody signed up yet"
          hint="Players can sign up from the Discord post, or you can add them above."
        />
      ) : (
        <Card>
          <CardBody className="pt-4">
            <ul className="divide-y divide-border">
              {regs.map((r) => (
                <li key={r.id} className="flex flex-wrap items-center gap-2 py-2.5">
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm">
                      {r.team_label || r.member_ids.join(" + ")}
                    </span>
                    <span className="block truncate font-mono text-[11px] text-muted">
                      {r.member_ids.join(", ")}
                    </span>
                  </span>
                  <Badge
                    className={cn(
                      "capitalize",
                      r.status === "accepted"
                        ? "border-success/30 bg-success/10 text-success"
                        : r.status === "banned"
                          ? "border-danger/30 bg-danger/10 text-danger"
                          : "border-border bg-white/5 text-muted"
                    )}
                  >
                    {r.status}
                  </Badge>
                  {canManage ? (
                    <div className="flex gap-1">
                      {r.status !== "accepted" ? (
                        <Button size="sm" variant="ghost" disabled={busy} onClick={() => setStatus(r.id, "accepted")}>
                          Accept
                        </Button>
                      ) : null}
                      {r.status !== "banned" ? (
                        <Button size="sm" variant="ghost" disabled={busy} onClick={() => setStatus(r.id, "banned")}>
                          Ban
                        </Button>
                      ) : null}
                      <Button size="sm" variant="ghost" disabled={busy} onClick={() => remove(r.id)}>
                        Remove
                      </Button>
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Matches
// ---------------------------------------------------------------------------

function MatchesTab({ matches }: { matches: Array<{
  id: number;
  version: number;
  status: string;
  started_at?: string | null;
  sealed_at?: string | null;
  winner_discord_id?: string | null;
}> }) {
  if (!matches.length) {
    return (
      <EmptyState
        title="No matches recorded"
        hint="A match opens automatically when the host's capture sees the lobby timer start (stG 1) and seals when it ends (stG 3). You can also open one from the classic dashboard."
      />
    );
  }
  return (
    <Card>
      <CardBody className="pt-4">
        <ul className="divide-y divide-border">
          {matches.map((m) => (
            <li key={m.id} className="flex items-center gap-3 py-2.5 text-sm">
              <span className="w-14 shrink-0 font-mono text-xs text-muted">v{m.version}</span>
              <span className="min-w-0 flex-1">
                <span className="block">
                  Match #{m.id}
                  {m.winner_discord_id ? (
                    <span className="ml-2 text-xs text-muted">
                      winner {m.winner_discord_id.slice(0, 8)}
                    </span>
                  ) : null}
                </span>
                <span className="block text-xs text-muted">
                  opened {formatDate(m.started_at)}
                  {m.sealed_at ? ` · sealed ${formatDate(m.sealed_at)}` : ""}
                </span>
              </span>
              <Badge
                className={cn(
                  m.status === "sealed" || m.status === "manual"
                    ? "border-success/30 bg-success/10 text-success"
                    : "border-amber-500/30 bg-amber-500/10 text-amber-300"
                )}
              >
                {m.status}
              </Badge>
            </li>
          ))}
        </ul>
        <p className="mt-3 text-xs text-muted">
          Per-kill line items, remapping and manual point edits still live on the classic board:
          <a href="/comp/dashboard" className="ml-1 text-violet-300 hover:text-violet-200">
            open it
          </a>
          .
        </p>
      </CardBody>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

function SettingsTab({
  gid,
  tid,
  tourney,
  canManage,
  onSaved,
}: {
  gid: number;
  tid: number;
  tourney: import("@/lib/api").Tourney;
  canManage: boolean;
  onSaved: () => void;
}) {
  const [form, setForm] = useState({
    name: tourney.name,
    description: tourney.description || "",
    code: tourney.code || "",
    mode: tourney.mode,
    game_mode: tourney.game_mode || "",
    region: tourney.region || "EU",
    shoot_timer: tourney.shoot_timer || "4:30",
    series_len: tourney.series_len,
    max_players: tourney.max_players,
    host_playing: tourney.host_playing,
    pr_enabled: tourney.pr_enabled,
    registration_open: tourney.registration_open,
  });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const set = <K extends keyof typeof form>(k: K, v: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await api.updateTourney(gid, tid, {
        ...form,
        description: form.description || null,
        code: form.code || null,
        game_mode: form.game_mode || null,
      });
      setMsg("Saved.");
      onSaved();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not save");
    } finally {
      setBusy(false);
    }
  };

  const locked = !canManage || tourney.status === "ended";

  return (
    <Card>
      <CardBody className="pt-5">
        {locked ? (
          <div className="mb-4">
            <Alert tone="info">
              {tourney.status === "ended"
                ? "This tournament has ended — its settings are now read-only."
                : "You can view these settings but not change them."}
            </Alert>
          </div>
        ) : null}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name" className="sm:col-span-2">
            <Input value={form.name} onChange={(e) => set("name", e.target.value)} disabled={locked} />
          </Field>
          <Field label="Description" className="sm:col-span-2">
            <Input
              value={form.description}
              onChange={(e) => set("description", e.target.value)}
              disabled={locked}
            />
          </Field>
          <Field label="Room code">
            <Input
              value={form.code}
              onChange={(e) => set("code", e.target.value.toUpperCase())}
              maxLength={5}
              disabled={locked}
            />
          </Field>
          <Field label="Format">
            <Select
              value={form.mode}
              onChange={(e) => set("mode", e.target.value as "solo" | "duo")}
              disabled={locked}
            >
              <option value="solo">Solo</option>
              <option value="duo">Duo</option>
            </Select>
          </Field>
          <Field label="Game mode">
            <Input value={form.game_mode} onChange={(e) => set("game_mode", e.target.value)} disabled={locked} />
          </Field>
          <Field label="Region">
            <Input value={form.region} onChange={(e) => set("region", e.target.value)} disabled={locked} />
          </Field>
          <Field label="Shoot timer">
            <Input value={form.shoot_timer} onChange={(e) => set("shoot_timer", e.target.value)} disabled={locked} />
          </Field>
          <Field label="Games planned" hint="0 = open ended.">
            <Input
              type="number"
              min={0}
              value={form.series_len}
              onChange={(e) => set("series_len", Number(e.target.value))}
              disabled={locked}
            />
          </Field>
          <Field label="Player limit" hint="0 = no limit.">
            <Input
              type="number"
              min={0}
              value={form.max_players}
              onChange={(e) => set("max_players", Number(e.target.value))}
              disabled={locked}
            />
          </Field>
          <Switch
            checked={form.host_playing}
            onChange={(v) => !locked && set("host_playing", v)}
            label="Host plays too"
          />
          <Switch
            checked={form.pr_enabled}
            onChange={(v) => !locked && set("pr_enabled", v)}
            label="Calculate PR on close"
          />
          <Switch
            checked={form.registration_open}
            onChange={(v) => !locked && set("registration_open", v)}
            label="Registration open"
          />
        </div>

        {msg ? (
          <div className="mt-4">
            <Alert tone={msg === "Saved." ? "success" : "error"}>{msg}</Alert>
          </div>
        ) : null}

        {!locked ? (
          <div className="mt-5 flex justify-end">
            <Button onClick={save} disabled={busy}>
              {busy ? "Saving…" : "Save changes"}
            </Button>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}

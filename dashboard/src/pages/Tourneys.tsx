import { useState } from "react";
import { Link, useNavigate, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { Plus, X } from "lucide-react";
import { api, type Tourney } from "@/lib/api";
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
  Textarea,
} from "@/components/ui";
import { statusLabel, statusTone, timeAgo } from "@/lib/utils";

const GAME_MODES = [
  { value: "", label: "Any / not set" },
  { value: "zw_lg", label: "Late Game Zonewars" },
  { value: "zw", label: "Zone Wars" },
  { value: "wwzw", label: "Duo Wild West" },
  { value: "ww", label: "Solo Wild West" },
  { value: "bf", label: "Boxfights" },
  { value: "BF2V2", label: "Boxfights 2v2" },
  { value: "VA", label: "V-Arena" },
  { value: "tdm_bvr", label: "Red vs Blue" },
  { value: "r_1v1", label: "Realistic 1v1" },
  { value: "df", label: "Free Mode" },
];

type Ctx = { guild: { id: number; name: string }; canManage: boolean };

export function TourneysPage() {
  const { guildId } = useParams();
  const { canManage } = useOutletContext<Ctx>();
  const [params, setParams] = useSearchParams();
  const gid = Number(guildId);

  const { data, loading, error, reload } = useAsync(() => api.tourneys(gid), [gid]);
  const [showForm, setShowForm] = useState(params.get("new") === "1");

  const closeForm = () => {
    setShowForm(false);
    params.delete("new");
    setParams(params, { replace: true });
  };

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Tournaments</h1>
          <p className="mt-1 text-sm text-muted">
            A tournament holds the matches. Matches hold the kills, knocks and placements.
          </p>
        </div>
        {canManage && !showForm ? (
          <Button onClick={() => setShowForm(true)}>
            <Plus className="h-4 w-4" /> New tournament
          </Button>
        ) : null}
      </header>

      {showForm ? (
        <CreateTourneyForm
          guildId={gid}
          onClose={closeForm}
          onCreated={() => {
            closeForm();
            reload();
          }}
        />
      ) : null}

      {error ? <Alert tone="error">{error}</Alert> : null}

      {loading ? (
        <div className="space-y-2">
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
        </div>
      ) : (data?.tourneys?.length ?? 0) === 0 ? (
        <EmptyState
          title="No tournaments yet"
          hint="Create one to start taking signups. Nothing about it is permanent — you can edit it until it ends."
          action={
            canManage ? (
              <Button variant="secondary" size="sm" onClick={() => setShowForm(true)}>
                <Plus className="h-3.5 w-3.5" /> Create tournament
              </Button>
            ) : undefined
          }
        />
      ) : (
        <div className="space-y-2">
          {data!.tourneys.map((t) => (
            <Link key={t.id} to={`/dashboard/${gid}/tourneys/${t.id}`} className="block">
              <Card className="transition-colors hover:border-primary/40">
                <CardBody className="flex flex-wrap items-center gap-3 py-3.5">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium">{t.name}</span>
                      <Badge className={statusTone(t.status)}>{statusLabel(t.status)}</Badge>
                    </div>
                    <p className="mt-0.5 text-xs text-muted">
                      #{t.id} · {t.mode} · {t.code ? `room ${t.code}` : "no room code"} ·{" "}
                      {t.max_players ? `max ${t.max_players}` : "no player limit"} ·{" "}
                      {timeAgo(t.created_at)}
                    </p>
                  </div>
                  <div className="flex items-center gap-3 text-xs text-muted">
                    {t.pr_enabled ? <Badge className="border-border bg-white/5">PR on</Badge> : null}
                    <span className="tabular-nums">
                      {t.series_len ? `${t.series_len} games` : "open ended"}
                    </span>
                  </div>
                </CardBody>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function CreateTourneyForm({
  guildId,
  onClose,
  onCreated,
}: {
  guildId: number;
  onClose: () => void;
  onCreated: (t: Tourney) => void;
}) {
  const nav = useNavigate();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [form, setForm] = useState({
    name: "",
    description: "",
    mode: "solo" as "solo" | "duo",
    game_mode: "",
    series_len: 0,
    max_players: 0,
    region: "EU",
    shoot_timer: "4:30",
    code: "",
    scheduled_at: "",
    host_playing: true,
    pr_enabled: true,
    kill: 1,
    win: 3,
    placement: "10,7,5,3,1",
  });

  const set = <K extends keyof typeof form>(k: K, v: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const submit = async () => {
    setError(null);
    setSaving(true);
    try {
      const placement = form.placement
        .split(",")
        .map((s) => Number(s.trim()))
        .filter((n) => Number.isFinite(n) && n >= 0);
      const { tourney } = await api.createTourney(guildId, {
        name: form.name.trim(),
        description: form.description.trim() || null,
        mode: form.mode,
        game_mode: form.game_mode || null,
        series_len: Number(form.series_len) || 0,
        max_players: Number(form.max_players) || 0,
        region: form.region,
        shoot_timer: form.shoot_timer,
        code: form.code.trim().toUpperCase() || null,
        scheduled_at: form.scheduled_at || null,
        host_playing: form.host_playing,
        pr_enabled: form.pr_enabled,
        points_table: {
          kill: Number(form.kill) || 0,
          win: Number(form.win) || 0,
          placement: placement.length ? placement : [10, 7, 5, 3, 1],
        },
      });
      onCreated(tourney);
      nav(`/dashboard/${guildId}/tourneys/${tourney.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the tournament");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="border-primary/30">
      <CardBody className="pt-5">
        <div className="mb-4 flex items-start justify-between">
          <div>
            <h2 className="text-sm font-semibold">New tournament</h2>
            <p className="text-xs text-muted">
              Everything here can be edited later, until the tournament ends.
            </p>
          </div>
          <button onClick={onClose} className="rounded-md p-1 text-muted hover:bg-white/8">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name" className="sm:col-span-2">
            <Input
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="Friday Night Solo Cup"
              maxLength={64}
            />
          </Field>

          <Field label="Description" className="sm:col-span-2" hint="Optional. Shown to players.">
            <Textarea
              value={form.description}
              onChange={(e) => set("description", e.target.value)}
              placeholder="Rules, prize info, anything players should know."
              maxLength={500}
            />
          </Field>

          <Field label="Format">
            <Select value={form.mode} onChange={(e) => set("mode", e.target.value as "solo" | "duo")}>
              <option value="solo">Solo — one player per signup</option>
              <option value="duo">Duo — teams of two</option>
            </Select>
          </Field>

          <Field label="Game mode">
            <Select value={form.game_mode} onChange={(e) => set("game_mode", e.target.value)}>
              {GAME_MODES.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="Room code" hint="5 letters or digits. Needed for auto capture.">
            <Input
              value={form.code}
              onChange={(e) => set("code", e.target.value.toUpperCase())}
              placeholder="ABC12"
              maxLength={5}
            />
          </Field>

          <Field label="Player limit" hint="0 means no limit. Duos need an even number.">
            <Input
              type="number"
              min={0}
              value={form.max_players}
              onChange={(e) => set("max_players", Number(e.target.value))}
            />
          </Field>

          <Field label="Games planned" hint="0 means open ended.">
            <Input
              type="number"
              min={0}
              value={form.series_len}
              onChange={(e) => set("series_len", Number(e.target.value))}
            />
          </Field>

          <Field label="Scheduled start" hint="Optional. Leave empty to start manually.">
            <Input
              type="datetime-local"
              value={form.scheduled_at}
              onChange={(e) => set("scheduled_at", e.target.value)}
            />
          </Field>

          <Field label="Region">
            <Input value={form.region} onChange={(e) => set("region", e.target.value)} maxLength={16} />
          </Field>

          <Field label="Shoot timer" hint="Shown to players as the round length.">
            <Input
              value={form.shoot_timer}
              onChange={(e) => set("shoot_timer", e.target.value)}
              maxLength={16}
            />
          </Field>

          <div className="sm:col-span-2">
            <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-muted">
              Points per match
            </p>
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Per kill">
                <Input
                  type="number"
                  min={0}
                  value={form.kill}
                  onChange={(e) => set("kill", Number(e.target.value))}
                />
              </Field>
              <Field label="Per win">
                <Input
                  type="number"
                  min={0}
                  value={form.win}
                  onChange={(e) => set("win", Number(e.target.value))}
                />
              </Field>
              <Field label="Placement points" hint="1st, 2nd, 3rd… comma separated.">
                <Input
                  value={form.placement}
                  onChange={(e) => set("placement", e.target.value)}
                  placeholder="10,7,5,3,1"
                />
              </Field>
            </div>
          </div>

          <Switch
            checked={form.host_playing}
            onChange={(v) => set("host_playing", v)}
            label="Host plays too"
            hint="If off, the host's own kills never score in this tournament."
          />
          <Switch
            checked={form.pr_enabled}
            onChange={(v) => set("pr_enabled", v)}
            label="Calculate PR"
            hint="Rating is recalculated when you close the tournament."
          />
        </div>

        {error ? (
          <div className="mt-4">
            <Alert tone="error">{error}</Alert>
          </div>
        ) : null}

        <div className="mt-5 flex items-center justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={saving || form.name.trim().length < 2}>
            {saving ? "Creating…" : "Create tournament"}
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}

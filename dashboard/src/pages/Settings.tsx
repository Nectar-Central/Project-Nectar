import { useState } from "react";
import { useOutletContext, useParams } from "react-router-dom";
import { api, type GuildSettings } from "@/lib/api";
import { useAsync } from "@/lib/auth";
import {
  Alert,
  Button,
  Card,
  CardBody,
  Field,
  Input,
  Skeleton,
  Switch,
} from "@/components/ui";

type Ctx = { guild: { id: number }; canManage: boolean };

export function SettingsPage() {
  const { guildId } = useParams();
  const { canManage } = useOutletContext<Ctx>();
  const gid = Number(guildId);
  const { data, loading, error, reload } = useAsync(() => api.guild(gid), [gid]);

  const settings = (data?.guild?.settings || {}) as GuildSettings;
  const [form, setForm] = useState<GuildSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const current = form ?? settings;
  const set = <K extends keyof GuildSettings>(k: K, v: GuildSettings[K]) =>
    setForm({ ...current, [k]: v });

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await api.saveSettings(gid, current);
      setForm(null);
      reload();
      setMsg("Saved.");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not save settings");
    } finally {
      setBusy(false);
    }
  };

  const saveNickname = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await api.setNickname(gid, current.bot_nickname || "");
      setMsg("Bot nickname saved — it changes on the bot's next sync.");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not save nickname");
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <Skeleton className="h-64" />;
  if (error) return <Alert tone="error">{error}</Alert>;

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-muted">Only affects this server.</p>
      </header>

      <Card>
        <CardBody className="space-y-4 pt-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Bot nickname" hint="How the bot's name shows in this server.">
              <Input
                value={current.bot_nickname || ""}
                onChange={(e) => set("bot_nickname", e.target.value)}
                placeholder="Nectar"
                maxLength={32}
                disabled={!canManage}
              />
            </Field>
            <Field label="Accent colour" hint="Hex colour used on this server's boards.">
              <Input
                value={current.accent_color || ""}
                onChange={(e) => set("accent_color", e.target.value)}
                placeholder="#7c3aed"
                maxLength={9}
                disabled={!canManage}
              />
            </Field>
            <Field label="Results channel ID" hint="Where final standings get posted.">
              <Input
                value={current.results_channel_id || ""}
                onChange={(e) => set("results_channel_id", e.target.value)}
                placeholder="123456789012345678"
                disabled={!canManage}
              />
            </Field>
            <Field label="Announce channel ID" hint="Where tournament posts and pings go.">
              <Input
                value={current.announce_channel_id || ""}
                onChange={(e) => set("announce_channel_id", e.target.value)}
                placeholder="123456789012345678"
                disabled={!canManage}
              />
            </Field>
          </div>

          <Switch
            checked={current.leaderboard_public !== false}
            onChange={(v) => canManage && set("leaderboard_public", v)}
            label="Public leaderboard"
            hint="Off hides this server's board from anyone without dashboard access."
          />

          {msg ? (
            <Alert tone={msg === "Saved." ? "success" : "info"}>{msg}</Alert>
          ) : null}

          {canManage ? (
            <div className="flex justify-end gap-2">
              <Button variant="secondary" onClick={saveNickname} disabled={busy}>
                Save nickname
              </Button>
              <Button onClick={save} disabled={busy}>
                {busy ? "Saving…" : "Save settings"}
              </Button>
            </div>
          ) : null}
        </CardBody>
      </Card>

      <Card>
        <CardBody className="pt-5">
          <h2 className="text-sm font-semibold">Default points table</h2>
          <p className="mb-3 text-xs text-muted">
            Used for new tournaments unless you change it per tournament. Changes do not touch
            tournaments that already exist.
          </p>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Per kill">
              <Input
                type="number"
                min={0}
                value={current.default_points_table?.kill ?? 1}
                onChange={(e) =>
                  set("default_points_table", {
                    ...(current.default_points_table || { kill: 1, win: 3, placement: [10, 7, 5, 3, 1] }),
                    kill: Number(e.target.value) || 0,
                  })
                }
                disabled={!canManage}
              />
            </Field>
            <Field label="Per win">
              <Input
                type="number"
                min={0}
                value={current.default_points_table?.win ?? 3}
                onChange={(e) =>
                  set("default_points_table", {
                    ...(current.default_points_table || { kill: 1, win: 3, placement: [10, 7, 5, 3, 1] }),
                    win: Number(e.target.value) || 0,
                  })
                }
                disabled={!canManage}
              />
            </Field>
            <Field label="Placement" hint="Comma separated: 1st, 2nd, 3rd…">
              <Input
                value={(current.default_points_table?.placement || [10, 7, 5, 3, 1]).join(",")}
                onChange={(e) =>
                  set("default_points_table", {
                    ...(current.default_points_table || { kill: 1, win: 3, placement: [10, 7, 5, 3, 1] }),
                    placement: e.target.value
                      .split(",")
                      .map((s) => Number(s.trim()))
                      .filter((n) => Number.isFinite(n)),
                  })
                }
                disabled={!canManage}
              />
            </Field>
          </div>
        </CardBody>
      </Card>
    </div>
  );
}

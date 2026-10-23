import { Link, useOutletContext, useParams } from "react-router-dom";
import { Activity, Plus, Trophy, Users } from "lucide-react";
import { api, type GuildRow } from "@/lib/api";
import { useAsync } from "@/lib/auth";
import { Badge, Button, Card, CardBody, EmptyState, Skeleton } from "@/components/ui";
import { statusLabel, statusTone, timeAgo } from "@/lib/utils";

type Ctx = { guild: { id: number; name: string }; canManage: boolean };

export function GuildHomePage() {
  const { guildId } = useParams();
  const { canManage } = useOutletContext<Ctx>();
  const gid = Number(guildId);

  const { data, loading, error } = useAsync(() => api.guild(gid), [gid]);
  const guild: GuildRow | undefined = data?.guild;
  const stats = data?.stats || {};

  if (error) {
    return <EmptyState title="Could not load this server" hint={error} />;
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{guild?.name || "…"}</h1>
          <p className="mt-1 text-sm text-muted">
            Everything below is scoped to this server only.
          </p>
        </div>
        {canManage ? (
          <Link to={`/dashboard/${gid}/tourneys?new=1`}>
            <Button>
              <Plus className="h-4 w-4" /> New tournament
            </Button>
          </Link>
        ) : null}
      </header>

      <div className="grid gap-3 sm:grid-cols-3">
        <StatCard
          icon={<Trophy className="h-4 w-4" />}
          label="Tournaments"
          value={stats.tourneys_total}
          hint={`${stats.active ?? 0} active · ${stats.ended ?? 0} ended`}
          loading={loading}
        />
        <StatCard
          icon={<Users className="h-4 w-4" />}
          label="Players"
          value={stats.players}
          hint="Registered in this server"
          loading={loading}
        />
        <StatCard
          icon={<Activity className="h-4 w-4" />}
          label="Staff"
          value={data?.staff?.length}
          hint="Owners, admins, moderators"
          loading={loading}
        />
      </div>

      <Card>
        <CardBody className="pt-5">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">
              Recent tournaments
            </h2>
            <Link
              to={`/dashboard/${gid}/tourneys`}
              className="text-xs text-violet-300 hover:text-violet-200"
            >
              View all
            </Link>
          </div>

          {loading ? (
            <div className="space-y-2">
              <Skeleton className="h-12" />
              <Skeleton className="h-12" />
            </div>
          ) : (data?.recent_tourneys?.length ?? 0) === 0 ? (
            <EmptyState
              title="No tournaments yet"
              hint="Create one and it appears here with live standings."
              action={
                canManage ? (
                  <Link to={`/dashboard/${gid}/tourneys?new=1`}>
                    <Button variant="secondary" size="sm">
                      <Plus className="h-3.5 w-3.5" /> Create the first one
                    </Button>
                  </Link>
                ) : undefined
              }
            />
          ) : (
            <ul className="divide-y divide-border">
              {data!.recent_tourneys.map((t) => (
                <li key={t.id}>
                  <Link
                    to={`/dashboard/${gid}/tourneys/${t.id}`}
                    className="flex items-center gap-3 py-2.5 transition-colors hover:text-violet-200"
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">{t.name}</span>
                      <span className="block text-xs text-muted">
                        #{t.id} · {t.mode} · {t.code ? `room ${t.code}` : "no room code"} ·{" "}
                        {timeAgo(t.created_at)}
                      </span>
                    </span>
                    <Badge className={statusTone(t.status)}>{statusLabel(t.status)}</Badge>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>
    </div>
  );
}

function StatCard({
  icon,
  label,
  value,
  hint,
  loading,
}: {
  icon: React.ReactNode;
  label: string;
  value?: number | null;
  hint?: string;
  loading?: boolean;
}) {
  return (
    <Card>
      <CardBody className="pt-5">
        <div className="flex items-center gap-2 text-muted">
          {icon}
          <span className="text-xs font-medium uppercase tracking-wide">{label}</span>
        </div>
        <p className="mt-2 text-2xl font-semibold tabular-nums">
          {loading ? <Skeleton className="h-7 w-16" /> : value ?? "—"}
        </p>
        {hint ? <p className="mt-1 text-xs text-muted">{hint}</p> : null}
      </CardBody>
    </Card>
  );
}

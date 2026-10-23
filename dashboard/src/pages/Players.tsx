import { useState } from "react";
import { useOutletContext, useParams } from "react-router-dom";
import { Search } from "lucide-react";
import { api, type GuildPlayer } from "@/lib/api";
import { useAsync } from "@/lib/auth";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  EmptyState,
  Input,
  Skeleton,
} from "@/components/ui";
import { formatDate } from "@/lib/utils";

type Ctx = { guild: { id: number }; canManage: boolean };

export function PlayersPage() {
  const { guildId } = useParams();
  const { canManage } = useOutletContext<Ctx>();
  const gid = Number(guildId);
  const [query, setQuery] = useState("");
  const [term, setTerm] = useState("");
  const { data, loading, error, reload } = useAsync(() => api.players(gid, term), [gid, term]);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Players</h1>
        <p className="mt-1 text-sm text-muted">
          Everyone who has played a tournament on this server. Bans here are per-server.
        </p>
      </header>

      <div className="flex gap-2">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && setTerm(query)}
            placeholder="Search by in-game name or Discord ID"
            className="pl-9"
          />
        </div>
        <Button variant="secondary" onClick={() => setTerm(query)}>
          Search
        </Button>
      </div>

      {error ? <Alert tone="error">{error}</Alert> : null}

      {loading ? (
        <Skeleton className="h-64" />
      ) : (data?.players?.length ?? 0) === 0 ? (
        <EmptyState
          title="No players found"
          hint={term ? "Try a different search." : "They appear after their first tournament here."}
        />
      ) : (
        <Card>
          <CardBody className="pt-4">
            <ul className="divide-y divide-border">
              {data!.players.map((p) => (
                <PlayerRow
                  key={p.discord_id}
                  player={p}
                  gid={gid}
                  canManage={canManage}
                  onChanged={reload}
                />
              ))}
            </ul>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

function PlayerRow({
  player,
  gid,
  canManage,
  onChanged,
}: {
  player: GuildPlayer;
  gid: number;
  canManage: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggleBan = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.updatePlayer(gid, player.discord_id, {
        is_banned: !player.is_banned,
        ban_reason: !player.is_banned ? "Banned from dashboard" : undefined,
      });
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not update player");
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="truncate text-sm">
              {player.game_username || player.discord_id.slice(0, 8)}
            </span>
            {player.is_banned ? (
              <Badge className="border-danger/30 bg-danger/10 text-danger">banned</Badge>
            ) : null}
          </span>
          <span className="block truncate text-[11px] text-muted">
            {player.discord_id} · joined {formatDate(player.registered_at)}
          </span>
        </span>
        <span className="text-xs tabular-nums text-muted">
          {player.kills} K · {player.wins} W · {player.matches} G ·{" "}
          <span className="text-violet-300">{Math.round(Number(player.pr))} PR</span>
        </span>
        {canManage ? (
          <Button
            size="sm"
            variant={player.is_banned ? "ghost" : "danger"}
            disabled={busy}
            onClick={toggleBan}
          >
            {player.is_banned ? "Unban" : "Ban"}
          </Button>
        ) : null}
      </div>
      {error ? <p className="mt-1 text-xs text-danger">{error}</p> : null}
    </li>
  );
}

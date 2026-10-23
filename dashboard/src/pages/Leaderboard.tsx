import { useOutletContext, useParams } from "react-router-dom";
import { api } from "@/lib/api";
import { useAsync } from "@/lib/auth";
import { Badge, Card, CardBody, EmptyState, Skeleton } from "@/components/ui";

type Ctx = { guild: { id: number }; canManage: boolean };

export function LeaderboardPage() {
  const { guildId } = useParams();
  useOutletContext<Ctx>();
  const gid = Number(guildId);
  const { data, loading, error } = useAsync(() => api.leaderboard(gid, 100), [gid]);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Leaderboard</h1>
        <p className="mt-1 text-sm text-muted">
          Cumulative across every tournament on this server. Other servers have their own board.
        </p>
      </header>

      {error ? <EmptyState title="Could not load the leaderboard" hint={error} /> : null}

      {loading ? (
        <Skeleton className="h-64" />
      ) : (data?.players?.length ?? 0) === 0 ? (
        <EmptyState
          title="No players yet"
          hint="Players appear here after their first tournament on this server."
        />
      ) : (
        <Card>
          <CardBody className="pt-4">
            <div className="overflow-x-auto scroll-thin">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wide text-muted">
                    <th className="pb-2 pr-2 font-medium">#</th>
                    <th className="pb-2 pr-2 font-medium">Player</th>
                    <th className="pb-2 pr-2 text-right font-medium">PR</th>
                    <th className="pb-2 pr-2 text-right font-medium">Kills</th>
                    <th className="pb-2 pr-2 text-right font-medium">Wins</th>
                    <th className="pb-2 pr-2 text-right font-medium">Games</th>
                    <th className="pb-2 text-right font-medium">Points</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {data!.players.map((p, i) => (
                    <tr key={p.discord_id}>
                      <td className="py-2 pr-2 tabular-nums text-muted">{i + 1}</td>
                      <td className="py-2 pr-2">
                        <span className="flex items-center gap-2">
                          {p.game_username || p.discord_id.slice(0, 8)}
                          {p.is_banned ? (
                            <Badge className="border-danger/30 bg-danger/10 text-danger">banned</Badge>
                          ) : null}
                        </span>
                      </td>
                      <td className="py-2 pr-2 text-right tabular-nums text-violet-300">
                        {Math.round(Number(p.pr))}
                      </td>
                      <td className="py-2 pr-2 text-right tabular-nums">{p.kills}</td>
                      <td className="py-2 pr-2 text-right tabular-nums">{p.wins}</td>
                      <td className="py-2 pr-2 text-right tabular-nums text-muted">{p.matches}</td>
                      <td className="py-2 text-right font-semibold tabular-nums">{p.points}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

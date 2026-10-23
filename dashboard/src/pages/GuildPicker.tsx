import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, ShieldOff } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { Badge, Button, EmptyState, Skeleton } from "@/components/ui";
import { initials } from "@/lib/utils";

export function GuildPickerPage() {
  const { guilds, user, loading, logout } = useAuth();
  const navigate = useNavigate();

  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-14">
      <div className="mb-10 flex items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-primary-strong text-base font-black text-white">
            N
          </span>
          <div>
            <h1 className="text-lg font-semibold tracking-tight">Your servers</h1>
            <p className="text-xs text-muted">
              Signed in as {user?.username || "…"}
            </p>
          </div>
        </div>
        <Button variant="ghost" size="sm" onClick={async () => { await logout(); navigate("/login"); }}>
          Sign out
        </Button>
      </div>

      {loading ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
        </div>
      ) : guilds.length === 0 ? (
        <EmptyState
          title="No servers yet"
          hint="Ask the owner of a Discord server to register it with /setup, then have them add you from the Staff page."
          action={
            <a href="https://discord.com" target="_blank" rel="noreferrer">
              <Button variant="secondary">Open Discord</Button>
            </a>
          }
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {guilds.map((g) => (
            <Link
              key={g.id}
              to={`/dashboard/${g.id}`}
              className="group flex items-center gap-3 rounded-[var(--radius-card)] border border-border bg-surface/80 p-4 transition-colors hover:border-primary/50 hover:bg-surface"
            >
              {g.icon_url ? (
                <img src={g.icon_url} alt="" className="h-11 w-11 rounded-lg object-cover" />
              ) : (
                <span className="grid h-11 w-11 place-items-center rounded-lg bg-primary-strong/25 text-sm font-bold text-violet-200">
                  {initials(g.name)}
                </span>
              )}
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{g.name}</span>
                <Badge className="mt-1 border-border bg-white/5 capitalize text-muted">
                  {g.role}
                </Badge>
              </span>
              <ArrowRight className="h-4 w-4 shrink-0 text-muted transition-transform group-hover:translate-x-0.5 group-hover:text-foreground" />
            </Link>
          ))}
        </div>
      )}

      <div className="mt-10 flex items-start gap-3 rounded-[var(--radius-card)] border border-border bg-surface/50 p-4">
        <ShieldOff className="mt-0.5 h-4 w-4 shrink-0 text-muted" />
        <p className="text-xs leading-relaxed text-muted">
          A server appears here only if you are its owner, an admin or a moderator in Nectar.
          Being a Discord administrator is not enough on its own — the server has to register
          first.
        </p>
      </div>
    </div>
  );
}

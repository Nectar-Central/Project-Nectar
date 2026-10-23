import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, ExternalLink, ShieldOff } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { Badge, Button, EmptyState, Skeleton } from "@/components/ui";
import { initials } from "@/lib/utils";

const BOT_INVITE_URL =
  "https://discord.com/oauth2/authorize?client_id=1563168127095672904&permissions=4504152000687185&integration_type=0&scope=bot+applications.commands";

export function GuildPickerPage() {
  const { guilds, user, loading, logout } = useAuth();
  const navigate = useNavigate();

  const registered = guilds.filter((g) => g.registered);
  const unregistered = guilds.filter((g) => !g.registered);

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
          hint="You need Manage Server permission on a Discord server to see it here."
          action={
            <a href="https://discord.com" target="_blank" rel="noreferrer">
              <Button variant="secondary">Open Discord</Button>
            </a>
          }
        />
      ) : (
        <>
          {registered.length > 0 && (
            <div className="grid gap-3 sm:grid-cols-2">
              {registered.map((g) => (
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
                    {g.role && (
                      <Badge className="mt-1 border-border bg-white/5 capitalize text-muted">
                        {g.role}
                      </Badge>
                    )}
                  </span>
                  <ArrowRight className="h-4 w-4 shrink-0 text-muted transition-transform group-hover:translate-x-0.5 group-hover:text-foreground" />
                </Link>
              ))}
            </div>
          )}

          {unregistered.length > 0 && (
            <>
              <h2 className="mb-3 mt-8 text-sm font-medium text-muted">
                Servers where Nectar isn't set up yet
              </h2>
              <div className="grid gap-3 sm:grid-cols-2">
                {unregistered.map((g) => (
                  <div
                    key={g.id}
                    className="flex items-center gap-3 rounded-[var(--radius-card)] border border-border/50 bg-surface/40 p-4 opacity-60"
                  >
                    {g.icon_url ? (
                      <img src={g.icon_url} alt="" className="h-11 w-11 rounded-lg object-cover" />
                    ) : (
                      <span className="grid h-11 w-11 place-items-center rounded-lg bg-white/5 text-sm font-bold text-muted">
                        {initials(g.name)}
                      </span>
                    )}
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">{g.name}</span>
                      <a
                        href={`${BOT_INVITE_URL}&guild_id=${g.id}`}
                        target="_blank"
                        rel="noreferrer"
                        className="mt-1 inline-flex items-center gap-1 text-xs text-primary hover:underline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        Invite bot first <ExternalLink className="h-3 w-3" />
                      </a>
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}

      <div className="mt-10 flex items-start gap-3 rounded-[var(--radius-card)] border border-border bg-surface/50 p-4">
        <ShieldOff className="mt-0.5 h-4 w-4 shrink-0 text-muted" />
        <p className="text-xs leading-relaxed text-muted">
          Registered servers (clickable) appear above. Dimmed servers haven't run
          <code className="mx-1 rounded bg-white/8 px-1.5 py-0.5">/setup</code>
          yet — invite the bot there first, then an administrator runs that command once.
        </p>
      </div>
    </div>
  );
}

import * as React from "react";
import { Link, NavLink, Outlet, useNavigate, useParams } from "react-router-dom";
import {
  BarChart3,
  CalendarDays,
  ChevronDown,
  LogOut,
  Settings,
  Shield,
  Trophy,
  Users,
} from "lucide-react";
import { useAuth } from "@/lib/auth";
import { Badge, Button, Spinner } from "@/components/ui";
import { cn, initials } from "@/lib/utils";

const NAV = [
  { to: "", label: "Overview", icon: BarChart3, end: true },
  { to: "tourneys", label: "Tournaments", icon: Trophy },
  { to: "leaderboard", label: "Leaderboard", icon: CalendarDays },
  { to: "players", label: "Players", icon: Users },
  { to: "admins", label: "Staff", icon: Shield },
  { to: "settings", label: "Settings", icon: Settings },
];

export function AppShell() {
  const { guilds, user, logout } = useAuth();
  const { guildId } = useParams();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = React.useState(false);
  const [switcherOpen, setSwitcherOpen] = React.useState(false);

  const gid = Number(guildId);
  const guild = guilds.find((g) => Number(g.id) === gid);
  const canManage = guild?.role === "owner" || guild?.role === "admin";

  if (!guild) {
    return (
      <div className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-3 px-6 text-center">
        <p className="text-sm text-muted">
          You do not have access to this server, or it is no longer registered.
        </p>
        <Button variant="secondary" onClick={() => navigate("/dashboard")}>
          Back to servers
        </Button>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen">
      {/* Sidebar */}
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-surface/50 lg:flex">
        <div className="flex items-center gap-2 px-5 py-5">
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-primary-strong text-sm font-black text-white">
            N
          </span>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold">Nectar</p>
            <p className="truncate text-[11px] text-muted">Tournament console</p>
          </div>
        </div>

        {/* Guild switcher */}
        <div className="relative px-3">
          <button
            onClick={() => setSwitcherOpen((v) => !v)}
            className="flex w-full items-center gap-2 rounded-lg border border-border bg-surface-2 px-2.5 py-2 text-left focus-ring"
          >
            <GuildAvatar name={guild.name} url={guild.icon_url} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-xs font-medium">{guild.name}</span>
              <span className="block truncate text-[10px] capitalize text-muted">{guild.role}</span>
            </span>
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" />
          </button>
          {switcherOpen ? (
            <div className="absolute left-3 right-3 z-20 mt-1 overflow-hidden rounded-lg border border-border bg-surface shadow-xl">
              {guilds.map((g) => (
                <Link
                  key={g.id}
                  to={`/dashboard/${g.id}`}
                  onClick={() => setSwitcherOpen(false)}
                  className={cn(
                    "flex items-center gap-2 px-2.5 py-2 text-xs hover:bg-white/6",
                    Number(g.id) === gid && "bg-white/6"
                  )}
                >
                  <GuildAvatar name={g.name} url={g.icon_url} size={20} />
                  <span className="min-w-0 flex-1 truncate">{g.name}</span>
                  <span className="text-[10px] capitalize text-muted">{g.role}</span>
                </Link>
              ))}
              <Link
                to="/dashboard"
                onClick={() => setSwitcherOpen(false)}
                className="block border-t border-border px-2.5 py-2 text-xs text-muted hover:bg-white/6"
              >
                All servers
              </Link>
            </div>
          ) : null}
        </div>

        <nav className="mt-4 flex-1 space-y-0.5 px-3">
          {NAV.map((item) => {
            const to = `/dashboard/${gid}${item.to ? `/${item.to}` : ""}`;
            return (
              <NavLink
                key={item.to}
                to={to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    "flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors",
                    isActive
                      ? "bg-primary-strong/15 text-foreground"
                      : "text-muted hover:bg-white/5 hover:text-foreground"
                  )
                }
              >
                <item.icon className="h-4 w-4" />
                {item.label}
              </NavLink>
            );
          })}
        </nav>

        <div className="border-t border-border p-3">
          <div className="flex items-center gap-2 rounded-lg px-1 py-1.5">
            {user?.avatar_url ? (
              <img src={user.avatar_url} alt="" className="h-7 w-7 rounded-full" />
            ) : (
              <span className="grid h-7 w-7 place-items-center rounded-full bg-white/10 text-[11px] font-semibold">
                {initials(user?.username)}
              </span>
            )}
            <span className="min-w-0 flex-1">
              <span className="block truncate text-xs">{user?.username || "Unknown"}</span>
              <span className="block text-[10px] text-muted">Discord</span>
            </span>
            <button
              onClick={async () => {
                await logout();
                navigate("/login");
              }}
              title="Sign out"
              className="rounded-md p-1 text-muted hover:bg-white/8 hover:text-foreground focus-ring"
            >
              <LogOut className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </aside>

      {/* Mobile top bar */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-3 border-b border-border bg-surface/40 px-4 py-3 lg:hidden">
          <button
            onClick={() => setMenuOpen((v) => !v)}
            className="flex items-center gap-2 rounded-lg border border-border px-2 py-1.5 text-xs"
          >
            <GuildAvatar name={guild.name} url={guild.icon_url} size={20} />
            <span className="max-w-[9rem] truncate">{guild.name}</span>
            <ChevronDown className="h-3 w-3" />
          </button>
          <Badge className="border-border bg-white/5 capitalize text-muted">{guild.role}</Badge>
        </header>

        {menuOpen ? (
          <div className="border-b border-border bg-surface px-3 py-2 lg:hidden">
            <div className="grid grid-cols-2 gap-1">
              {NAV.map((item) => {
                const to = `/dashboard/${gid}${item.to ? `/${item.to}` : ""}`;
                return (
                  <NavLink
                    key={item.to}
                    to={to}
                    end={item.end}
                    onClick={() => setMenuOpen(false)}
                    className={({ isActive }) =>
                      cn(
                        "flex items-center gap-2 rounded-lg px-2.5 py-2 text-xs",
                        isActive ? "bg-primary-strong/15" : "text-muted hover:bg-white/5"
                      )
                    }
                  >
                    <item.icon className="h-3.5 w-3.5" />
                    {item.label}
                  </NavLink>
                );
              })}
            </div>
          </div>
        ) : null}

        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-6">
          {!canManage ? (
            <div className="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
              You are a moderator on this server — you can view boards, but only owners and
              admins can change tournaments.
            </div>
          ) : null}
          <React.Suspense
            fallback={
              <div className="flex items-center gap-2 text-sm text-muted">
                <Spinner /> Loading…
              </div>
            }
          >
            <Outlet context={{ guild, canManage }} />
          </React.Suspense>
        </main>
      </div>
    </div>
  );
}

function GuildAvatar({
  name,
  url,
  size = 28,
}: {
  name: string;
  url?: string | null;
  size?: number;
}) {
  if (url) {
    return (
      <img
        src={url}
        alt=""
        style={{ width: size, height: size }}
        className="shrink-0 rounded-md object-cover"
      />
    );
  }
  return (
    <span
      style={{ width: size, height: size, fontSize: size * 0.4 }}
      className="grid shrink-0 place-items-center rounded-md bg-primary-strong/25 font-bold text-violet-200"
    >
      {initials(name)}
    </span>
  );
}

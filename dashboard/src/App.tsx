import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AppShell } from "@/components/AppShell";
import { useAuth } from "@/lib/auth";
import { Spinner } from "@/components/ui";
import { LoginPage } from "@/pages/Login";
import { GuildPickerPage } from "@/pages/GuildPicker";
import { GuildHomePage } from "@/pages/GuildHome";
import { TourneysPage } from "@/pages/Tourneys";
import { TourneyDetailPage } from "@/pages/TourneyDetail";
import { LeaderboardPage } from "@/pages/Leaderboard";
import { PlayersPage } from "@/pages/Players";
import { AdminsPage } from "@/pages/Admins";
import { SettingsPage } from "@/pages/Settings";

export default function App() {
  const { loading, user } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center gap-2 text-sm text-muted">
        <Spinner /> Loading Nectar…
      </div>
    );
  }

  const loggedIn = Boolean(user);

  return (
    <Routes>
      <Route
        path="/login"
        element={loggedIn ? <Navigate to="/dashboard" replace /> : <LoginPage />}
      />
      <Route
        path="/dashboard"
        element={loggedIn ? <GuildPickerPage /> : <RedirectToLogin from={location.pathname} />}
      />
      <Route
        path="/dashboard/:guildId"
        element={loggedIn ? <AppShell /> : <RedirectToLogin from={location.pathname} />}
      >
        <Route index element={<GuildHomePage />} />
        <Route path="tourneys" element={<TourneysPage />} />
        <Route path="tourneys/:tourneyId" element={<TourneyDetailPage />} />
        <Route path="leaderboard" element={<LeaderboardPage />} />
        <Route path="players" element={<PlayersPage />} />
        <Route path="admins" element={<AdminsPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to={loggedIn ? "/dashboard" : "/login"} replace />} />
    </Routes>
  );
}

function RedirectToLogin({ from }: { from: string }) {
  const target = `/login?next=${encodeURIComponent(from)}`;
  return <Navigate to={target} replace />;
}

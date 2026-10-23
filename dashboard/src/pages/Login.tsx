import { useSearchParams } from "react-router-dom";
import { Alert, Button } from "@/components/ui";

const REASONS: Record<string, string> = {
  cancelled: "You cancelled the Discord login.",
  state: "That login attempt expired. Please try again.",
  exchange: "The sign-in provider refused the login. Try again in a moment.",
  discord: "Your Discord account could not be read. Try again.",
  server: "The server could not create a session. Try again.",
};

export function LoginPage() {
  const [params] = useSearchParams();
  const reason = params.get("error");

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-md flex-col justify-center px-6">
      <div className="mb-8 flex items-center gap-3">
        <span className="grid h-11 w-11 place-items-center rounded-xl bg-primary-strong text-lg font-black text-white">
          N
        </span>
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Nectar</h1>
          <p className="text-xs text-muted">Tournament console</p>
        </div>
      </div>

      <h2 className="text-2xl font-semibold tracking-tight">Sign in to manage your server</h2>
      <p className="mt-2 text-sm text-muted">
        Sign-in is handled by Supabase using your Discord account. Nectar only receives your
        Discord ID, display name and avatar — never your messages, and never your password.
      </p>

      {reason ? (
        <div className="mt-5">
          <Alert tone="error">{REASONS[reason] || "Sign-in failed. Please try again."}</Alert>
        </div>
      ) : null}

      <a href="/auth/discord" className="mt-7">
        <Button size="lg" className="w-full">
          <DiscordGlyph /> Continue with Discord
        </Button>
      </a>

      <p className="mt-6 text-xs leading-relaxed text-muted">
        Not staff on any server yet? Ask a server owner to register the server with
        <code className="mx-1 rounded bg-white/8 px-1.5 py-0.5 text-[11px]">/setup</code>
        in Discord, then add you from the Staff page.
      </p>
    </div>
  );
}

function DiscordGlyph() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden="true">
      <path d="M20.317 4.369A19.79 19.79 0 0 0 15.885 3c-.2.36-.43.84-.59 1.22a18.27 18.27 0 0 0-5.59 0A12.6 12.6 0 0 0 9.11 3a19.74 19.74 0 0 0-4.43 1.37C1.9 8.57 1.1 12.66 1.5 16.7a19.9 19.9 0 0 0 6.03 3.04c.48-.66.9-1.36 1.27-2.1-.7-.26-1.37-.59-2-1 .17-.12.33-.25.49-.38a14.2 14.2 0 0 0 12.22 0c.16.13.32.26.49.38-.63.41-1.3.74-2 1 .37.74.79 1.44 1.27 2.1a19.86 19.86 0 0 0 6.03-3.04c.47-4.68-.8-8.73-3.32-12.33ZM8.55 14.24c-1.18 0-2.15-1.08-2.15-2.4 0-1.32.95-2.4 2.15-2.4 1.21 0 2.18 1.09 2.16 2.4 0 1.32-.95 2.4-2.16 2.4Zm6.9 0c-1.18 0-2.15-1.08-2.15-2.4 0-1.32.95-2.4 2.15-2.4 1.21 0 2.17 1.09 2.15 2.4 0 1.32-.94 2.4-2.15 2.4Z" />
    </svg>
  );
}

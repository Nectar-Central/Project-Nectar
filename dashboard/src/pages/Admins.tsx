import { useState } from "react";
import { useOutletContext, useParams } from "react-router-dom";
import { Trash2, UserPlus } from "lucide-react";
import { api } from "@/lib/api";
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
} from "@/components/ui";
import { formatDate } from "@/lib/utils";

type Ctx = { guild: { id: number }; canManage: boolean };

export function AdminsPage() {
  const { guildId } = useParams();
  const { canManage } = useOutletContext<Ctx>();
  const gid = Number(guildId);
  const { data, loading, error, reload } = useAsync(() => api.admins(gid), [gid]);

  const [userId, setUserId] = useState("");
  const [role, setRole] = useState("admin");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const add = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await api.addAdmin(gid, userId.trim(), role);
      setUserId("");
      reload();
      setMsg("Added.");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not add staff member");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    setMsg(null);
    try {
      await api.removeAdmin(gid, id);
      reload();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not remove staff member");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Staff</h1>
        <p className="mt-1 text-sm text-muted">
          Who can open this dashboard. Discord permissions are separate — this list is what Nectar
          trusts.
        </p>
      </header>

      {error ? <Alert tone="error">{error}</Alert> : null}

      {canManage ? (
        <Card>
          <CardBody className="pt-5">
            <h2 className="mb-3 text-sm font-semibold">Add someone</h2>
            <div className="grid gap-3 sm:grid-cols-[1fr_180px_auto] sm:items-end">
              <Field label="Discord user ID">
                <Input
                  value={userId}
                  onChange={(e) => setUserId(e.target.value)}
                  placeholder="123456789012345678"
                  inputMode="numeric"
                />
              </Field>
              <Field label="Role">
                <Select value={role} onChange={(e) => setRole(e.target.value)}>
                  <option value="admin">Admin — can change tournaments</option>
                  <option value="moderator">Moderator — view + bans only</option>
                </Select>
              </Field>
              <Button onClick={add} disabled={busy || !userId.trim()}>
                <UserPlus className="h-3.5 w-3.5" /> Add
              </Button>
            </div>
            {msg ? (
              <div className="mt-3">
                <Alert tone={msg === "Added." ? "success" : "error"}>{msg}</Alert>
              </div>
            ) : null}
          </CardBody>
        </Card>
      ) : null}

      {loading ? (
        <Skeleton className="h-40" />
      ) : (data?.admins?.length ?? 0) === 0 ? (
        <EmptyState title="No staff listed" hint="The server owner is added automatically on /setup." />
      ) : (
        <Card>
          <CardBody className="pt-4">
            <ul className="divide-y divide-border">
              {data!.admins.map((a) => (
                <li key={a.user_id} className="flex items-center gap-2 py-2.5">
                  <span className="min-w-0 flex-1">
                    <span className="block font-mono text-sm">{a.user_id}</span>
                    <span className="block text-[11px] text-muted">since {formatDate(a.added_at)}</span>
                  </span>
                  <Badge className="border-border bg-white/5 capitalize text-muted">{a.role}</Badge>
                  {canManage && a.role !== "owner" ? (
                    <Button
                      size="icon"
                      variant="ghost"
                      disabled={busy}
                      onClick={() => remove(a.user_id)}
                      title="Remove"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

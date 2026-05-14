"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type PendingUser, type User } from "@/lib/api";

type Granted = { username: string; pin: string } | null;

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    approved: "bg-forest/10 text-forest",
    pending: "bg-gold/10 text-gold",
    rejected: "bg-muted/15 text-muted",
  };
  return (
    <span className={`text-[10px] eyebrow px-2 py-1 rounded-full ${colors[status] || colors.rejected}`}>
      {status}
    </span>
  );
}

function Badge({ children, color }: { children: React.ReactNode; color: "gold" }) {
  const colors = { gold: "bg-gold/15 text-gold" };
  return (
    <span className={`text-[10px] eyebrow px-2 py-0.5 rounded-full ${colors[color]}`}>
      {children}
    </span>
  );
}

export default function AdminDashboard() {
  const router = useRouter();
  const [me, setMe] = useState<User | null>(null);
  const [pending, setPending] = useState<PendingUser[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [granted, setGranted] = useState<Granted>(null);
  const [loading, setLoading] = useState(true);
  const [showRejected, setShowRejected] = useState(false);

  async function loadAll() {
    try {
      const [{ user }, p, u] = await Promise.all([
        api.me(),
        api.admin.pending(),
        api.admin.users(),
      ]);
      if (!user.is_admin) {
        router.push("/caddy");
        return;
      }
      setMe(user);
      setPending(p.pending);
      setUsers(u.users);
    } catch {
      router.push("/login");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadAll();
  }, []);

  async function handleApprove(id: number) {
    const { username, pin } = await api.admin.approve(id);
    setGranted({ username, pin });
    loadAll();
  }
  async function handleReject(id: number) {
    if (!confirm("Reject this request?")) return;
    await api.admin.reject(id);
    loadAll();
  }
  async function handleResetPin(id: number, username: string) {
    if (!confirm(`Reset PIN for ${username}?`)) return;
    const { pin } = await api.admin.resetPin(id);
    setGranted({ username, pin });
  }
  async function handleDeactivate(id: number, username: string) {
    if (!confirm(`Deactivate ${username}? They will be signed out and unable to use Caddy.`)) return;
    await api.admin.deactivate(id);
    loadAll();
  }
  async function handleReactivate(id: number, username: string) {
    if (!confirm(`Reactivate ${username}?`)) return;
    await api.admin.reactivate(id);
    loadAll();
  }
  async function handleDelete(id: number, username: string) {
    if (!confirm(`PERMANENTLY DELETE ${username} and all their data? This cannot be undone.`)) return;
    await api.admin.delete(id);
    loadAll();
  }
  async function handleLogout() {
    await api.logout();
    router.push("/");
  }

  if (loading) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <p className="text-muted text-sm eyebrow">Loading...</p>
      </main>
    );
  }

  return (
    <main className="min-h-screen flex flex-col bg-cream">
      <header className="px-6 py-5 border-b border-line bg-paper">
        <div className="max-w-3xl mx-auto w-full flex items-center justify-between">
          <div>
            <Link href="/" className="wordmark text-2xl text-forest block">
              Caddy
            </Link>
            <p className="eyebrow text-gold mt-0.5">Admin · {me?.full_name}</p>
          </div>
          <div className="flex items-center gap-4">
            <Link
              href="/profile"
              className="text-[12px] eyebrow text-muted hover:text-forest transition"
            >
              My Profile
            </Link>
            <button
              onClick={handleLogout}
              className="text-[12px] eyebrow text-muted hover:text-forest transition"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <section className="flex-1 px-6 py-8 max-w-3xl mx-auto w-full">
        {granted && (
          <div className="bg-forest text-cream rounded-2xl p-6 mb-8 relative">
            <button
              onClick={() => setGranted(null)}
              className="absolute top-4 right-4 text-cream/60 hover:text-cream text-xl"
            >
              ×
            </button>
            <p className="eyebrow text-gold mb-2">Credentials to share</p>
            <p className="text-[13px] text-cream/80 mb-4">
              Send these to <span className="font-semibold text-cream">{granted.username}</span> directly. PIN is shown only once.
            </p>
            <div className="bg-forest-deep rounded-xl p-4 flex justify-between items-center">
              <div>
                <p className="eyebrow text-gold/70 mb-1">Username</p>
                <p className="text-lg text-cream font-mono">{granted.username}</p>
              </div>
              <div className="text-right">
                <p className="eyebrow text-gold/70 mb-1">PIN</p>
                <p className="text-2xl text-gold font-mono tracking-[0.3em]">{granted.pin}</p>
              </div>
            </div>
          </div>
        )}

        {/* Pending Requests */}
        <div className="mb-12">
          <div className="flex items-baseline justify-between mb-4">
            <h2 className="wordmark text-[28px] text-forest">Pending requests</h2>
            <span className="eyebrow text-muted">{pending.length}</span>
          </div>

          {pending.length === 0 ? (
            <div className="bg-paper border border-line border-dashed rounded-2xl p-8 text-center">
              <p className="text-muted text-[14px]">No pending requests right now.</p>
            </div>
          ) : (
            <div className="space-y-3">
              {pending.map((p) => (
                <div key={p.id} className="bg-paper border border-line rounded-2xl p-5">
                  <div className="flex items-start justify-between mb-2">
                    <div>
                      <p className="text-[16px] font-semibold text-forest">{p.full_name}</p>
                      <p className="text-[13px] text-muted">@{p.username}</p>
                    </div>
                    <p className="text-[11px] text-muted/80">
                      {new Date(p.created_at).toLocaleDateString()}
                    </p>
                  </div>
                  <div className="text-[13px] text-ink/80 space-y-0.5 mb-4">
                    <p>{p.email}</p>
                    {p.phone && <p className="text-muted">{p.phone}</p>}
                    {p.referral && (
                      <p className="text-[12px] text-muted mt-1.5">
                        <span className="eyebrow text-gold">via</span>{" "}
                        {p.referral}
                      </p>
                    )}
                    {p.reason && (
                      <p className="italic text-muted mt-2 border-l-2 border-gold pl-3">
                        {p.reason}
                      </p>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => handleApprove(p.id)}
                      className="flex-1 bg-forest text-cream py-2.5 px-4 rounded-full text-[13px] font-medium hover-lift"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() => handleReject(p.id)}
                      className="px-5 py-2.5 border border-line text-muted rounded-full text-[13px] hover:text-forest hover:border-forest transition"
                    >
                      Reject
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Active Users (approved + pending — no rejected) */}
        {(() => {
          const activeUsers = users.filter((u) => u.status !== "rejected");
          const rejectedUsers = users.filter((u) => u.status === "rejected");

          return (
            <>
              <div className="mb-10">
                <div className="flex items-baseline justify-between mb-4">
                  <h2 className="wordmark text-[28px] text-forest">Active users</h2>
                  <span className="eyebrow text-muted">{activeUsers.length}</span>
                </div>
                <div className="bg-paper border border-line rounded-2xl divide-y divide-line">
                  {activeUsers.map((u) => (
                    <div key={u.id} className="p-4">
                      <div className="flex items-start justify-between gap-3 mb-2">
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2 mb-0.5">
                            <p className="text-[14px] font-semibold text-forest truncate">
                              {u.full_name}
                            </p>
                            {u.is_admin && <Badge color="gold">Admin</Badge>}
                          </div>
                          <p className="text-[12px] text-muted truncate">@{u.username} · {u.email}</p>
                        </div>
                        <StatusBadge status={u.status} />
                      </div>
                      {u.status === "approved" && !u.is_admin && (
                        <div className="flex items-center gap-3 mt-2 pt-2 border-t border-line/50">
                          <button
                            onClick={() => handleResetPin(u.id, u.username)}
                            className="text-[11px] eyebrow text-muted hover:text-forest transition"
                          >
                            Reset PIN
                          </button>
                          <span className="text-line">·</span>
                          <button
                            onClick={() => handleDeactivate(u.id, u.username)}
                            className="text-[11px] eyebrow text-muted hover:text-forest transition"
                          >
                            Deactivate
                          </button>
                          <span className="text-line">·</span>
                          <button
                            onClick={() => handleDelete(u.id, u.username)}
                            className="text-[11px] eyebrow text-muted hover:text-red-700 transition"
                          >
                            Delete
                          </button>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>

              {/* Rejected — collapsed by default */}
              {rejectedUsers.length > 0 && (
                <div>
                  <button
                    onClick={() => setShowRejected(!showRejected)}
                    className="flex items-center justify-between w-full mb-3 group"
                  >
                    <div className="flex items-baseline gap-3">
                      <h2 className="wordmark text-[20px] text-muted group-hover:text-forest transition">
                        Rejected
                      </h2>
                      <span className="eyebrow text-muted">{rejectedUsers.length}</span>
                    </div>
                    <span className="text-muted group-hover:text-forest transition text-[18px]">
                      {showRejected ? "−" : "+"}
                    </span>
                  </button>
                  {showRejected && (
                    <div className="bg-paper/60 border border-line border-dashed rounded-2xl divide-y divide-line/60">
                      {rejectedUsers.map((u) => (
                        <div key={u.id} className="p-4">
                          <div className="flex items-start justify-between gap-3 mb-2">
                            <div className="min-w-0 flex-1 opacity-60">
                              <p className="text-[13px] text-muted truncate">{u.full_name}</p>
                              <p className="text-[11px] text-muted/70 truncate">@{u.username} · {u.email}</p>
                            </div>
                            <StatusBadge status={u.status} />
                          </div>
                          <div className="flex items-center gap-3 mt-2 pt-2 border-t border-line/40">
                            <button
                              onClick={() => handleReactivate(u.id, u.username)}
                              className="text-[11px] eyebrow text-muted hover:text-forest transition"
                            >
                              Reactivate
                            </button>
                            <span className="text-line">·</span>
                            <button
                              onClick={() => handleDelete(u.id, u.username)}
                              className="text-[11px] eyebrow text-muted hover:text-red-700 transition"
                            >
                              Delete
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </>
          );
        })()}
      </section>

      <footer className="px-6 py-8 max-w-3xl mx-auto w-full">
        <p className="eyebrow text-muted text-center">Caddy · 2026</p>
      </footer>
    </main>
  );
}

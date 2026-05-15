"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type User, type Round, type ArchivedConversation } from "@/lib/api";

const CLUB_ORDER = [
  "driver", "3-wood", "5-wood", "4-iron", "5-iron", "6-iron",
  "7-iron", "8-iron", "9-iron", "pitching_wedge", "gap_wedge",
  "sand_wedge", "lob_wedge",
];

const CLUB_LABELS: Record<string, string> = {
  driver: "Driver",
  "3-wood": "3-wood",
  "5-wood": "5-wood",
  "4-iron": "4-iron",
  "5-iron": "5-iron",
  "6-iron": "6-iron",
  "7-iron": "7-iron",
  "8-iron": "8-iron",
  "9-iron": "9-iron",
  pitching_wedge: "Pitching wedge",
  gap_wedge: "Gap wedge",
  sand_wedge: "Sand wedge",
  lob_wedge: "Lob wedge",
};

export default function ProfilePage() {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [conversations, setConversations] = useState<ArchivedConversation[]>([]);

  async function refreshUser() {
    const { user } = await api.me();
    setUser(user);
    return user;
  }

  useEffect(() => {
    api.me()
      .then(({ user }) => {
        setUser(user);
        api.caddy.conversations()
          .then(({ conversations }) => setConversations(conversations))
          .catch(() => setConversations([]));
      })
      .catch(() => router.push("/login"))
      .finally(() => setLoading(false));
  }, []);

  async function handleDeleteRound(originalIndex: number) {
    if (!confirm("Delete this round? Your handicap will recalculate.")) return;
    try {
      await api.deleteRound(originalIndex);
      await refreshUser();
    } catch (err) {
      alert("Couldn't delete round: " + (err instanceof Error ? err.message : "unknown"));
    }
  }

  async function handleLogout() {
    await api.logout();
    router.push("/");
  }

  if (loading || !user) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <p className="text-muted text-sm eyebrow">Loading...</p>
      </main>
    );
  }

  const inBag = CLUB_ORDER.filter((c) => user.bag?.[c]);

  return (
    <main className="min-h-screen flex flex-col bg-cream">
      <header className="px-6 py-5 border-b border-line bg-paper">
        <div className="max-w-2xl mx-auto w-full flex items-center justify-between">
          <div>
            <Link href="/" className="wordmark text-2xl text-forest block">
              Caddy
            </Link>
            <p className="eyebrow text-gold mt-0.5">Your profile</p>
          </div>
          <div className="flex items-center gap-4">
            <Link
              href="/setup/bag"
              className="text-[12px] eyebrow text-muted hover:text-forest transition"
            >
              Edit bag
            </Link>
            {user.is_admin && (
              <Link
                href="/admin"
                className="text-[12px] eyebrow text-muted hover:text-forest transition"
              >
                Admin
              </Link>
            )}
            <button
              onClick={handleLogout}
              className="text-[12px] eyebrow text-muted hover:text-forest transition"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <section className="flex-1 px-6 py-8 max-w-2xl mx-auto w-full">
        {/* Identity */}
        <div className="mb-6">
          <p className="eyebrow text-gold mb-2">{user.is_admin ? "Player + Admin" : "Player"}</p>
          <h1 className="wordmark text-[44px] leading-tight text-forest mb-1">
            {user.full_name}
          </h1>
          <p className="text-[14px] text-muted">@{user.username}</p>
        </div>

        {/* HERO CTA — Talk to Caddy */}
        <Link
          href="/caddy"
          className="group block mb-10 bg-forest hover:bg-forest-deep text-cream rounded-3xl px-6 py-5 transition shadow-md shadow-forest/20 hover-lift"
        >
          <div className="flex items-center justify-between gap-4">
            <div className="flex items-center gap-4">
              <div className="w-12 h-12 rounded-full bg-gold/20 flex items-center justify-center flex-shrink-0">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-gold">
                  <rect x="9" y="2" width="6" height="12" rx="3" />
                  <path d="M5 10v2a7 7 0 0 0 14 0v-2" />
                  <line x1="12" y1="19" x2="12" y2="22" />
                </svg>
              </div>
              <div>
                <p className="eyebrow text-gold mb-0.5">Talk to Caddy</p>
                <p className="text-[15px] text-cream/95 leading-snug">Get a recommendation. Log a score. Anytime.</p>
              </div>
            </div>
            <span className="text-gold/70 group-hover:text-gold group-hover:translate-x-1 transition text-xl">→</span>
          </div>
        </Link>

        {/* Top stats */}
        <div className="grid grid-cols-3 gap-3 mb-10">
          <Stat
            label="Handicap"
            value={user.handicap_index !== null && user.handicap_index !== undefined ? String(user.handicap_index) : "—"}
            sub={user.handicap_index === null || user.handicap_index === undefined ? "Need 3+ rounds" : "WHS index"}
          />
          <Stat
            label="In bag"
            value={String(inBag.length)}
            sub="clubs"
          />
          <Stat
            label="Home course"
            value={user.home_course || "—"}
            sub={user.home_course ? "" : "Not set"}
          />
        </div>

        {/* Bag */}
        <Section title="Your bag">
          {inBag.length === 0 ? (
            <Empty text="No clubs in your bag yet. Run onboarding to add them." />
          ) : (
            <div className="bg-paper border border-line rounded-2xl divide-y divide-line">
              {inBag.map((club) => (
                <div key={club} className="px-5 py-3 flex items-center justify-between">
                  <span className="text-[14px] text-ink">{CLUB_LABELS[club]}</span>
                  <span className="text-[14px] text-forest font-medium">
                    {user.bag?.[club]} <span className="text-muted text-[12px]">yards</span>
                  </span>
                </div>
              ))}
            </div>
          )}
        </Section>

        {/* Tendencies */}
        <Section title="Tendencies">
          <div className="bg-paper border border-line rounded-2xl p-5 space-y-4">
            <Field label="Driver miss" value={user.driver_miss} />
            <Field label="Iron miss" value={user.iron_miss} />
          </div>
        </Section>

        {/* Recent rounds */}
        <Section title="Recent rounds">
          <RecentRounds rounds={user.rounds || []} onDelete={handleDeleteRound} />
        </Section>

        {/* Past conversations — every chat archived, never lost */}
        <Section title="Past conversations">
          <PastConversations conversations={conversations} />
        </Section>

        {/* Trackman / Claude tendencies summary */}
        <Section title="Caddy's read on your game">
          {user.tendencies_summary ? (
            <div className="bg-forest text-cream rounded-2xl p-6">
              <p className="eyebrow text-gold mb-3">Generated by Caddy</p>
              <p className="text-[14px] leading-relaxed whitespace-pre-line">
                {user.tendencies_summary}
              </p>
            </div>
          ) : (
            <Empty text="Caddy will write your tendencies summary after your first round or Trackman upload." />
          )}
        </Section>
      </section>

      <footer className="px-6 py-8 max-w-2xl mx-auto w-full">
        <p className="eyebrow text-muted text-center">Caddy · 2026</p>
      </footer>
    </main>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-10">
      <h2 className="wordmark text-[24px] text-forest mb-3">{title}</h2>
      {children}
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="bg-paper border border-line rounded-2xl px-4 py-4">
      <p className="eyebrow text-gold mb-1">{label}</p>
      <p className="text-[24px] text-forest font-semibold leading-none mb-1">{value}</p>
      <p className="text-[10px] text-muted">{sub}</p>
    </div>
  );
}

function Field({ label, value }: { label: string; value?: string | null }) {
  return (
    <div>
      <p className="eyebrow text-muted mb-1">{label}</p>
      <p className="text-[14px] text-ink leading-relaxed">
        {value || <span className="text-muted italic">Not set</span>}
      </p>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return (
    <div className="bg-paper border border-line border-dashed rounded-2xl p-6 text-center">
      <p className="text-[13px] text-muted">{text}</p>
    </div>
  );
}

function RecentRounds({
  rounds,
  onDelete,
}: {
  rounds: Round[];
  onDelete: (originalIndex: number) => void;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);

  if (rounds.length === 0) {
    return <Empty text="No rounds logged yet. Caddy will track these as you play." />;
  }

  // Pair each round with its original index BEFORE sorting (we need it to delete)
  const indexed = rounds.map((r, originalIndex) => ({ r, originalIndex }));
  // Sort by date descending (most recent first)
  const sorted = [...indexed].sort((a, b) =>
    (b.r.date || "").localeCompare(a.r.date || "")
  );

  function formatDate(dateStr: string) {
    if (!dateStr) return "";
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return dateStr;
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }

  return (
    <div className="bg-paper border border-line rounded-2xl divide-y divide-line">
      {sorted.map(({ r, originalIndex }, i) => {
        const isOpen = expanded === i;
        const hasDetails = r.hole_scores || r.differential != null || r.course_rating != null;
        return (
          <div key={originalIndex}>
            <button
              onClick={() => hasDetails && setExpanded(isOpen ? null : i)}
              disabled={!hasDetails}
              className={`w-full px-5 py-4 flex items-center justify-between text-left ${
                hasDetails ? "hover:bg-cream/40 transition cursor-pointer" : "cursor-default"
              }`}
            >
              <div className="min-w-0 flex-1">
                <p className="text-[14px] font-semibold text-forest truncate">{r.course || "Unknown course"}</p>
                <p className="text-[11px] text-muted mt-0.5">{formatDate(r.date)}</p>
              </div>
              <div className="flex items-center gap-3 flex-shrink-0 ml-3">
                <div className="text-right">
                  <p className="text-[18px] font-semibold text-forest leading-none">{r.score}</p>
                  {r.differential != null && (
                    <p className="text-[10px] text-muted mt-1">diff {r.differential.toFixed(1)}</p>
                  )}
                </div>
                {hasDetails && (
                  <span className={`text-muted text-[12px] transition-transform ${isOpen ? "rotate-180" : ""}`}>
                    ▾
                  </span>
                )}
              </div>
            </button>

            {isOpen && hasDetails && (
              <div className="px-5 pb-5 pt-1 bg-cream/30">
                {r.hole_scores && r.hole_scores.length > 0 && (
                  <div className="mb-3">
                    <p className="eyebrow text-gold mb-2">Hole-by-hole</p>
                    <div className="grid grid-cols-9 gap-1 text-center">
                      {r.hole_scores.map((s, idx) => (
                        <div key={idx} className="bg-paper border border-line rounded-md py-2">
                          <p className="text-[9px] text-muted">{idx + 1}</p>
                          <p className="text-[13px] font-semibold text-forest">{s ?? "—"}</p>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                <div className="grid grid-cols-3 gap-2 text-[11px] mb-4">
                  {r.course_rating != null && (
                    <Stat2 label="Rating" value={r.course_rating.toFixed(1)} />
                  )}
                  {r.slope_rating != null && (
                    <Stat2 label="Slope" value={String(r.slope_rating)} />
                  )}
                  {r.holes != null && (
                    <Stat2 label="Holes" value={String(r.holes)} />
                  )}
                </div>
                <div className="flex justify-end pt-2 border-t border-line/50">
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(originalIndex);
                    }}
                    className="text-[11px] eyebrow text-muted hover:text-red-700 transition"
                  >
                    Delete round
                  </button>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function PastConversations({ conversations }: { conversations: ArchivedConversation[] }) {
  if (conversations.length === 0) {
    return <Empty text="No past conversations yet. Every chat with Caddy is archived here, even casual ones." />;
  }
  function fmt(date: string) {
    if (!date) return "";
    const d = new Date(date);
    if (isNaN(d.getTime())) return date;
    return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }
  return (
    <div className="bg-paper border border-line rounded-2xl divide-y divide-line">
      {conversations.map((c) => (
        <Link
          key={c.id}
          href={`/conversations/${c.id}`}
          className="block px-5 py-3 hover:bg-cream/40 transition"
        >
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 mb-0.5">
                <span className={`text-[10px] eyebrow px-2 py-0.5 rounded-full ${
                  c.kind === "round" ? "bg-forest/10 text-forest" : "bg-gold/10 text-gold"
                }`}>
                  {c.kind === "round" ? "Round" : "Chat"}
                </span>
                {c.course_name && (
                  <span className="text-[13px] text-forest truncate">{c.course_name}</span>
                )}
              </div>
              <p className="text-[11px] text-muted">{fmt(c.ended_at)}</p>
            </div>
            {c.total_score != null && (
              <div className="text-right flex-shrink-0">
                <p className="text-[16px] font-semibold text-forest leading-none">{c.total_score}</p>
                {c.round_metadata?.differential != null && (
                  <p className="text-[10px] text-muted mt-0.5">diff {c.round_metadata.differential.toFixed(1)}</p>
                )}
              </div>
            )}
            <span className="text-muted/50 text-sm">→</span>
          </div>
        </Link>
      ))}
    </div>
  );
}

function Stat2({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-paper border border-line rounded-md px-3 py-2">
      <p className="eyebrow text-muted">{label}</p>
      <p className="text-[13px] font-semibold text-forest mt-0.5">{value}</p>
    </div>
  );
}

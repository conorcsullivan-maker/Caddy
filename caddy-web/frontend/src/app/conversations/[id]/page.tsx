"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type ArchivedConversationDetail, type User } from "@/lib/api";

export default function ConversationDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const id = parseInt(String(params?.id || ""), 10);

  const [conv, setConv] = useState<ArchivedConversationDetail | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) {
      setError("Bad conversation id");
      setLoading(false);
      return;
    }
    Promise.all([
      api.caddy.conversation(id),
      api.me().then((r) => r.user).catch(() => null),
    ])
      .then(([c, u]) => {
        setConv(c);
        setUser(u);
      })
      .catch((e: Error) => {
        setError(e.message);
      })
      .finally(() => setLoading(false));
  }, [id]);

  if (loading) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <p className="text-muted text-sm eyebrow">Loading...</p>
      </main>
    );
  }
  if (error || !conv) {
    return (
      <main className="min-h-screen flex flex-col items-center justify-center px-6 text-center">
        <p className="eyebrow text-muted mb-3">Couldn&apos;t load conversation</p>
        <p className="text-[14px] text-ink mb-6">{error}</p>
        <Link href="/profile" className="text-forest underline-offset-4 hover:underline">
          Back to profile
        </Link>
      </main>
    );
  }

  function fmt(date: string) {
    if (!date) return "";
    const d = new Date(date);
    if (isNaN(d.getTime())) return date;
    return d.toLocaleString("en-US", {
      month: "short", day: "numeric", year: "numeric",
      hour: "numeric", minute: "2-digit",
    });
  }

  return (
    <main className="min-h-screen flex flex-col bg-cream">
      {/* Header */}
      <header className="px-5 py-4 border-b border-line bg-paper flex-shrink-0">
        <div className="max-w-2xl mx-auto w-full flex items-center justify-between">
          <Link href="/profile" className="text-[12px] eyebrow text-muted hover:text-forest transition">
            ← Profile
          </Link>
          <span className="wordmark text-xl text-forest">Caddy</span>
          {user?.can_export_conversations ? (
            <a
              href={api.caddy.downloadConversationUrl(id)}
              className="text-[12px] eyebrow text-muted hover:text-forest transition"
              title="Download this conversation as a Word document"
            >
              ↓ Word
            </a>
          ) : (
            <span className="text-[12px] eyebrow text-muted/0">_</span>
          )}
        </div>
      </header>

      {/* Conversation summary */}
      <section className="px-5 py-6 border-b border-line bg-paper">
        <div className="max-w-2xl mx-auto w-full">
          <div className="flex items-center gap-2 mb-2">
            <span className={`text-[10px] eyebrow px-2 py-0.5 rounded-full ${
              conv.kind === "round" ? "bg-forest/10 text-forest" : "bg-gold/10 text-gold"
            }`}>
              {conv.kind === "round" ? "Logged round" : "Casual chat"}
            </span>
            <span className="text-[11px] text-muted">{fmt(conv.ended_at)}</span>
          </div>
          {conv.course_name && (
            <h1 className="wordmark text-[28px] text-forest leading-tight">{conv.course_name}</h1>
          )}
          {conv.total_score != null && (
            <div className="flex items-center gap-4 mt-3 text-[13px]">
              <span><span className="eyebrow text-muted mr-1">Score</span> <span className="font-semibold text-forest">{conv.total_score}</span></span>
              {conv.round_metadata?.differential != null && (
                <span><span className="eyebrow text-muted mr-1">Differential</span> {conv.round_metadata.differential.toFixed(1)}</span>
              )}
              {conv.round_metadata?.handicap_after != null && (
                <span><span className="eyebrow text-muted mr-1">Hcp after</span> {conv.round_metadata.handicap_after}</span>
              )}
            </div>
          )}
        </div>
      </section>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-6">
        <div className="max-w-2xl mx-auto w-full space-y-4">
          {conv.messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[80%] rounded-3xl px-5 py-3 text-[15px] leading-relaxed whitespace-pre-wrap ${
                  m.role === "user"
                    ? "bg-forest text-cream rounded-br-md"
                    : "bg-paper border border-line text-ink rounded-bl-md"
                }`}
              >
                {m.content}
              </div>
            </div>
          ))}
        </div>
      </div>

      <footer className="px-6 py-6 max-w-2xl mx-auto w-full">
        <p className="eyebrow text-muted text-center">Caddy · 2026</p>
      </footer>
    </main>
  );
}

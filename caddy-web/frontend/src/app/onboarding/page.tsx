"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";

export default function OnboardingPage() {
  const router = useRouter();
  const [fullName, setFullName] = useState("");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [referral, setReferral] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await api.signup({
        full_name: fullName.trim(),
        username: username.trim().toLowerCase(),
        email: email.trim(),
        phone: phone.trim() || undefined,
        referral: referral.trim() || undefined,
        reason: reason.trim() || undefined,
      });
      router.push("/pending");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen flex flex-col">
      <header className="px-6 py-5 max-w-md mx-auto w-full">
        <Link href="/" className="wordmark text-2xl text-forest">
          Caddy
        </Link>
      </header>

      <section className="flex-1 flex flex-col justify-center px-6 max-w-md mx-auto w-full py-6">
        <p className="eyebrow text-gold mb-3">Request access</p>
        <h1 className="wordmark text-[40px] leading-tight text-forest mb-4">
          Join the Caddy<br />beta.
        </h1>

        <div className="w-12 h-[2px] bg-gold mb-5" />

        <p className="text-[15px] text-muted leading-relaxed mb-7">
          Caddy is in invite-only beta. Fill this out and we&apos;ll get back to you with your sign-in details.
        </p>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4 mb-6">
          <div>
            <label className="eyebrow text-muted block mb-2">Full name</label>
            <input
              type="text"
              autoComplete="name"
              required
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              className="w-full bg-paper border border-line rounded-2xl px-4 py-4 text-[16px] text-ink placeholder:text-muted/60 focus:outline-none focus:border-forest transition"
              placeholder="Jane Smith"
            />
          </div>

          <div>
            <label className="eyebrow text-muted block mb-2">Username</label>
            <input
              type="text"
              autoCapitalize="none"
              autoCorrect="off"
              required
              minLength={3}
              maxLength={30}
              pattern="[a-zA-Z0-9_]+"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full bg-paper border border-line rounded-2xl px-4 py-4 text-[16px] text-ink placeholder:text-muted/60 focus:outline-none focus:border-forest transition"
              placeholder="janesmith"
            />
            <p className="text-[11px] text-muted/80 mt-1.5 ml-1">Letters, numbers, underscores only.</p>
          </div>

          <div>
            <label className="eyebrow text-muted block mb-2">Email</label>
            <input
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full bg-paper border border-line rounded-2xl px-4 py-4 text-[16px] text-ink placeholder:text-muted/60 focus:outline-none focus:border-forest transition"
              placeholder="jane@example.com"
            />
          </div>

          <div>
            <label className="eyebrow text-muted block mb-2">
              Phone <span className="lowercase tracking-normal text-muted/60">(optional)</span>
            </label>
            <input
              type="tel"
              autoComplete="tel"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              className="w-full bg-paper border border-line rounded-2xl px-4 py-4 text-[16px] text-ink placeholder:text-muted/60 focus:outline-none focus:border-forest transition"
              placeholder="(555) 123-4567"
            />
          </div>

          <div>
            <label className="eyebrow text-muted block mb-2">
              How did you hear about Caddy? <span className="lowercase tracking-normal text-muted/60">(optional)</span>
            </label>
            <input
              type="text"
              value={referral}
              onChange={(e) => setReferral(e.target.value)}
              maxLength={200}
              className="w-full bg-paper border border-line rounded-2xl px-4 py-4 text-[16px] text-ink placeholder:text-muted/60 focus:outline-none focus:border-forest transition"
              placeholder="A friend, social, etc."
            />
          </div>

          <div>
            <label className="eyebrow text-muted block mb-2">
              Why caddy? <span className="lowercase tracking-normal text-muted/60">(optional)</span>
            </label>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              maxLength={500}
              rows={3}
              className="w-full bg-paper border border-line rounded-2xl px-4 py-4 text-[15px] text-ink placeholder:text-muted/60 focus:outline-none focus:border-forest transition resize-none"
              placeholder="Tell us a bit about your game..."
            />
          </div>

          {error && (
            <p className="text-[13px] text-red-700 bg-red-50 border border-red-100 rounded-xl px-3 py-2">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="bg-forest text-cream py-4 px-6 rounded-full font-medium text-[15px] tracking-wide mt-2 disabled:opacity-50 disabled:cursor-not-allowed hover-lift"
          >
            {loading ? "Submitting..." : "Request access"}
          </button>
        </form>

        <Link
          href="/login"
          className="text-[13px] text-forest text-center underline-offset-4 hover:underline"
        >
          Already approved? Sign in
        </Link>
      </section>

      <footer className="px-6 py-8 max-w-md mx-auto w-full">
        <p className="eyebrow text-muted text-center">Caddy · 2026</p>
      </footer>
    </main>
  );
}

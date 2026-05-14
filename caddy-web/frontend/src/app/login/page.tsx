"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const { user } = await api.login(username, pin);
      if (user.is_admin) router.push("/admin");
      else if (!user.onboarded) router.push("/setup/bag");
      else router.push("/profile");
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

      <section className="flex-1 flex flex-col justify-center px-6 max-w-md mx-auto w-full">
        <p className="eyebrow text-gold mb-3">Sign in</p>
        <h1 className="wordmark text-[44px] leading-tight text-forest mb-8">
          Welcome back.
        </h1>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4 mb-6">
          <div>
            <label className="eyebrow text-muted block mb-2">Username</label>
            <input
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              autoCorrect="off"
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full bg-paper border border-line rounded-2xl px-4 py-4 text-[16px] text-ink placeholder:text-muted/60 focus:outline-none focus:border-forest transition"
              placeholder="your username"
            />
          </div>
          <div>
            <label className="eyebrow text-muted block mb-2">PIN</label>
            <input
              type="password"
              inputMode="numeric"
              maxLength={4}
              autoComplete="current-password"
              required
              value={pin}
              onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))}
              className="w-full bg-paper border border-line rounded-2xl px-4 py-4 text-[16px] text-ink placeholder:text-muted/60 focus:outline-none focus:border-forest transition tracking-[0.5em]"
              placeholder="••••"
            />
          </div>

          {error && (
            <p className="text-[13px] text-red-700 bg-red-50 border border-red-100 rounded-xl px-3 py-2">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading || !username || pin.length !== 4}
            className="bg-forest text-cream py-4 px-6 rounded-full font-medium text-[15px] tracking-wide mt-2 disabled:opacity-50 disabled:cursor-not-allowed hover-lift"
          >
            {loading ? "Signing in..." : "Sign in"}
          </button>
        </form>

        <Link
          href="/onboarding"
          className="text-[13px] text-forest text-center underline-offset-4 hover:underline"
        >
          New here? Request an account
        </Link>
      </section>

      <footer className="px-6 py-8 max-w-md mx-auto w-full">
        <p className="eyebrow text-muted text-center">Caddy · 2026</p>
      </footer>
    </main>
  );
}

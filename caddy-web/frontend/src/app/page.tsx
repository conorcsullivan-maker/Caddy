import Link from "next/link";

export default function LandingPage() {
  return (
    <main className="min-h-screen flex flex-col">
      {/* Top nav */}
      <header className="px-6 py-5 flex items-center justify-between max-w-md sm:max-w-2xl mx-auto w-full">
        <span className="wordmark text-2xl text-forest">Caddy</span>
        <Link
          href="/login"
          className="eyebrow text-muted hover:text-forest transition"
        >
          Sign in
        </Link>
      </header>

      {/* Hero */}
      <section className="flex-1 flex flex-col justify-center px-6 max-w-md sm:max-w-2xl mx-auto w-full">
        <p className="eyebrow text-gold mb-5">Beta · 2026</p>

        <h1 className="wordmark text-[64px] sm:text-[88px] leading-[0.95] text-forest mb-6">
          The caddy<br />that knows<br />your game.
        </h1>

        <div className="w-12 h-[2px] bg-gold mb-6" />

        <p className="text-[18px] sm:text-[20px] text-ink/85 leading-relaxed mb-3">
          Voice-first. Course-aware. Built to learn your swing the way a real caddy does — over time, round by round.
        </p>
        <p className="text-[15px] text-muted leading-relaxed mb-10 italic">
          Ask for a club. Log a score. Caddy remembers everything.
        </p>

        {/* CTAs */}
        <div className="flex flex-col gap-3 mb-12">
          <Link
            href="/onboarding"
            className="hover-lift bg-forest text-cream py-4 px-6 rounded-full text-center font-medium text-[15px] tracking-wide shadow-sm"
          >
            Get started
          </Link>
          <Link
            href="/login"
            className="hover-lift border border-line text-forest py-4 px-6 rounded-full text-center font-medium text-[15px] tracking-wide"
          >
            I already have an account
          </Link>
        </div>

        {/* Mini features */}
        <div className="grid grid-cols-2 gap-x-6 gap-y-5 mb-10 pt-6 border-t border-line">
          <Feature label="Voice-first" body="Just talk. Caddy listens, thinks, and answers out loud." />
          <Feature label="Knows the course" body="30,000+ courses loaded with hole-by-hole data." />
          <Feature label="Tracks your score" body="Live scorecard. Auto-handicap. WHS-accurate." />
          <Feature label="Learns your swing" body="Trackman + on-course logging build your profile." />
        </div>
      </section>

      {/* Footer */}
      <footer className="px-6 py-8 max-w-md sm:max-w-2xl mx-auto w-full">
        <p className="eyebrow text-muted text-center">Caddy · 2026</p>
      </footer>
    </main>
  );
}

function Feature({ label, body }: { label: string; body: string }) {
  return (
    <div>
      <p className="text-[13px] font-semibold text-forest mb-1">{label}</p>
      <p className="text-[12px] text-muted leading-relaxed">{body}</p>
    </div>
  );
}

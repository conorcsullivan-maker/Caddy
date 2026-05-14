import Link from "next/link";

export default function PendingPage() {
  return (
    <main className="min-h-screen flex flex-col">
      <header className="px-6 py-5 max-w-md mx-auto w-full">
        <Link href="/" className="wordmark text-2xl text-forest">
          Caddy
        </Link>
      </header>

      <section className="flex-1 flex flex-col justify-center px-6 max-w-md mx-auto w-full">
        <p className="eyebrow text-gold mb-3">Request received</p>
        <h1 className="wordmark text-[44px] leading-tight text-forest mb-6">
          You&apos;re on the list.
        </h1>

        <div className="w-12 h-[2px] bg-gold mb-6" />

        <p className="text-[16px] text-ink/85 leading-relaxed mb-3">
          Thanks for requesting access to the Caddy beta. We&apos;ll review your request and reach out personally with your sign-in details.
        </p>
        <p className="text-[14px] text-muted leading-relaxed mb-10 italic">
          Keep an eye on your email or phone — we usually get back to people within a day or two.
        </p>

        <div className="bg-paper border border-line rounded-2xl p-6 mb-10">
          <p className="eyebrow text-gold mb-3">What happens next</p>
          <ol className="space-y-3 text-[14px] text-ink">
            <li className="flex gap-3">
              <span className="text-gold font-semibold">1.</span>
              <span>We review your request manually.</span>
            </li>
            <li className="flex gap-3">
              <span className="text-gold font-semibold">2.</span>
              <span>We send you your sign-in PIN privately.</span>
            </li>
            <li className="flex gap-3">
              <span className="text-gold font-semibold">3.</span>
              <span>You sign in and set up your bag — and you&apos;re in.</span>
            </li>
          </ol>
        </div>

        <Link
          href="/"
          className="text-[13px] text-forest text-center underline-offset-4 hover:underline"
        >
          Back to home
        </Link>
      </section>

      <footer className="px-6 py-8 max-w-md mx-auto w-full">
        <p className="eyebrow text-muted text-center">Caddy · 2026</p>
      </footer>
    </main>
  );
}

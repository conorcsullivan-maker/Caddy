"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type User } from "@/lib/api";

const CLUBS: { key: string; label: string; range: [number, number]; placeholder: string }[] = [
  { key: "driver",         label: "Driver",         range: [100, 400], placeholder: "e.g. 250" },
  { key: "3-wood",         label: "3-wood",         range: [80, 350],  placeholder: "e.g. 230" },
  { key: "5-wood",         label: "5-wood",         range: [70, 300],  placeholder: "e.g. 215" },
  { key: "4-iron",         label: "4-iron",         range: [60, 270],  placeholder: "e.g. 200" },
  { key: "5-iron",         label: "5-iron",         range: [60, 250],  placeholder: "e.g. 185" },
  { key: "6-iron",         label: "6-iron",         range: [50, 230],  placeholder: "e.g. 175" },
  { key: "7-iron",         label: "7-iron",         range: [50, 210],  placeholder: "e.g. 160" },
  { key: "8-iron",         label: "8-iron",         range: [40, 190],  placeholder: "e.g. 150" },
  { key: "9-iron",         label: "9-iron",         range: [40, 170],  placeholder: "e.g. 135" },
  { key: "pitching_wedge", label: "Pitching wedge", range: [30, 150],  placeholder: "e.g. 120" },
  { key: "gap_wedge",      label: "Gap wedge",      range: [30, 140],  placeholder: "e.g. 105" },
  { key: "sand_wedge",     label: "Sand wedge",     range: [20, 130],  placeholder: "e.g. 90" },
  { key: "lob_wedge",      label: "Lob wedge",      range: [20, 120],  placeholder: "e.g. 70" },
];

export default function BagSetupPage() {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form state
  const [bag, setBag] = useState<Record<string, string>>({});
  const [driverMiss, setDriverMiss] = useState("");
  const [ironMiss, setIronMiss] = useState("");
  const [homeCourse, setHomeCourse] = useState("");

  useEffect(() => {
    api.me()
      .then(({ user }) => {
        setUser(user);
        // Pre-populate if user already has a bag (editing)
        if (user.bag) {
          const existing: Record<string, string> = {};
          for (const [k, v] of Object.entries(user.bag)) {
            if (v !== null && v !== undefined) existing[k] = String(v);
          }
          setBag(existing);
        }
        if (user.driver_miss) setDriverMiss(user.driver_miss);
        if (user.iron_miss) setIronMiss(user.iron_miss);
        if (user.home_course) setHomeCourse(user.home_course);
      })
      .catch(() => router.push("/login"))
      .finally(() => setLoading(false));
  }, []);

  function setClub(key: string, value: string) {
    const cleaned = value.replace(/\D/g, "").slice(0, 3);
    setBag((b) => ({ ...b, [key]: cleaned }));
  }

  function clubWarning(key: string): string | null {
    const val = bag[key];
    if (!val) return null;
    const num = parseInt(val, 10);
    const club = CLUBS.find((c) => c.key === key)!;
    const [lo, hi] = club.range;
    if (num < lo || num > hi) {
      return `Outside typical range (${lo}–${hi})`;
    }
    return null;
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSaving(true);
    try {
      const bagPayload: Record<string, number | null> = {};
      for (const c of CLUBS) {
        const v = bag[c.key];
        bagPayload[c.key] = v ? parseInt(v, 10) : null;
      }
      await api.setup({
        bag: bagPayload,
        driver_miss: driverMiss.trim() || undefined,
        iron_miss: ironMiss.trim() || undefined,
        home_course: homeCourse.trim() || undefined,
      });
      router.push("/profile");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setSaving(false);
    }
  }

  if (loading || !user) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <p className="text-muted text-sm eyebrow">Loading...</p>
      </main>
    );
  }

  const inBagCount = Object.values(bag).filter((v) => v && parseInt(v, 10) > 0).length;
  const isEditing = user.onboarded;

  return (
    <main className="min-h-screen flex flex-col bg-cream">
      <header className="px-6 py-5 border-b border-line bg-paper sticky top-0 z-10">
        <div className="max-w-md mx-auto w-full flex items-center justify-between">
          <Link href="/" className="wordmark text-2xl text-forest">
            Caddy
          </Link>
          <span className="eyebrow text-muted">{inBagCount} clubs</span>
        </div>
      </header>

      <section className="flex-1 px-6 py-8 max-w-md mx-auto w-full">
        <p className="eyebrow text-gold mb-3">{isEditing ? "Edit your bag" : "Welcome"}</p>
        <h1 className="wordmark text-[40px] leading-tight text-forest mb-4">
          {isEditing ? "Update your bag." : "Set up your bag."}
        </h1>
        <div className="w-12 h-[2px] bg-gold mb-5" />
        <p className="text-[15px] text-muted leading-relaxed mb-8">
          Enter your typical carry distance for each club. Leave blank if you don&apos;t carry it. Don&apos;t worry about being exact — Caddy refines these as you play and upload Trackman data.
        </p>

        <form onSubmit={handleSave} className="space-y-8">
          {/* Bag */}
          <div>
            <p className="eyebrow text-gold mb-3">Your bag</p>
            <div className="bg-paper border border-line rounded-2xl divide-y divide-line">
              {CLUBS.map((c) => {
                const warn = clubWarning(c.key);
                return (
                  <div key={c.key} className="p-3 flex items-center gap-3">
                    <label className="flex-1 text-[14px] text-ink">{c.label}</label>
                    <div className="flex items-center gap-2">
                      <input
                        type="text"
                        inputMode="numeric"
                        value={bag[c.key] || ""}
                        onChange={(e) => setClub(c.key, e.target.value)}
                        placeholder={c.placeholder}
                        className="w-20 bg-cream border border-line rounded-lg px-3 py-2 text-[15px] text-ink placeholder:text-muted/50 text-right focus:outline-none focus:border-forest transition"
                      />
                      <span className="text-[12px] text-muted w-8">yds</span>
                    </div>
                  </div>
                );
              })}
            </div>
            {/* Warnings */}
            {CLUBS.some((c) => clubWarning(c.key)) && (
              <div className="mt-3 text-[12px] text-muted space-y-1">
                {CLUBS.map((c) => {
                  const warn = clubWarning(c.key);
                  if (!warn) return null;
                  return (
                    <p key={c.key} className="flex gap-2">
                      <span className="text-gold">!</span>
                      <span>
                        <span className="font-medium text-ink">{c.label}:</span> {warn}
                      </span>
                    </p>
                  );
                })}
              </div>
            )}
          </div>

          {/* Tendencies */}
          <div>
            <p className="eyebrow text-gold mb-3">Your tendencies</p>
            <div className="space-y-4">
              <div>
                <label className="text-[12px] text-muted block mb-1.5">
                  Driver miss <span className="text-muted/60">(optional)</span>
                </label>
                <textarea
                  value={driverMiss}
                  onChange={(e) => setDriverMiss(e.target.value)}
                  rows={2}
                  maxLength={300}
                  placeholder="e.g. snaps right late on tight tee shots"
                  className="w-full bg-paper border border-line rounded-2xl px-4 py-3 text-[14px] text-ink placeholder:text-muted/50 focus:outline-none focus:border-forest transition resize-none"
                />
              </div>
              <div>
                <label className="text-[12px] text-muted block mb-1.5">
                  Iron miss <span className="text-muted/60">(optional)</span>
                </label>
                <textarea
                  value={ironMiss}
                  onChange={(e) => setIronMiss(e.target.value)}
                  rows={2}
                  maxLength={300}
                  placeholder="e.g. left, alignment issue at setup"
                  className="w-full bg-paper border border-line rounded-2xl px-4 py-3 text-[14px] text-ink placeholder:text-muted/50 focus:outline-none focus:border-forest transition resize-none"
                />
              </div>
            </div>
          </div>

          {/* Home course */}
          <div>
            <p className="eyebrow text-gold mb-3">Home course</p>
            <input
              type="text"
              value={homeCourse}
              onChange={(e) => setHomeCourse(e.target.value)}
              maxLength={120}
              placeholder="e.g. Granite Links Golf Club"
              className="w-full bg-paper border border-line rounded-2xl px-4 py-3 text-[15px] text-ink placeholder:text-muted/50 focus:outline-none focus:border-forest transition"
            />
            <p className="text-[11px] text-muted/80 mt-2 ml-1">Optional — Caddy can also detect courses on the fly.</p>
          </div>

          {error && (
            <p className="text-[13px] text-red-700 bg-red-50 border border-red-100 rounded-xl px-3 py-2">
              {error}
            </p>
          )}

          <div className="pt-2">
            <button
              type="submit"
              disabled={saving || inBagCount === 0}
              className="w-full bg-forest text-cream py-4 px-6 rounded-full font-medium text-[15px] tracking-wide disabled:opacity-50 disabled:cursor-not-allowed hover-lift"
            >
              {saving ? "Saving..." : isEditing ? "Save changes" : "Save and continue"}
            </button>
            {inBagCount === 0 && (
              <p className="text-[12px] text-muted text-center mt-3 italic">
                Add at least one club to continue.
              </p>
            )}
          </div>
        </form>
      </section>

      <footer className="px-6 py-8 max-w-md mx-auto w-full">
        <p className="eyebrow text-muted text-center">Caddy · 2026</p>
      </footer>
    </main>
  );
}

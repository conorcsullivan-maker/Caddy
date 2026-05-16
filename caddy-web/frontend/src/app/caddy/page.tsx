"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, type User, type RoundState, type WeatherSnapshot } from "@/lib/api";

type Message = { role: "user" | "assistant"; content: string };
type Location = { lat: number; lng: number } | null;

export default function CaddyPage() {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [muted, setMuted] = useState(false);
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [roundState, setRoundState] = useState<RoundState>({ hole_scores: [], current_hole: 1 });
  const [location, setLocation] = useState<Location>(null);
  const [weather, setWeather] = useState<WeatherSnapshot | null>(null);
  const [locationStatus, setLocationStatus] = useState<"idle" | "asking" | "granted" | "denied">("idle");

  const [photoFile, setPhotoFile] = useState<File | null>(null);
  const [photoPreview, setPhotoPreview] = useState<string | null>(null);

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const audioElementRef = useRef<HTMLAudioElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Initial load — auth check first, then history independently so a history
  // error doesn't kick you back to login.
  useEffect(() => {
    api.me()
      .then(({ user }) => {
        setUser(user);
        api.caddy.history()
          .then(({ history, round_state }) => {
            setMessages(history);
            if (round_state) setRoundState(round_state);
          })
          .catch(() => setMessages([]));
      })
      .catch(() => router.push("/login"))
      .finally(() => setLoading(false));
  }, []);

  // Request geolocation once on page load so Caddy has live weather context.
  // Permission is per-origin, so iOS/Chrome will only ask once across visits.
  useEffect(() => {
    if (typeof navigator === "undefined" || !navigator.geolocation) return;
    setLocationStatus("asking");
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setLocation({ lat: pos.coords.latitude, lng: pos.coords.longitude });
        setLocationStatus("granted");
      },
      () => setLocationStatus("denied"),
      { timeout: 10000, maximumAge: 5 * 60 * 1000 } // accept 5-min cached fix
    );
  }, []);

  // Populate the weather strip as soon as we have location, so it appears
  // without waiting for the first chat message.
  useEffect(() => {
    if (!location) return;
    api.caddy
      .weather(location.lat, location.lng)
      .then(({ weather: w }) => { if (w) setWeather(w); })
      .catch(() => { /* silent — strip just stays hidden */ });
  }, [location]);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, transcribing, sending]);

  // Auto-resize textarea as user types — grows up to ~6 lines, then scrolls
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`;
  }, [input]);

  async function speakText(text: string) {
    if (muted) return;
    try {
      const blob = await api.caddy.fetchSpeech(text);
      const url = URL.createObjectURL(blob);
      if (audioElementRef.current) {
        audioElementRef.current.pause();
        URL.revokeObjectURL(audioElementRef.current.src);
      }
      const audio = new Audio(url);
      audioElementRef.current = audio;
      audio.play().catch((err) => {
        // Most common cause: mobile Safari autoplay policy blocked playback
        // because we haven't had a recent user gesture. Log it so we can see
        // in DevTools instead of failing silently.
        console.warn("[caddy] audio.play blocked:", err?.name, err?.message);
      });
    } catch (err) {
      console.warn("[caddy] TTS fetch failed:", err);
      // User can still read the text
    }
  }

  async function handleSendText(e?: React.FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || sending) return;
    setError(null);
    setInput("");
    // Optimistic user bubble
    setMessages((m) => [...m, { role: "user", content: text }]);
    setSending(true);
    try {
      const { reply, round_state, weather: w } = await api.caddy.message(text, location);
      setMessages((m) => [...m, { role: "assistant", content: reply }]);
      if (round_state) setRoundState(round_state);
      if (w) setWeather(w);
      speakText(reply);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
      // Roll back the optimistic message
      setMessages((m) => m.slice(0, -1));
      setInput(text);
    } finally {
      setSending(false);
    }
  }

  function playTone(frequency: number) {
    try {
      const Ctor = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new Ctor();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = frequency;
      osc.type = "sine";
      gain.gain.setValueAtTime(0.18, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.09);
      osc.start();
      osc.stop(ctx.currentTime + 0.09);
    } catch {}
  }

  function vibrate(ms: number) {
    if (typeof navigator !== "undefined" && navigator.vibrate) {
      navigator.vibrate(ms);
    }
  }

  async function startRecording() {
    setError(null);
    // Instant feedback — flip to recording state BEFORE the browser permission check
    setRecording(true);
    vibrate(40);
    playTone(880); // bright "start" tone
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioChunksRef.current = [];
      const recorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
      mediaRecorderRef.current = recorder;
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunksRef.current.push(e.data);
      };
      recorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(audioChunksRef.current, { type: "audio/webm" });
        if (blob.size < 500) {
          setError("Didn't catch that — try again.");
          return;
        }
        setTranscribing(true);
        try {
          const { transcript, reply, round_state, weather: w } = await api.caddy.voice(blob, location);
          // Skip the user bubble when Whisper didn't catch anything — Caddy
          // will respond with a "say it again?" message on its own.
          setMessages((m) => [
            ...m,
            ...(transcript ? [{ role: "user" as const, content: transcript }] : []),
            { role: "assistant", content: reply },
          ]);
          if (round_state) setRoundState(round_state);
          if (w) setWeather(w);
          speakText(reply);
        } catch (err) {
          setError(err instanceof Error ? err.message : "Voice failed");
        } finally {
          setTranscribing(false);
        }
      };
      recorder.start();
    } catch (err) {
      setRecording(false);
      setError("Mic access denied. Allow microphone in browser settings.");
    }
  }

  function stopRecording() {
    vibrate(40);
    playTone(580); // softer "stop" tone
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== "inactive") {
      mediaRecorderRef.current.stop();
    }
    setRecording(false);
  }

  function toggleRecording() {
    if (recording) stopRecording();
    else startRecording();
  }

  function clearPhoto() {
    if (photoPreview) URL.revokeObjectURL(photoPreview);
    setPhotoFile(null);
    setPhotoPreview(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function handlePhotoSelect(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    if (photoPreview) URL.revokeObjectURL(photoPreview);
    setPhotoFile(file);
    setPhotoPreview(URL.createObjectURL(file));
  }

  async function handleSendPhoto() {
    if (!photoFile || sending) return;
    const file = photoFile;
    const text = input.trim();
    const preview = photoPreview;
    clearPhoto();
    setInput("");
    setMessages((m) => [...m, { role: "user", content: text || "📷 Scorecard" }]);
    setSending(true);
    setError(null);
    try {
      const { reply, round_state, weather: w } = await api.caddy.photo(file, text || undefined, location);
      setMessages((m) => [...m, { role: "assistant", content: reply }]);
      if (round_state) setRoundState(round_state);
      if (w) setWeather(w);
      speakText(reply);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Photo upload failed");
      setMessages((m) => m.slice(0, -1));
      // Restore photo so user can retry
      setPhotoFile(file);
      setPhotoPreview(preview);
    } finally {
      setSending(false);
    }
  }

  async function handleReset() {
    if (!confirm("Start a new conversation? This one gets archived to your past conversations — nothing is lost.")) return;
    await api.caddy.reset();
    setMessages([]);
    setRoundState({ hole_scores: [], current_hole: 1 });
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

  return (
    <main className="h-[100dvh] flex flex-col bg-cream">
      {/* Header */}
      <header className="px-5 py-4 border-b border-line bg-paper flex-shrink-0">
        <div className="max-w-2xl mx-auto w-full flex items-center justify-between">
          <Link href="/" className="wordmark text-2xl text-forest">
            Caddy
          </Link>
          <div className="flex items-center gap-4">
            <button
              onClick={handleReset}
              className="text-[12px] eyebrow text-muted hover:text-forest transition"
            >
              Reset
            </button>
            <Link
              href="/profile"
              className="text-[12px] eyebrow text-muted hover:text-forest transition"
            >
              Profile
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

      {/* Weather strip — only shows when we have live data */}
      {weather?.current && (
        <WeatherStrip weather={weather} />
      )}
      {locationStatus === "denied" && (
        <div className="bg-cream/60 border-b border-line px-5 py-2 text-[11px] text-muted text-center flex-shrink-0">
          Location off — Caddy won&apos;t have live wind/weather. Enable in browser settings to fix.
        </div>
      )}

      {/* Live round status bar — shows as soon as there's any round activity:
          a loaded course, any logged score, or progress past hole 1. */}
      {hasRoundActivity(roundState) && (
        <div className="bg-forest text-cream px-5 py-1.5 border-b border-forest-deep flex-shrink-0">
          <div className="max-w-2xl mx-auto w-full text-[11px] flex items-center gap-3">
            <span className="eyebrow text-gold flex-shrink-0">Round</span>
            <span className="text-cream/95 truncate">
              {roundState.course
                ? shortCourseName(roundState.course.club_name) +
                  (roundState.tee?.tee_name ? ` · ${roundState.tee.tee_name}` : "")
                : "No course loaded"}
              {` · ${formatHoleStatus(roundState)}`}
            </span>
          </div>
        </div>
      )}

      {/* Message thread */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-6">
        <div className="max-w-2xl mx-auto w-full space-y-4">
          {messages.length === 0 && (
            <EmptyState firstName={user.full_name.split(" ")[0]} />
          )}
          {messages.map((m, i) => (
            <Bubble key={i} role={m.role} content={m.content} />
          ))}
          {transcribing && (
            <div className="flex justify-end">
              <div className="bg-forest/10 text-forest/60 italic text-[14px] rounded-2xl px-4 py-2">
                Listening...
              </div>
            </div>
          )}
          {sending && (
            <div className="flex justify-start">
              <div className="bg-forest/10 text-forest/60 italic text-[14px] rounded-2xl px-4 py-2">
                Thinking...
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Input bar */}
      <div className="border-t border-line bg-paper flex-shrink-0">
        <div className="max-w-2xl mx-auto w-full px-4 py-3">
          {error && (
            <p className="text-[12px] text-red-700 bg-red-50 border border-red-100 rounded-lg px-3 py-2 mb-2">
              {error}
            </p>
          )}
          {/* Photo preview */}
          {photoPreview && (
            <div className="flex items-center gap-2 mb-2">
              <div className="relative">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={photoPreview} alt="Scorecard" className="h-16 w-auto rounded-lg object-cover border border-line" />
                <button
                  type="button"
                  onClick={clearPhoto}
                  className="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-ink text-cream text-[11px] flex items-center justify-center"
                  aria-label="Remove photo"
                >
                  ✕
                </button>
              </div>
              <span className="text-[12px] text-muted">Scorecard photo — add a note or tap send</span>
            </div>
          )}
          <form onSubmit={photoFile ? (e) => { e.preventDefault(); handleSendPhoto(); } : handleSendText} className="flex items-end gap-2">
            {/* Mute toggle */}
            <button
              type="button"
              onClick={() => setMuted(!muted)}
              className={`flex-shrink-0 w-9 h-9 mb-1.5 rounded-full flex items-center justify-center transition ${
                muted
                  ? "bg-red-100 text-red-700 ring-2 ring-red-300 hover:bg-red-200"
                  : "text-muted hover:text-forest hover:bg-cream/60"
              }`}
              title={muted ? "Caddy is muted — tap to unmute" : "Mute Caddy voice"}
              aria-label={muted ? "Unmute" : "Mute"}
            >
              {muted ? <SpeakerMutedIcon /> : <SpeakerIcon />}
            </button>

            {/* Hidden file input */}
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={handlePhotoSelect}
            />
            {/* Camera button */}
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={sending || transcribing}
              className="flex-shrink-0 w-9 h-9 mb-1.5 rounded-full text-muted hover:text-forest hover:bg-cream/60 flex items-center justify-center transition disabled:opacity-40"
              title="Upload scorecard photo"
              aria-label="Upload scorecard"
            >
              <CameraIcon />
            </button>

            <div className={`flex-1 border rounded-3xl px-4 py-3 transition ${
              recording
                ? "bg-red-50 border-red-300"
                : "bg-cream border-line focus-within:border-forest"
            }`}>
              {recording ? (
                <div className="flex items-center gap-2 py-1">
                  <span className="w-2 h-2 rounded-full bg-red-600 animate-pulse" />
                  <span className="text-[15px] text-red-700 italic">Listening — tap mic to send</span>
                </div>
              ) : (
                <textarea
                  ref={textareaRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      photoFile ? handleSendPhoto() : handleSendText();
                    }
                  }}
                  placeholder={photoFile ? "Add a note (optional)..." : "Talk to Caddy..."}
                  disabled={transcribing}
                  rows={1}
                  className="w-full bg-transparent text-[15px] text-ink placeholder:text-muted/60 focus:outline-none resize-none overflow-y-auto block"
                  style={{ minHeight: "22px", maxHeight: "160px" }}
                />
              )}
            </div>
            {(input.trim() || photoFile) ? (
              <button
                type="submit"
                disabled={sending}
                className="flex-shrink-0 w-12 h-12 rounded-full bg-forest text-cream flex items-center justify-center hover-lift disabled:opacity-50"
                aria-label="Send"
              >
                <SendIcon />
              </button>
            ) : (
              <div className="relative flex-shrink-0 w-12 h-12">
                {recording && (
                  <>
                    <span className="absolute inset-0 rounded-full bg-red-500/30 animate-ping" />
                    <span className="absolute -inset-1 rounded-full ring-2 ring-red-500/40 animate-pulse" />
                  </>
                )}
                <button
                  type="button"
                  onClick={toggleRecording}
                  disabled={transcribing}
                  className={`relative w-12 h-12 rounded-full flex items-center justify-center transition-all duration-150 ${
                    recording
                      ? "bg-red-600 text-white scale-110 shadow-lg shadow-red-600/40"
                      : "bg-forest text-cream hover-lift"
                  } disabled:opacity-50`}
                  aria-label={recording ? "Stop recording" : "Tap to speak"}
                >
                  <MicIcon />
                </button>
              </div>
            )}
          </form>
        </div>
      </div>
    </main>
  );
}

function WeatherStrip({ weather }: { weather: WeatherSnapshot }) {
  const cur = weather.current;
  if (!cur) return null;
  const hasCriticalAlert = (weather.alerts || []).some((a) => {
    const e = (a.event || "").toLowerCase();
    const s = (a.severity || "").toLowerCase();
    return e.includes("tornado") || e.includes("thunderstorm") ||
           e.includes("lightning") || s === "severe" || s === "extreme";
  });

  if (hasCriticalAlert) {
    const alert = weather.alerts?.[0];
    return (
      <div className="bg-red-700 text-white px-5 py-2.5 border-b border-red-900 flex-shrink-0">
        <div className="max-w-2xl mx-auto w-full text-[12px] flex items-center gap-2">
          <span className="font-bold">⚠️ {alert?.event || "Severe weather"}</span>
          <span className="text-white/80 truncate">{alert?.headline}</span>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-paper border-b border-line px-5 py-1.5 flex-shrink-0">
      <div className="max-w-2xl mx-auto w-full text-[11px] text-muted flex items-center gap-3">
        <span className="eyebrow text-gold">Weather</span>
        <span className="text-ink truncate">
          {cur.temperature}°{cur.temperature_unit || "F"}
          {cur.short_forecast ? ` · ${cur.short_forecast}` : ""}
          {cur.wind_speed ? ` · wind ${cur.wind_speed} ${cur.wind_direction || ""}` : ""}
          {cur.precip_chance ? ` · ${cur.precip_chance}% rain` : ""}
        </span>
      </div>
    </div>
  );
}

// Drop common suffixes so long names like "William J. Devine Golf Course" fit
function shortCourseName(name?: string): string {
  if (!name) return "";
  return name
    .replace(/\s+(Golf Course|Golf Club|Country Club|Golf Links|Links)$/i, "")
    .trim();
}

// A round is "active" the moment any score has been logged or the player has
// moved past hole 1, even if no course is loaded. The banner uses this so
// the player can always see what hole they're on and what they've shot.
function hasRoundActivity(state: RoundState): boolean {
  if (state.course) return true;
  if (state.hole_scores?.some((s) => s !== null && s !== undefined)) return true;
  if ((state.current_hole || 1) > 1) return true;
  return false;
}

function formatHoleStatus(state: RoundState): string {
  const played = (state.hole_scores || [])
    .map((s, i) => ({ score: s, par: state.tee?.holes?.[i]?.par }))
    .filter((h): h is { score: number; par: number | undefined } => h.score !== null);
  const cur = state.current_hole || played.length + 1;
  if (played.length === 0) return `Hole ${cur}`;
  const total = played.reduce((a, h) => a + (h.score ?? 0), 0);
  const parTotal = played.reduce((a, h) => a + (h.par ?? 0), 0);
  const vs = parTotal ? total - parTotal : null;
  const vsLabel = vs === null ? "" : vs === 0 ? "E" : vs > 0 ? ` (+${vs})` : ` (${vs})`;
  return `Hole ${cur} · ${total}${vsLabel}`;
}

function EmptyState({ firstName }: { firstName: string }) {
  return (
    <div className="text-center py-16 px-4">
      <p className="eyebrow text-gold mb-3">Welcome, {firstName}</p>
      <h2 className="wordmark text-[36px] text-forest leading-tight mb-4">
        What&apos;s the situation?
      </h2>
      <div className="w-12 h-[2px] bg-gold mx-auto mb-5" />
      <p className="text-[14px] text-muted max-w-sm mx-auto leading-relaxed">
        Tap the mic and describe your shot, or type below. Caddy knows your bag, your tendencies, and the courses you&apos;ve played.
      </p>
    </div>
  );
}

function Bubble({ role, content }: { role: string; content: string }) {
  const isUser = role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[80%] rounded-3xl px-5 py-3 text-[15px] leading-relaxed whitespace-pre-wrap ${
          isUser
            ? "bg-forest text-cream rounded-br-md"
            : "bg-paper border border-line text-ink rounded-bl-md"
        }`}
      >
        {content}
      </div>
    </div>
  );
}

function SendIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <line x1="5" y1="12" x2="19" y2="12" />
      <polyline points="12 5 19 12 12 19" />
    </svg>
  );
}

function MicIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="2" width="6" height="12" rx="3" />
      <path d="M5 10v2a7 7 0 0 0 14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="22" />
    </svg>
  );
}

function SpeakerIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
      <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
      <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
    </svg>
  );
}

function CameraIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" />
      <circle cx="12" cy="13" r="4" />
    </svg>
  );
}

function SpeakerMutedIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
      <line x1="22" y1="9" x2="16" y2="15" />
      <line x1="16" y1="9" x2="22" y2="15" />
    </svg>
  );
}

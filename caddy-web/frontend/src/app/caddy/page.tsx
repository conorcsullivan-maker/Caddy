"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, type User } from "@/lib/api";

type Message = { role: "user" | "assistant"; content: string };

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

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const audioElementRef = useRef<HTMLAudioElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Initial load — auth check first, then history independently so a history
  // error doesn't kick you back to login.
  useEffect(() => {
    api.me()
      .then(({ user }) => {
        setUser(user);
        // History is non-critical — if it fails, just start with empty messages
        api.caddy.history()
          .then(({ history }) => setMessages(history))
          .catch(() => setMessages([]));
      })
      .catch(() => router.push("/login"))
      .finally(() => setLoading(false));
  }, []);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, transcribing, sending]);

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
      audio.play().catch(() => {});
    } catch {
      // Silent fail — user can still read the text
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
      const { reply } = await api.caddy.message(text);
      setMessages((m) => [...m, { role: "assistant", content: reply }]);
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
          const { transcript, reply } = await api.caddy.voice(blob);
          setMessages((m) => [
            ...m,
            { role: "user", content: transcript },
            { role: "assistant", content: reply },
          ]);
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

  async function handleReset() {
    if (!confirm("Clear this conversation? Caddy won't remember anything from it.")) return;
    await api.caddy.reset();
    setMessages([]);
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
              onClick={() => setMuted(!muted)}
              className={`p-1 rounded transition ${muted ? "text-muted/70 hover:text-forest" : "text-muted hover:text-forest"}`}
              title={muted ? "Unmute caddy voice" : "Mute caddy voice"}
              aria-label={muted ? "Unmute" : "Mute"}
            >
              {muted ? <SpeakerMutedIcon /> : <SpeakerIcon />}
            </button>
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
          <form onSubmit={handleSendText} className="flex items-end gap-2">
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
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      handleSendText();
                    }
                  }}
                  placeholder="Talk to Caddy..."
                  disabled={transcribing}
                  rows={1}
                  className="w-full bg-transparent text-[15px] text-ink placeholder:text-muted/60 focus:outline-none resize-none max-h-[120px]"
                  style={{ minHeight: "22px" }}
                />
              )}
            </div>
            {input.trim() ? (
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

function SpeakerMutedIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
      <line x1="22" y1="9" x2="16" y2="15" />
      <line x1="16" y1="9" x2="22" y2="15" />
    </svg>
  );
}

// Thin API client for the Caddy backend.
// All requests use relative paths — `/api/*` on the same origin.
// • In local dev, next.config.ts rewrites `/api/*` to http://localhost:8000.
// • In production, vercel.json rewrites `/api/*` to the deployed Render backend.
// This keeps cookies first-party (critical for mobile Safari) and avoids CORS.
const API_BASE = "";

export type Round = {
  date: string;
  course: string;
  score: number;
  holes?: number | null;
  hole_scores?: (number | null)[] | null;
  course_rating?: number | null;
  slope_rating?: number | null;
  differential?: number | null;
};

export type User = {
  id: number;
  username: string;
  full_name: string;
  email?: string;
  phone?: string;
  status: "pending" | "approved" | "rejected";
  is_admin: boolean;
  onboarded: boolean;
  bag?: Record<string, number | null>;
  driver_miss?: string | null;
  iron_miss?: string | null;
  home_course?: string | null;
  handicap_index?: number | null;
  tendencies_summary?: string | null;
  rounds?: Round[];
};

export type PendingUser = {
  id: number;
  username: string;
  full_name: string;
  email: string;
  phone?: string;
  reason?: string;
  referral?: string;
  created_at: string;
};

export type BagSetup = {
  bag: Record<string, number | null>;
  driver_miss?: string;
  iron_miss?: string;
  home_course?: string;
};

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });
  if (!res.ok) {
    let detail = "";
    try {
      const data = await res.json();
      // FastAPI validation errors return detail as an array of {loc, msg, type}
      if (Array.isArray(data.detail)) {
        detail = data.detail
          .map((e: { loc?: string[]; msg?: string }) => {
            const field = e.loc?.[e.loc.length - 1] || "input";
            return `${field}: ${e.msg || "invalid"}`;
          })
          .join(", ");
      } else if (typeof data.detail === "string") {
        detail = data.detail;
      } else if (data.message) {
        detail = data.message;
      }
    } catch {}
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return res.json();
}

export const api = {
  signup: (payload: {
    full_name: string;
    username: string;
    email: string;
    phone?: string;
    reason?: string;
    referral?: string;
  }) =>
    request<{ status: string; message: string }>("/api/signup", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  login: (username: string, pin: string) =>
    request<{ user: User }>("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, pin }),
    }),

  logout: () => request<{ status: string }>("/api/logout", { method: "POST" }),

  me: () => request<{ user: User }>("/api/me"),

  setup: (payload: BagSetup) =>
    request<{ user: User }>("/api/me/setup", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  caddy: {
    history: () =>
      request<{ history: { role: "user" | "assistant"; content: string }[] }>(
        "/api/caddy/history"
      ),
    reset: () =>
      request<{ status: string }>("/api/caddy/reset", { method: "POST" }),
    message: (message: string) =>
      request<{ reply: string; user_message: string }>("/api/caddy/message", {
        method: "POST",
        body: JSON.stringify({ message }),
      }),
    voice: async (audio: Blob) => {
      const form = new FormData();
      form.append("audio", audio, "speech.webm");
      const res = await fetch(`${API_BASE}/api/caddy/voice`, {
        method: "POST",
        credentials: "include",
        body: form,
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Request failed (${res.status})`);
      }
      return res.json() as Promise<{ transcript: string; reply: string }>;
    },
    speakUrl: (text: string) => {
      // Returns a URL that will produce TTS audio when fetched (with auth cookie)
      const params = new URLSearchParams({ message: text });
      return `${API_BASE}/api/caddy/speak?${params.toString()}`;
    },
    fetchSpeech: async (text: string): Promise<Blob> => {
      const res = await fetch(`${API_BASE}/api/caddy/speak`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      if (!res.ok) throw new Error(`TTS failed (${res.status})`);
      return res.blob();
    },
  },

  admin: {
    pending: () => request<{ pending: PendingUser[] }>("/api/admin/pending"),
    users: () => request<{ users: User[] }>("/api/admin/users"),
    approve: (id: number) =>
      request<{ username: string; pin: string }>(
        `/api/admin/approve/${id}`,
        { method: "POST" }
      ),
    reject: (id: number) =>
      request<{ status: string }>(`/api/admin/reject/${id}`, {
        method: "POST",
      }),
    resetPin: (id: number) =>
      request<{ username: string; pin: string }>(
        `/api/admin/reset_pin/${id}`,
        { method: "POST" }
      ),
    deactivate: (id: number) =>
      request<{ status: string; username: string }>(
        `/api/admin/deactivate/${id}`,
        { method: "POST" }
      ),
    reactivate: (id: number) =>
      request<{ status: string; username: string }>(
        `/api/admin/reactivate/${id}`,
        { method: "POST" }
      ),
    delete: (id: number) =>
      request<{ status: string; username: string }>(
        `/api/admin/delete/${id}`,
        { method: "DELETE" }
      ),
  },
};

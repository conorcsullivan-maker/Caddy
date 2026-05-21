// Thin API client for the Caddy backend.
// All requests use relative paths — `/api/*` on the same origin.
// • In local dev, next.config.ts rewrites `/api/*` to http://localhost:8000.
// • In production, vercel.json rewrites `/api/*` to the deployed Render backend.
// This keeps cookies first-party (critical for mobile Safari) and avoids CORS.
const API_BASE = "";

export type RoundState = {
  course?: { club_name?: string } | null;
  tee?: { tee_name?: string; holes?: { par: number; yardage: number }[] } | null;
  hole_scores?: (number | null)[];
  current_hole?: number;
};

export type ChatEvent =
  | { type: "course_loaded"; course_name: string; tee_name: string }
  | { type: "course_not_found"; query: string }
  | { type: "course_unloaded" }
  | { type: "tee_changed"; tee_name: string }
  | { type: "score_logged"; hole: number; score: number; par?: number | null }
  | { type: "drive_inferred"; hole: number; hole_yardage: number; remaining: number; inferred_drive: number }
  | { type: "round_complete"; course_name: string; total_score?: number | null; differential?: number | null; handicap?: number | null }
  | { type: "weather_alert"; alerts: string[] }
  | { type: "transcript_unclear" };

export type WeatherSnapshot = {
  current?: {
    short_forecast?: string;
    temperature?: number;
    temperature_unit?: string;
    wind_speed?: string;
    wind_direction?: string;
    precip_chance?: number | null;
    humidity?: number | null;
  } | null;
  alerts?: { event?: string; headline?: string; severity?: string; urgency?: string }[];
};

export type ArchivedConversation = {
  id: number;
  kind: "casual" | "round";
  course_name?: string | null;
  total_score?: number | null;
  started_at: string;
  ended_at: string;
  round_metadata?: {
    hole_scores?: (number | null)[];
    course_rating?: number | null;
    slope_rating?: number | null;
    differential?: number | null;
    handicap_after?: number | null;
  } | null;
};

export type ArchivedConversationDetail = ArchivedConversation & {
  messages: { role: "user" | "assistant"; content: string }[];
};

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

export type ShotBucket = {
  count: number;
  total_carry: number;
  sum_sq: number;
  best: number;
  worst: number;
  left: number;
  right: number;
  center: number;
};

export type ClubShotStats = {
  trackman?: ShotBucket;
  course?: ShotBucket;
};

export type UserEngagement = {
  clubs_with_distance: number;
  rounds_count: number;
  trackman_sessions: number;
  // True when trackman_sessions was inferred from a tendencies_summary
  // containing Trackman-specific phrasing rather than from a tracked
  // session ID. Pre-today uploads fall into this category — we know they
  // happened but don't know the exact count.
  trackman_backfilled: boolean;
  has_tendencies: boolean;
  last_activity: string | null;
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
  can_export_conversations: boolean;
  bag?: Record<string, number | null>;
  driver_miss?: string | null;
  iron_miss?: string | null;
  home_course?: string | null;
  handicap_index?: number | null;
  tendencies_summary?: string | null;
  shot_stats?: Record<string, ClubShotStats>;
  rounds?: Round[];
  // Only populated on the admin users list endpoint
  engagement?: UserEngagement;
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

  uploadTrackman: async (input: { url?: string; csvFile?: File }) => {
    const form = new FormData();
    if (input.url) form.append("url", input.url);
    if (input.csvFile) form.append("csv_file", input.csvFile);
    const res = await fetch(`${API_BASE}/api/me/trackman`, {
      method: "POST",
      credentials: "include",
      body: form,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `Trackman upload failed (${res.status})`);
    }
    return res.json() as Promise<{
      user: User;
      shot_count: number;
      tendencies_summary: string;
      duplicate?: boolean;
    }>;
  },

  deleteRound: (index: number) =>
    request<{ status: string; rounds_remaining: number; handicap_index: number | null }>(
      `/api/me/rounds/${index}`,
      { method: "DELETE" }
    ),

  caddy: {
    history: () =>
      request<{
        history: { role: "user" | "assistant"; content: string }[];
        round_state: RoundState;
      }>("/api/caddy/history"),
    weather: (lat: number, lng: number) =>
      request<{ weather: WeatherSnapshot | null }>(
        `/api/caddy/weather?lat=${lat}&lng=${lng}`
      ),
    editScore: (hole: number, score: number | null) =>
      request<{ round_state: RoundState }>("/api/caddy/edit-score", {
        method: "POST",
        body: JSON.stringify({ hole, score }),
      }),
    reset: () =>
      request<{ status: string; archived_conversation_id?: number | null }>(
        "/api/caddy/reset",
        { method: "POST" }
      ),
    message: (message: string, location?: { lat: number; lng: number } | null) =>
      request<{
        reply: string;
        user_message: string;
        round_state: RoundState;
        events: ChatEvent[];
        weather?: WeatherSnapshot | null;
      }>("/api/caddy/message", {
        method: "POST",
        body: JSON.stringify({
          message,
          ...(location ? { lat: location.lat, lng: location.lng } : {}),
        }),
      }),
    voice: async (audio: Blob, location?: { lat: number; lng: number } | null) => {
      const form = new FormData();
      form.append("audio", audio, "speech.webm");
      const url = location
        ? `${API_BASE}/api/caddy/voice?lat=${location.lat}&lng=${location.lng}`
        : `${API_BASE}/api/caddy/voice`;
      const res = await fetch(url, {
        method: "POST",
        credentials: "include",
        body: form,
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Request failed (${res.status})`);
      }
      return res.json() as Promise<{
        transcript: string;
        reply: string;
        round_state: RoundState;
        events: ChatEvent[];
        weather?: WeatherSnapshot | null;
      }>;
    },
    photo: async (image: File, message?: string, location?: { lat: number; lng: number } | null) => {
      const form = new FormData();
      form.append("image", image);
      if (message) form.append("message", message);
      const params = new URLSearchParams();
      if (location) {
        params.set("lat", String(location.lat));
        params.set("lng", String(location.lng));
      }
      const url = params.size > 0
        ? `${API_BASE}/api/caddy/photo?${params.toString()}`
        : `${API_BASE}/api/caddy/photo`;
      const res = await fetch(url, { method: "POST", credentials: "include", body: form });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Request failed (${res.status})`);
      }
      return res.json() as Promise<{
        reply: string;
        user_message: string;
        round_state: RoundState;
        events: ChatEvent[];
        weather?: WeatherSnapshot | null;
      }>;
    },
    conversations: () =>
      request<{ conversations: ArchivedConversation[] }>("/api/caddy/conversations"),
    conversation: (id: number) =>
      request<ArchivedConversationDetail>(`/api/caddy/conversations/${id}`),
    // URLs for the .docx download endpoints. Cookies travel with the request
    // because /api/* is proxied through the frontend origin, so a plain
    // <a href={url} download> tag is enough — no fetch/blob plumbing needed.
    downloadConversationUrl: (id: number) =>
      `${API_BASE}/api/caddy/conversations/${id}/download`,
    downloadActiveConversationUrl: () =>
      `${API_BASE}/api/caddy/conversations/active/download`,
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

// Typed API client for the Caddy backend — mobile edition.
//
// Unlike the web client (which proxies /api/* through the Vercel origin so
// cookies stay first-party), the native app talks straight to the Render API
// and authenticates with a bearer token stored in the keychain. Native apps
// have no CORS and no third-party-cookie problem, so no proxy is needed.
//
// Keep the types in sync with caddy-web/frontend/src/lib/api.ts — that file
// is the source of truth for the backend contract.
import { getToken } from "./auth";

// For local dev against a machine on your network, change this to
// e.g. "http://192.168.1.42:8000" (and run the backend with `python3 main.py`).
export const API_BASE = "https://caddy-api.onrender.com";

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
  | { type: "transcript_unclear" }
  | {
      type: "relative_wind";
      description: string;
      headwind_mph: number;
      crosswind_mph: number;
      speed_mph: number;
      hole_bearing_deg: number;
      wind_from_compass: string;
    }
  | { type: "gps_yardage"; hole: number; yards_to_green: number }
  | { type: "shot_logged"; club: string; distance: number; direction?: "left" | "right" | "center" | null; source?: "gps" };

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

export type User = {
  id: number;
  username: string;
  full_name: string;
  is_admin: boolean;
  onboarded: boolean;
  handicap_index?: number | null;
};

export type ChatResponse = {
  reply: string;
  user_message: string;
  round_state: RoundState;
  events: ChatEvent[];
  weather?: WeatherSnapshot | null;
};

export type Location = { lat: number; lng: number } | null;

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = await getToken();
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = "";
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : "";
    } catch {}
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return res.json();
}

export const api = {
  login: (username: string, pin: string) =>
    request<{ user: User; token: string }>("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, pin }),
    }),

  me: () => request<{ user: User }>("/api/me"),

  caddy: {
    history: () =>
      request<{
        history: { role: "user" | "assistant"; content: string }[];
        round_state: RoundState;
      }>("/api/caddy/history"),

    weather: (lat: number, lng: number) =>
      request<{ weather: WeatherSnapshot | null }>(`/api/caddy/weather?lat=${lat}&lng=${lng}`),

    reset: () =>
      request<{ status: string }>("/api/caddy/reset", { method: "POST" }),

    message: (message: string, location: Location) =>
      request<ChatResponse>("/api/caddy/message", {
        method: "POST",
        body: JSON.stringify({
          message,
          ...(location ? { lat: location.lat, lng: location.lng } : {}),
        }),
      }),

    photo: async (uri: string, message: string | undefined, location: Location): Promise<ChatResponse> => {
      const token = await getToken();
      const form = new FormData();
      // React Native FormData file part: {uri, name, type}
      form.append("image", { uri, name: "photo.jpg", type: "image/jpeg" } as unknown as Blob);
      if (message) form.append("message", message);
      const params = location ? `?lat=${location.lat}&lng=${location.lng}` : "";
      const res = await fetch(`${API_BASE}/api/caddy/photo${params}`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        body: form,
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Photo upload failed (${res.status})`);
      }
      return res.json();
    },
  },
};

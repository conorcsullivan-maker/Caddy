# Caddy — AI Golf Caddy

Co-founded by Conor Sullivan (`sullydakid`) and Drew Smiley (`smiley`). Live beta at [caddy-sepia.vercel.app](https://caddy-sepia.vercel.app).

This CLAUDE.md is loaded automatically when working anywhere under `~/Desktop/Caddy/`. Read it before making changes.

---

## Repo map

```
Caddy/
├── caddy-web/
│   ├── frontend/                       Next.js 16 + Tailwind v4 + TS → deployed to Vercel
│   │   ├── AGENTS.md                   READ THIS before writing Next.js code (breaking changes vs training data)
│   │   ├── vercel.json                 rewrites /api/* → Render backend (KEEP — first-party cookies for mobile Safari)
│   │   ├── next.config.ts              local-dev equivalent of the vercel rewrite
│   │   └── src/
│   │       ├── app/                    Next.js pages
│   │       │   ├── caddy/page.tsx      the chat UI (voice + text + photo)
│   │       │   ├── profile/page.tsx    bag, shot stats, rounds, past conversations
│   │       │   ├── setup/bag/          first-time onboarding
│   │       │   ├── admin/              user management, beta CSV export
│   │       │   ├── conversations/[id]  archived-chat viewer
│   │       │   └── onboarding/         signup form
│   │       └── lib/api.ts              typed API client (single source of truth for backend contract)
│   │
│   └── backend/                        FastAPI + SQLite → deployed to Render Starter with /data persistent disk
│       ├── main.py                     ★ FastAPI routes, DB schema, auth, admin, chat pipeline (process_user_message)
│       ├── caddy_engine.py             Claude prompts (BASE_PROMPT), Whisper STT, TTS, caddy_reply
│       ├── caddy_round.py              course/tee/score detection, handicap, course overrides
│       ├── caddy_weather.py            NWS API integration + format_weather_context
│       ├── caddy_trackman.py           Trackman URL/CSV ingestion, SHOT_TIER_* constants, tendencies summary
│       ├── caddy_export.py             .docx conversation export (owner-only)
│       ├── caddy_geo.py                OSM Overpass geometry + relative-wind math (auto-wind feature)
│       ├── course_overrides/*.json     per-course nicknames, yardage fixes, hazard notes
│       ├── requirements.txt
│       ├── venv/                       local Python venv (Python 3.9)
│       └── caddy.db                    local SQLite (prod DB lives on Render's /data)
│
├── caddy_voice.py                      older Mac wake-word voice client (still works, JSON profiles)
├── profiles/                           JSON profiles for the Mac voice client
├── make_idea_binder.js                 idea binder generator
└── .env                                gitignored — API keys for local dev
```

---

## Running locally

**Frontend (port 3000):**
```
cd ~/Desktop/Caddy/caddy-web/frontend && npm run dev
```

**Backend (port 8000):**
```
cd ~/Desktop/Caddy/caddy-web/backend && source venv/bin/activate && python3 main.py
```

Frontend proxies `/api/*` to `localhost:8000` in dev. Local SQLite at `caddy-web/backend/caddy.db`.

**Env** (`~/Desktop/Caddy/.env`): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOLF_COURSE_API_KEY`. Also `BOOTSTRAP_ADMIN_USERNAME=sullydakid` + `BOOTSTRAP_ADMIN_PIN=6252` to seed the first admin on a fresh DB.

---

## Deployment

- **Backend** on Render → auto-deploys on push to `main`. Takes ~2-3 min; expect 502s during the swap.
- **Frontend** on Vercel → auto-deploys on push, usually <1 min.
- `.env` values are set as env vars in Render's dashboard. Production also sets `COOKIE_SECURE=true`, `DATA_DIR=/data`, `FRONTEND_ORIGIN=https://caddy-sepia.vercel.app`.

---

## Non-negotiable conventions

1. **Cookies / mobile Safari**: `/api/*` MUST be proxied through the frontend origin (`vercel.json` + `next.config.ts`). Cookies are first-party. Do NOT switch to CORS + third-party cookies — mobile Safari blocks them.
2. **`load_dotenv(override=True)`** in `main.py`: required because Conor's shell has an empty `ANTHROPIC_API_KEY=` set by Claude Desktop.
3. **Next.js 16 breaking changes**: read `caddy-web/frontend/AGENTS.md` and `node_modules/next/dist/docs/` before writing Next.js code. Don't assume training-data patterns still work.
4. **Only recommend clubs in the player's bag** (hard rule in `caddy_engine.py:BASE_PROMPT`). If the yardage calls for a club they don't have, say so.
5. **Use the exact bag label** for wedges — never translate "Sand wedge" ↔ "56 wedge" or vice versa. The bag entry is the source of truth for what the player expects to hear.
6. **Confidence tiers** are computed in code (`caddy_trackman.shot_count_tier`), never in Claude's head. Prompt receives a pre-computed label like "HIGH CONFIDENCE" and uses it.
7. **Full conversation persistence** — `save_conversation` no longer truncates. `CLAUDE_CONTEXT_MESSAGES=60` in `caddy_engine.py` limits only what's sent to Claude per turn, not what's stored.
8. **Export allowlist** is hardcoded `{"sullydakid", "smiley"}` in main.py — deliberately not gated on `is_admin` so future admins don't silently get export rights.
9. **Claude Haiku wraps JSON in markdown fences.** `_extract_json` in `caddy_round.py` handles it — don't bypass.
10. **Partial-round `differential`** produces nonsense; the calc assumes 18 holes. Add a guard if we start supporting partial rounds properly.

---

## Data model highlights

**`users` table** — one row per player. Key columns:
- `bag TEXT` (JSON) — `{"driver": 310, "3-wood": 250, ..., "custom_chipper": 60}`
- `shot_stats TEXT` (JSON) — unified pooled shot data: `{club_label: {trackman: bucket, course: bucket}}`. Bucket shape: `{count, total_carry, sum_sq, best, worst, left, right, center}`. Direction only meaningful for on-course data.
- `trackman_session_ids TEXT` (JSON list) — for dedup on re-upload
- `conversation_history TEXT` (JSON) — active chat (persisted in full)
- `active_round_state TEXT` (JSON) — `{course, tee, hole_scores, current_hole, started_at, course_confirmed}`
- `tendencies_summary TEXT` — Claude-generated qualitative narrative (NO numbers — those are in `shot_stats`)

**`conversations` table** — archived chats. Every completed round → `kind='round'`. Every reset → `kind='casual'`. Never deleted.

**`sessions` table** — auth tokens (30-day cookies).

**`course_geometry` table** (auto-wind feature) — cached OSM per-course hole geometry. Keyed by `(source, course_id)`.

---

## Message pipeline (`process_user_message` in main.py)

Every player message flows through this:

1. End-of-round detection (short-circuits everything else)
2. Course rejection detection (unloads a mis-loaded course)
3. Course detection + load (fires OSM geometry fetch in background)
4. Tee change detection
5. Score detection + logging
6. Drive distance inference (from "remaining yardage" on known holes; auto-logs to shot_stats)
7. Course-note detection (hazards mentioned in passing)
8. Build system prompt context: course + score + weather + **computed relative wind (auto-wind)**
9. `caddy_reply` → Claude Opus response
10. Persist history + round state

Events (`round_state`, `weather`, `events: ChatEvent[]`) are returned to the frontend so the UI can render round bar, weather strip, alerts, and (new) auto-wind badge.

---

## Recent significant changes (chronological, most recent first)

**Auto-wind pipeline** *(uncommitted as of 2026-05-24)* — `caddy_geo.py` fetches golf=hole/tee/green from OSM Overpass around a course's lat/lng (from golfcourseapi.com's own coordinates), spatially pairs tees + greens to their hole via containment in the hole polygon, computes tee→green bearing, and decomposes NWS wind into headwind/tailwind + crosswind from player POV. Cached per course in `course_geometry` table. Prompt gets a `COMPUTED RELATIVE WIND` block Claude must use verbatim; when unavailable it falls back to "ask once per hole, reuse the answer." Weather strip on `/caddy` shows an "auto" badge with the direction. Coverage of golfcourseapi.com sample: ~68% STRONG+PARTIAL.

**Conversation .docx export** *(2026-05-21, commit `2b1d390`)* — owner-only download (Conor + Drew) for archived chats and the active chat. Endpoints allowlist-gated. Also fixed the latent bug where `save_conversation` was truncating history to 60 messages on every save (only the per-turn Claude context should be capped, not persistence).

**Bag-exact wedge labels + club-in-bag lock-down** *(2026-05-21, commit `c7c47cd`)* — Caddy must call wedges what the player entered them as, and can never recommend a club not in the bag.

**Admin CSV export** *(commit `2b3993b`)* — one-row-per-user beta engagement dump at `/api/admin/export.csv`.

**Custom-club bag setup** *(commit `f64e24d`)* — hybrids, 7-wood, "Add another club" for custom entries. Custom-key format: `custom_<name>`.

**Unified `shot_stats`** — pooled Trackman + on-course shot data. Confidence tiers computed in code.

---

## Where to look

- **Memory / cross-session context**: `~/.claude/projects/-Users-conorsullivan/memory/project_caddy.md`
- **Session transcripts**: search via `mcp__ccd_session_mgmt__search_session_transcripts` (deferred tool — load via ToolSearch first)
- **Live Overpass sanity check**: `/tmp/osm_holes.py` (throwaway; useful reference for query shape)
- **Drew's beta feedback template**: `~/Desktop/Caddy_Beta_Feedback_Drew.docx`
- **Product notes / idea binder generator**: `~/Desktop/Caddy/make_idea_binder.js` (`NODE_PATH=/opt/homebrew/lib/node_modules node ...`)

---

## Current priorities (from Drew's on-course feedback + roadmap)

1. **Commit + push the auto-wind feature** so Drew's next round tests it.
2. **Approach-shot logging beyond driver** — currently only Driver is inferred from remaining-yardage. Need to parse "hit 7-iron from 145" out of player messages and log to `shot_stats[club].course`.
3. **PWA icons** for home-screen install polish (manifest exists, PNGs don't).
4. **Trackman session deletion UI** (dedup column exists, no DELETE endpoint).
5. **Course confirmation prompt** ("Butter Brook in Westford, MA — that right?") before attributing notes.

Long-term / vision (not next-sprint work): iPhone native app (RN vs Swift undecided), wearable-camera swing analysis, terrain analysis via USGS 3DEP, paired-play mode, Postgres migration around ~50 concurrent users.

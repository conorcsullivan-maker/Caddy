# Caddy Mobile (Expo / React Native)

The iPhone app. Talks directly to the Render backend with bearer-token auth
(no cookie proxy needed — that's a browser problem).

## Run it on your phone (fastest)

```bash
cd caddy-mobile
npx expo start
```

Scan the QR code with the iPhone camera → opens in **Expo Go** (install it
from the App Store first). Log in with your normal Caddy username + PIN.

## What works today (v0.1)

- Login (token stored in the iOS keychain)
- Full chat with the caddy — same pipeline as the web app
- GPS sent with every message → auto-wind, auto-yardage, GPS shot tracking
- Round bar (course · tee · hole · score vs par · yards to green)
- Weather strip
- 📷 camera button → scorecard loading + lie reading

## Not yet (next iterations)

- Voice (tap-to-talk + TTS replies) — the on-course primary modality
- Background location (every-shot tracking without opening the app) — needs a
  dev build, not Expo Go
- Profile / past rounds / scorecard editor screens
- Push notifications

## Local backend development

Point `API_BASE` in `src/api.ts` at your machine's LAN address
(e.g. `http://192.168.1.42:8000`) and run the backend with
`cd caddy-web/backend && source venv/bin/activate && python3 main.py`.
The phone and the machine must be on the same network.

## Keep the contract in sync

`src/api.ts` mirrors `caddy-web/frontend/src/lib/api.ts`. When the backend
contract changes, update both.

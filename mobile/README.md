# Mobile app (placeholder)

To be scaffolded in Phase 3.

## Planned stack

- **Expo (React Native)** — one codebase for iOS and Android
- **TypeScript**
- **TanStack Query** for API state
- **Expo Notifications** for push alerts when a new banko value bet appears

## Planned screens

- Auth (login/register)
- Fixture list (today / tomorrow / this week)
- Match detail — same two tabs as the web app (**Tüm Tahminler** / **Değer Bahisler**)
- Coupon builder — swipe-to-add legs, live combined odds/edge calculation
- Profile — bankroll, ROI, history

## Notifications

The backend will emit events to a user-specific queue when new value bets are detected.
The mobile app subscribes and shows silent pushes — user opens the app to see the pick.

No auto-betting, no deep-links into bilyoner — this is a decision-support tool only.

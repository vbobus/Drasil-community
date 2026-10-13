# Showcase assets

All interface captures in this README come from the current Drasil frontend with fictional data. They do not show a real account, a live model response, or a Google Calendar connection.

| Asset | Source |
| --- | --- |
| `community-demo.gif` | Continuous Playwright browser recording, encoded with FFmpeg; weekly planning, calendar, review/apply, habit check-in/editing, and My Day |
| `community-workspace.png` | Actual desktop planner with fictional tasks |
| `community-calendar.png` | Actual week calendar with fictional bookings |
| `community-review.png` | Actual review controls receiving a mocked proposal |
| `community-habits.png` | Actual habit list after a mocked saved check-in |
| `community-mobile.png` | Actual compact Habits page with fictional history |
| `community-cover.png` | Browser composition of desktop/mobile captures over the generated paper background; no invented app UI |
| `community-background.png` | Generated background using the built-in image tool; the tool did not expose a model selector |

The recording fixture defines a field-guide project, personal tasks, sketch practice, and three habits. It uses a fixed October 12, 2026 date in UTC. API responses, saves, and AI proposals are in memory. Captured approval changes the mock calendar; captured check-in changes mock XP. This demonstrates the interface and its actions, not live inference, durable storage, or third-party sync.

## Record again

Install Node.js 24 with npm 10 or newer, the frontend dependencies, Chromium for Playwright, and FFmpeg on your PATH:

```sh
npm ci --prefix frontend
cd frontend
npx playwright install chromium
node scripts/record-community-demo.mjs
node scripts/render-community-showcase.mjs
```

The recorder starts its own loopback Vite server with no proxy and an empty environment directory. It uses new browser contexts, does not load an existing profile or storage state, intercepts every API request, and blocks external browser requests. It never starts a backend or opens an account database. Raw video and the recording audit stay in ignored `.tmp/community-demo`; only curated images and the GIF in `docs/assets` are published. The renderer reuses the generated background; it makes no image API request.

Keep the GIF below the public export's 10 MiB per-file limit. Review the captures and every scene before committing. Do not replace mock fixtures with exported account data or use a signed-in personal browser for recording.

## Background provenance

Generated October 12, 2026 with the built-in image tool. No existing personal image was supplied. The requested model version could not be selected or verified through that tool.

Prompt: “Use case: ads-marketing. Create one clean editorial background image for the Drasil open-source planning app README showcase, landscape 16:9. Background only, no interface, no text, no logo, no devices, no people, no glowing tree, no neon, no particle effects, no circuit boards, no gradients-as-decoration. A quiet warm off-white architectural paper surface with one restrained folded navy paper form entering from the far right edge and a small muted sage paper plane near the bottom-right. Real tactile paper photographed with soft directional daylight, crisp elegant edges and gently offset natural shadows, very subtle texture. The upper-left two thirds and center remain open, nearly uniform pale paper so real app screenshots and a short wordmark can be composited clearly. Palette grounded in Drasil: paper #f5f1e9, navy #111a2c, muted sage #a9bca0. Modern, spacious, precise, calm; reduce ornament. Intended use: background behind separately captured real product screens, not an invented product mockup.”

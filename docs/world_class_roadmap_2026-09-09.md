# Making PredictX World Class (for a serious solo tool)

*Written September 9, 2026, based on a direct look at both the `predictx` backend and the `football_frontend` Ionic app.*

You said "world class" here means: a serious personal tool you can fully trust and be proud of — not a company product, not a Bet365 competitor. With that bar in mind, and focusing on the three areas you picked (smarter predictions, a polished app, and infrastructure that can grow), here's where things actually stand and what would move the needle most.

One honest note before the three sections: all three of these sit on top of a reliability cleanup that's already underway and only about a third finished (23 of roughly 557 spots in the code where an error was being silently swallowed instead of logged, one still-open item is that the backend runs in a mode meant for development, not for something you depend on). You didn't pick "finish reliability work" as a focus this time, and that's your call — but a smarter model or a prettier screen won't matter much if the app occasionally fails silently underneath them. I'd suggest treating that cleanup as a standing background task rather than fully shelving it, and I've folded the most load-bearing pieces of it into the infrastructure section below rather than repeating it as a fourth pillar.

## 1. Smarter predictions

**What's already there is genuinely solid.** This isn't a toy — you have an ensemble of real statistical models (Dixon-Coles, ELO, Poisson, an odds-implied model, and a weight optimiser that blends them), plus a separate AI/LLM layer for bet-builder and chat-style analysis, plus a whole learning subsystem meant to let the app improve itself from wins and losses over time. The recent work confirming known-league picks win 72.9% of the time versus 65% for unknown leagues — and fixing the code so that difference actually gets used live — is exactly the kind of thing that makes a prediction engine trustworthy rather than just clever-looking.

**Where it's leaking value right now:** several of the learning tables that are supposed to make picks better over time have been found empty or broken in recent audits (wrong column names, dead code that was never wired in, batch windows that never closed). Every one of those is a place where the system *thinks* it's learning but isn't. Finishing that cleanup is higher leverage than adding new models, because a new model built on top of a broken learning loop just inherits the same blind spot.

**What would make it "world class" beyond that:**
- A standing, automatic "how did we actually do" report — win rate, calibration (do 70%-confidence picks really win about 70% of the time?), broken down by league/market — generated on a schedule, not just when you happen to run a tool by hand. You already have the pieces (`tools/post_reset_accuracy_report.py`, the league report); the gap is turning that into a routine habit rather than a one-off.
- A simple backtesting habit: before changing a model or weight, run it against past matches first and compare against the current version, instead of finding out in production whether a change helped or hurt. Right now there's no clear "test it on history before it goes live" step.
- Only after the above two are solid: expanding the signal set (referee tendencies, lineups/injuries, weather) is worthwhile — but it's the last thing on this list on purpose, because new signals on a leaky foundation just add more things that can silently misfire.

## 2. A polished user experience

I looked at `football_frontend` — this is a real, working Ionic/React/Capacitor mobile app (~19,000 lines), not a stub. It already covers authentication, a splash flow, match views, an analytics section, and a bet-slip flow, and it supports football, basketball, and tennis. That's a meaningful head start most solo projects don't have.

A few things stood out as gaps rather than strengths:
- **No visible crash or error reporting.** The backend just went through exactly this fix (nothing was logged anywhere before this week). The frontend looks like it's in the same spot — if the app breaks on your phone, you likely only know because you happened to be looking at it. A free tool like Sentry (or Firebase Crashlytics, since you already pull in Firebase for push notifications) would close that gap quickly and cheaply.
- **No accessibility or design-consistency review yet.** The app uses a mix of MUI, Tailwind, and custom components across many screens — a natural place for small inconsistencies (spacing, contrast, tap-target size) to creep in as it grows. This is a good moment to do that review, while the app is still small enough to fix in a day rather than a month.
- **The routing library (`react-router` v5) is a few major versions behind.** Not urgent — it still works fine — but worth knowing it's there so it doesn't become a bigger jump later.
- **No automated frontend tests found**, similar to the backend's thin test coverage (see below).

Concretely, I'd suggest a short, focused pass: run a design and accessibility review against the 3-4 screens you use most (match list, match detail, bet builder), fix what it finds, and wire up basic crash reporting. That's a small amount of work that directly raises how "trustworthy and professional" the app feels day to day — which is the actual definition of polish for a tool you use yourself.

## 3. Scale & growth infrastructure

This is where the most "invisible but important" work lives, and most of it is already mapped out from recent sessions — it just isn't finished:

- **Database:** you're still mostly on SQLite (62 files talk to it directly), with a move to CockroachDB started and paused after migrating just the `users` table. SQLite is fine for one person on one device; it becomes a real limit the moment you want the app reachable from more than one place at once, or want it to survive a crashed process without any risk of a corrupted file.
- **How the app actually runs:** right now it's started with `uvicorn --reload` in a plain terminal window (via `launch_predictx.bat` or Termux). That flag exists for development — it means single process, restarts on any file change, and nothing restarts it if it crashes or the window closes. For something you want to trust, this is the single biggest "it just stopped and I didn't notice" risk. Fixing it needs one decision from you first: will this run on your Windows PC, on the Android/Termux setup, or somewhere hosted? That decision changes whether the fix is a Windows service (NSSM), a proper `tmux` + watchdog setup on Termux, or a small cloud host with Docker.
- **You won't know when it's down.** There's no uptime or error alerting — the live-bet "no response" bug earlier this month was found by noticing it, not by the system telling you. A cheap fix: a scheduled ping to `/health` that messages you (Telegram or Pushover both have free tiers) the moment it fails.
- **Test coverage is thin for the size of the codebase** — 3 test files across 164 backend Python files. That's a real risk once CockroachDB and the reliability sweep are both in flight, because it's easy for a change in one area to quietly break another with nothing to catch it.
- **No CI.** Nothing currently runs even the 3 existing tests automatically when you push a change. A basic GitHub Actions workflow that runs the test suite on every commit is cheap to set up and catches regressions before they reach the running app.
- **Secrets hygiene is actually good** — `.env` is properly excluded from git, so that's one thing that's already done right.

## Suggested order, given you're doing this solo

1. Finish the reliability sweep (logging is done; keep working through the silent-error cleanup) and make the hosting/process decision so the app stops running in dev mode.
2. Finish the CockroachDB migration, table by table, as already planned.
3. Add CI and grow test coverage past the current 3 files — do this alongside 1 and 2, since you'll be touching the same code anyway.
4. Turn the accuracy/calibration report into a routine, and close out the remaining learning-pipeline gaps.
5. Do the frontend design/accessibility pass and add crash reporting.
6. Only then: expand prediction signals and add new features.

This isn't the only valid order — it's just the one that avoids building new things on top of parts that are still being fixed. Happy to pick any single item from this and actually start on it.

# Lifestyle question bot for X

A small, low-cost, standalone app that turns selected lifestyle-survey questions into randomized X polls. It makes **no AI/LLM calls**, so it consumes no model tokens. It uses the official X API when enabled.

## Deploy to Vercel

This repository includes a cloud version in `api/index.py`. It uses Vercel Functions, Vercel Cron, and a Vercel KV/Upstash Redis store, so it works from any device and does not depend on your Mac being on.

1. Create a new GitHub repository and push this folder.
2. Import that GitHub repository into Vercel.
3. Add a Vercel KV (or Upstash Redis) integration, then copy its REST URL and token into the environment variables below.
4. In Vercel **Settings → Environment Variables**, add the five values in `.env.vercel.example`.
5. Deploy. Vercel will prompt for `DASHBOARD_PASSWORD` when you open the site. The daily cron posts at 10:00 UTC (12:00 South Africa time during SAST).

The cloud dashboard offers pause/resume, post-now, candidate discovery, and explicit Like, Repost, Quote, and allowed Reply actions. The Cron request is protected by `CRON_SECRET`; the dashboard is password protected.

## What it does

- posts one randomized, non-repeating survey question per day to your profile; questions that meet X poll limits become polls, and the rest are open questions;
- records every selection and publish attempt in SQLite;
- searches for relevant recent posts and puts suggested replies into a review queue;
- never automatically replies to other people by default;
- defaults to safe `dry_run` mode.

The supplied survey's full 255-question set is included, including demographic and sensitive topics. Review the text in `data/questions.json` before enabling live posting if you do not want those topics used publicly.

## Run it

1. Copy `.env.example` to `.env` and leave `DRY_RUN=true` for the first run.
2. Start it (or double-click `start-bot.command` in Finder):

   ```sh
   python3 app.py
   ```

3. Open [http://127.0.0.1:8787](http://127.0.0.1:8787). Click **Post next poll now** to verify the flow.
4. Create an X developer app and obtain a user-context OAuth 2.0 access token that can create Posts. Put it in `X_USER_ACCESS_TOKEN`, set `DRY_RUN=false`, and restart the app.

The process checks once per minute whether a randomized scheduled time is due. Keep it running with a service manager (launchd on macOS, systemd on Linux, or a cloud host).

## Managing it yourself

The dashboard has everything needed for day-to-day control: pause/resume publishing, test the X connection, post a question immediately, set tomorrow's randomized posting window, review candidate posts, then explicitly Like, Repost, Quote, or Reply when X permits it. Double-click `stop-bot.command` to stop it.

## Reply workflow

Click **Find candidate replies**. The app searches a small fixed set of lifestyle topics through X's recent-search endpoint and applies simple keyword matching. It saves at most five candidates for review. Click **Publish** only when the suggested reply is genuinely useful in that conversation.

Do not turn the reply queue into a mass-commenter: leave `REPLIES_PER_DAY=0` unless you later add strict, tested caps and review rules.

## Limitations and safety

X access, scopes, prices, and rate limits change; confirm them in the developer console before enabling live mode. A successful dry-run does not mean your account has the required write access. The bot does not try to bypass X interface controls or platform rules.

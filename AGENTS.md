# SisengAI website — agent guide

Use this file as the operating context for future work on the public SisengAI website and its supporting AI services.

## What this repository publishes

This is a Quarto website for SisengAI. A push to `main` triggers GitHub Actions, renders the site with Quarto 1.9.37, and publishes the static output to the `gh-pages` branch. The public site is served at `www.sisengai.com`.

The site is a service business website, not primarily a portfolio. Its job is to explain offers clearly and direct suitable visitors to a short Google Meet conversation.

### Established business decisions

- The SisengAI logo belongs in the top-left navbar home link only. Do not repeat it as a large in-page hero mark.
- The main CTA is the Google Calendar appointment page: https://calendar.app.google/d7Ttfbm1Masri4vB8
- Client contact on the site should lead to Google Meet calls or the direct email form. Do not add Telegram, WhatsApp, Calendly, n8n, or Make as client-contact flows.
- Pi.dev is the preferred automation/delivery environment when automation implementation is discussed.
- Current offers are:
  1. Automation Blueprint
  2. Workflow Automation
  3. AI Personal Assistant — research, concise briefings, agreed personality/voice, and approval boundaries
  4. AI Website and Chatbot — client-facing websites, grounded on-site chatbots, lead qualification, and hand-off
- The master CV published on the site is `resume/RickyMacharm_CV_2026.pdf`. Keep earlier CV files intact unless the user specifically asks to replace/remove them.

## Essential commands

Render before any website hand-off:

    uv run quarto render

Preview locally:

    uv run quarto preview

The repository’s GitHub Action is `.github/workflows/publish.yml`. Keep its Quarto version aligned with the local version. The workflow currently uses 1.9.37.

A Quarto CSS-variable export warning has occurred with this theme even when rendering succeeds. Treat a non-zero render or a theme compilation failure as a real failure; a successful render with only that known export warning still produces site output. Remove the generated `_quarto_internal_scss_error.scss` artifact before committing it.

## Important layout

- `_quarto.yml` — navigation, global metadata, dual theme setup, footer, and chatbot widget include
- `index.qmd` — service-led homepage and free Website Brief entry point
- `services.qmd` — full offer descriptions
- `contact.qmd` — Google Meet booking plus visitor-composed email form
- `free-website-brief.qmd` — public Website Brief user interface
- `resume/index.qmd` — embedded/downloadable master CV
- `chatbot/app.py` — existing self-hosted RAG chatbot service; this is not run by GitHub Pages
- `chatbot/website_research_assistant.py` — separate public Website Research Assistant backend; this is not run by GitHub Pages
- `chatbot-widget.html` — small public-site loader for the existing chat bubble
- `html/styles.scss` — light theme
- `html/styles-dark.scss` — dark theme

## Frontend and brand rules

The light theme uses cream, mahogany, stone, and amber. The dark theme uses zinc and amber. Both are complete, separate SCSS files.

When adding or changing a component:

1. Update both `html/styles.scss` and `html/styles-dark.scss`.
2. Use each theme’s own variables; do not copy light-theme tokens into the dark stylesheet.
3. Render the entire site and inspect the relevant output.
4. Keep the visual language editorial, warm, practical, and restrained. The Website Brief is a utility panel, not a competing second hero.

Typography is Playfair Display for headings, Inter for body/UI, and JetBrains Mono for technical labels. Typical radius: 0.75rem for buttons, 1rem for cards.

## Direct email contact form

The public contact form is a static-site mailto form. It collects a name, email, subject, and message on the page, then opens the visitor's configured email application with a pre-filled message addressed to Ricky.Macharm@SisengAI.com. It does not send email silently from GitHub Pages.

If a future task requires form submission without a visitor email application, select and explicitly configure an email-form backend/service; do not put email-service credentials in browser code.

## Existing chatbot

The live widget is loaded from `https://bot.sisengai.com/widget/sisengai-demo.js`. It is a Python FastAPI service with:

- SQLite storage for bots, source chunks, and leads
- public-site ingestion plus TF-IDF retrieval
- an OpenAI-compatible model call
- a Shadow DOM widget

Configuration is environment based:

- `CHATBOT_MODEL`
- `CHATBOT_BASE_URL`
- `OPENCODE_GO_API_KEY` or compatible fallback key names
- `CHATBOT_ADMIN_KEY`
- `CHATBOT_DATA`

Never commit API keys or copy environment files into the repository.

### Current chatbot incident

On 2026-09-09, the live health endpoint returned 200 and the demo widget existed, but a valid chat request such as “hello” returned HTTP 500. The local code sends greetings directly to the model provider and lets upstream HTTP/response-shape errors escape uncaught. The static-site push did not cause that failure.

Before changing the provider:

1. Inspect the deployed bot service logs to capture the upstream error.
2. Configure the replacement provider and model through server environment variables, not source-code secrets.
3. Add graceful provider error handling so the visitor receives a short retry message rather than a generic widget error.
4. Verify `/health`, a greeting, a grounded website question, lead capture, and the embedded widget.
5. Commit source/config documentation only; deploy service secrets through the hosting environment.

OpenRouter is the candidate provider discussed with the user. Confirm the currently supported model ID and pricing from official OpenRouter documentation at implementation time; model availability changes.

## Free Website Brief

The public page at `/free-website-brief.html` is a normal Quarto page with the same navbar, footer, and styling as the rest of the site. Visitors submit a public URL, receive a source-grounded brief, then can ask follow-up questions about that page.

Its Python service is intentionally separate from the existing support chatbot:

- isolated endpoints, SQLite data, sessions, prompts, and rate limits
- one public HTML page per analysis
- short-lived sessions (24 hours)
- blocks local/private/reserved addresses and custom ports
- checks redirect destinations, respects readable robots rules, and caps response size
- never place AI keys in the Quarto page or browser JavaScript

### Current deployment state and plan

The page UI has been published, but GitHub Pages cannot run its Python backend. The default browser configuration currently names `research.sisengai.com` as a future dedicated API origin. The user prefers no new public subdomain.

Preferred next architecture:

1. Keep the visitor-facing page at `www.sisengai.com/free-website-brief.html`.
2. After repairing the existing bot service, deploy/mount the Website Brief backend behind the existing bot host, for example `https://bot.sisengai.com/research`.
3. Update `free-website-brief.qmd` to call that verified route.
4. Give the research service a dedicated API key and data directory, strict allowed browser origins, request limits, and network-level egress rules that deny private/internal IP ranges.
5. Test with a permitted public website, an invalid URL, a local/private URL, a redirect, a follow-up question, and mobile layout.
6. Only then describe the free analysis as live in public-facing copy.

Do not reuse the existing chatbot’s public `/ingest` endpoint for arbitrary visitor URLs.

## Change workflow

For content or UI work:

1. Read the relevant QMD, SCSS, and config files before editing.
2. Preserve user changes that are unrelated to the requested work.
3. Render the website.
4. Check the rendered page and mobile layout where practical.
5. Review `git diff --check` and `git status`.
6. Commit and push only when the user asks.

For backend or deployment work:

1. Keep public static-site code and secret-bearing backend configuration separate.
2. Create a repeatable local request test before changing provider, model, retrieval, or scraping behaviour.
3. Use friendly structured API errors; never turn provider failures into unexplained HTTP 500 responses.
4. Do not deploy or alter DNS without explicit user approval.

# SisengAI — Design System & Maintenance Guide

## Quick Start

```bash
# Render locally (Jupyter notebooks need project venv)
uv run quarto render

# Preview with live reload
uv run quarto preview
```

Site is deployed to **www.sisengai.com** via GitHub Actions on push to `main`. The CI publishes to the `gh-pages` branch which GitHub Pages serves.

---

## Quarto Version Trap

**Your local Quarto and CI Quarto must match.** If they don't, YAML validation errors or missing features will silently break the build.

| Where | Version | Set in |
|---|---|---|
| Local | `1.9.37` | `quarto check` |
| CI | `1.9.37` | `.github/workflows/publish.yml:20` |

If you upgrade quarto locally (`quarto upgrade`), update the CI version too.

### Names that changed between Quarto versions

| Feature | 1.5 name | 1.9 name | Used in |
|---|---|---|---|
| Callout style | `callout-style` | `callout-appearance` | `_quarto.yml` |
| Highlight | — | `highlight-style` | (not used, relies on theme default) |

We use the 1.5-compatible names to stay safe: `callout-style: simple`.

---

## Design System — LivingStory

### Colors

| Token | Value | Usage |
|---|---|---|
| `$cream-50` | `#FDFBF7` | Page background |
| `$cream-200` | `#F2EBD8` | Card borders, navbar border |
| `$primary-600` | `#D4720A` | Primary buttons, links, active states |
| `$primary-700` | `#B05A08` | Button hover |
| `$mahogany-950` | `#321208` | Headings, dark section bg |
| `$mahogany-900` | `#5C2D1E` | Secondary borders, card bg (dark mode) |
| `$stone-600` | `#57534E` | Body text |
| `$sage-700` | `#2D5C3C` | Project tags |
| `#832C1C` | — | Dark mode borders |

### Typography

| Role | Font | Weight |
|---|---|---|
| Headings (`h1`–`h4`) | Playfair Display | 700 |
| Body, nav, UI | Inter | 400/500/600 |
| Code | JetBrains Mono | 400/500 |

`h1`: `clamp(2.25rem, 5vw, 4.5rem)` — responsive, 36px–72px
`h2`: `clamp(1.875rem, 4vw, 3rem)` — 30px–48px
Body: `1.0625rem` (17px), line-height `1.65`

### Border Radius

| Element | Radius |
|---|---|
| Buttons | `0.75rem` (12px) |
| Cards | `1rem` (16px) |
| Badges/pills | `9999px` (full pill) |
| Nav links | `0.5rem` (8px) |

### Shadows

```scss
$box-shadow:     0 4px 24px rgba(92, 45, 30, 0.10);  // warm mahogany
$box-shadow-lg:  0 8px 48px rgba(92, 45, 30, 0.15);
// Buttons also get: 0 4px 24px rgba(212, 114, 10, 0.25); // amber glow
```

### Section Layout

Pages use alternating backgrounds for visual rhythm:

```
hero-section        → cream (default)
content-section     → cream (default)
content-section-alt → white
content-section-dark→ dark mahogany (#321208)
```

Sections have `5rem` top/bottom padding with labels (`.section-label` amber pill badge) and subtitles (`.section-subtitle` max-width 672px).

### Cards

```html
<div class="exp-card">        <!-- or project-card -->
  <p class="exp-title">...</p>
  <p class="exp-institution">...</p>
  <p class="exp-description">...</p>
  <span class="exp-badge">2021</span>  <!-- amber pill badge -->
</div>
```

Cards lift 3px on hover with warmer shadow. In dark mode, cards use `$mahogany-900` background.

### Dark Mode

Triggered by Quarto's built-in `data-bs-theme="dark"` toggle. The entire dark palette is in `html/styles.scss` under `[data-bs-theme="dark"] { ... }`.

Dark theme colors differ from light entirely — cream becomes `#190B04`, white cards become `#3D2315`, amber primary becomes `#F08C14`.

---

## File Map

| File | Role |
|---|---|
| `_quarto.yml` | Site config: theme, navbar, footer, repo-actions, fonts |
| `html/styles.scss` | Complete design system (~400 lines): colors, typography, cards, hero, navbar, footer, dark mode |
| `index.qmd` | Homepage: hero + stats bar + about card + education cards + latest posts listing |
| `blog/index.qmd` | Blog listing page (grid, 2 columns) |
| `blog/_metadata.yml` | Defaults for all blog posts: freeze, banner, callout-style, repo-actions, giscus |
| `projects/index.qmd` | Project cards in a 2-column grid |
| `resume/index.qmd` | PDF resume embed |
| `about.qmd` | Simple about page |
| `404.qmd` | Custom 404 with redirect |
| `.github/workflows/publish.yml` | CI: render → publish to gh-pages |

---

## How To…

### Change the primary color

Edit `html/styles.scss` lines 13–16:
```scss
$primary-600: #D4720A;  // buttons, links
$primary-700: #B05A08;  // hover state
```
Also update dark mode `#F08C14` near line 640.

### Change heading font

Edit `html/styles.scss` line 41:
```scss
$headings-font-family: "Playfair Display", Georgia, "Times New Roman", serif;
```
Also update the `@import url(...)` on line 101 to include the new Google Font.

### Add a new page

1. Create `new-page.qmd` at the project root (or in a subfolder)
2. Add to `_quarto.yml` navbar under `left:` or `right:`
3. Render — Quarto auto-discovers `.qmd` files

### Add a new blog post

Create a folder under `blog/` with the date prefix (e.g., `blog/2026-June-01-My-Post/`), put an `.qmd` or `.ipynb` inside. The blog listing auto-discovers it.

### Add a section label (pill badge)

```markdown
<span class="section-label">Your Label</span>
```

### Use alternating section backgrounds

```markdown
::: {.content-section}
<!-- cream background -->
:::

::: {.content-section-alt}
<!-- white background -->
:::
```

### Test locally before pushing

```bash
uv run quarto render && uv run quarto preview
```

### Debug CI failures

1. Check that CI Quarto version matches local: `quarto check` vs `.github/workflows/publish.yml:20`
2. Check that YAML keys use 1.5-compatible names (see table above)
3. GitHub Actions logs are at `https://github.com/theAfricanQuant/theAfricanQuant.github.io/actions`

### Regenerate after deleting _freeze/

```bash
rm -rf _freeze/ _site/
uv run quarto render
```

This forces full re-execution of all Jupyter notebooks — needs the project venv with jupyter installed.

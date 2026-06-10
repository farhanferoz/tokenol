# Skills as a peer entity-mix on the Breakdown page — design

**Status:** approved 2026-06-10. Follow-up to the Skills cost dimension (merged to `main` @ `b5ad1f0`).

## Problem

The Skills cost dimension shipped with a presentation inconsistency: a dedicated **"Skills" nav tab** and an isolated **"Skills" section** bolted to the bottom of the Breakdown page. No other entity dimension (Project, Model, Tool) gets its own tab — they're all just panels on the Breakdown page. Clicking "Skills" routed to `/skills`, which served the same Breakdown page and was meant to auto-scroll to the bottom panel (the scroll fired before the charts above finished growing the page, so it didn't land). The net effect: clicking "Skills" appeared to show "Breakdown," with the Skill Mix panel buried under everything.

The page was *already* uneven before Skills: Project + Model share a **"Breakdowns"** heading, but Tool got its own **"Tools"** heading for no structural reason.

## Principle

A skill is just another **breakdown dimension**, reached exactly like tool/model/project: a ranked panel on the Breakdown page with click-through to a per-entity detail page (`/skill/<name>`, mirroring `/tool/<name>`). No tab, no special landing, no isolated section.

## Decision

Unify all four entity mixes under a single **"Breakdowns"** section, fixing the pre-existing Tool oddity at the same time.

### Target Breakdown-page structure

```
Time
  Daily Billable Tokens | Daily Cache Re-use

Breakdowns                                  ← one heading for all four entity mixes
  Tokens by Project | Model Mix             (unchanged 2-1 grid)
  Tool Mix                                  (full width, moved up from its own "Tools" section)
  Skill Mix                                 (full width, moved up from its own "Skills" section)

Nav: Overview | Breakdown                   (no Skills tab)
```

Subtitle changes from *"tokens by day, project, model, and tool"* to *"tokens by day, project, model, tool, and skill."*

## Changes

1. **Nav** — `src/tokenol/serve/static/index.html` + `breakdown.html`: remove the `<a href="/skills" class="nav-tab">Skills</a>` tab.
2. **`breakdown.html`** — collapse the `Breakdowns` / `Tools` / `Skills` section headings into one `Breakdowns` section containing three grid rows (Project|Model, Tool Mix, Skill Mix). Update the page subtitle. Element ids (`bp-tools-*`, `bp-skills-*`, `bd-skills-*`, `bp-skills-bars`, …) are unchanged — only their DOM position moves.
3. **`breakdown.js`** — delete the `if (location.pathname === '/skills') { … }` nav-highlight + `scrollIntoView` block (no tab, no `/skills` landing to special-case). All Skill Mix fetch/render/pill wiring is untouched.
4. **`app.py`** — remove the `/skills` page route (`skills_page`). **Keep** `/skill/{name}` (detail page / click-through), `/api/breakdown/skills`, `/api/skill/{name}`. A request to `/skills` now 404s (route never shipped; no bookmarks to protect).
5. **CHANGELOG** (`## 0.7.0 — Unreleased`) — drop "a 'Skills' nav tab is added"; describe Skill Mix as a panel in the Breakdowns section.
6. **Tests** (`tests/test_serve_app.py`) — add a guard that `/skills` 404s and that the `Skills` nav tab is absent from `/breakdown` and `/` HTML, while `/breakdown` still serves the `bp-skills-bars` panel and `/skill/tiered-review` still serves HTML. Update/replace the existing `test_skill_page_serves_html` only if it targeted the `/skills` route (it targets `/skill/{name}`, which stays — leave it).

## Out of scope (unchanged)

- The `/skill/<name>` detail page (scorecards, 30-day chart, inline-vs-sub-agent split).
- The Cost-by-skill bars on model/project detail pages.
- All parser/state/rollups/persistence backend — this is purely placement + reachability.

## Net effect

Removes more than it adds: one page route, one JS block, and two section-heading `<div>`s deleted; one panel relocated. No backend or data-model change. The Skill dimension becomes structurally indistinguishable from Tool/Model/Project — which is the whole point.

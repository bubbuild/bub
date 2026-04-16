# AGENTS.md — Bub Website

> Guidelines for AI agents (Copilot, Claude Code, Cursor, etc.) working on the `website/` directory.

---

## Project Overview

This is the **Bub marketing & docs site** — a static Astro site combining:

- **Landing pages** (home, 404) with custom components.
- **Starlight docs** (`/en/…`, `/zh-cn/…`) auto-generated from `src/content/docs/`.
- **Blog** (`/posts/…`, `/zh-cn/posts/…`) powered by Astro content collections.

Site URL: `https://bub.build`

---

## Tech Stack

| Layer         | Tool                                   |
|---------------|----------------------------------------|
| Framework     | **Astro 6** (static output)            |
| Docs          | **@astrojs/starlight** ≥ 0.38          |
| Styling       | **Tailwind CSS v4** via `@tailwindcss/vite` |
| Component lib | shadcn/ui conventions (base-vega style) |
| Animations    | `motion` (formerly Framer Motion)      |
| Code blocks   | `astro-expressive-code`                |
| Fonts         | Outfit Variable (sans), JetBrains Mono Variable (mono) |
| Icons         | **@lucide/astro** — see Icon section below |
| Type checking | TypeScript strict mode                 |

---

## Package Manager

**pnpm** — always use `pnpm` to install packages and run scripts.

```bash
pnpm install          # install deps
pnpm dev              # dev server
pnpm build            # production build
pnpm preview          # preview production build
```

---

## Directory Structure

```
website/
├── astro.config.mjs          # Astro + Starlight + Tailwind config
├── components.json            # shadcn/ui settings (base-vega style)
├── package.json
├── tsconfig.json              # strict, path alias @/* → src/*
├── public/                    # static assets (logos, favicon)
├── src/
│   ├── components/            # shared Astro components
│   │   ├── ui/                # primitives (Icon, SectionHeading)
│   │   ├── NavBar.astro
│   │   ├── Footer.astro
│   │   ├── Hero.astro
│   │   ├── Features.astro
│   │   ├── HookIntro.astro
│   │   ├── TapeModel.astro
│   │   ├── Testimonials.astro
│   │   ├── Contributors.astro
│   │   ├── PostCard.astro
│   │   └── ThemeToggle.astro
│   ├── content/
│   │   ├── docs/              # Starlight markdown (en/, zh-cn/)
│   │   ├── i18n/              # Starlight UI string overrides (zh-CN.json) — DO NOT duplicate into src/i18n/
│   │   └── posts/             # Blog posts (en/, zh-cn/)
│   ├── i18n/
│   │   ├── ui.ts              # Flat-key UI strings: nav, footer, 404, posts, site meta
│   │   ├── utils.ts           # getLangFromUrl, useTranslations, getNavProps, etc.
│   │   └── landing-page.ts    # Single source of truth for all landing-page copy (both locales)
│   ├── layouts/
│   │   ├── BaseLayout.astro   # Shared HTML shell (head, nav, footer, scripts)
│   │   ├── LandingLayout.astro
│   │   ├── PostLayout.astro
│   │   └── PostListLayout.astro
│   ├── pages/
│   │   ├── index.astro        # EN landing
│   │   ├── 404.astro
│   │   ├── posts/             # EN blog
│   │   └── zh-cn/             # ZH-CN landing + blog
│   └── styles/
│       └── global.css         # Tailwind v4 + CSS custom properties
└── DESIGN.md                  # Visual design guide
```

---

## i18n Architecture

### Two systems, one site

1. **Starlight docs**: i18n is configured in `astro.config.mjs` (`locales`, `defaultLocale`, sidebar `translations`). Override Starlight UI strings in `src/content/i18n/zh-CN.json`.

2. **Custom pages** (landing, blog, 404): use the project's own i18n module at `src/i18n/`.

### Custom i18n module — `src/i18n/`

| File               | Purpose |
|--------------------|---------|
| `ui.ts`            | **Flat-key** UI string dictionary for shared UI: site meta, nav, footer, 404, post list, language switcher. English is source of truth; other locales override selectively. |
| `utils.ts`         | `getLangFromUrl()`, `useTranslations()`, `useTranslatedPath()`, `getNavProps()`, `getAlternateLocaleHref()` helpers. |
| `landing-page.ts`  | **Single source of truth for all landing-page copy** — both locales. Structured data (feature arrays with icons/colors, testimonials, hook stages, hero text, CTAs). |

> **Convention**: Landing-page components (Hero, Features, HookIntro, TapeModel, Testimonials) are **pure presentation** — they receive all text/content via props from `landing-page.ts`. They have no hardcoded text defaults. Non-landing UI strings (nav, footer, 404, posts) go in `ui.ts` and are accessed via `t()`.

### Adding a UI string (non-landing pages)

1. Add the English string to `ui.ts` under the `en` object.
2. Add the translation under the target locale.
3. Use `const t = useTranslations(locale); t('your.key')` in any `.astro` file.

### Adding landing-page content

1. Add the English content to `landing-page.ts` under the `en` section.
2. Add the translation under the target locale section.
3. Pass as props from the page to the component via `getLandingPageCopy(locale)`.

### Adding a new locale

1. Add the locale key to `languages` in `ui.ts`.
2. Add a full entry in the `ui` dictionary (copy `en` and translate).
3. Add content in `landing-page.ts` if needed.
4. Create page directories under `src/pages/<locale>/` and `src/content/docs/<locale>/`.
5. Update `astro.config.mjs` `locales` object.

### URL scheme

- English (default): **no prefix** — `/`, `/posts/`, etc.
- Chinese: `/zh-cn/`, `/zh-cn/posts/`, etc.
- Starlight docs always have a locale prefix: `/en/getting-started/`, `/zh-cn/getting-started/`.

---

## Icons — @lucide/astro

All icons come from **Lucide** via `@lucide/astro`. The project wraps them in `src/components/ui/Icon.astro` which handles camelCase → PascalCase name mapping.

```astro
<Icon name="arrowUpRight" size={15} />
<Icon name="github" size={14} />   <!-- special-cased brand icon -->
```

- Use **camelCase** icon names (e.g., `arrowUpRight`, `radioTower`, `fileSearch`).
- Browse icons at https://lucide.dev/icons.
- The GitHub brand icon is manually defined inside `Icon.astro` (not a Lucide built-in).

---

## Theme System — CSS Custom Properties

The design system uses **oklch** color tokens defined in `src/styles/global.css`.

### Light / Dark

- Light tokens in `:root { }`.
- Dark tokens in `.dark { }` (toggled via class on `<html>`).
- Theme toggle logic lives in `ThemeToggle.astro` (in `NavBar.astro`), with an inline init script in `BaseLayout.astro` `<head>` to prevent FOUC.
- Stored in `localStorage` under key `bub-theme`.

### Core tokens (selection)

| Token                | Usage                              |
|----------------------|------------------------------------|
| `--background`       | Page background                    |
| `--foreground`       | Primary text                       |
| `--primary`          | CTA buttons, active states         |
| `--primary-foreground` | Text on primary backgrounds     |
| `--secondary`        | Hover backgrounds, cards           |
| `--muted-foreground` | Secondary text, meta info          |
| `--border`           | All borders                        |
| `--card` / `--card-foreground` | Card surfaces           |
| `--chart-1` … `--chart-5` | Accent colors for charts/tags |

### Fonts

| CSS var         | Font                     | Usage                |
|-----------------|--------------------------|----------------------|
| `--font-sans`   | Outfit Variable          | Body, headings       |
| `--font-mono`   | JetBrains Mono Variable  | Code, badges, labels |

### Radius

Radius tokens follow a scale: `--radius-sm` through `--radius-4xl`, all computed from `--radius: 0.625rem`.

---

## Layout Hierarchy

```
BaseLayout.astro          ← HTML shell, <head>, NavBar, Footer, scroll-reveal, back-to-top
├── LandingLayout.astro   ← Hero + section components
├── PostLayout.astro      ← Single blog post with article styles
├── PostListLayout.astro  ← Blog listing page
└── (404.astro uses BaseLayout directly)
```

**All pages go through BaseLayout.** Never duplicate `<!doctype>`, `<head>`, `NavBar`, `Footer`, or scroll-reveal scripts.

---

## Coding Conventions

- **Astro components** for static content; avoid client-side JS unless interactive.
- **Tailwind utility classes** for styling; avoid custom CSS unless for animations.
- `class:list` for conditional classes.
- Props interfaces at the top of the frontmatter.
- `data-reveal` attribute on elements that should animate on scroll.
- Keep line length ≤ 120 chars.

---

## Before Committing

1. `pnpm build` — must pass with no errors.
2. Check for hardcoded nav props — use `getNavProps()` from `i18n/utils.ts`.
3. Check for duplicated HTML shell — compose on `BaseLayout`.
4. Ensure all user-visible strings use the correct source:
   - Landing-page copy → `landing-page.ts`
   - Shared UI strings (nav, footer, 404, posts) → `ui.ts`
   - Never duplicate text between the two files.

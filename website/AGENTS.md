# AGENTS.md — Bub Website

> Guidelines for AI agents (Copilot, Claude Code, Cursor, etc.) working on the `website/` directory.

---

## Project Overview

This is the **Bub marketing & docs site** — a static Astro site combining:

- **Landing pages** (home, 404) with custom components — i18n via `src/i18n/`.
- **Starlight docs** (`/docs/getting-started/…`, `/zh-cn/docs/getting-started/…`) auto-generated from `src/content/docs/` — i18n via Starlight built-in.
- **Blog** (`/posts/…`, `/zh-cn/posts/…`) powered by Astro content collections — i18n via `src/i18n/`.

Site URL: `https://bub.build`

---

## Tech Stack

| Layer         | Tool                                   |
|---------------|----------------------------------------|
| Framework     | **Astro 6** (static output)            |
| Docs          | **@astrojs/starlight** ≥ 0.38          |
| Styling       | **Tailwind CSS v4** via `@tailwindcss/vite` + `@astrojs/starlight-tailwind` |
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
├── astro.config.mjs          # Astro + Starlight + Tailwind config (locales, sidebar translations)
├── components.json            # shadcn/ui settings (base-vega style)
├── package.json
├── tsconfig.json              # strict, path alias @/* → src/*
├── public/                    # static assets (logos, favicon)
├── src/
│   ├── content.config.ts      # Content collections: docs (Starlight), i18n (Starlight), posts (blog)
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
│   │   ├── docs/              # [Zone 1] Starlight content collection
│   │   │   ├── docs/              # EN docs — maps to /docs/… routes
│   │   │   │   ├── getting-started/
│   │   │   │   ├── concepts/
│   │   │   │   ├── guides/
│   │   │   │   └── extending/
│   │   │   └── zh-cn/             # Chinese translations
│   │   │       └── docs/
│   │   │           ├── getting-started/
│   │   │           ├── concepts/
│   │   │           └── …
│   │   ├── i18n/              # [Zone 1] Starlight UI string overrides — BCP-47 filenames
│   │   │   └── zh-CN.json    # ⚠️ ONLY for Starlight UI — never duplicate into src/i18n/
│   │   └── posts/             # [Zone 3] Blog posts
│   │       ├── en/            # English posts
│   │       └── zh-cn/         # Chinese posts
│   ├── i18n/                  # [Zone 2] Custom page i18n module (NOT for Starlight docs)
│   │   ├── ui.ts              # Flat-key UI strings: nav, footer, 404, posts, site meta
│   │   ├── utils.ts           # getLangFromUrl, useTranslations, getNavProps, etc.
│   │   └── landing-page.ts    # Single source of truth for all landing-page copy (both locales)
│   ├── layouts/
│   │   ├── BaseLayout.astro   # Shared HTML shell (head, nav, footer, scripts)
│   │   ├── LandingLayout.astro
│   │   ├── PostLayout.astro
│   │   └── PostListLayout.astro
│   ├── pages/
│   │   ├── 404.astro              # 404 page (custom page, uses Zone 2 i18n)
│   │   └── [...locale]/           # Dynamic locale routing — generates EN (root) + ZH-CN variants
│   │       ├── index.astro        # Landing page (both locales via getStaticPaths)
│   │       └── posts/
│   │           ├── index.astro    # Post list (both locales via getStaticPaths)
│   │           └── [slug].astro   # Single post (both locales via getStaticPaths)
│   └── styles/
│       └── global.css         # Tailwind v4 + Starlight bridge + CSS custom properties
└── DESIGN.md                  # Visual design guide
```

---

## i18n Architecture

> **Design reference**: This architecture follows patterns from the [Starlight docs site](https://github.com/withastro/starlight/tree/main/docs) and the [Astro docs site](https://github.com/withastro/docs). Both use Starlight for documentation and separate systems for non-doc pages. Study those repos when in doubt.

### Three zones, clear boundaries

The site has **three distinct i18n zones**. Each zone has its own translation mechanism. **Never mix them.**

| Zone | Pages | i18n mechanism | String source |
|------|-------|---------------|---------------|
| **1 — Starlight docs** | `/docs/…`, `/zh-cn/docs/…` (all under `src/content/docs/`) | Starlight built-in i18n | `src/content/i18n/{locale}.json` + sidebar `translations` in config |
| **2 — Custom pages** | Landing (`/`, `/zh-cn/`), 404 | Project's own `src/i18n/` module | `src/i18n/ui.ts` + `src/i18n/landing-page.ts` |
| **3 — Blog** | `/posts/…`, `/zh-cn/posts/…` | Content collection + project `src/i18n/` | Post markdown in `src/content/posts/{locale}/`, UI strings in `ui.ts` |

### Zone 1 — Starlight docs i18n

Follows the **exact pattern** from [`withastro/starlight/docs`](https://github.com/withastro/starlight/tree/main/docs).

**Locale configuration** — `astro.config.mjs`:
- English is the **`root` locale** — content files live under `src/content/docs/docs/` (e.g., `docs/getting-started/installation.md`).
- Other locales get **subdirectories**: `src/content/docs/zh-cn/docs/getting-started/installation.md`.
- The inner `docs/` directory creates the `/docs/` URL prefix — Starlight 0.38 has no `routePrefix` option, so this nesting is the standard way to namespace docs routes.
- This keeps the URL scheme consistent: `/docs/…` for docs, `/posts/…` for blog.

```js
// astro.config.mjs
locales: {
  root: { label: 'English', lang: 'en' },      // ← root = no prefix for EN docs
  'zh-cn': { label: '简体中文', lang: 'zh-CN' },
},
```

**Starlight UI string overrides** — `src/content/i18n/{locale}.json`:
- File names use **BCP-47 language tags** (e.g., `zh-CN.json`, not `zh-cn.json`).
- Override Starlight's built-in UI strings (theme toggle, nav labels, pagination, 404 text, etc.).
- Extend with custom keys via `i18nSchema({ extend: z.object({...}) })` in `content.config.ts` (see [Starlight docs `content.config.ts`](https://github.com/withastro/starlight/blob/main/docs/src/content.config.ts) and [Astro docs `i18n-schema.ts`](https://github.com/withastro/docs/blob/main/src/content/i18n-schema.ts) for examples).
- **Do NOT** put these strings in `src/i18n/ui.ts` — that's Zone 2 only.

**Sidebar translations** — inline in `astro.config.mjs`:
- Uses the `translations` property on each sidebar group/item.
- Keys are **BCP-47 language tags** (e.g., `'zh-CN'`), NOT URL-slug locale keys.
- This matches the Starlight docs pattern where sidebar labels carry translation maps inline.

```js
sidebar: [{
  label: 'Getting Started',
  translations: { 'zh-CN': '快速开始' },
  autogenerate: { directory: 'docs/getting-started' },
}]
```

**Content schema** — `src/content.config.ts`:
- Uses `docsLoader()` + `docsSchema()` from `@astrojs/starlight/loaders` and `@astrojs/starlight/schema`.
- Uses `i18nLoader()` + `i18nSchema()` for the i18n collection.
- Extend the i18n schema with a Zod object if custom Starlight component overrides need translated keys.
- Extend the docs schema if docs need custom frontmatter fields.

### Zone 2 — Custom page i18n (`src/i18n/`)

For pages **outside Starlight** (landing page, 404). Starlight has no control over these pages.

**`[...locale]` rest-param routing** — All custom pages live under `src/pages/[...locale]/`. A single page file generates both locales via `getStaticPaths()`:

```ts
// In any page under src/pages/[...locale]/
export function getStaticPaths() {
  return Object.keys(languages).map((lang) => ({
    params: { locale: lang === defaultLang ? undefined : lang },
    props: { locale: lang as Locale },
  }));
}
// → generates /          (en, root)
// → generates /zh-cn/    (zh-cn)
```

**Never duplicate a page file per locale** — use this pattern instead. The 404 page is the only exception (lives at `src/pages/404.astro` root because Astro requires it there).

| File               | Purpose |
|--------------------|---------|
| `ui.ts`            | **Flat-key** UI string dictionary for shared UI: site meta, nav, footer, 404, post list, language switcher. English is source of truth; other locales override selectively. |
| `utils.ts`         | `getLangFromUrl()`, `useTranslations()`, `useTranslatedPath()`, `getNavProps()`, `getAlternateLocaleHref()` helpers. |
| `landing-page.ts`  | **Single source of truth for all landing-page copy** — both locales. Structured data (feature arrays with icons/colors, testimonials, hook stages, hero text, CTAs). |

> **Convention** (from Astro docs pattern): Landing-page components (Hero, Features, HookIntro, TapeModel, Testimonials) are **pure presentation** — they receive all text/content via props from `landing-page.ts`. They have no hardcoded text defaults. Non-landing UI strings (nav, footer, 404, posts) go in `ui.ts` and are accessed via `t()`.

### Zone 3 — Blog i18n

Blog posts are a **content collection** (`src/content/posts/{locale}/`). The collection schema includes a `locale` field to identify the language. Blog UI strings (page titles, back-links, etc.) live in `src/i18n/ui.ts` (Zone 2), not in Starlight's i18n.

### Boundary rules

| ✅ Do | ❌ Don't |
|-------|----------|
| Put Starlight UI overrides in `src/content/i18n/` | Duplicate Starlight keys into `src/i18n/ui.ts` |
| Put nav/footer/404/post-list strings in `src/i18n/ui.ts` | Put custom page strings in `src/content/i18n/` |
| Put landing-page structured copy in `src/i18n/landing-page.ts` | Put landing text in `ui.ts` or Starlight i18n |
| Use `[...locale]` rest-param pages for custom pages | Duplicate page files per locale (e.g., `zh-cn/posts/`) |
| Use `translations` on sidebar items in `astro.config.mjs` | Create separate sidebar translation files for 2 locales |
| Use BCP-47 tags (`zh-CN`) in Starlight config/i18n files | Use URL slugs (`zh-cn`) in Starlight i18n JSON filenames |
| Use URL slugs (`zh-cn`) in directory paths and page routes | Use BCP-47 tags in directory/route paths |

### Adding a UI string (non-landing pages)

1. Add the English string to `ui.ts` under the `en` object.
2. Add the translation under the target locale.
3. Use `const t = useTranslations(locale); t('your.key')` in any `.astro` file.

### Adding landing-page content

1. Add the English content to `landing-page.ts` under the `en` section.
2. Add the translation under the target locale section.
3. Pass as props from the page to the component via `getLandingPageCopy(locale)`.

### Adding a Starlight UI string override

1. Add the translated string to `src/content/i18n/zh-CN.json` (or the target locale file).
2. If the key is custom (not a built-in Starlight key), define it in the Zod extension inside `content.config.ts` via `i18nSchema({ extend: z.object({ 'your.key': z.string().optional() }) })`.
3. Reference: [Starlight i18n docs](https://starlight.astro.build/guides/i18n/).

### Adding a new locale

1. **Starlight zone**: Add the locale to `astro.config.mjs` `locales`. Create `src/content/docs/<locale>/docs/` with translated docs. Create `src/content/i18n/<BCP-47>.json` for UI overrides.
2. **Custom page zone**: Add the locale key to `languages` in `ui.ts`. Add translations in `ui.ts` and `landing-page.ts`. The `[...locale]` pages auto-generate routes via `getStaticPaths()`.
3. **Blog zone**: Create `src/content/posts/<locale>/` with translated posts.

### URL scheme

- English (default): **no prefix** — `/`, `/posts/`, `/docs/getting-started/`, etc.
- Chinese: `/zh-cn/`, `/zh-cn/posts/`, `/zh-cn/docs/getting-started/`, etc.
- Starlight docs have a `/docs/` prefix via the `docs/` content subdirectory.
- Custom pages (landing, blog) use `[...locale]` rest-param routing — one page file generates both `/` and `/zh-cn/` variants.

### Locale key conventions

| Context | Key format | Example |
|---------|-----------|---------|
| URL paths & directory names | lowercase slug | `zh-cn` |
| `astro.config.mjs` locale keys | lowercase slug | `'zh-cn': { ... }` |
| `astro.config.mjs` sidebar `translations` keys | BCP-47 | `'zh-CN': '快速开始'` |
| `src/content/i18n/` filenames | BCP-47 | `zh-CN.json` |
| HTML `lang` attribute | BCP-47 | `zh-CN` |
| `src/i18n/ui.ts` locale keys | lowercase slug | `'zh-cn': { ... }` |

### Adding a new page

**Custom page** (landing/blog style):
1. Create `src/pages/[...locale]/<path>.astro` with `getStaticPaths()` generating all locales.
2. Import and wrap with the appropriate layout (BaseLayout, PostLayout, etc.).
3. Extract text into `ui.ts` (Zone 2) with appropriate flat keys.
4. Run `pnpm build` to verify.

**Starlight doc page**:
1. Create the English file in `src/content/docs/docs/<section>/<slug>.md`.
2. Create the Chinese translation in `src/content/docs/zh-cn/docs/<section>/<slug>.md`.
3. Sidebar item is auto-generated if the parent group uses `autogenerate`. Otherwise add to `sidebar` in `astro.config.mjs` with `translations`.
4. Run `pnpm build` to verify.

### Adding a new component

1. Create in `src/components/`.
2. **No hardcoded text defaults** — components are pure presentation. Accept all text/content via props.
3. Let the caller pass content via `getLandingPageCopy()` or `t()` from the page level.
4. Add `data-reveal` if the component should animate on scroll.

### Common mistakes

| Mistake | Fix |
|---------|-----|
| Writing `<!doctype html>` in a page | Use BaseLayout |
| Hardcoding nav/footer props in a page | Use `getNavProps()` |
| Adding nested objects to `ui.ts` | Use flat `'section.key'` format |
| Using non-Lucide icon libraries | Use `@lucide/astro` via `Icon.astro` |
| Adding `dark:text-*` for themed colors | Use semantic tokens (`text-foreground`) that auto-switch |
| Duplicating scroll-reveal `<script>` | BaseLayout includes it |
| Putting landing text in `ui.ts` | Use `landing-page.ts` — `ui.ts` is for shared UI only |
| Hardcoding English in component defaults | Components are pure presentation; all text from i18n |
| Duplicating a page file per locale | Use `[...locale]` rest-param routing with `getStaticPaths()` |
| Putting custom page strings in `src/content/i18n/` | Zone 1 only. Custom pages use `src/i18n/` (Zone 2) |
| Putting Starlight overrides in `src/i18n/ui.ts` | Zone 2 only. Starlight uses `src/content/i18n/` (Zone 1) |
| Naming i18n JSON with URL slugs (`zh-cn.json`) | Use BCP-47 (`zh-CN.json`) for Starlight i18n files |
| Using BCP-47 in directory paths (`docs/zh-CN/`) | Use lowercase slugs (`docs/zh-cn/`) for directories |
| Using `'zh-cn'` in sidebar `translations` keys | Use BCP-47 (`'zh-CN'`) |
| Adding hue/chroma to Starlight color overrides | Site is monochrome — keep oklch with 0 chroma |
| Adding new `--sl-color-*` overrides without checking the existing unlayered block | Edit the existing `:root { --sl-color-* }` / `.dark, [data-theme="dark"] { --sl-color-* }` blocks — they override the bridge for correct monochrome contrast |
| Importing `@import "tailwindcss"` in `global.css` | Use `tailwindcss/theme.css` + `tailwindcss/utilities.css` in proper layers — full import brings Preflight that breaks Starlight/EC |

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
- Dark tokens in `.dark, [data-theme="dark"] { }` — both selectors are needed to cover landing pages (`.dark` class) and Starlight docs (`[data-theme]` attribute). `ThemeToggle.astro` sets both.
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

### Starlight + Tailwind v4 integration

Starlight ships with a blue accent (hue 224/234) and blue-tinted grays. The main site is **monochrome** (all oklch colors have `0` chroma). The bridge is `@astrojs/starlight-tailwind` — it reads `--color-accent-*` and `--color-gray-*` from the `@theme` block and generates `--sl-color-*` variables in `@layer utilities`. However, the bridge's mapping (e.g. `--sl-color-accent` ← `--color-accent-600`) does not produce the right contrast for a monochrome theme, so **manual unlayered `--sl-*` overrides are still required** for precise control. Unlayered CSS always beats `@layer utilities`.

**Reference:** [Starlight CSS + Tailwind guide](https://starlight.astro.build/guides/css-and-tailwind/)

**How `global.css` is structured (follow this order):**

```
1. Font imports (@import "@fontsource-variable/…")
2. Cascade layer ordering:  @layer base, starlight, theme, components, utilities;
3. Starlight bridge:        @import '@astrojs/starlight-tailwind';
4. Tailwind layers:         @import 'tailwindcss/theme.css' layer(theme);
                            @import 'tailwindcss/utilities.css' layer(utilities);
5. Animation utilities:     @import "tw-animate-css";  (unlayered — @utility can't nest)
6. @theme inline { … }     — fonts, Starlight color scales, site design tokens, radius
7. :root { … }             — raw light tokens (unlayered)
8. .dark, [data-theme="dark"] { … } — raw dark tokens (unlayered)
9. :root { --sl-font/color overrides } — unlayered to beat bridge @layer utilities
10. .dark, [data-theme="dark"] { --sl-color-* overrides }
11. @layer base { … }       — Tailwind preflight + site base resets (lowest priority)
```

**Why this order matters:**

| Layer | Contains | Priority |
|---|---|---|
| `@layer base` | Site resets (`* { border-border }`, body bg/text, etc.) | Lowest |
| `@layer starlight` | Starlight + expressive-code styles | Overrides `base` |
| `@layer theme` | Tailwind theme variables | Overrides `starlight` |
| `@layer utilities` | Tailwind utilities | Overrides `theme` |
| Unlayered CSS | Raw tokens, `--sl-font-*` / `--sl-color-*` overrides, `@media` queries | Highest |

**Key rules:**

- **NEVER use `@import "tailwindcss"`** — it brings in the full Preflight reset that conflicts with Starlight and astro-expressive-code. Only import `tailwindcss/theme.css` and `tailwindcss/utilities.css` in their proper layers.
- **Define Starlight colors via `@theme` scales** — `--color-accent-50` through `--color-accent-950` and `--color-gray-50` through `--color-gray-950`. The bridge reads these and generates `--sl-color-*` in `@layer utilities`.
- **Override `--sl-*` colors and fonts manually (unlayered)** — the bridge's auto-mapped values don't produce the right contrast for the monochrome theme. Unlayered `:root` / `.dark, [data-theme="dark"]` blocks with explicit `--sl-color-*` values win over the bridge's `@layer utilities` output.
- **Import `tailwindcss/preflight.css` in `@layer base`** — restores box-sizing, link resets, and other base styles that the split Tailwind import omits. Because `base` is the lowest layer, EC and Starlight styles still override it.
- **`tw-animate-css` must be imported unlayered** — it contains `@utility` directives that cannot be nested inside `@layer`.
- **The `@layer base` `*` reset is safe** — because EC styles live in `@layer starlight.components` (higher priority), they always win.

**Starlight color scales (in `@theme`):**

Both scales use pure neutral oklch values (0 chroma) to match the site's monochrome identity:

| Scale | Example values | Starlight usage |
|---|---|---|
| `--color-accent-50` … `--color-accent-950` | oklch(0.985 0 0) → oklch(0.145 0 0) | Links, highlights, active nav items |
| `--color-gray-50` … `--color-gray-950` | oklch(0.985 0 0) → oklch(0.145 0 0) | Backgrounds, text, borders |

> **Do NOT** add hue or chroma to these scales — the entire site identity is neutral/monochrome. Aside callout colors (orange, green, blue, purple, red) are intentionally left at Starlight defaults for semantic clarity.

**How to customize Starlight appearance going forward:**

1. To change Starlight's gray palette → update `--color-gray-*` values in `@theme inline`.
2. To change Starlight's accent color → update `--color-accent-*` values in `@theme inline`.
3. To change fonts → update `--font-sans` / `--font-mono` in `@theme inline` AND `--sl-font` / `--sl-font-mono` in the unlayered `:root` block.
4. To add site-specific overrides that should beat Starlight → put them **unlayered** (outside any `@layer`).
5. To add base resets that Starlight/EC can override → put them in `@layer base`.

**Overriding expressive-code on custom pages (e.g., Hero):**

When using `<Code />` from `astro-expressive-code` outside Starlight docs, override EC's CSS custom properties in the component's scoped `<style>` block:

```css
.hero-terminal :global(.expressive-code) {
    --ec-codeBg: var(--card);
    --ec-frm-edTabBarBg: var(--secondary);
    /* … etc. — see Hero.astro for the full list */
}
```

Scoped Astro styles are unlayered, so they beat both `@layer base` and `@layer starlight`.

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
4. Ensure all user-visible strings use the correct zone:
   - **Zone 1** (Starlight docs): UI overrides in `src/content/i18n/`, sidebar translations inline in config.
   - **Zone 2** (Custom pages): Landing copy → `landing-page.ts`, shared UI strings → `ui.ts`.
   - **Zone 3** (Blog): Post content in `src/content/posts/{locale}/`, UI strings in `ui.ts`.
   - Never duplicate text across zones.

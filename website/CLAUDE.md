# CLAUDE.md — AI Coding Guidelines for Bub Website

> Specific instructions for Claude / LLM-based coding agents working in `website/`.
> Read **AGENTS.md** first for full project context.

---

## Quick Reference

```bash
pnpm dev              # start dev server
pnpm build            # production build (MUST pass before committing)
```

---

## Key Rules

### 1. Always compose on BaseLayout

Every page must use `BaseLayout.astro` (directly or via a child layout). **Never** write a raw `<!doctype html>` / `<head>` / `<body>` in a page or layout — that's BaseLayout's job.

```astro
---
import BaseLayout from '../layouts/BaseLayout.astro';
---
<BaseLayout title="My Page" description="…">
  <main id="main-content">…</main>
</BaseLayout>
```

### 2. i18n — two files, clear boundaries

**Shared UI strings** (nav, footer, 404, posts, site meta) → `src/i18n/ui.ts` with **flat dot-separated keys**:

```ts
// ✅ Good — in ui.ts
'nav.docs': 'Docs',
'404.title': 'Page not found.',

// ❌ Bad — nested objects
nav: { docs: 'Docs' }
```

**Landing-page copy** (hero, features, hook stages, testimonials) → `src/i18n/landing-page.ts`:

```ts
// ✅ Good — in landing-page.ts, structured data
const copy = getLandingPageCopy(locale);
<Hero {...copy.hero} />

// ❌ Bad — duplicating landing text in ui.ts
'hero.badge': 'Hook-first · Tape-driven'  // dead key, never read by t()
```

Use `t()` for shared UI, `getLandingPageCopy()` for landing pages:

```astro
---
import { getLangFromUrl, useTranslations } from '../i18n/utils';
import { getLandingPageCopy } from '../i18n/landing-page';
const locale = getLangFromUrl(Astro.url);
const t = useTranslations(locale);        // nav, footer, 404, posts
const copy = getLandingPageCopy(locale);   // landing page sections
---
```

### 3. Never hardcode nav/footer props

Use `getNavProps(locale, pathname)` from `i18n/utils.ts`. BaseLayout already handles this automatically.

### 4. Icons via Lucide only

```astro
import Icon from '../components/ui/Icon.astro';
<Icon name="arrowUpRight" size={15} />
```

- Names are **camelCase** (e.g., `radioTower`, `fileSearch`).
- Browse: https://lucide.dev/icons
- The `github` icon is a custom SVG inside `Icon.astro`.

### 5. Styling with Tailwind + theme tokens

- Use Tailwind utilities referencing CSS custom property tokens: `text-foreground`, `bg-primary`, `border-border`, etc.
- For dark mode, tokens switch automatically via the `.dark` class — **do not** use `dark:` prefix for token-based colors.
- Use `dark:` only for asset switching (e.g., `dark:hidden` / `dark:block` for logo variants).

### 6. Scroll reveal

Add `data-reveal` to any element that should fade-in on scroll. BaseLayout handles the animation script.

### 7. Content vs UI strings

| What | Where |
|------|-------|
| Nav labels, footer, 404 copy, post list, meta titles | `src/i18n/ui.ts` (flat keys, via `t()`) |
| Landing-page sections (hero, features, hook stages, testimonials) | `src/i18n/landing-page.ts` (structured, via `getLandingPageCopy()`) |
| Starlight docs UI overrides | `src/content/i18n/zh-CN.json` |
| Docs content | `src/content/docs/{en,zh-cn}/` |
| Blog posts | `src/content/posts/{en,zh-cn}/` |

> **Never duplicate** text between `ui.ts` and `landing-page.ts`. Each string has exactly one source of truth.

### 8. Adding a new page

1. Create `src/pages/<path>.astro` (or `src/pages/zh-cn/<path>.astro` for Chinese).
2. Import and wrap with the appropriate layout (BaseLayout, PostLayout, etc.).
3. Extract any text into `ui.ts` with appropriate flat keys.
4. Run `pnpm build` to verify.

### 9. Adding a new component

1. Create in `src/components/`.
2. **No hardcoded text defaults** — components are pure presentation. Accept all text/content via props.
3. Let the caller pass content via `getLandingPageCopy()` or `t()` from the page level.
4. Add `data-reveal` if the component should animate on scroll.

---

## Locale Mapping

| Locale key | URL prefix | HTML lang | Display name |
|------------|-----------|-----------|-------------|
| `en`       | _(none)_  | `en`      | English     |
| `zh-cn`    | `/zh-cn/` | `zh-CN`   | 简体中文      |

---

## File Organization Reminders

- `src/i18n/ui.ts` — **single source of truth** for shared UI strings (nav, footer, 404, posts).
- `src/i18n/landing-page.ts` — **single source of truth** for all landing-page copy (both locales).
- `src/content/i18n/` — Starlight-only UI overrides. Never duplicated into `src/i18n/`.
- `src/layouts/BaseLayout.astro` — **single source of truth** for the HTML shell.
- `src/components/ui/Icon.astro` — **single icon component**, all icons go through this.
- `src/styles/global.css` — **single source of truth** for design tokens and base styles.

---

## Common Mistakes to Avoid

| Mistake | Fix |
|---------|-----|
| Writing `<!doctype html>` in a page | Use BaseLayout |
| Hardcoding `docsHref`, `blogHref`, etc. in a page | Use `getNavProps()` |
| Adding nested objects to `ui.ts` | Use flat `'section.key'` format |
| Using `@heroicons` or other icon libraries | Use `@lucide/astro` via `Icon.astro` |
| Adding `dark:text-*` for themed colors | Use semantic tokens (`text-foreground`) that auto-switch |
| Duplicating scroll-reveal `<script>` in layouts | BaseLayout includes it |
| Putting landing-page text in `ui.ts` | Use `landing-page.ts` — `ui.ts` is only for shared UI strings |
| Hardcoding English text as component prop defaults | Components are pure presentation; all text comes from i18n files |

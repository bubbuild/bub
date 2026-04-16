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

### 2. Use flat-key i18n — no nesting, no hardcoded strings

All user-visible text goes in `src/i18n/ui.ts` with **flat dot-separated keys**.

```ts
// ✅ Good
'nav.docs': 'Docs',
'hero.cta': 'Get Started',

// ❌ Bad — nested objects
nav: { docs: 'Docs' }
```

Use the `t()` helper in components:

```astro
---
import { getLangFromUrl, useTranslations } from '../i18n/utils';
const locale = getLangFromUrl(Astro.url);
const t = useTranslations(locale);
---
<h1>{t('hero.title')}</h1>
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
| Nav labels, button text, 404 copy, meta titles | `src/i18n/ui.ts` (flat keys) |
| Landing section content (features array, testimonials, hook stages) | `src/i18n/landing-page.ts` |
| Starlight docs UI overrides | `src/content/i18n/zh-CN.json` |
| Docs content | `src/content/docs/{en,zh-cn}/` |
| Blog posts | `src/content/posts/{en,zh-cn}/` |

### 8. Adding a new page

1. Create `src/pages/<path>.astro` (or `src/pages/zh-cn/<path>.astro` for Chinese).
2. Import and wrap with the appropriate layout (BaseLayout, PostLayout, etc.).
3. Extract any text into `ui.ts` with appropriate flat keys.
4. Run `pnpm build` to verify.

### 9. Adding a new component

1. Create in `src/components/`.
2. Accept content via props with English defaults.
3. Let the caller pass translated content via `t()` or section copy objects.
4. Add `data-reveal` if the component should animate on scroll.

---

## Locale Mapping

| Locale key | URL prefix | HTML lang | Display name |
|------------|-----------|-----------|-------------|
| `en`       | _(none)_  | `en`      | English     |
| `zh-cn`    | `/zh-cn/` | `zh-CN`   | 简体中文      |

---

## File Organization Reminders

- `src/i18n/ui.ts` — **single source of truth** for all UI strings.
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

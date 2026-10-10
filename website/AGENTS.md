# Website

Astro builds this site. Starlight serves the docs. The landing page, the blog, and the 404 page are site pages. The package manager is pnpm.

In `website/`, run `pnpm install`, `pnpm dev`, `pnpm build`, or `pnpm preview`. From the repository root, `make docs`, `make docs-test`, and `make docs-preview` call these scripts.

## Files

English URLs have no prefix. Chinese URLs use the prefix `/zh-cn/`.

Starlight uses a root locale for English. A root locale requires `lang`. Do not add an `en/` directory for the docs.

The docs use the `/docs/` subpath. Put English docs in `src/content/docs/docs/`. Put Chinese docs in `src/content/docs/zh-cn/docs/`. Use the same path under each locale. Starlight serves the default locale when a translation is missing.

| Content | Path |
|---|---|
| Blog posts | `src/content/posts/en/` and `src/content/posts/zh-cn/` |
| Landing page and blog routes | `src/pages/[...locale]/` |
| 404 page | `src/pages/404.astro` |
| Starlight UI strings | `src/content/i18n/zh-CN.json` |
| Nav, footer, 404, posts, site meta | `src/i18n/ui.ts` |
| Landing sections | `src/data/landing-page.ts` |

Site pages take their prefix from `src/i18n/utils.ts`. Do not put landing text in `ui.ts`. `ui.ts` uses flat keys (`section.key`). A missing key uses the English text.

`Astro.locals.t` reads Starlight UI strings. `Astro.locals.starlightRoute` throws on a site page. On site pages, call `useTranslations()`.

The landing page sets `prerender` to `false`. The blog routes are static. Starlight docs stay pre-rendered. Keep `disable404Route: true`.

A new locale needs all of these items:

- a Starlight `locales` entry with a BCP-47 `lang`
- a `src/content/docs/<slug>/docs/` tree with the same paths as English
- a BCP-47 file in `src/content/i18n/`
- a `languages` entry and its strings in `ui.ts`
- a record in `landing-page.ts`
- a `src/content/posts/<slug>/` directory

## Limits

Do not set Astro `i18n` in `astro.config.mjs`. This project sets Starlight `locales`. Starlight rejects both configurations. Starlight rejects Astro `routing: "manual"`.

Keep the `docs` collection on `docsLoader()` and `docsSchema()`. Keep the `i18n` collection on `i18nLoader()` and `i18nSchema()`.

In `src/styles/global.css`, keep this setup:

```css
@layer base, starlight, theme, components, utilities;
@import "@astrojs/starlight-tailwind";
@import "tailwindcss/theme.css" layer(theme);
@import "tailwindcss/utilities.css" layer(utilities);
```

Do not import `tailwindcss` in that file. That import adds Preflight. Preflight overrides Starlight styles.

Docs colors are the unlayered `--sl-color-*` block in `src/styles/global.css`. The `--color-gray-*` and `--color-accent-*` scales do not change those colors.

Astro 7 sets the default of `compressHTML` to `'jsx'`. This config sets `compressHTML` to `true`. That value keeps one space between inline elements. Do not remove it.

Close every non-void tag. Do not add `src/fetch.ts`. Astro reserves that file name.

Build site pages with `BaseLayout`. Do not copy the document shell, the nav, or the footer.

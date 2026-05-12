// @ts-check
import { fileURLToPath } from 'node:url';
import { defineConfig, envField } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';
import starlight from '@astrojs/starlight';
import cloudflare from '@astrojs/cloudflare';
import mermaid from 'astro-mermaid';

const astro_image_mode =
  process.env.BUB_ASTRO_IMAGE_MODE ?? (process.argv.includes('dev') ? 'dev' : 'build');

/** @type {import('@astrojs/cloudflare').Options['imageService']} */
const image_service =
  astro_image_mode === 'dev' ? 'cloudflare' : { build: 'compile', runtime: 'passthrough' };

export default defineConfig({
  // SSG by default; landing pages opt-in to SSR via `export const prerender = false`.
  adapter: cloudflare({
    // Prefer an explicit mode from the calling command so local docs workflows
    // stay deterministic. Fall back to the Astro command name for direct
    // `pnpm dev` / `pnpm build` usage inside `website/`.
    imageService: image_service,
    prerenderEnvironment: 'node',
  }),
  site: process.env.SITE_URL ?? 'https://bub.build',
  env: {
    schema: {
      SITE_URL: envField.string({
        context: 'client',
        access: 'public',
        default: 'https://bub.build',
        optional: true,
        url: true,
      }),
    },
  },
  vite: {
    plugins: [tailwindcss()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
  },
  integrations: [
    // Must come BEFORE starlight so its remark plugin transforms `mermaid`
    // code blocks before Starlight's markdown pipeline highlights them.
    mermaid({
      theme: 'neutral',
      autoTheme: true,
    }),
    starlight({
      title: 'Bub',
      description: 'A common shape for agents that live alongside people.',
      expressiveCode: false,
      logo: {
        light: './src/assets/bub-logo.png',
        dark: './src/assets/bub-logo-dark.png',
        alt: 'Bub',
      },
      // Use the resolved file URL so Vite's module graph reliably
      // includes global.css on every Starlight docs page in dev mode.
      // (Astro v6 scopes CSS per-page from the import graph; with a
      // relative path Vite occasionally fails to resolve / dedupe in
      // dev, leaving the docs route unstyled even though
      // `astro build` + preview both work.)
      customCss: [fileURLToPath(new URL('./src/styles/global.css', import.meta.url))],
      disable404Route: true,
      locales: {
        root: {
          label: 'English',
          lang: 'en',
        },
        'zh-cn': {
          label: '简体中文',
          lang: 'zh-CN',
        },
      },
      social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/bubbuild/bub' }],
      sidebar: [
        {
          label: 'Getting Started',
          translations: { 'zh-CN': '快速开始' },
          autogenerate: { directory: 'docs/getting-started' },
        },
        {
          label: 'Concepts',
          translations: { 'zh-CN': '概念' },
          autogenerate: { directory: 'docs/concepts' },
        },
        {
          label: 'Operate',
          translations: { 'zh-CN': '运行' },
          autogenerate: { directory: 'docs/operate' },
        },
        {
          label: 'Build',
          translations: { 'zh-CN': '构建' },
          autogenerate: { directory: 'docs/build' },
        },
        {
          label: 'Reference',
          translations: { 'zh-CN': '参考' },
          autogenerate: { directory: 'docs/reference' },
        },
      ],
    }),
  ],
});

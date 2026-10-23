// @ts-check
import { fileURLToPath } from 'node:url';
import { defineConfig, envField } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';
import starlight from '@astrojs/starlight';
import cloudflare from '@astrojs/cloudflare';
import headingAnchors from './src/integrations/heading-anchors.mjs';
import peleDiagrams from './src/integrations/pele.mjs';

const astro_image_mode =
  process.env.BUB_ASTRO_IMAGE_MODE ?? (process.argv.includes('dev') ? 'dev' : 'build');

/** @type {import('@astrojs/cloudflare').Options['imageService']} */
const image_service =
  astro_image_mode === 'dev' ? 'cloudflare' : { build: 'compile', runtime: 'passthrough' };

export default defineConfig({
  // Preserve HTML-aware spacing between inline elements after the Astro 7 upgrade.
  compressHTML: true,
  // The site has no per-user state; avoid provisioning an unused SESSION KV namespace.
  session: false,
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
    // Keep the dependency cache of `astro build` away from the one `astro dev`
    // uses. When both share `node_modules/.vite`, every build rewrites the
    // optimized deps and a running dev server answers with "There is a new
    // version of the pre-bundle" instead of hot-reloading.
    cacheDir:
      process.env.VITE_CACHE_DIR ??
      (astro_image_mode === 'dev' ? 'node_modules/.vite' : 'node_modules/.vite-build'),
    plugins: [tailwindcss()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
  },
  integrations: [
    // Let headings carry an explicit id so every locale shares the same anchor.
    headingAnchors(),
    // Transform Mermaid fences before Starlight highlights code blocks.
    peleDiagrams(),
    starlight({
      title: 'Bub',
      description: 'Build an agent in Python or customize the Bub application.',
      expressiveCode: false,
      favicon: '/favicon.ico',
      head: [
        {
          tag: 'link',
          attrs: {
            rel: 'icon',
            type: 'image/png',
            href: '/favicon-light.png',
            sizes: '64x64',
            media: '(prefers-color-scheme: light)',
            'data-favicon-theme': 'light',
          },
        },
        {
          tag: 'link',
          attrs: {
            rel: 'icon',
            type: 'image/png',
            href: '/favicon-dark.png',
            sizes: '64x64',
            media: '(prefers-color-scheme: dark)',
            'data-favicon-theme': 'dark',
          },
        },
        {
          tag: 'script',
          attrs: {
            src: '/favicon-theme.js',
            defer: true,
          },
        },
      ],
      logo: {
        light: './src/assets/bub-logo.png',
        dark: './src/assets/bub-logo-dark.png',
        alt: 'Bub',
      },
      // Use the resolved file URL so Vite's module graph reliably
      // includes global.css on every Starlight docs page in dev mode.
      // (Astro scopes CSS per-page from the import graph; with a
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
          label: 'Get started',
          translations: { 'zh-CN': '开始' },
          items: ['docs/getting-started', 'docs/getting-started/install', 'docs/getting-started/quickstart'],
        },
        {
          label: 'Concepts',
          translations: { 'zh-CN': '核心概念' },
          items: [
            'docs/concepts',
            'docs/concepts/turn',
            'docs/concepts/tape',
            'docs/concepts/events',
            'docs/concepts/extension-points',
          ],
        },
        {
          label: 'Use Bub App',
          translations: { 'zh-CN': '使用 Bub 应用' },
          items: [
            {
              label: 'Run',
              translations: { 'zh-CN': '运行' },
              collapsed: false,
              items: [
                'docs/app/workspace',
                'docs/app/configure',
                'docs/app/terminal',
                'docs/app/channels',
              ],
            },
            {
              label: 'Extend',
              translations: { 'zh-CN': '扩展' },
              collapsed: false,
              items: [
                'docs/app/tools',
                'docs/app/skills',
                'docs/app/plugins',
                'docs/app/hooks',
              ],
            },
            {
              label: 'Ship',
              translations: { 'zh-CN': '上线' },
              collapsed: false,
              items: [
                'docs/app/deploy',
                'docs/app/distribution',
              ],
            },
          ],
        },
        {
          label: 'Build your own Bub Agent',
          translations: { 'zh-CN': '构建你的 Bub Agent' },
          items: [
            'docs/sdk',
            'docs/sdk/options',
            'docs/sdk/tools',
            'docs/sdk/reuse-plugins',
            'docs/sdk/web-service',
          ],
        },
        {
          label: 'Integrations',
          translations: { 'zh-CN': '集成' },
          items: [
            'docs/integrations',
            'docs/integrations/mcp',
            'docs/integrations/agent-plugins',
            'docs/integrations/acp',
            'docs/integrations/local-models',
            'docs/integrations/storage',
            'docs/integrations/observability',
          ],
        },
        {
          label: 'Reference',
          translations: { 'zh-CN': '参考' },
          items: [
            'docs/reference/cli',
            'docs/reference/python-api',
            'docs/reference/settings',
            'docs/reference/hooks',
          ],
        },
        {
          label: 'Migration',
          translations: { 'zh-CN': '迁移' },
          items: ['docs/migration/v0-6'],
        },
      ],
    }),
  ],
});

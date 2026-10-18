// @ts-check
import { fileURLToPath } from 'node:url';
import { defineConfig, envField } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';
import starlight from '@astrojs/starlight';
import cloudflare from '@astrojs/cloudflare';
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
    plugins: [tailwindcss()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
  },
  integrations: [
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
          label: 'Start here',
          translations: { 'zh-CN': '从这里开始' },
          items: ['docs/getting-started', 'docs/getting-started/install'],
        },
        {
          label: 'Build your own agent',
          translations: { 'zh-CN': '构建自己的 Agent' },
          items: ['docs/build/sdk', 'docs/build/reuse-plugins', 'docs/tutorials/fastapi'],
        },
        {
          label: 'Customize the Bub app',
          translations: { 'zh-CN': '定制 Bub 应用' },
          items: [
            'docs/build',
            'docs/getting-started/first-turn',
            'docs/concepts/turn-pipeline',
            {
              label: 'Extend the app',
              translations: { 'zh-CN': '扩展应用' },
              collapsed: true,
              items: [
                'docs/build/app-tools',
                'docs/getting-started/first-skill',
                'docs/build/skills',
                'docs/getting-started/first-plugin',
                'docs/build/plugins',
                'docs/build/hooks',
                'docs/build/distribution',
              ],
            },
            {
              label: 'Configure and run',
              translations: { 'zh-CN': '配置与运行' },
              collapsed: true,
              items: [
                'docs/operate',
                'docs/operate/configure',
                'docs/operate/channels',
                'docs/operate/channels/cli',
                'docs/operate/channels/telegram',
                'docs/operate/deploy',
              ],
            },
            {
              label: 'Integrations',
              translations: { 'zh-CN': '接入集成' },
              collapsed: true,
              items: [
                {
                  slug: 'docs/tutorials',
                  label: 'Integration index',
                  translations: { 'zh-CN': '集成索引' },
                },
                'docs/tutorials/mcp',
                'docs/tutorials/agent-plugins',
                'docs/tutorials/language-extensions',
                'docs/tutorials/acp-server',
                'docs/tutorials/tapestore-sqlalchemy',
                'docs/tutorials/observability',
                'docs/tutorials/local-llama-cpp',
              ],
            },
          ],
        },
        {
          label: 'Shared concepts and capabilities',
          translations: { 'zh-CN': '公共概念与能力' },
          items: [
            'docs/concepts',
            'docs/build/tools',
            'docs/concepts/tape-and-context',
            'docs/concepts/events',
            'docs/concepts/surfaces',
            'docs/concepts/philosophy',
          ],
        },
        {
          label: 'Reference',
          translations: { 'zh-CN': '参考' },
          items: [
            {
              slug: 'docs/reference',
              label: 'Agent and app APIs',
              translations: { 'zh-CN': 'Agent 与应用接口' },
            },
            {
              label: 'Application reference',
              translations: { 'zh-CN': '应用参考' },
              items: [
                'docs/reference/hooks',
                'docs/reference/cli',
                'docs/reference/settings',
                'docs/reference/types',
              ],
            },
            'docs/reference/versions',
          ],
        },
      ],
    }),
  ],
});

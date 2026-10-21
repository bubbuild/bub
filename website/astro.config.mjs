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
  // Documentation moved in the 0.6 documentation restructure. Keep old URLs working.
  redirects: {
    '/docs': '/docs/getting-started/',
    '/docs/getting-started/first-turn': '/docs/app/first-session/',
    '/docs/getting-started/first-skill': '/docs/app/skills/',
    '/docs/getting-started/first-plugin': '/docs/app/plugins/',
    '/docs/build': '/docs/app/',
    '/docs/build/sdk': '/docs/sdk/',
    '/docs/build/tools': '/docs/sdk/tools/',
    '/docs/build/app-tools': '/docs/app/tools/',
    '/docs/build/skills': '/docs/app/skills/',
    '/docs/build/plugins': '/docs/app/plugins/',
    '/docs/build/hooks': '/docs/app/hooks/',
    '/docs/build/reuse-plugins': '/docs/sdk/app-plugins/',
    '/docs/build/distribution': '/docs/app/package/',
    '/docs/concepts/philosophy': '/docs/concepts/',
    '/docs/concepts/design': '/docs/concepts/',
    '/docs/concepts/surfaces': '/docs/concepts/extension-points/',
    '/docs/concepts/tape-and-context': '/docs/concepts/tape/',
    '/docs/concepts/turn-pipeline': '/docs/concepts/turn/',
    '/docs/operate': '/docs/app/',
    '/docs/operate/configure': '/docs/app/configure/',
    '/docs/operate/channels': '/docs/app/gateway/',
    '/docs/operate/channels/cli': '/docs/app/chat/',
    '/docs/operate/channels/telegram': '/docs/app/telegram/',
    '/docs/operate/deploy': '/docs/app/deploy/',
    '/docs/tutorials': '/docs/integrations/',
    '/docs/tutorials/mcp': '/docs/integrations/mcp/',
    '/docs/tutorials/acp-server': '/docs/integrations/acp/',
    '/docs/tutorials/agent-plugins': '/docs/integrations/agent-plugins/',
    '/docs/tutorials/fastapi': '/docs/sdk/web-service/',
    '/docs/tutorials/language-extensions': '/docs/app/language-extensions/',
    '/docs/tutorials/local-llama-cpp': '/docs/integrations/local-models/',
    '/docs/tutorials/tapestore-sqlalchemy': '/docs/integrations/storage/',
    '/docs/tutorials/observability': '/docs/integrations/observability/',
  },
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
            'docs/app',
            {
              label: 'Run it',
              translations: { 'zh-CN': '运行与部署' },
              collapsed: false,
              items: [
                'docs/app/first-session',
                'docs/app/configure',
                'docs/app/chat',
                'docs/app/gateway',
                'docs/app/telegram',
                'docs/app/deploy',
                'docs/app/package',
              ],
            },
            {
              label: 'Extend it',
              translations: { 'zh-CN': '扩展它' },
              collapsed: true,
              items: [
                'docs/app/tools',
                'docs/app/skills',
                'docs/app/plugins',
                'docs/app/hooks',
                'docs/app/language-extensions',
              ],
            },
          ],
        },
        {
          label: 'Build your own Bub Agent',
          translations: { 'zh-CN': '构建你的 Bub Agent' },
          items: [
            'docs/sdk',
            'docs/sdk/tools',
            'docs/sdk/web-service',
            'docs/sdk/app-plugins',
          ],
        },
        {
          label: 'Integrations',
          translations: { 'zh-CN': '集成' },
          items: [
            'docs/integrations',
            'docs/integrations/local-models',
            'docs/integrations/storage',
            'docs/integrations/observability',
            'docs/integrations/mcp',
            'docs/integrations/agent-plugins',
            'docs/integrations/acp',
          ],
        },
        {
          label: 'Reference',
          translations: { 'zh-CN': '参考' },
          items: [
            'docs/reference',
            'docs/reference/cli',
            'docs/reference/settings',
            'docs/reference/types',
            'docs/reference/hooks',
            'docs/reference/versions',
          ],
        },
      ],
    }),
  ],
});

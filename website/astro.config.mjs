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
    '/docs/app': '/docs/app/workspace/',
    '/docs/app/chat': '/docs/app/terminal/',
    '/docs/app/first-session': '/docs/app/workspace/',
    '/docs/app/gateway': '/docs/app/channels/',
    '/docs/app/language-extensions': '/docs/app/plugins/',
    '/docs/app/package': '/docs/app/distribution/',
    '/docs/app/telegram': '/docs/app/channels/',
    '/docs/build': '/docs/app/workspace/',
    '/docs/build/app-tools': '/docs/app/tools/',
    '/docs/build/distribution': '/docs/app/distribution/',
    '/docs/build/hooks': '/docs/app/hooks/',
    '/docs/build/plugins': '/docs/app/plugins/',
    '/docs/build/reuse-plugins': '/docs/sdk/app-plugins/',
    '/docs/build/sdk': '/docs/sdk/',
    '/docs/build/skills': '/docs/app/skills/',
    '/docs/build/tools': '/docs/sdk/tools/',
    '/docs/concepts/design': '/docs/concepts/',
    '/docs/concepts/philosophy': '/docs/concepts/',
    '/docs/concepts/surfaces': '/docs/concepts/extension-points/',
    '/docs/concepts/tape-and-context': '/docs/concepts/tape/',
    '/docs/concepts/turn-pipeline': '/docs/concepts/turn/',
    '/docs/getting-started/first-plugin': '/docs/app/plugins/',
    '/docs/getting-started/first-skill': '/docs/app/skills/',
    '/docs/getting-started/first-turn': '/docs/app/workspace/',
    '/docs/operate': '/docs/app/workspace/',
    '/docs/operate/channels': '/docs/app/channels/',
    '/docs/operate/channels/cli': '/docs/app/terminal/',
    '/docs/operate/channels/telegram': '/docs/app/channels/',
    '/docs/operate/configure': '/docs/app/configure/',
    '/docs/operate/deploy': '/docs/app/deploy/',
    '/docs/tutorials': '/docs/integrations/',
    '/docs/tutorials/acp-server': '/docs/integrations/acp/',
    '/docs/tutorials/agent-plugins': '/docs/integrations/agent-plugins/',
    '/docs/tutorials/fastapi': '/docs/sdk/web-service/',
    '/docs/tutorials/language-extensions': '/docs/app/language-extensions/',
    '/docs/tutorials/local-llama-cpp': '/docs/integrations/local-models/',
    '/docs/tutorials/mcp': '/docs/integrations/mcp/',
    '/docs/tutorials/observability': '/docs/integrations/observability/',
    '/docs/tutorials/tapestore-sqlalchemy': '/docs/integrations/storage/',
    '/zh-cn/docs': '/zh-cn/docs/getting-started/',
    '/zh-cn/docs/app': '/zh-cn/docs/app/workspace/',
    '/zh-cn/docs/app/chat': '/zh-cn/docs/app/terminal/',
    '/zh-cn/docs/app/first-session': '/zh-cn/docs/app/workspace/',
    '/zh-cn/docs/app/gateway': '/zh-cn/docs/app/channels/',
    '/zh-cn/docs/app/language-extensions': '/zh-cn/docs/app/plugins/',
    '/zh-cn/docs/app/package': '/zh-cn/docs/app/distribution/',
    '/zh-cn/docs/app/telegram': '/zh-cn/docs/app/channels/',
    '/zh-cn/docs/build': '/zh-cn/docs/app/workspace/',
    '/zh-cn/docs/build/app-tools': '/zh-cn/docs/app/tools/',
    '/zh-cn/docs/build/distribution': '/zh-cn/docs/app/distribution/',
    '/zh-cn/docs/build/hooks': '/zh-cn/docs/app/hooks/',
    '/zh-cn/docs/build/plugins': '/zh-cn/docs/app/plugins/',
    '/zh-cn/docs/build/reuse-plugins': '/zh-cn/docs/sdk/app-plugins/',
    '/zh-cn/docs/build/sdk': '/zh-cn/docs/sdk/',
    '/zh-cn/docs/build/skills': '/zh-cn/docs/app/skills/',
    '/zh-cn/docs/build/tools': '/zh-cn/docs/sdk/tools/',
    '/zh-cn/docs/concepts/design': '/zh-cn/docs/concepts/',
    '/zh-cn/docs/concepts/philosophy': '/zh-cn/docs/concepts/',
    '/zh-cn/docs/concepts/surfaces': '/zh-cn/docs/concepts/extension-points/',
    '/zh-cn/docs/concepts/tape-and-context': '/zh-cn/docs/concepts/tape/',
    '/zh-cn/docs/concepts/turn-pipeline': '/zh-cn/docs/concepts/turn/',
    '/zh-cn/docs/getting-started/first-plugin': '/zh-cn/docs/app/plugins/',
    '/zh-cn/docs/getting-started/first-skill': '/zh-cn/docs/app/skills/',
    '/zh-cn/docs/getting-started/first-turn': '/zh-cn/docs/app/workspace/',
    '/zh-cn/docs/operate': '/zh-cn/docs/app/workspace/',
    '/zh-cn/docs/operate/channels': '/zh-cn/docs/app/channels/',
    '/zh-cn/docs/operate/channels/cli': '/zh-cn/docs/app/terminal/',
    '/zh-cn/docs/operate/channels/telegram': '/zh-cn/docs/app/channels/',
    '/zh-cn/docs/operate/configure': '/zh-cn/docs/app/configure/',
    '/zh-cn/docs/operate/deploy': '/zh-cn/docs/app/deploy/',
    '/zh-cn/docs/tutorials': '/zh-cn/docs/integrations/',
    '/zh-cn/docs/tutorials/acp-server': '/zh-cn/docs/integrations/acp/',
    '/zh-cn/docs/tutorials/agent-plugins': '/zh-cn/docs/integrations/agent-plugins/',
    '/zh-cn/docs/tutorials/fastapi': '/zh-cn/docs/sdk/web-service/',
    '/zh-cn/docs/tutorials/language-extensions': '/zh-cn/docs/app/language-extensions/',
    '/zh-cn/docs/tutorials/local-llama-cpp': '/zh-cn/docs/integrations/local-models/',
    '/zh-cn/docs/tutorials/mcp': '/zh-cn/docs/integrations/mcp/',
    '/zh-cn/docs/tutorials/observability': '/zh-cn/docs/integrations/observability/',
    '/zh-cn/docs/tutorials/tapestore-sqlalchemy': '/zh-cn/docs/integrations/storage/',
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

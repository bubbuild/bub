// @ts-check
import { fileURLToPath } from 'node:url';
import { defineConfig, envField } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';
import starlight from '@astrojs/starlight';
import cloudflare from '@astrojs/cloudflare';

export default defineConfig({
  // SSG by default; landing pages opt-in to SSR via `export const prerender = false`.
  adapter: cloudflare({
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
    starlight({
      title: 'Bub',
      description: 'A common shape for agents that live alongside people.',
      expressiveCode: false,
      logo: {
        light: './src/assets/bub-logo.png',
        dark: './src/assets/bub-logo-dark.png',
        alt: 'Bub',
      },
      customCss: ['./src/styles/global.css'],
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
          label: 'Guides',
          translations: { 'zh-CN': '指南' },
          autogenerate: { directory: 'docs/guides' },
        },
        {
          label: 'Extending',
          translations: { 'zh-CN': '扩展' },
          autogenerate: { directory: 'docs/extending' },
        },
      ],
    }),
  ],
});

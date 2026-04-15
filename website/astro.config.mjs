// @ts-check
import { defineConfig } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://bub.build',
  vite: {
    plugins: [tailwindcss()],
  },
  integrations: [
    starlight({
      title: 'Bub',
      description: 'A common shape for agents that live alongside people.',
      customCss: ['./src/styles/global.css'],
      defaultLocale: 'en',
      locales: {
        en: {
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
          autogenerate: { directory: 'getting-started' },
        },
        {
          label: 'Concepts',
          translations: { 'zh-CN': '概念' },
          autogenerate: { directory: 'concepts' },
        },
        {
          label: 'Guides',
          translations: { 'zh-CN': '指南' },
          autogenerate: { directory: 'guides' },
        },
        {
          label: 'Extending',
          translations: { 'zh-CN': '扩展' },
          autogenerate: { directory: 'extending' },
        },
        {
          label: 'Blog',
          translations: { 'zh-CN': '博客' },
          autogenerate: { directory: 'blog' },
        },
      ],
    }),
  ],
});

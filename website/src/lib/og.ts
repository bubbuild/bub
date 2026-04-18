/**
 * Shared helpers for OG image generation.
 *
 * Collects page data from the posts content collection and builds a
 * `pages` object keyed by route path that `astro-og-canvas` expects.
 */
import { getCollection } from 'astro:content';
import type { OGImageOptions } from 'astro-og-canvas';

export interface PageMeta {
  title: string;
  description: string;
}

const FONT_DIR = './node_modules/@fontsource-variable';
const LOGO_PATH = './src/assets/bub-logo.png';

const fonts = [
  `${FONT_DIR}/outfit/files/outfit-latin-wght-normal.woff2`,
  `${FONT_DIR}/jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2`,
];

/** Collect every page that should receive an OG image. */
export async function collectPages(): Promise<Record<string, PageMeta>> {
  const posts = await getCollection('posts');

  const pages: Record<string, PageMeta> = {};

  for (const post of posts) {
    const route = `posts/${post.id.replace(/\.md$/, '')}`;
    pages[route] = {
      title: post.data.title,
      description: post.data.description,
    };
  }

  pages['index'] = {
    title: 'Bub',
    description: 'A common shape for agents that live alongside people.',
  };

  return pages;
}

/** Light Gradient — soft white-to-lavender gradient, dark text, indigo accent. */
export const styleLightGradient = (_path: string, page: PageMeta): OGImageOptions => ({
  title: page.title,
  description: page.description,
  dir: 'ltr',
  bgGradient: [[250, 250, 255], [235, 230, 250]],
  border: { color: [99, 102, 241], width: 12, side: 'inline-start' },
  logo: { path: LOGO_PATH, size: [80] },
  padding: 60,
  font: {
    title: {
      color: [30, 30, 46],
      size: 64,
      weight: 'Bold',
      families: ['Outfit'],
      lineHeight: 1.2,
    },
    description: {
      color: [88, 88, 120],
      size: 30,
      families: ['Outfit'],
      lineHeight: 1.5,
    },
  },
  fonts,
});

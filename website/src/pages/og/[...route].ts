/**
 * OG Image endpoint — generates a 1200×630 PNG for every page that
 * needs a social sharing card (blog posts + landing page).
 *
 * Images are available at `/og/<route>.png`, e.g. `/og/index.png`
 * or `/og/posts/en/socialized-evaluation.png`.
 */
import { OGImageRoute } from 'astro-og-canvas';
import { collectPages, styleLightGradient } from '@/lib/og';

const pages = await collectPages();

export const { getStaticPaths, GET } = await OGImageRoute({
  param: 'route',
  pages,
  getImageOptions: styleLightGradient,
});

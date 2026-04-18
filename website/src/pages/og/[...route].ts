/**
 * OG Image endpoint — generates a 1200×630 PNG for every page that
 * needs a social sharing card (blog posts + landing page).
 *
 * Images are available at `/og/<route>.png`, e.g. `/og/index.png`
 * or `/og/posts/en/socialized-evaluation.png`.
 */
import type { APIRoute, GetStaticPaths } from 'astro';
import { collectPages, loadFonts, generateOgImage } from '@/lib/og';

const pages = await collectPages();

const allText = Object.values(pages)
  .map((p) => `${p.title} ${p.description}`)
  .join('');

const fonts = loadFonts(allText);

export const getStaticPaths: GetStaticPaths = () =>
  Object.keys(pages).map((route) => ({ params: { route: `${route}.png` } }));

export const GET: APIRoute = async ({ params }) => {
  const route = params.route!.replace(/\.png$/, '');
  const page = pages[route];

  if (!page) return new Response('Not found', { status: 404 });

  const png = await generateOgImage(page, fonts);
  return new Response(png, {
    headers: {
      'Content-Type': 'image/png',
      'Cache-Control': 'public, max-age=31536000, immutable',
    },
  });
};

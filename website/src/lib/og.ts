/**
 * OG image generation using Satori + Sharp.
 *
 * Fonts are loaded from @fontsource packages at build time — the same
 * source used for site CSS in global.css.  Satori reads `.woff` files
 * directly (it supports WOFF but not WOFF2).  For Noto Sans SC (CJK),
 * only the unicode-range subsets actually needed by page text are loaded.
 */
import satori from 'satori';
import sharp from 'sharp';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { getCollection } from 'astro:content';

/* ─── Types ─── */

export interface PageMeta {
  title: string;
  description: string;
}

interface FontEntry {
  name: string;
  data: ArrayBuffer;
  weight: number;
  style: 'normal';
}

export interface FontsResult {
  entries: FontEntry[];
  /** CSS font-family value covering all loaded subsets. */
  fontFamily: string;
}

/* ─── Font loading (@fontsource packages) ─── */

const OUTFIT_DIR = 'node_modules/@fontsource/outfit/files';
const NOTO_DIR = 'node_modules/@fontsource/noto-sans-sc/files';
const NOTO_UNICODE = 'node_modules/@fontsource/noto-sans-sc/unicode.json';

/** Read a `.woff` file and return its data as an ArrayBuffer. */
function readWoff(path: string): ArrayBuffer {
  const buf = readFileSync(path);
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
}

/** Parse fontsource unicode-range string into [lo, hi] pairs. */
function parseUnicodeRange(range: string): Array<[number, number]> {
  return range
    .split(',')
    .map((part) => {
      const m = part.trim().match(/^U\+([0-9A-Fa-f]+)(?:-([0-9A-Fa-f]+))?$/);
      if (!m) return null;
      return [parseInt(m[1], 16), m[2] ? parseInt(m[2], 16) : parseInt(m[1], 16)] as [number, number];
    })
    .filter(Boolean) as Array<[number, number]>;
}

/** Find which fontsource subset IDs are needed to cover `text`. */
function findNeededSubsets(text: string, unicodeJson: Record<string, string>): string[] {
  const parsed = Object.entries(unicodeJson).map(([key, range]) => ({
    id: key.replace(/^\[|\]$/g, ''),
    ranges: parseUnicodeRange(range),
  }));

  const needed = new Set<string>();
  for (const ch of new Set(text)) {
    const cp = ch.codePointAt(0)!;
    for (const { id, ranges } of parsed) {
      if (ranges.some(([lo, hi]) => cp >= lo && cp <= hi)) {
        needed.add(id);
        break;
      }
    }
  }
  return [...needed];
}

/**
 * Load the minimal set of fonts needed for all OG cards.
 *
 * Outfit (Latin) from @fontsource/outfit,
 * Noto Sans SC (CJK) from @fontsource/noto-sans-sc.
 * Only the unicode-range subsets covering `allText` are loaded.
 *
 * Each CJK subset gets a unique font name because satori deduplicates
 * entries with the same (name, weight, style) — keeping only the first.
 */
export function loadFonts(allText: string): FontsResult {
  const WEIGHTS = [400, 700] as const;
  const fonts: FontEntry[] = [];
  const families: string[] = ['Outfit'];

  // Outfit — latin + latin-ext subsets at each weight
  for (const weight of WEIGHTS) {
    for (const subset of ['latin', 'latin-ext']) {
      fonts.push({
        name: 'Outfit',
        data: readWoff(join(OUTFIT_DIR, `outfit-${subset}-${weight}-normal.woff`)),
        weight,
        style: 'normal',
      });
    }
  }

  // Noto Sans SC — only subsets covering characters beyond Basic Latin
  const cjkChars = [...new Set(allText)].filter((ch) => ch.codePointAt(0)! > 0x024f).join('');
  if (cjkChars) {
    const unicodeJson: Record<string, string> = JSON.parse(readFileSync(NOTO_UNICODE, 'utf-8'));
    const subsetIds = findNeededSubsets(cjkChars, unicodeJson);
    for (const id of subsetIds) {
      const familyName = `NotoSC-${id}`;
      families.push(familyName);
      for (const weight of WEIGHTS) {
        fonts.push({
          name: familyName,
          data: readWoff(join(NOTO_DIR, `noto-sans-sc-${id}-${weight}-normal.woff`)),
          weight,
          style: 'normal',
        });
      }
    }
  }

  return { entries: fonts, fontFamily: families.join(', ') };
}

/* ─── Page collection ─── */

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

  pages['zh-cn/index'] = {
    title: 'Bub',
    description: '与 Human 同在的轻量级 Agent 运行时。',
  };

  return pages;
}

/* ─── Card template & image generation ─── */

const LOGO_PATH = './src/assets/bub-logo.png';

let logoCache: string | null = null;
function getLogoDataUri(): string {
  if (!logoCache) {
    logoCache = `data:image/png;base64,${readFileSync(LOGO_PATH).toString('base64')}`;
  }
  return logoCache;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function cardMarkup(title: string, description: string, logoSrc: string, fontFamily: string): any {
  return {
    type: 'div',
    props: {
      style: {
        width: '100%',
        height: '100%',
        display: 'flex',
        backgroundImage: 'linear-gradient(135deg, rgb(250,250,252), rgb(235,238,248))',
      },
      children: [
        {
          type: 'div',
          props: {
            style: {
              display: 'flex',
              flexDirection: 'column',
              justifyContent: 'center',
              padding: 60,
              flex: 1,
            },
            children: [
              {
                type: 'img',
                props: {
                  src: logoSrc,
                  width: 80,
                  height: 80,
                  style: { marginBottom: 32 },
                },
              },
              {
                type: 'div',
                props: {
                  style: {
                    fontSize: 64,
                    fontWeight: 700,
                    color: 'rgb(25,25,30)',
                    lineHeight: 1.2,
                    fontFamily,
                  },
                  children: title,
                },
              },
              {
                type: 'div',
                props: {
                  style: {
                    fontSize: 30,
                    color: 'rgb(110,110,118)',
                    lineHeight: 1.5,
                    fontFamily,
                    marginTop: 20,
                  },
                  children: description,
                },
              },
            ],
          },
        },
      ],
    },
  };
}

export async function generateOgImage(
  page: PageMeta,
  fontsResult: FontsResult,
): Promise<Buffer> {
  const svg = await satori(
    cardMarkup(page.title, page.description, getLogoDataUri(), fontsResult.fontFamily),
    { width: 1200, height: 630, fonts: fontsResult.entries },
  );

  return sharp(Buffer.from(svg)).png().toBuffer();
}

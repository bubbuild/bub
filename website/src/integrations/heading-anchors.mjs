// Starlight derives a heading's id from its text, so a section keeps a different
// anchor in every locale (`## Storage` → `#storage`, `## 存储` → `#存储`). Any
// cross-page link therefore breaks as soon as one locale renames a heading.
//
// Enable Sätteri's heading attributes so a heading can carry an explicit id
// (`## 存储 {#storage}`), which every locale then shares.

import { isSatteriProcessor, satteri } from '@astrojs/markdown-satteri';

/** @returns {import('astro').AstroIntegration} */
export default function headingAnchors() {
  return {
    name: 'bub-heading-anchors',
    hooks: {
      'astro:config:setup'({ config, updateConfig }) {
        const processor = config.markdown.processor;
        if (!isSatteriProcessor(processor)) {
          throw new Error('Explicit heading anchors require the Sätteri Markdown processor.');
        }
        const options = processor.options;
        updateConfig({
          markdown: {
            processor: satteri({
              ...options,
              features: { ...options.features, headingAttributes: true },
            }),
          },
        });
      },
    },
  };
}

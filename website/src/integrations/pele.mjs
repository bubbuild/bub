import { createHash } from 'node:crypto';
import { isSatteriProcessor, satteri } from '@astrojs/markdown-satteri';
import { render } from 'pele';

function escapeAttribute(value) {
  return value.replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

/** @type {import('satteri').MdastPluginDefinition} */
export const pelePlugin = {
  name: 'bub-pele',
  options: { position: true },
  code(node, context) {
    if (node.lang !== 'mermaid') return;

    const id = createHash('sha256')
      .update(`${context.fileURL}:${JSON.stringify(node.position)}:${node.value}`)
      .digest('hex').slice(0, 12);
    // Fail the build on invalid diagrams, and keep an SVG visible without JavaScript.
    const { svg } = render(node.value, {
      idPrefix: `pele-${id}`,
      fontFamily: 'Outfit Variable, sans-serif',
      fontFamilyMono: 'JetBrains Mono Variable, monospace',
    });
    return {
      type: 'html',
      value: `<div class="pele-diagram not-content" data-pele-source="${escapeAttribute(node.value)}">${svg}</div>`,
    };
  },
};

/** @returns {import('astro').AstroIntegration} */
export default function peleDiagrams() {
  return {
    name: 'bub-pele',
    hooks: {
      'astro:config:setup'({ config, updateConfig, injectScript }) {
        const processor = config.markdown.processor;
        if (!isSatteriProcessor(processor)) {
          throw new Error('Bub diagram rendering requires the Sätteri Markdown processor.');
        }
        const options = processor.options;
        updateConfig({
          markdown: {
            processor: satteri({
              ...options,
              mdastPlugins: [...options.mdastPlugins, pelePlugin],
            }),
          },
        });
        injectScript('page', "import '/src/scripts/pele.ts';");
      },
    },
  };
}

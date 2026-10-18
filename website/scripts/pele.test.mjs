import assert from 'node:assert/strict';
import test from 'node:test';
import { markdownToHtml } from 'satteri';
import { pelePlugin } from '../src/integrations/pele.mjs';

const renderMarkdown = async (source) => (await markdownToHtml(source, {
  mdastPlugins: [pelePlugin],
})).html;

test('renders Mermaid fences as SVG', async () => {
  const html = await renderMarkdown('```mermaid\nflowchart LR\n A{"Ready?"} --> B["One<br/>Two & three"]\n```');
  assert.match(html, /<svg/);
  assert.match(html, /Ready\?/);
});

test('keeps ordinary code fences unchanged', async () => {
  const source = '```python\nprint("hello")\n```';
  assert.equal(await renderMarkdown(source), (await markdownToHtml(source)).html);
});

test('reports invalid diagrams during compilation', async () => {
  await assert.rejects(() => renderMarkdown('```mermaid\nthis is not a diagram\n```'));
});

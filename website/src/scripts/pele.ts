import type { Mounted } from 'pele';

const diagrams = new Map<HTMLElement, Mounted>();
const pending = new Set<HTMLElement>();

async function mountDiagrams() {
  const containers = document.querySelectorAll<HTMLElement>('[data-pele-source]');
  if (!containers.length) return;

  // Load the renderer only on pages with diagrams, then load each diagram type on demand.
  const { mountAsync } = await import('pele/lazy');
  for (const container of containers) {
    if (!container.isConnected || diagrams.has(container) || pending.has(container)) continue;
    pending.add(container);
    try {
      const chinese = document.documentElement.lang.toLowerCase().startsWith('zh');
      const diagram = await mountAsync(container, container.dataset.peleSource!, {
        zoom: {
          labels: chinese
            ? { zoomIn: '放大', zoomOut: '缩小', reset: '重置' }
            : { zoomIn: 'Zoom in', zoomOut: 'Zoom out', reset: 'Reset' },
        },
      });
      if (container.isConnected) diagrams.set(container, diagram);
      else diagram.destroy();
    } finally {
      pending.delete(container);
    }
  }
}

function destroyDiagrams() {
  for (const diagram of diagrams.values()) diagram.destroy();
  diagrams.clear();
}

void mountDiagrams();
document.addEventListener('astro:page-load', mountDiagrams);
document.addEventListener('astro:before-swap', destroyDiagrams);

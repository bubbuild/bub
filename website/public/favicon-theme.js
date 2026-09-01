(() => {
  const root = document.documentElement;
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');

  const getTheme = () => {
    const documentTheme = root.dataset.theme;
    if (documentTheme === 'light' || documentTheme === 'dark') {
      return documentTheme;
    }

    try {
      const storedTheme = localStorage.getItem('starlight-theme');
      if (storedTheme === 'light' || storedTheme === 'dark') {
        return storedTheme;
      }
    } catch {
      // Storage can be unavailable in privacy-restricted browser contexts.
    }

    return systemTheme.matches ? 'dark' : 'light';
  };

  const updateFavicon = () => {
    const theme = getTheme();
    for (const icon of document.querySelectorAll('[data-favicon-theme]')) {
      icon.media = icon.dataset.faviconTheme === theme ? 'all' : 'not all';
    }
  };

  updateFavicon();
  new MutationObserver(updateFavicon).observe(root, {
    attributes: true,
    attributeFilter: ['class', 'data-theme'],
  });
  systemTheme.addEventListener('change', updateFavicon);
})();

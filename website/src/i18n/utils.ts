// ---------------------------------------------------------------------------
// i18n helper utilities — Astro recipe pattern
// ---------------------------------------------------------------------------

import { ui, defaultLang, showDefaultLang, type Locale, type UIKey } from './ui';

/** Extract locale from a URL pathname. Falls back to defaultLang. */
export function getLangFromUrl(url: URL): Locale {
  const [, seg] = url.pathname.split('/');
  if (seg && seg in ui) return seg as Locale;
  return defaultLang;
}

/** Return the BCP-47 lang tag for an HTML `lang` attribute. */
export function getHtmlLang(locale: Locale): string {
  const map: Record<Locale, string> = { en: 'en', 'zh-cn': 'zh-CN' };
  return map[locale] ?? locale;
}

/** Return a `t()` function bound to the given locale. */
export function useTranslations(lang: Locale) {
  return function t(key: UIKey): string {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (ui[lang] as any)[key] ?? (ui[defaultLang] as any)[key] ?? key;
  };
}

/** Return a path-translator bound to the given locale. */
export function useTranslatedPath(lang: Locale) {
  return function translatePath(path: string, l: Locale = lang): string {
    return !showDefaultLang && l === defaultLang ? path : `/${l}${path}`;
  };
}

/** Compute the "switch language" href for a given pathname. */
export function getAlternateLocaleHref(pathname: string, currentLocale: Locale): string {
  const otherLocale: Locale = currentLocale === 'en' ? 'zh-cn' : 'en';
  if (currentLocale === defaultLang) {
    // en: /posts/foo/ → /zh-cn/posts/foo/
    return `/${otherLocale}${pathname}`;
  }
  // zh-cn: /zh-cn/posts/foo/ → /posts/foo/
  const stripped = pathname.replace(new RegExp(`^/${currentLocale}`), '');
  return stripped || '/';
}

// ---------------------------------------------------------------------------
// Derived nav props — replaces all hardcoded navProps objects in pages
// ---------------------------------------------------------------------------

export interface NavProps {
  docsHref: string;
  blogHref: string;
  languageHref: string;
  languageLabel: string;
  homeHref: string;
  docsLabel: string;
  blogLabel: string;
  hubLabel: string;
  menuLabel: string;
  closeLabel: string;
}

/** Build NavBar props from locale + current pathname. */
export function getNavProps(locale: Locale, pathname: string): NavProps {
  const t = useTranslations(locale);
  const tp = useTranslatedPath(locale);

  // With root locale, English docs have no prefix; other locales use /{locale}
  const docsPrefix = locale === defaultLang ? '' : `/${locale}`;

  return {
    docsHref: `${docsPrefix}/docs/getting-started/`,
    blogHref: tp('/posts/'),
    languageHref: getAlternateLocaleHref(pathname, locale),
    languageLabel: t('lang.switch'),
    homeHref: tp('/'),
    docsLabel: t('nav.docs'),
    blogLabel: t('nav.blog'),
    hubLabel: t('nav.plugins'),
    menuLabel: t('nav.menu'),
    closeLabel: t('nav.close'),
  };
}

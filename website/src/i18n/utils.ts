// ---------------------------------------------------------------------------
// i18n helper utilities — Astro recipe pattern
// ---------------------------------------------------------------------------

import { ui, languages, defaultLang, showDefaultLang, type Locale, type UIKey } from '@/i18n/ui';

type StaticLocalePath = {
  params: { locale?: string };
  props: { locale: Locale };
};

type PostDateLength = 'short' | 'long';

const htmlLangMap: Record<Locale, string> = {
  en: 'en',
  'zh-cn': 'zh-CN',
};

/** Return every supported locale in route generation order. */
export function getLocales(): Locale[] {
  return Object.keys(languages) as Locale[];
}

/** Runtime guard for locale-like input. */
export function isLocale(value: string | undefined): value is Locale {
  return typeof value === 'string' && value in languages;
}

/** Extract locale from a URL pathname. Falls back to defaultLang. */
export function getLangFromUrl(url: URL): Locale {
  const [, seg] = url.pathname.split('/');
  return isLocale(seg) ? seg : defaultLang;
}

/** Normalize a catch-all route param into a valid locale, or null when unknown. */
export function getLocaleFromParam(localeParam?: string): Locale | null {
  if (localeParam === undefined) return defaultLang;
  return isLocale(localeParam) ? localeParam : null;
}

/** Map a locale to its route param representation. */
export function getLocaleRouteParam(locale: Locale): string | undefined {
  return locale === defaultLang ? undefined : locale;
}

/** Shared locale route generation for `[...locale]` pages. */
export function getStaticLocalePaths(): StaticLocalePath[] {
  return getLocales().map((locale) => ({
    params: { locale: getLocaleRouteParam(locale) },
    props: { locale },
  }));
}

/** Return the BCP-47 lang tag for an HTML `lang` attribute. */
export function getHtmlLang(locale: Locale): string {
  return htmlLangMap[locale] ?? locale;
}

/** Return a `t()` function bound to the given locale. */
export function useTranslations(lang: Locale) {
  const localeUi = ui[lang] as Partial<Record<UIKey, string>>;
  const defaultUi = ui[defaultLang];

  return function t(key: UIKey): string {
    return localeUi[key] ?? defaultUi[key] ?? key;
  };
}

/** Return the locale prefix used in URLs. */
export function getLocalePrefix(locale: Locale): string {
  return !showDefaultLang && locale === defaultLang ? '' : `/${locale}`;
}

/** Translate a site-relative path for the given locale. */
export function getLocalizedPath(path: string, locale: Locale): string {
  return `${getLocalePrefix(locale)}${path}`;
}

/** Return a path-translator bound to the given locale. */
export function useTranslatedPath(lang: Locale) {
  return function translatePath(path: string, locale: Locale = lang): string {
    return getLocalizedPath(path, locale);
  };
}

/** Build the locale-aware docs landing href. */
export function getDocsHref(locale: Locale, path = '/getting-started/'): string {
  return `${getLocalePrefix(locale)}/docs${path}`;
}

/** Build the locale-aware post list href. */
export function getPostsHref(locale: Locale): string {
  return getLocalizedPath('/posts/', locale);
}

/** Build the locale-aware single post href. */
export function getPostHref(locale: Locale, slug: string): string {
  return getLocalizedPath(`/posts/${slug}/`, locale);
}

/** Strip the locale directory prefix from a content collection id. */
export function getPostSlug(entryId: string, locale: Locale): string {
  const prefix = `${locale}/`;
  return entryId.startsWith(prefix) ? entryId.slice(prefix.length) : entryId;
}

/** Format a post date for the current locale. */
export function formatPostDate(date: Date, locale: Locale, month: PostDateLength = 'long'): string {
  return date.toLocaleDateString(getHtmlLang(locale), {
    year: 'numeric',
    month,
    day: 'numeric',
  });
}

/** Compute the "switch language" href for a given pathname. */
export function getAlternateLocaleHref(pathname: string, currentLocale: Locale): string {
  const otherLocale = getLocales().find((locale) => locale !== currentLocale) ?? defaultLang;

  if (currentLocale === defaultLang) {
    return getLocalizedPath(pathname, otherLocale);
  }

  const strippedPath = pathname.replace(new RegExp(`^/${currentLocale}`), '') || '/';
  return getLocalizedPath(strippedPath, otherLocale);
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
  githubLabel: string;
  menuLabel: string;
  closeLabel: string;
}

/** Build NavBar props from locale + current pathname. */
export function getNavProps(locale: Locale, pathname: string): NavProps {
  const t = useTranslations(locale);

  return {
    docsHref: getDocsHref(locale),
    blogHref: getPostsHref(locale),
    languageHref: getAlternateLocaleHref(pathname, locale),
    languageLabel: t('lang.switch'),
    homeHref: getLocalizedPath('/', locale),
    docsLabel: t('nav.docs'),
    blogLabel: t('nav.blog'),
    hubLabel: t('nav.plugins'),
    githubLabel: t('nav.github'),
    menuLabel: t('nav.menu'),
    closeLabel: t('nav.close'),
  };
}

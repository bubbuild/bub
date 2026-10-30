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

export function getLocales(): Locale[] {
  return Object.keys(languages) as Locale[];
}

export function isLocale(value: string | undefined): value is Locale {
  return typeof value === 'string' && value in languages;
}

export function getLangFromUrl(url: URL): Locale {
  const [, seg] = url.pathname.split('/');
  return isLocale(seg) ? seg : defaultLang;
}

/** Catch-all param → locale, or null when unknown. Missing param → defaultLang. */
export function getLocaleFromParam(localeParam?: string): Locale | null {
  if (localeParam === undefined) return defaultLang;
  return isLocale(localeParam) ? localeParam : null;
}

/** Default locale omits the route segment when `showDefaultLang` is false. */
export function getLocaleRouteParam(locale: Locale): string | undefined {
  return locale === defaultLang ? undefined : locale;
}

export function getStaticLocalePaths(): StaticLocalePath[] {
  return getLocales().map((locale) => ({
    params: { locale: getLocaleRouteParam(locale) },
    props: { locale },
  }));
}

export function getHtmlLang(locale: Locale): string {
  return htmlLangMap[locale];
}

export function useTranslations(lang: Locale) {
  const localeUi = ui[lang] as Partial<Record<UIKey, string>>;
  const defaultUi = ui[defaultLang];

  return function t(key: UIKey): string {
    return localeUi[key] ?? defaultUi[key];
  };
}

export function getLocalePrefix(locale: Locale): string {
  return !showDefaultLang && locale === defaultLang ? '' : `/${locale}`;
}

export function getLocalizedPath(path: string, locale: Locale): string {
  return `${getLocalePrefix(locale)}${path}`;
}

export function useTranslatedPath(lang: Locale) {
  return function translatePath(path: string, locale: Locale = lang): string {
    return getLocalizedPath(path, locale);
  };
}

export function getDocsHref(locale: Locale, path = '/getting-started/'): string {
  return `${getLocalePrefix(locale)}/docs${path}`;
}

export function getPostsHref(locale: Locale): string {
  return getLocalizedPath('/posts/', locale);
}

export function getPostHref(locale: Locale, slug: string): string {
  return getLocalizedPath(`/posts/${slug}/`, locale);
}

/** `en/bootstrap-milestone` → `bootstrap-milestone`. */
export function getPostSlug(entryId: string, locale: Locale): string {
  const prefix = `${locale}/`;
  return entryId.startsWith(prefix) ? entryId.slice(prefix.length) : entryId;
}

export function formatPostDate(date: Date, locale: Locale, month: PostDateLength = 'long'): string {
  return date.toLocaleDateString(getHtmlLang(locale), {
    year: 'numeric',
    month,
    day: 'numeric',
  });
}

export function getAlternateLocaleHref(pathname: string, currentLocale: Locale): string {
  const otherLocale = getLocales().find((locale) => locale !== currentLocale) ?? defaultLang;

  // Only one 404 route is generated, so the language switcher cannot map `/404/`
  // to a localized 404 page. Send readers to the other locale's home page instead.
  if (pathname === '/404' || pathname === '/404/') {
    return getLocalizedPath('/', otherLocale);
  }

  if (currentLocale === defaultLang) {
    return getLocalizedPath(pathname, otherLocale);
  }

  const strippedPath = pathname.replace(new RegExp(`^/${currentLocale}`), '') || '/';
  return getLocalizedPath(strippedPath, otherLocale);
}

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
    menuLabel: t('nav.menu'),
    closeLabel: t('nav.close'),
  };
}

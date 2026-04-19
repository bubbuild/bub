// ---------------------------------------------------------------------------
// Flat-key i18n UI string dictionary — Astro recipe pattern
// All keys are dot-separated, no nesting. Easy to grep & maintain.
// ---------------------------------------------------------------------------

export const languages = {
  en: 'English',
  'zh-cn': '简体中文',
} as const;

export type Locale = keyof typeof languages;

export const defaultLang: Locale = 'en';

/** When false the default locale has NO prefix: `/about` instead of `/en/about` */
export const showDefaultLang = false;

// ---------------------------------------------------------------------------
// UI strings — flat keys for nav, footer, 404, posts, and site meta.
// Landing-page structured data (hero, features, etc.) lives in data/landing-page.ts.
// English is the source of truth. Other locales override selectively;
// missing keys automatically fall back to English.
// ---------------------------------------------------------------------------

export const ui = {
  en: {
    // Site meta
    'site.title': 'Bub | A tiny runtime for agents that live alongside people',
    'site.description': 'Bub is a tiny, hook-driven agent runtime for real conversations.',

    // Nav
    'nav.docs': 'Docs',
    'nav.blog': 'Blog',
    'nav.plugins': 'Plugins',
    'nav.github': 'GitHub',
    'nav.menu': 'Menu',
    'nav.close': 'Close',

    // Footer
    'footer.copyright': `© ${new Date().getFullYear()} Bub Contributors`,

    // 404
    '404.title': 'Page not found.',
    '404.description': "The page you're looking for doesn't exist or has been moved.",
    '404.home': 'Back to home',
    '404.docs': 'Read the docs',

    // Post list
    'posts.title': 'Posts',
    'posts.description': 'Thoughts on agent design, collaboration, and building Bub.',
    'posts.back': 'Back to Posts',

    // Language switcher label (shown as the "other" language)
    'lang.switch': '中文',

    // Tape model section
    'tapeModel.learnMore': 'Learn more at tape.system',
  },

  'zh-cn': {
    // Site meta
    'site.title': 'Bub | 与 Human 同在的轻量级 Agent 运行时',
    'site.description': 'Bub 是一个为真实对话打造的轻量、hook 驱动的 agent 运行时。',

    // Nav
    'nav.docs': '文档',
    'nav.blog': '博客',
    'nav.plugins': '插件',
    'nav.github': 'GitHub',
    'nav.menu': '菜单',
    'nav.close': '关闭',

    // Footer
    'footer.copyright': `© ${new Date().getFullYear()} Bub Contributors`,

    // 404
    '404.title': '页面不存在。',
    '404.description': '你访问的页面不存在或已被移动。',
    '404.home': '返回首页',
    '404.docs': '阅读文档',

    // Post list
    'posts.title': '文章',
    'posts.description': '关于 agent 设计、协作与构建 Bub 的思考。',
    'posts.back': '返回文章列表',

    // Language switcher label
    'lang.switch': 'English',

    // Tape model section
    'tapeModel.learnMore': '前往 tape.system 了解更多',
  },
} as const;

// Type for a valid UI key (from the default locale)
export type UIKey = keyof (typeof ui)[typeof defaultLang];

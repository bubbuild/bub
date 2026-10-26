// Flat-key i18n UI strings (Astro recipe). Landing content → data/landing-page.ts.

export const languages = {
  en: 'English',
  'zh-cn': '简体中文',
} as const;

export type Locale = keyof typeof languages;

export const defaultLang: Locale = 'en';

/** When false the default locale has NO prefix: `/about` instead of `/en/about` */
export const showDefaultLang = false;

// English is the source of truth; other locales override selectively.
export const ui = {
  en: {
    // Site meta
    'site.title': 'Bub | A tiny agent runtime, composable with plugins',
    'site.description':
      'Bub is a tiny agent runtime, composable with plugins. Start with a working agent and customize how it thinks, remembers, and connects.',

    // Nav
    'nav.docs': 'Docs',
    'nav.blog': 'Blog',
    'nav.plugins': 'Plugins',
    'nav.menu': 'Menu',
    'nav.close': 'Close',

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
  },

  'zh-cn': {
    // Site meta
    'site.title': 'Bub | 轻量的 Agent 运行时，以插件自由组合',
    'site.description': 'Bub 是轻量的 Agent 运行时，以插件自由组合。开箱即用，再按需定制模型执行、记忆和消息渠道。',

    // Nav
    'nav.docs': '文档',
    'nav.blog': '博客',
    'nav.plugins': '插件',
    'nav.menu': '菜单',
    'nav.close': '关闭',

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
  },
} as const;

export type UIKey = keyof (typeof ui)[typeof defaultLang];

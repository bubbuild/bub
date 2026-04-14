type Metric = {
  label: string;
  value: string;
};

type NavCopy = {
  docsHref: string;
  blogHref: string;
  languageHref: string;
  languageLabel: string;
  homeHref?: string;
  docsLabel?: string;
  blogLabel?: string;
  githubLabel?: string;
  menuLabel?: string;
  closeLabel?: string;
};

type HeroCopy = {
  badge?: string;
  title?: string;
  description?: string;
  primaryHref: string;
  primaryLabel?: string;
  secondaryHref?: string;
  secondaryLabel?: string;
  metrics?: Metric[];
  panelEyebrow?: string;
  panelDescription?: string;
  panelBadge?: string;
  renderOutboundLabel?: string;
  bottomNotes?: string[];
};

type LandingPageCopy = {
  htmlLang: string;
  title: string;
  description: string;
  nav: NavCopy;
  hero: HeroCopy;
};

export type LandingLocale = 'en' | 'zh-cn';

const landingPageCopy: Record<LandingLocale, LandingPageCopy> = {
  en: {
    htmlLang: 'en',
    title: 'Bub | Agents that live alongside people',
    description: 'A common shape for agents that live alongside people.',
    nav: {
      docsHref: '/en/docs/',
      blogHref: '/en/blog/socialized-evaluation/',
      languageHref: '/zh-cn/',
      languageLabel: '中文',
    },
    hero: {
      primaryHref: '/en/getting-started/installation/',
    },
  },
  'zh-cn': {
    htmlLang: 'zh-CN',
    title: 'Bub | 与人并行存在的 agent 统一形状',
    description: '一种为与人并行存在的 agents 设计的通用运行时形状。',
    nav: {
      docsHref: '/zh-cn/docs/',
      blogHref: '/zh-cn/blog/socialized-evaluation/',
      languageHref: '/',
      languageLabel: 'English',
      homeHref: '/zh-cn/',
      docsLabel: '文档',
      blogLabel: '博客',
      githubLabel: 'GitHub',
      menuLabel: '菜单',
      closeLabel: '关闭',
    },
    hero: {
      badge: '起源于群聊',
      title: '一种为与人并行存在的 agent 设计的通用形状。',
      description:
        'Hook-first。Tape 驱动上下文。Channel 无关。它不是为单人演示准备的，而是为 agent 与人类在真实协作环境中共存而生。',
      primaryHref: '/zh-cn/getting-started/installation/',
      primaryLabel: '开始使用',
      secondaryLabel: '查看 GitHub',
      metrics: [
        { label: '核心体积', value: '~200 行' },
        { label: '上下文模型', value: 'Tape 驱动' },
        { label: '渠道', value: 'CLI + Telegram' },
      ],
      panelEyebrow: 'Turn pipeline',
      panelDescription: '用 CSS 动画呈现 Bub 的核心运行时流转。',
      panelBadge: '运行形状',
      bottomNotes: [
        'Hooks 始终可替换，所以核心不需要为了 channel 或 provider 写特殊分支。',
        '同一条 turn shape 可以同时覆盖 CLI、Telegram 和未来的 adapter，而不破坏编排模型。',
      ],
    },
  },
};

export function getLandingPageCopy(locale: LandingLocale): LandingPageCopy {
  return landingPageCopy[locale];
}

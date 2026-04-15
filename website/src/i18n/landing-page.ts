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
};

type FeatureItem = { icon: string; title: string; description: string; color?: string };

type FeaturesCopy = {
  eyebrow?: string;
  heading?: string;
  subheading?: string;
  features?: FeatureItem[];
};

type HookIntroCopy = {
  eyebrow?: string;
  heading?: string;
  description?: string[];
  hookStages?: { name: string; note: string }[];
};

type TapeModelCopy = {
  eyebrow?: string;
  heading?: string;
  description?: string[];
};

type TestimonialsCopy = {
  eyebrow?: string;
  heading?: string;
  testimonials?: { name: string; handle: string; text: string }[];
};

type FooterCopy = {
  docsHref?: string;
  docsLabel?: string;
  githubLabel?: string;
  licenseLabel?: string;
  copyright?: string;
};

type LandingPageCopy = {
  htmlLang: string;
  title: string;
  description: string;
  nav: NavCopy;
  hero: HeroCopy;
  features?: FeaturesCopy;
  footer?: FooterCopy;
};

export type LandingLocale = 'en' | 'zh-cn';

const landingPageCopy: Record<LandingLocale, LandingPageCopy> = {
  en: {
    htmlLang: 'en',
    title: 'Bub | A tiny runtime for agents that live alongside people',
    description: 'Bub is a tiny, hook-driven agent runtime for real conversations.',
    nav: {
      docsHref: '/en/getting-started/',
      blogHref: '/en/blog/socialized-evaluation/',
      languageHref: '/zh-cn/',
      languageLabel: '中文',
    },
    hero: {
      primaryHref: '/en/getting-started/installation/',
    },
    footer: {
      docsHref: '/en/getting-started/',
    },
  },
  'zh-cn': {
    htmlLang: 'zh-CN',
    title: 'Bub | 与人并行的轻量 agent 运行时',
    description: 'Bub 是一个为真实对话打造的轻量、hook 驱动的 agent 运行时。',
    nav: {
      docsHref: '/zh-cn/getting-started/',
      blogHref: '/zh-cn/blog/socialized-evaluation/',
      languageHref: '/',
      languageLabel: 'English',
      homeHref: '/zh-cn/',
      docsLabel: '文档',
      blogLabel: '博客',
      githubLabel: 'GitHub',
      menuLabel: '菜单',
      closeLabel: '关闭',
      hubLabel: '插件市场',
    },
    hero: {
      badge: 'Hook-first · Tape 驱动 · Channel 无关',
      title: 'Bub 是一个为与人并行的 agent 设计的轻量运行时。',
      description:
        '~200 行核心代码。Hooks 重塑每个 turn 阶段。Tapes 记录每一个决策。Channels 适配任何表面——CLI、Telegram 或你自己的。',
      primaryHref: '/zh-cn/getting-started/installation/',
      primaryLabel: '开始使用',
      secondaryLabel: '查看 GitHub',
    },
    features: {
      eyebrow: '核心设计',
      heading: '每一个决定都有理由。',
      subheading: 'Bub 从第一天起就为真实的多 agent 协作而设计，而不是事后追加的。',
      features: [
        {
          icon: 'webhook',
          title: 'Hook-First',
          description: '约 200 行核心代码。每个 turn 阶段都是一个 pluggy hook。内置实现只是默认插件——可随时替换任意阶段。',
          color: 'coral',
        },
        {
          icon: 'layers',
          title: 'Tape 上下文',
          description: '上下文从追加式 tape 记录中重建，而非积累在 session 状态中。没有有损摘要，没有幻影记忆。',
          color: 'amber',
        },
        {
          icon: 'radioTower',
          title: 'Channel 无关',
          description: '同一条 process_inbound() 管道驱动 CLI、Telegram 及你添加的任何 channel。Hooks 从不感知运行在哪个界面。',
          color: 'teal',
        },
        {
          icon: 'fileSearch',
          title: 'Skills as Docs',
          description: 'Skills 是带验证 frontmatter 的 SKILL.md 文件——不是带魔法注册的代码模块。可发现、可覆盖、可审计。',
          color: 'purple',
        },
        {
          icon: 'users',
          title: '操作者对等',
          description: '人类与 agent 共享同一操作者模型：相同的边界、证据链和交接语义。没有特殊情况。',
          color: 'green',
        },
        {
          icon: 'wrench',
          title: '插件系统',
          description: '通过 group="bub" 的 Python entry-points 注册。后注册的插件优先运行并覆盖前者。没有框架特权。',
          color: 'blue',
        },
      ],
    },
    footer: {
      docsHref: '/zh-cn/getting-started/',
      docsLabel: '文档',
      githubLabel: 'GitHub',
      licenseLabel: 'Apache-2.0',
    },
  },
};

export function getLandingPageCopy(locale: LandingLocale): LandingPageCopy {
  return landingPageCopy[locale];
}

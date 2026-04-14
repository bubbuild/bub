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

type FeatureItem = { icon: string; title: string; description: string };

type FeaturesCopy = {
  eyebrow?: string;
  heading?: string;
  subheading?: string;
  features?: FeatureItem[];
};

type QuickStartStep = { number: string; title: string; code: string; lang: string };

type QuickStartCopy = {
  eyebrow?: string;
  heading?: string;
  steps?: QuickStartStep[];
  primaryHref?: string;
  primaryLabel?: string;
};

type HighlightItem = { label: string; description: string };

type PhilosophyCopy = {
  eyebrow?: string;
  heading?: string;
  body?: string[];
  quoteText?: string;
  blogHref?: string;
  blogLabel?: string;
  highlights?: HighlightItem[];
};

type FooterCopy = {
  docsHref?: string;
  docsLabel?: string;
  blogHref?: string;
  blogLabel?: string;
  githubLabel?: string;
  licenseLabel?: string;
  builtWith?: string;
  copyright?: string;
};

type LandingPageCopy = {
  htmlLang: string;
  title: string;
  description: string;
  nav: NavCopy;
  hero: HeroCopy;
  features?: FeaturesCopy;
  quickStart?: QuickStartCopy;
  philosophy?: PhilosophyCopy;
  footer?: FooterCopy;
};

export type LandingLocale = 'en' | 'zh-cn';

const landingPageCopy: Record<LandingLocale, LandingPageCopy> = {
  en: {
    htmlLang: 'en',
    title: 'Bub | Agents that live alongside people',
    description: 'A common shape for agents that live alongside people.',
    nav: {
      docsHref: '/en/getting-started/',
      blogHref: '/en/blog/socialized-evaluation/',
      languageHref: '/zh-cn/',
      languageLabel: '中文',
    },
    hero: {
      primaryHref: '/en/getting-started/installation/',
    },
    philosophy: {
      blogHref: '/en/blog/socialized-evaluation/',
    },
    quickStart: {
      primaryHref: '/en/getting-started/installation/',
    },
    footer: {
      docsHref: '/en/getting-started/',
      blogHref: '/en/blog/',
    },
  },
  'zh-cn': {
    htmlLang: 'zh-CN',
    title: 'Bub | 与人并行存在的 agent 统一形状',
    description: '一种为与人并行存在的 agents 设计的通用运行时形状。',
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
    features: {
      eyebrow: '核心设计',
      heading: '每一个决定都有理由。',
      subheading: 'Bub 从第一天起就为真实的多 agent 协作而设计，而不是事后追加的。',
      features: [
        {
          icon: '🪝',
          title: 'Hook-First',
          description: '约 200 行核心代码。每个 turn 阶段都是一个 pluggy hook。内置实现只是默认插件——可随时替换任意阶段。',
        },
        {
          icon: '📼',
          title: 'Tape 上下文',
          description: '上下文从追加式 tape 记录中重建，而非积累在 session 状态中。没有有损摘要，没有幻影记忆。',
        },
        {
          icon: '🔌',
          title: 'Channel 无关',
          description: '同一条 process_inbound() 管道驱动 CLI、Telegram 及你添加的任何 channel。Hooks 从不感知运行在哪个界面。',
        },
        {
          icon: '📄',
          title: 'Skills as Docs',
          description: 'Skills 是带验证 frontmatter 的 SKILL.md 文件——不是带魔法注册的代码模块。可发现、可覆盖、可审计。',
        },
        {
          icon: '👥',
          title: '操作者对等',
          description: '人类与 agent 共享同一操作者模型：相同的边界、证据链和交接语义。没有特殊情况。',
        },
        {
          icon: '🧩',
          title: '插件系统',
          description: '通过 group="bub" 的 Python entry-points 注册。后注册的插件优先运行并覆盖前者。没有框架特权。',
        },
      ],
    },
    quickStart: {
      eyebrow: '快速开始',
      heading: '三步即可运行。',
      steps: [
        { number: '01', title: '安装', code: 'pip install bub', lang: 'bash' },
        {
          number: '02',
          title: '配置',
          code: 'cp env.example .env\n# 设置 BUB_MODEL 和你的 provider key',
          lang: 'bash',
        },
        {
          number: '03',
          title: '运行',
          code: 'uv run bub chat                      # 交互模式\nuv run bub run "总结这个 repo"       # 单次任务\nuv run bub gateway                   # channel 监听',
          lang: 'bash',
        },
      ],
      primaryHref: '/zh-cn/getting-started/installation/',
      primaryLabel: '查看完整安装指南',
    },
    philosophy: {
      eyebrow: '设计哲学',
      heading: '起源于群聊，\n而非演示。',
      body: [
        '大多数 agent 系统是为单用户演示构建的。Bub 则是为更混乱的现实而生：agent 与真实人类在共享对话中共存——并发任务、不完整上下文、没有人在等待。',
        'Bub 将人类与 agent 视为对等操作者：相同的边界、相同的证据链、相同的交接语义。当工作变得混乱时，它应该仍然是一个可靠的队友。',
      ],
      quoteText: '构建在真实社交系统中有用的 agent，而不只是在孤立演示中令人印象深刻。',
      blogHref: '/zh-cn/blog/socialized-evaluation/',
      blogLabel: '阅读：社会化评估',
      highlights: [
        { label: '操作者对等', description: '人类与 agent 共享同一协作模型。' },
        { label: '可验证执行', description: 'Tape 记录使每个操作都可审计、可续接。' },
        { label: 'Channel 中立', description: '一条管道，任意界面——CLI、Telegram 或自定义。' },
      ],
    },
    footer: {
      docsHref: '/zh-cn/getting-started/',
      docsLabel: '文档',
      blogHref: '/zh-cn/blog/',
      blogLabel: '博客',
      githubLabel: 'GitHub',
      licenseLabel: 'Apache-2.0',
      builtWith: '基于 Astro + Starlight 构建',
    },
  },
};

export function getLandingPageCopy(locale: LandingLocale): LandingPageCopy {
  return landingPageCopy[locale];
}

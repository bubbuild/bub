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
// UI strings — flat keys, alphabetical within groups
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

    // Hero
    'hero.badge': 'Hook-first · Tape-driven · Channel-agnostic',
    'hero.title': 'Bub is a tiny runtime for agents that live alongside people.',
    'hero.description': '~200 lines of core code. Hooks reshape every turn stage. Tapes record every decision. Channels adapt to any surface — CLI, Telegram, or your own.',
    'hero.cta': 'Get Started',
    'hero.contributors': 'Developed by contributors worldwide',

    // Features
    'features.eyebrow': 'Why Bub',
    'features.heading': 'Every decision has a reason.',
    'features.subheading': 'Bub was designed for real multi-agent collaboration from day one — not retrofitted for it.',
    'features.hookFirst.title': 'Hook-First',
    'features.hookFirst.description': '~200-line core. Every turn stage is a pluggy hook. Builtins are just default plugins — override any stage without forking the runtime.',
    'features.tapeContext.title': 'Tape Context',
    'features.tapeContext.description': 'Context is reconstructed from append-only tape records, not accumulated in session state. No lossy summaries, no phantom memory.',
    'features.channelAgnostic.title': 'Channel-Agnostic',
    'features.channelAgnostic.description': "The same process_inbound() pipeline drives CLI, Telegram, and any channel you add. Hooks never know which surface they're on.",
    'features.skillsAsDocs.title': 'Skills as Docs',
    'features.skillsAsDocs.description': 'Skills are SKILL.md files with validated frontmatter — not code modules with magic registration. Discoverable, overridable, auditable.',
    'features.operatorEquivalence.title': 'Operator Equivalence',
    'features.operatorEquivalence.description': 'Humans and agents share the same operator model: same boundaries, same evidence trails, same handoff semantics. No special cases.',
    'features.pluginSystem.title': 'Plugin System',
    'features.pluginSystem.description': 'Python entry-points under group="bub". Later-registered plugins run first and override earlier ones. No framework privilege.',

    // Hook intro
    'hookIntro.eyebrow': 'Architecture',
    'hookIntro.heading': 'Hooks define every turn stage.',
    'hookIntro.description.0': 'Every stage in a Bub turn is a pluggy hook. The built-in implementation is just another plugin.',
    'hookIntro.description.1': 'Override any stage by registering your own. Later plugins take priority. No forking, no framework privilege.',
    'hookIntro.stage.resolve_session': 'Route to the right conversation',
    'hookIntro.stage.load_state': 'Reconstruct context from tape',
    'hookIntro.stage.build_prompt': 'Assemble system + history + tools',
    'hookIntro.stage.run_model': 'Call the LLM provider',
    'hookIntro.stage.render_outbound': 'Format the reply for the channel',
    'hookIntro.stage.save_state': 'Persist to tape',
    'hookIntro.stage.dispatch_outbound': 'Send to CLI / Telegram / etc.',

    // Tape model
    'tapeModel.eyebrow': 'Context model',
    'tapeModel.heading': 'Tape: the only source of truth.',
    'tapeModel.description.0': "Context isn't kept in fragile session state. It's reconstructed from an append-only tape — a ledger of immutable records.",
    'tapeModel.description.1': 'Corrections append new entries. They never overwrite. Every action is auditable, continuable, and safe to replay.',

    // Testimonials
    'testimonials.eyebrow': 'Community',
    'testimonials.heading': 'What people are saying.',

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
    'posts.back': '← Back to Posts',

    // Language switcher label (shown as the "other" language)
    'lang.switch': '中文',
  },

  'zh-cn': {
    // Site meta
    'site.title': 'Bub | 与人并行的轻量 agent 运行时',
    'site.description': 'Bub 是一个为真实对话打造的轻量、hook 驱动的 agent 运行时。',

    // Nav
    'nav.docs': '文档',
    'nav.blog': '博客',
    'nav.plugins': '插件',
    'nav.github': 'GitHub',
    'nav.menu': '菜单',
    'nav.close': '关闭',

    // Hero
    'hero.badge': 'Hook-first · Tape 驱动 · Channel 无关',
    'hero.title': 'Bub 是一个为与人并行的 agent 设计的轻量运行时。',
    'hero.description': '~200 行核心代码。Hooks 重塑每个 turn 阶段。Tapes 记录每一个决策。Channels 适配任何表面——CLI、Telegram 或你自己的。',
    'hero.cta': '开始使用',
    'hero.contributors': '由全球贡献者共同开发',

    // Features
    'features.eyebrow': '核心设计',
    'features.heading': '每一个决定都有理由。',
    'features.subheading': 'Bub 从第一天起就为真实的多 agent 协作而设计，而不是事后追加的。',
    'features.hookFirst.title': 'Hook-First',
    'features.hookFirst.description': '约 200 行核心代码。每个 turn 阶段都是一个 pluggy hook。内置实现只是默认插件——可随时替换任意阶段。',
    'features.tapeContext.title': 'Tape 上下文',
    'features.tapeContext.description': '上下文从追加式 tape 记录中重建，而非积累在 session 状态中。没有有损摘要，没有幻影记忆。',
    'features.channelAgnostic.title': 'Channel 无关',
    'features.channelAgnostic.description': '同一条 process_inbound() 管道驱动 CLI、Telegram 及你添加的任何 channel。Hooks 从不感知运行在哪个界面。',
    'features.skillsAsDocs.title': 'Skills as Docs',
    'features.skillsAsDocs.description': 'Skills 是带验证 frontmatter 的 SKILL.md 文件——不是带魔法注册的代码模块。可发现、可覆盖、可审计。',
    'features.operatorEquivalence.title': '操作者对等',
    'features.operatorEquivalence.description': '人类与 agent 共享同一操作者模型：相同的边界、证据链和交接语义。没有特殊情况。',
    'features.pluginSystem.title': '插件系统',
    'features.pluginSystem.description': '通过 group="bub" 的 Python entry-points 注册。后注册的插件优先运行并覆盖前者。没有框架特权。',

    // Hook intro
    'hookIntro.eyebrow': '架构',
    'hookIntro.heading': 'Hooks 定义每个 turn 阶段。',
    'hookIntro.description.0': 'Bub turn 中的每个阶段都是一个 pluggy hook。内置实现只是另一个插件。',
    'hookIntro.description.1': '通过注册你自己的 hook 覆盖任意阶段。后注册的插件优先执行。无需 fork，没有框架特权。',
    'hookIntro.stage.resolve_session': '路由到正确的会话',
    'hookIntro.stage.load_state': '从 tape 重建上下文',
    'hookIntro.stage.build_prompt': '组装 system + 历史 + 工具',
    'hookIntro.stage.run_model': '调用 LLM 提供者',
    'hookIntro.stage.render_outbound': '为 channel 格式化回复',
    'hookIntro.stage.save_state': '持久化到 tape',
    'hookIntro.stage.dispatch_outbound': '发送到 CLI / Telegram 等',

    // Tape model
    'tapeModel.eyebrow': '上下文模型',
    'tapeModel.heading': 'Tape：唯一的事实来源。',
    'tapeModel.description.0': '上下文不保存在脆弱的 session 状态中，而是从追加式 tape——一个不可变记录的账本——中重建。',
    'tapeModel.description.1': '修正会追加新条目，而非覆盖旧条目。每个操作都可审计、可续接、可安全重放。',

    // Testimonials
    'testimonials.eyebrow': '社区',
    'testimonials.heading': '大家怎么说。',

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
    'posts.back': '← 返回文章列表',

    // Language switcher label
    'lang.switch': 'English',
  },
} as const;

// Type for a valid UI key (from the default locale)
export type UIKey = keyof (typeof ui)[typeof defaultLang];

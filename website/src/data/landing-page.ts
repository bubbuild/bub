// ---------------------------------------------------------------------------
// Landing-page structured data — single source of truth for all landing-page
// content. Locale-specific text (nav, footer, 404, etc.) lives in i18n/ui.ts.
// Testimonial items are loaded separately from src/data/userwall.yml via the
// Astro content collection; only section labels (eyebrow/heading) live here.
// ---------------------------------------------------------------------------

export type HeroData = {
  badge?: string;
  title?: string;
  description?: string;
  primaryHref: string;
  primaryLabel?: string;
  contributorsLabel?: string;
};

export type FeatureItem = { icon: string; title: string; description: string; color?: string };

export type FeaturesData = {
  eyebrow?: string;
  heading?: string;
  subheading?: string;
  features?: FeatureItem[];
};

export type HookIntroData = {
  eyebrow?: string;
  heading?: string;
  description?: string[];
  hookStages?: { name: string; note: string }[];
};

export type TapeModelData = {
  eyebrow?: string;
  heading?: string;
  description?: string[];
};

export type TestimonialsData = {
  eyebrow?: string;
  heading?: string;
  /** Populated at runtime from the userwall content collection. */
  testimonials?: { name: string; handle: string; text: string; avatar?: string; platform?: string }[];
};

export type LandingPageData = {
  hero: HeroData;
  features?: FeaturesData;
  hookIntro?: HookIntroData;
  tapeModel?: TapeModelData;
  testimonials?: TestimonialsData;
};

export type LandingLocale = 'en' | 'zh-cn';

const landingPageData: Record<LandingLocale, LandingPageData> = {
  en: {
    hero: {
      badge: 'Hook-first · Tape-driven · Channel-agnostic',
      title: 'Bub is a tiny runtime for agents that live alongside people.',
      description:
        '~200 lines of core code. Hooks reshape every turn stage. Tapes record every decision. Channels adapt to any surface — CLI, Telegram, or your own.',
      primaryHref: '/docs/getting-started/installation/',
      primaryLabel: 'Get Started',
      contributorsLabel: 'Developed by contributors worldwide',
    },
    features: {
      eyebrow: 'Why Bub',
      heading: 'Every decision has a reason.',
      subheading: 'Bub was designed for real multi-agent collaboration from day one — not retrofitted for it.',
      features: [
        {
          icon: 'webhook',
          title: 'Hook-First',
          description:
            '~200-line core. Every turn stage is a pluggy hook. Builtins are just default plugins — override any stage without forking the runtime.',
          color: 'coral',
        },
        {
          icon: 'layers',
          title: 'Tape Context',
          description:
            'Context is reconstructed from append-only tape records, not accumulated in session state. No lossy summaries, no phantom memory.',
          color: 'amber',
        },
        {
          icon: 'radioTower',
          title: 'Channel-Agnostic',
          description:
            "The same process_inbound() pipeline drives CLI, Telegram, and any channel you add. Hooks never know which surface they're on.",
          color: 'teal',
        },
        {
          icon: 'fileSearch',
          title: 'Skills as Docs',
          description:
            'Skills are SKILL.md files with validated frontmatter — not code modules with magic registration. Discoverable, overridable, auditable.',
          color: 'purple',
        },
        {
          icon: 'users',
          title: 'Operator Equivalence',
          description:
            'Humans and agents share the same operator model: same boundaries, same evidence trails, same handoff semantics. No special cases.',
          color: 'green',
        },
        {
          icon: 'puzzle',
          title: 'Plugin System',
          description:
            'Python entry-points under group="bub". Later-registered plugins run first and override earlier ones. No framework privilege.',
          color: 'blue',
        },
      ],
    },
    hookIntro: {
      eyebrow: 'Architecture',
      heading: 'Hooks define every turn stage.',
      description: [
        'Every stage in a Bub turn is a pluggy hook. The built-in implementation is just another plugin.',
        'Override any stage by registering your own. Later plugins take priority. No forking, no framework privilege.',
      ],
      hookStages: [
        { name: 'resolve_session', note: 'Route to the right conversation' },
        { name: 'load_state', note: 'Reconstruct context from tape' },
        { name: 'build_prompt', note: 'Assemble system + history + tools' },
        { name: 'run_model', note: 'Call the LLM provider' },
        { name: 'render_outbound', note: 'Format the reply for the channel' },
        { name: 'save_state', note: 'Persist to tape' },
        { name: 'dispatch_outbound', note: 'Send to CLI / Telegram / etc.' },
      ],
    },
    tapeModel: {
      eyebrow: 'Context model',
      heading: 'Tape: a unified fact model.',
      description: [
        "Context isn't accumulated in session state. It's reconstructed from an append-only tape — a sequence of immutable facts. Entries record what happened; anchors mark phase boundaries and carry structured state.",
        'Corrections append new facts that supersede old ones — never overwrite. Views are assembled from anchors forward, not inherited wholesale. Every decision is auditable, replayable, and forkable.',
      ],
    },
    testimonials: {
      eyebrow: 'Community',
      heading: 'What people are saying.',
    },
  },
  'zh-cn': {
    hero: {
      badge: 'Hook-first · Tape 驱动 · Channel 无关',
      title: 'Bub 是一个为与人并行的 agent 设计的轻量运行时。',
      description:
        '~200 行核心代码。Hooks 重塑每个 turn 阶段。Tapes 记录每一个决策。Channels 适配任何表面——CLI、Telegram 或你自己的。',
      primaryHref: '/zh-cn/docs/getting-started/installation/',
      primaryLabel: '开始使用',
      contributorsLabel: '由全球贡献者共同开发',
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
          icon: 'puzzle',
          title: '插件系统',
          description: '通过 group="bub" 的 Python entry-points 注册。后注册的插件优先运行并覆盖前者。没有框架特权。',
          color: 'blue',
        },
      ],
    },
    hookIntro: {
      eyebrow: '架构',
      heading: 'Hooks 串连每次交互。',
      description: [
        'Bub 中每个阶段都是一个 pluggy hook。内置实现只是另一个插件。',
        '通过注册你自己的 hook 覆盖任意阶段。后注册的插件优先执行。无需 fork，没有框架特权。',
      ],
      hookStages: [
        { name: 'resolve_session', note: '路由到正确的会话' },
        { name: 'load_state', note: '从 tape 重建上下文' },
        { name: 'build_prompt', note: '组装 system + 历史 + 工具' },
        { name: 'run_model', note: '调用 LLM 提供者' },
        { name: 'render_outbound', note: '为 channel 格式化回复' },
        { name: 'save_state', note: '持久化到 tape' },
        { name: 'dispatch_outbound', note: '发送到 CLI / Telegram 等' },
      ],
    },
    tapeModel: {
      eyebrow: '上下文模型',
      heading: 'Tape：统一的事实模型。',
      description: [
        '上下文不积累在 session 状态中，而是从追加式 tape——不可变事实的序列——中重建。Entry 记录发生了什么；Anchor 标记阶段边界并携带结构化状态。',
        '修正追加新事实来取代旧事实——从不覆盖。View 从 anchor 往后组装，而非整体继承。每一个决策都可审计、可重放、可 fork。',
      ],
    },
    testimonials: {
      eyebrow: '社区',
      heading: '大家怎么说。',
    },
  },
};

export function getLandingPageData(locale: LandingLocale): LandingPageData {
  return landingPageData[locale];
}

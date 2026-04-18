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

export type FeatureItem = { icon: string; title: string; description: string };

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
      eyebrow: 'Features',
      heading: 'Every decision has a reason.',
      subheading: 'Bub was designed for real multi-agent collaboration from day one — not retrofitted for it.',
      features: [
        {
          icon: 'webhook',
          title: 'Hook-First',
          description:
            '~200-line core. Every turn stage is a pluggy hook. Builtins are just default plugins — override any stage without forking the runtime.',
        },
        {
          icon: 'layers',
          title: 'Tape Context',
          description:
            'Context is reconstructed from append-only tape records, not accumulated in session state. No lossy summaries, no phantom memory.',
        },
        {
          icon: 'radioTower',
          title: 'Channel-Agnostic',
          description:
            "The same process_inbound() pipeline drives CLI, Telegram, and any channel you add. Hooks never know which surface they're on.",
        },
        {
          icon: 'fileSearch',
          title: 'Skills as Docs',
          description:
            'Skills are SKILL.md files with validated frontmatter — not code modules with magic registration. Discoverable, overridable, auditable.',
        },
        {
          icon: 'users',
          title: 'Operator Equivalence',
          description:
            'Humans and agents share the same operator model: same boundaries, same evidence trails, same handoff semantics. No special cases.',
        },
        {
          icon: 'puzzle',
          title: 'Plugin System',
          description:
            'Python entry-points under group="bub". Later-registered plugins run first and override earlier ones. No framework privilege.',
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
      badge: 'Hook 优先 · Tape 驱动 · Channel 任选',
      title: 'Bub，与 Human 同在的轻量级 Agent 运行时。',
      description:
        '核心代码约 200 行。每个阶段通过 Hook 驱动。由 Tape 模型记录每次决策。 随时接入 CLI、Telegram 或任意你喜欢的 Channel。',
      primaryHref: '/zh-cn/docs/getting-started/installation/',
      primaryLabel: '开始使用',
      contributorsLabel: '由全球开发者共同打造',
    },
    features: {
      eyebrow: '特性',
      heading: '每个决定都有理由。',
      subheading: 'Bub 从第一天起就为真实的多 agent 协作而精心设计，而不是拍脑门决定。',
      features: [
        {
          icon: 'webhook',
          title: 'Hook 优先',
          description: '约 200 行核心代码。每个阶段都是一个可插拔的 hook，灵活的 Hook 设计可以实现任何满足你需要的插件。而内置实现只是默认插件——你可以随时替换任意阶段。',
        },
        {
          icon: 'layers',
          title: 'Tape 上下文',
          description: '所有上下文均可从追加式 tape 记录中重建、Fork，而非堆叠在 session 状态中。无损摘要，避免幻觉记忆。',
        },
        {
          icon: 'radioTower',
          title: 'Channel 任选',
          description: '同一个 Pipeline 上驱动 CLI、Telegram 甚至你添加的任何 channel。Hooks 从不感知运行在哪个地方，可以是任意来源。',
        },
        {
          icon: 'fileSearch',
          title: 'Skills as Docs',
          description: 'Skills 是带验证 frontmatter 的 SKILL.md 文件——而黑箱代码模块。确保你的 SKILL 都是可被发现、覆盖且审计。',
        },
        {
          icon: 'users',
          title: '操作对等',
          description: '人类与 Agent 共享同一操作者模型：相同的边界、证据链和 Hand-off 语义。',
        },
        {
          icon: 'puzzle',
          title: '插件系统',
          description: '显式覆盖插件设计：后注册的插件优先运行并覆盖前者，没有任何框架优先限制。',
        },
      ],
    },
    hookIntro: {
      eyebrow: '架构',
      heading: '处处都是 Hooks。',
      description: [
        'Bub 中每个阶段都是一个可插拔的 hook。',
        '通过注册你自己的 hook 覆盖任意阶段。无需 fork。',
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
      heading: 'Tape，拍板。',
      description: [
        '上下文不积累在 session 状态中，而是从追加式 tape——不可变事实的序列——中重建：Entry 记录发生了什么、Anchor 标记阶段边界并携带结构化状态。',
        '通过修正追加新事实来取代旧事实——从不覆盖。视图从 anchor 往后组装，而非整体继承。每次决策都可审计、可重放、可 fork。',
      ],
    },
    testimonials: {
      eyebrow: '社区',
      heading: '好东西，值得赞。',
    },
  },
};

export function getLandingPageData(locale: LandingLocale): LandingPageData {
  return landingPageData[locale];
}

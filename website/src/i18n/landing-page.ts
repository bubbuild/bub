// ---------------------------------------------------------------------------
// Landing-page content copy — single source of truth for all landing-page text.
// Structured data (arrays, nested objects with icons/colors/hrefs) lives here.
// Nav, footer, 404, and post-list copy live in i18n/ui.ts.
// ---------------------------------------------------------------------------

type HeroCopy = {
  badge?: string;
  title?: string;
  description?: string;
  primaryHref: string;
  primaryLabel?: string;
  contributorsLabel?: string;
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

type LandingPageCopy = {
  hero: HeroCopy;
  features?: FeaturesCopy;
  hookIntro?: HookIntroCopy;
  tapeModel?: TapeModelCopy;
  testimonials?: TestimonialsCopy;
};

export type LandingLocale = 'en' | 'zh-cn';

const landingPageCopy: Record<LandingLocale, LandingPageCopy> = {
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
      testimonials: [
        { name: 'Alex Chen', handle: '@alexchen_dev', text: 'The hook system is brilliant. I replaced the built-in model runner with my own in under 20 lines. No forking, no framework hacks. Everything else just kept working.' },
        { name: 'Sam Wright', handle: '@samwright', text: "Finally, an agent framework that doesn't pretend group conversations are a solved problem. Bub's tape model just makes sense for real-world collaboration." },
        { name: 'Priya Nair', handle: '@priya_builds', text: 'What sets Bub apart: the same pipeline drives CLI and Telegram. I added a Slack channel adapter in a weekend. Zero changes to my hooks.' },
        { name: 'Jordan Lee', handle: '@jordanlee', text: 'Tape-driven context means I can replay and audit every agent decision. No more mystery about why the agent said what it said.' },
        { name: 'Maria Gomez', handle: '@maria_ai', text: 'Skills as Markdown files is such a refreshing idea. No magic registration, no hidden state. Just discoverable, version-controlled documents.' },
        { name: 'Tom Baker', handle: '@tombaker_', text: 'We run Bub in production with 3 different LLM providers. The plugin system handles provider switching with zero downtime.' },
        { name: 'Yuki Tanaka', handle: '@yuki_dev', text: 'Operator equivalence changed how I think about human-agent collaboration. Humans and agents sharing the same model makes handoffs seamless.' },
        { name: 'Li Wei', handle: '@liwei_ml', text: '~200 lines of core code. I read the entire framework in one sitting. That kind of simplicity is rare and valuable.' },
        { name: 'Emma Scott', handle: '@emma_builds', text: "The best part about Bub is what it doesn't do. No opinions about which LLM, no lock-in to a provider, no hidden abstractions." },
        { name: 'Ryan Park', handle: '@ryanpark', text: "Deployed Bub on Telegram for our team's workspace. The agent handles concurrent conversations without any special configuration." },
        { name: 'Nina Patel', handle: '@ninapatel', text: 'Corrections in the tape never overwrite. They append. This makes debugging agent behavior trivially easy.' },
        { name: 'David Kim', handle: '@dkim_eng', text: 'Hook-first means we control every stage. When our compliance team needed audit logs, we added a save_state hook plugin in an afternoon.' },
      ],
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
      testimonials: [
        { name: 'Alex Chen', handle: '@alexchen_dev', text: 'Hook 系统设计得很妙。我用不到 20 行代码就替换了内置的模型运行器。不需要 fork，不需要框架 hack。其他一切照常运转。' },
        { name: 'Sam Wright', handle: '@samwright', text: '终于有一个不假装群聊是已解决问题的 agent 框架了。Bub 的 tape 模型对真实世界的协作就是合理的。' },
        { name: 'Priya Nair', handle: '@priya_builds', text: 'Bub 的独特之处：同一条管道同时驱动 CLI 和 Telegram。我用一个周末就加了一个 Slack channel 适配器，hook 零修改。' },
        { name: 'Jordan Lee', handle: '@jordanlee', text: 'Tape 驱动的上下文意味着我可以回放和审计每一个 agent 决策。再也不用猜 agent 为什么这么说了。' },
        { name: 'Maria Gomez', handle: '@maria_ai', text: 'Skills 作为 Markdown 文件的想法让人眼前一亮。没有魔法注册，没有隐藏状态。就是可发现的、版本控制的文档。' },
        { name: 'Tom Baker', handle: '@tombaker_', text: '我们在生产环境用 3 个不同的 LLM 提供者跑 Bub。插件系统实现了零停机的提供者切换。' },
        { name: 'Yuki Tanaka', handle: '@yuki_dev', text: '操作者对等改变了我对人-agent 协作的认知。人类和 agent 共享同一模型让交接变得无缝。' },
        { name: 'Li Wei', handle: '@liwei_ml', text: '约 200 行核心代码。我一口气读完了整个框架。这种简洁性非常珍贵。' },
        { name: 'Emma Scott', handle: '@emma_builds', text: 'Bub 最好的地方在于它不做什么。不限定 LLM，不锁定提供者，没有隐藏的抽象。' },
        { name: 'Ryan Park', handle: '@ryanpark', text: '在 Telegram 上为团队部署了 Bub。agent 无需任何特殊配置就能处理并发对话。' },
        { name: 'Nina Patel', handle: '@ninapatel', text: 'Tape 中的修正永远不覆盖，而是追加。这让调试 agent 行为变得轻而易举。' },
        { name: 'David Kim', handle: '@dkim_eng', text: 'Hook-first 意味着我们控制每个阶段。合规团队需要审计日志时，我们一个下午就用 save_state hook 插件搞定了。' },
      ],
    },
  },
};

export function getLandingPageCopy(locale: LandingLocale): LandingPageCopy {
  return landingPageCopy[locale];
}

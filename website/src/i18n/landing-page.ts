// ---------------------------------------------------------------------------
// Landing-page content copy — section-level content that's too rich for flat
// UI strings (arrays, nested objects with icons/colors). Nav, footer, and 404
// copy now live in i18n/ui.ts.
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
      primaryHref: '/en/getting-started/installation/',
    },
  },
  'zh-cn': {
    hero: {
      badge: 'Hook-first · Tape 驱动 · Channel 无关',
      title: 'Bub 是一个为与人并行的 agent 设计的轻量运行时。',
      description:
        '~200 行核心代码。Hooks 重塑每个 turn 阶段。Tapes 记录每一个决策。Channels 适配任何表面——CLI、Telegram 或你自己的。',
      primaryHref: '/zh-cn/getting-started/installation/',
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
          icon: 'wrench',
          title: '插件系统',
          description: '通过 group="bub" 的 Python entry-points 注册。后注册的插件优先运行并覆盖前者。没有框架特权。',
          color: 'blue',
        },
      ],
    },
    hookIntro: {
      eyebrow: '架构',
      heading: 'Hooks 定义每个 turn 阶段。',
      description: [
        'Bub turn 中的每个阶段都是一个 pluggy hook。内置实现只是另一个插件。',
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
      heading: 'Tape：唯一的事实来源。',
      description: [
        '上下文不保存在脆弱的 session 状态中，而是从追加式 tape——一个不可变记录的账本——中重建。',
        '修正会追加新条目，而非覆盖旧条目。每个操作都可审计、可续接、可安全重放。',
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

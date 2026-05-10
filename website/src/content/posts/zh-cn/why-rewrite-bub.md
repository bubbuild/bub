---
title: "我们为什么要重写 bub?"
description: "用微内核和插件化帮助 bub 解决维护负担和用户复杂性问题。"
date: 2026-04-07
locale: zh-cn
tags:
  - design
  - engineering
---

## 引子

[bub](https://bub.build) 最开始是 [PsiACE](https://github.com/psiace) 做的一个小型 Python Agent 项目，用来实践自己的 Agent 想法。2026 年 1 月的最后十天里，OpenClaw 经过两次改名风波迎来了暴发，所有人都在讨论，都在使用。而我[^1]不满足于当个纯粹的使用者，我想要拆解开看看它是怎么做的，虽然基于 LLM 的了解我能勾勒出大致的轮廓，但真要了解它，还得深入代码中去。那时已经到了二月，OpenClaw 的代码库已经非常庞大了，每天合入的 PR 不计其数，全身上下充满了 vibe 的气氛（双关）。于是了解到了 [Nanobot](https://github.com/HKUDS/nanobot) 这个项目，号称是 OpenClaw 的极简实现，这非常适合拿来学习[^2]。于是我很快就跑起了一个实例，放到 TG 群里使用。

很快我们发现 Nanobot 并不适合群聊中的场景，于是我们决定根据现有的理解，和针对群聊的场景，来改造一下 bub，把它变成一个真正的类似龙虾的自主 Agent[^3]。这个版本在 2 月 6 日就完成了，在 Agent 的内核上增加了 Telegram 的消息收发能力，和[基于 tape 的记忆机制](https://tape.systems)。但在短短一个月后，我们决定对 bub 进行[大幅重写](https://github.com/bubbuild/bub/pull/85)，下面我想分享一些我的思考。

[^1]: 本文虽使用了「我」这个人称代词，实际上并非我一人的智慧结晶，bub 是在多人使用的基础上不断反馈迭代，只是为了叙述方便权且如此。
[^2]: 参考 Nanobot 团队关于设计哲学的[文章](https://x.com/xubinrencs/status/2041186947994091872?s=20)
[^3]: 关于这次复刻，可以参考我的上一篇文章：[创造一只龙虾，需要些什么?](https://frostming.com/posts/2026/create-a-claw)

## 问题

最开始 Bub 只有 Telegram 的通信能力，后来又增加了 Discord 的支持，然后，为了解决王不见王的问题，我增加了个新的消息渠道：[tg-message-feed](https://x.com/frostming90/status/2024484547950498300?s=20)，增加一个渠道意味着要在 `bub.channels` 增加一个新的类，以及一些新的工具或技能。此外，在使用的过程中我们也积累了一些技能，其中有些是针对特定场景的，而另一些则比较通用，我们希望把这些额外的能力抽取出来给用户使用。这时我发现，我们需要把这些都集成进 bub 本体中，并增加许多特性开关。我意识到这样下去，bub 迟早会变成另一个 nanobot，甚至另一个 OpenClaw，大家可以看看 Nanobot 现在的 config：[schema.py](https://github.com/HKUDS/nanobot/blob/82dec12f6641fac66172fbf9337a39a674629c6e/nanobot/config/schema.py#L202)，看看那庞大的 `ProviderConfig`，大家能意识到问题所在吗？

大多数用户，可能只用到一两个 Provider，一两个 Channel，却要面对如此多的配置项，或者面板开关，即使它们默认是不启用的，用户仍然要迷失在其中。

另一个问题是维护负担，每个人都有不同的使用偏好，如果你的开源项目受欢迎，你马上会收到很多增加新功能的 PR，毫不意外地，都是 vibe 的。这没问题，我不反对。你看着社区越来越壮大，贡献人数节节攀升，他们都往项目里增加功能，一时间人来人往，门庭若市，自豪感油然而生。那么，然后呢？热闹的派对终将结束，原来的贡献者继续新的征程，你看着新增的 `whatsapp.py` 支持，自己却从来不用，这时地球的另一端有人发了一个 bug report，你要怎么办？是让 vibe 打败 vibe，做一个全盲维护，还是往手机上装一个 Whatsapp，捣鼓一个完全陌生的软件？

对于内核的修改就更棘手了，Bub 是基于 tape 的，但 Alice 觉得这个系统不够好，想要改成「先进」的三层记忆架构，Bob 觉得太复杂了，不如用[Nowledge Mem](https://mem.nowledge.co/)，难道要实现多个 MemoryBackend，然后用特性开关来选择吗？

## 思考

所以我在思考一个问题，在 Vibe coding 的时代，我们开源的到底是什么？那个新增的 `whatsapp.py`，除了给项目增加数据，对维护者本人意味着什么？项目维护者为什么要对一个随机用户 vibe 的代码负责？

我的答案是，把额外的功能，分离出去，变成一个**精心设计的轻内核**+**随便 vibe 的功能插件**的架构。这个内核要足够稳定，而且能让 Agent 容易理解，由维护者保证质量；而功能插件则利用开放的接口去扩展，可以任意 vibe，甚至直接让 Agent 为功能需求本地生成代码来实现。这两者的维护模型完全不同，前者严，后者宽。同时这也解决了按需安装的问题，你只用关心已经安装的插件的配置。在内核方面，我不太相信现今 Coding Agent 的能力，选择了手工古法实现————这有可能是我最后一次这样做。主要是合理抽象、单向依赖和接口的最小化和自由度。

采用了这样的设计后，在理想情况下，主项目得到的贡献不会很多，然而每个用户都维护一些自己的插件，并且生态会被公开索引在 [hub.bub.build](https://hub.bub.build)（数据源自 [bubbuild/buildscape](https://github.com/bubbuild/buildscape)），让其他人能发现它们。每个人用的都是不同的插件集合，适合自己的使用场景。

另外，利用 [PEP 517 build hook](https://github.com/frostming/pdm-build-skills)，我还实现了把 skill 文件和插件打包在一起，这是非常常见的场景————当你增加了飞书的支持，你通常需要一个飞书的技能。

## 接口

那么现在的 bub 都有哪些扩展接口呢？我在这里列举一些主要的接口和插件例子：

1. `load_state()` 和 `save_state()`，它们分别在一个 Agent turn[^4] 的开始和结束被调用，其中 `load_state()` 可以返回一个状态字典，这个状态将在整个 turn 中共享，这两个接口通常可以实现 `pre_turn` 和 `post_turn` 钩子，以及状态的注入和持久化。例子：[nowledge-mem-bub](https://github.com/nowledge-co/community/tree/main/nowledge-mem-bub-plugin)
2. `run_model()`，这是 Agent 调用的核心接口，负责处理 user prompt 得到模型的输出。所以简单的原样返回即可以把 bub 变成一个 Echo agent，以及调用其他 Agent 处理 prompt，比如 [bub-codex](https://github.com/bubbuild/bub-contrib/blob/main/packages/bub-codex/)
3. `register_cli_commands()`，这个接口允许插件注册一些命令行命令，比如：[bub-wechat](https://github.com/bubbuild/bub-contrib/blob/38475520e77db6e2c697f96d0e7ab06fc36c67de/packages/bub-wechat/src/bub_wechat/plugin.py#L11-L21)
4. `provide_tape_store()`，这个接口允许插件自定义 tape 的存储方式，可以保存在 DB 里，或者保存在一个外挂的服务中，非常适合用来改造记忆系统。例子：[bub-tapestore-sqlite](https://github.com/bubbuild/bub-contrib/tree/main/packages/bub-tapestore-sqlite)
5. `provide_channels()`，这个接口允许插件提供一个或多个渠道，这个渠道在整个应用周期的开始时启动，结束时销毁，所以不仅可以用来做消息收发的通道，也适合任何需要长时间运行的服务，比如 HTTP server，[bub 的定时任务系统](https://github.com/bubbuild/bub-contrib/tree/main/packages/bub-schedule)就是基于这个来实现的，尽管它听上去和「渠道」没什么联系。

完整的插件接口参考 [bub 的文档](https://bub.build/docs/build/hooks/)。

[^4]: 一个 turn 指的是 Agent 处理一次 user prompt 的完整过程，包括一整个 ReAct loop 的执行。

## 最后

最近我们也在 bub 上做了很多好玩的东西：[小爱音箱][xiaoai]，[folotoy]，[Robo eyes][face]，都是用现有的插件接口实现的。其实这些插件的代码我都没怎么看过，完全是 vibe 的产物。也欢迎大家来 vibe bub 的插件，并把它们登记到 [hub.bub.build](https://hub.bub.build)，让更多人能找到。

[xiaoai]: https://github.com/bubbuild/bub-xiaoai
[folotoy]: https://github.com/bubbuild/bub-folotoy
[face]: https://github.com/bubbuild/bub-face

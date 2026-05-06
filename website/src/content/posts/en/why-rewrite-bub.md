---
title: "Why We Rewrote Bub"
description: "How a microkernel and plugin architecture help Bub reduce maintenance burden and configuration complexity."
date: 2026-04-07
locale: en
tags:
  - design
  - engineering
---

## Introduction

[bub](https://bub.build) began as a small Python agent project by [PsiACE](https://github.com/psiace), mainly as a way to experiment with agent ideas. In the last ten days of January 2026, OpenClaw went viral after two renames. Everyone was talking about it, and everyone was using it. I[^1] did not want to stay a user. I wanted to take it apart and understand how it worked. LLMs had already given me a rough mental model, but to really understand it, I had to read the code.

By then it was already February, and the OpenClaw codebase had become huge. PRs were landing constantly, and the whole project was soaked in vibe, in every sense of the word. That is when I found [Nanobot](https://github.com/HKUDS/nanobot), which positioned itself as a minimal implementation of OpenClaw. It looked much easier to learn from[^2], so I quickly spun up an instance and started using it in Telegram groups.

It did not take long for us to realize that Nanobot was not a good fit for group chat scenarios. So we decided to reshape bub around what we had learned and what group chats actually needed, turning it into a true autonomous agent in the spirit of Claw[^3]. That version was finished on February 6. We added Telegram messaging on top of the agent runtime, along with a [tape-based memory model](https://tape.systems). But only a month later, we decided to [rewrite bub in a major way](https://github.com/bubbuild/bub/pull/85). This post explains why.

[^1]: I use “I” in this post for convenience, but bub is not the result of one person alone. It evolved through repeated feedback from multiple users.
[^2]: See the Nanobot team's post on its design philosophy [here](https://x.com/xubinrencs/status/2041186947994091872?s=20).
[^3]: For more on that first reimplementation, see my earlier post: [What Does It Take to Create a Claw?](https://frostming.com/posts/2026/create-a-claw)

## The Problems

At first, Bub only supported Telegram. Later it gained Discord support. Then, to avoid bot-to-bot conflicts, I added another message channel: [tg-message-feed](https://x.com/frostming90/status/2024484547950498300?s=20). Every new channel meant another class under `bub.channels`, plus more tools or skills. Over time, we also accumulated a growing set of skills. Some were tied to specific scenarios, while others were more general and worth exposing to users.

That was when the problem became obvious: all of this would have to be integrated into Bub itself, with more and more feature flags layered on top. If we kept going in that direction, Bub would eventually become another Nanobot, or even another OpenClaw. Just look at Nanobot's current config in [schema.py](https://github.com/HKUDS/nanobot/blob/82dec12f6641fac66172fbf9337a39a674629c6e/nanobot/config/schema.py#L202), especially that enormous `ProviderConfig`. The problem is not hard to see.

Most users only need one or two providers and one or two channels, yet they still end up staring at a wall of settings and switches. Even if most of them are disabled by default, the complexity is still there, and users still get lost in it.

The other problem is maintenance burden. Different people want different things. As soon as an open source project becomes popular, you start getting PRs for new features, and yes, many of them are vibe-coded. I do not object to that. It is exciting to watch a community grow. More contributors show up, more features land, and for a while everything looks lively and healthy.

But then what?

The party ends. Original contributors move on. You are left looking at a newly added `whatsapp.py` integration that you never use, and then a bug report arrives from the other side of the world. What are you supposed to do? Fight vibe with vibe and maintain it blind, or install WhatsApp on your phone and start debugging a workflow you do not even use?

Kernel-level changes are even harder. Bub is built around tape, but Alice thinks tape is outdated and wants a more “advanced” three-layer memory architecture. Bob thinks that is overkill and would rather use [Nowledge Mem](https://mem.nowledge.co/). Should Bub really implement multiple `MemoryBackend`s and expose them all behind feature flags?

## The Idea

That led me to a bigger question: in the era of vibe coding, what exactly are we open-sourcing?

What does an extra `whatsapp.py` actually mean to the maintainer, other than more code in the repository? Why should the maintainer be responsible for code that a random user vibed into existence?

My answer is to split extra functionality out of the core and move to a model with a **carefully designed lightweight kernel** plus **freely vibe-coded feature plugins**.

The kernel should be stable, easy for agents to understand, and quality-controlled by maintainers. Plugins should extend the system through open interfaces, with far fewer constraints. They can be written however people want, or even generated locally by an agent to satisfy a specific need. These two layers have completely different maintenance models: the kernel should be strict, while plugins can be loose.

This also solves the problem of optional installation. Users only need to care about the configuration of the plugins they actually install. As for the kernel itself, I still do not trust current coding agents enough to let them design it for me, so I built it the old-fashioned way by hand. This may well be the last time I do that. The priorities were clear abstractions, one-way dependencies, and interfaces that stay minimal without becoming rigid.

In the ideal version of this model, the main project does not receive a huge number of feature contributions. Instead, each user maintains some of their own plugins, and the ecosystem is indexed publicly on [hub.bub.build](https://hub.bub.build) (sourced from [bubbuild/buildscape](https://github.com/bubbuild/buildscape)) so others can discover them. Different users will run different combinations, depending on their own scenarios.

Using a [PEP 517 build hook](https://github.com/frostming/pdm-build-skills), I also made it possible to package skill files together with plugins. That is a very common case: if you add Feishu support, you usually also want a Feishu skill.

## Interfaces

So what extension points does Bub provide today? Here are a few of the main ones, along with example plugins:

1. `load_state()` and `save_state()`: these are called at the beginning and end of an agent turn[^4]. `load_state()` can return a state dictionary that is shared throughout the entire turn. Together, these hooks can be used to implement `pre_turn` and `post_turn` behavior, as well as state injection and persistence. Example: [nowledge-mem-bub](https://github.com/nowledge-co/community/tree/main/nowledge-mem-bub-plugin)
2. `run_model()`: this is the core interface for model invocation. It takes a user prompt and returns model output. A trivial passthrough implementation turns Bub into an echo agent, while a more advanced one can delegate prompts to other agents, as in [bub-codex](https://github.com/bubbuild/bub-contrib/blob/main/packages/bub-codex/)
3. `register_cli_commands()`: allows plugins to register CLI commands. Example: [bub-wechat](https://github.com/bubbuild/bub-contrib/blob/38475520e77db6e2c697f96d0e7ab06fc36c67de/packages/bub-wechat/src/bub_wechat/plugin.py#L11-L21)
4. `provide_tape_store()`: allows plugins to define custom tape storage, whether in a database or in an external service. This makes it a natural extension point for memory-system experimentation. Example: [bub-tapestore-sqlite](https://github.com/bubbuild/bub-contrib/tree/main/packages/bub-tapestore-sqlite)
5. `provide_channels()`: allows plugins to provide one or more channels. These channels are started when the application starts and torn down when it exits. That makes them useful not only for messaging, but also for any long-running service, such as an HTTP server. [Bub's scheduling system](https://github.com/bubbuild/bub-contrib/tree/main/packages/bub-schedule), for example, is built on this interface even though it does not sound like a “channel” at first glance.

For the full plugin API, see the [Bub documentation](https://bub.build/docs/build/hooks/).

[^4]: A turn is one complete pass of handling a user prompt, including the entire ReAct loop.

## Closing

Recently we have built a lot of fun things on top of Bub, including [XiaoAI speakers][xiaoai], [folotoy][folotoy], and [Robo eyes][face]. All of them were implemented through the existing plugin interfaces. To be honest, I have barely read some of that plugin code myself. It is pure vibe in the wild.

If that sounds fun, come vibe some Bub plugins too — and list them on [hub.bub.build](https://hub.bub.build) so others can find them.

[xiaoai]: https://github.com/bubbuild/bub-xiaoai
[folotoy]: https://github.com/bubbuild/bub-folotoy
[face]: https://github.com/bubbuild/bub-face

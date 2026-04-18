import { defineCollection, z } from 'astro:content';
import { docsLoader, i18nLoader } from '@astrojs/starlight/loaders';
import { docsSchema, i18nSchema } from '@astrojs/starlight/schema';
import { file, glob } from 'astro/loaders';

const posts = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/posts' }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    date: z.coerce.date(),
    locale: z.string(),
    tags: z.array(z.string()).optional(),
  }),
});

const userwall = defineCollection({
  loader: file('./src/data/userwall.yml'),
  schema: z.object({
    name: z.string(),
    handle: z.string(),
    platform: z.string().optional(),
    github: z.string().optional(),
    text: z.string(),
    ref: z.string().url().optional(),
  }),
});

export const collections = {
  docs: defineCollection({ loader: docsLoader(), schema: docsSchema() }),
  i18n: defineCollection({ loader: i18nLoader(), schema: i18nSchema() }),
  posts,
  userwall,
};

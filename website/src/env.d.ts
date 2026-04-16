/// <reference path="../.astro/types.d.ts" />

interface ImportMetaEnv {
  /** GitHub personal access token injected at build time for GitHub API calls.
   *  Increases the API rate limit from 60 to 5000 req/hour.
   *  Never bundled into the output — only used during `astro build`. */
  readonly GITHUB_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

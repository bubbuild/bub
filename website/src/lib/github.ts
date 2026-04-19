import repoStatsSnapshot from '@/data/github-snapshot';

export const REPO_OWNER = 'bubbuild';
export const REPO_NAME = 'bub';
export const REPO_SLUG = `${REPO_OWNER}/${REPO_NAME}`;
export const REPO_URL = `https://github.com/${REPO_SLUG}`;
export const CONTRIBUTORS_URL = `${REPO_URL}/graphs/contributors`;

export interface GitHubContributor {
  login: string;
  avatar_url: string;
  html_url: string;
  contributions: number;
  type: 'User' | 'Bot' | string;
}

export interface RepoStats {
  stars: number;
  starsFormatted: string | undefined;
  contributors: GitHubContributor[];
}

export function getRepoStats(): RepoStats {
  return repoStatsSnapshot;
}

export function gitHubAvatarUrl(username: string | undefined, size = 80): string | undefined {
  return username ? `https://github.com/${username}.png?size=${size}` : undefined;
}

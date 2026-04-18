// ---------------------------------------------------------------------------
// GitHub API utilities
//
// Data is fetched at most once every CACHE_TTL_MS (24 h) via a module-level
// in-memory cache.  This means:
//   • SSG  — cache is cold on every `astro build`, so data is always fresh.
//   • SSR  — the cache is shared across requests within the same server
//             process and expires after 24 h, keeping API usage minimal.
//
// GITHUB_TOKEN is read from the environment at call time and is never
// bundled into any client-side output.
// ---------------------------------------------------------------------------

export const REPO_OWNER = 'bubbuild';
export const REPO_NAME = 'bub';
export const REPO_SLUG = `${REPO_OWNER}/${REPO_NAME}`;
export const REPO_URL = `https://github.com/${REPO_SLUG}`;
export const CONTRIBUTORS_URL = `${REPO_URL}/graphs/contributors`;
const API_BASE = 'https://api.github.com';

/** How long (ms) to keep cached data before re-fetching. Default: 24 h. */
const CACHE_TTL_MS = 24 * 60 * 60 * 1_000;

export interface GitHubContributor {
  login: string;
  avatar_url: string;
  html_url: string;
  contributions: number;
  type: 'User' | 'Bot' | string;
}

export interface RepoStats {
  /** Raw star count (0 when unavailable). */
  stars: number;
  /** Formatted star string, e.g. "1.2k" (undefined when stars === 0). */
  starsFormatted: string | undefined;
  /** Top contributors ordered by contribution count. */
  contributors: GitHubContributor[];
}

// ---------------------------------------------------------------------------
// Module-level cache — survives across requests in SSR; cold on each build.
// ---------------------------------------------------------------------------
interface Cache {
  stats: RepoStats;
  fetchedAt: number;
}

let _cache: Cache | null = null;

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function buildHeaders(): HeadersInit {
  const token = import.meta.env.GITHUB_TOKEN;
  return {
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

/** Format a raw star count into a human-readable string (e.g. 1234 → "1.2k"). */
export function formatStars(count: number): string | undefined {
  if (count <= 0) return undefined;
  if (count >= 1_000) {
    return `${(count / 1_000).toFixed(1).replace(/\.0$/, '')}k`;
  }
  return String(count);
}

async function fetchFromAPI(contributorLimit: number): Promise<RepoStats> {
  const headers = buildHeaders();

  const [repoRes, contribRes] = await Promise.allSettled([
    fetch(`${API_BASE}/repos/${REPO_OWNER}/${REPO_NAME}`, { headers }),
    fetch(
      // Fetch extra entries so bots filtered out still leave `contributorLimit` humans.
      `${API_BASE}/repos/${REPO_OWNER}/${REPO_NAME}/contributors?per_page=${contributorLimit + 10}&anon=false`,
      { headers },
    ),
  ]);

  let stars = 0;
  if (repoRes.status === 'fulfilled' && repoRes.value.ok) {
    const data = await repoRes.value.json() as { stargazers_count: number };
    stars = data.stargazers_count ?? 0;
  }

  let contributors: GitHubContributor[] = [];
  if (contribRes.status === 'fulfilled' && contribRes.value.ok) {
    const data = await contribRes.value.json() as GitHubContributor[];
    contributors = Array.isArray(data)
      ? data.filter((c) => c.type === 'User').slice(0, contributorLimit)
      : [];
  }

  return { stars, starsFormatted: formatStars(stars), contributors };
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Return cached repo stats, refreshing when the cache is cold or stale.
 *
 * @param contributorLimit  Max number of contributors to return (default 15).
 */
export async function getRepoStats(contributorLimit = 15): Promise<RepoStats> {
  const now = Date.now();
  if (_cache && now - _cache.fetchedAt < CACHE_TTL_MS) {
    return _cache.stats;
  }

  try {
    const stats = await fetchFromAPI(contributorLimit);
    _cache = { stats, fetchedAt: now };
    return stats;
  } catch {
    // On total failure, return stale cache if available, else empty defaults.
    if (_cache) return _cache.stats;
    return { stars: 0, starsFormatted: undefined, contributors: [] };
  }
}

/**
 * Build a GitHub avatar URL for a username.
 *
 * Uses GitHub's redirect-based endpoint — no API call, no token, no rate limit.
 * Returns `undefined` when no username is provided.
 */
export function gitHubAvatarUrl(username: string | undefined, size = 80): string | undefined {
  return username ? `https://github.com/${username}.png?size=${size}` : undefined;
}

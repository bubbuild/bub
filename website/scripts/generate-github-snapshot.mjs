import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const repoOwner = 'bubbuild';
const repoName = 'bub';
const contributorLimit = 15;
const outputPath = path.resolve('src/data/github-snapshot.ts');
const apiBase = 'https://api.github.com';
const fallbackSnapshot = {
  stars: 0,
  starsFormatted: undefined,
  contributors: [],
};

function formatStars(count) {
  if (count <= 0) return undefined;
  if (count >= 1_000) {
    return `${(count / 1_000).toFixed(1).replace(/\\.0$/, '')}k`;
  }
  return String(count);
}

function buildHeaders() {
  return {
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    ...(process.env.GITHUB_TOKEN ? { Authorization: `Bearer ${process.env.GITHUB_TOKEN}` } : {}),
  };
}

function renderSnapshot(stats) {
  return `import type { RepoStats } from '@/lib/github';

const repoStatsSnapshot: RepoStats = {
  stars: ${stats.stars},
  starsFormatted: ${stats.starsFormatted === undefined ? 'undefined' : JSON.stringify(stats.starsFormatted)},
  contributors: ${JSON.stringify(stats.contributors, null, 2)},
};

export default repoStatsSnapshot;
`;
}

async function fetchRepoStats() {
  const headers = buildHeaders();
  const [repoRes, contribRes] = await Promise.allSettled([
    fetch(`${apiBase}/repos/${repoOwner}/${repoName}`, { headers }),
    fetch(
      `${apiBase}/repos/${repoOwner}/${repoName}/contributors?per_page=${contributorLimit + 10}&anon=false`,
      { headers },
    ),
  ]);

  let stars = 0;
  if (repoRes.status === 'fulfilled' && repoRes.value.ok) {
    const data = await repoRes.value.json();
    stars = data.stargazers_count ?? 0;
  }

  let contributors = [];
  if (contribRes.status === 'fulfilled' && contribRes.value.ok) {
    const data = await contribRes.value.json();
    contributors = Array.isArray(data)
      ? data
          .filter((contributor) => contributor.type === 'User')
          .slice(0, contributorLimit)
          .map((contributor) => ({
            login: contributor.login,
            avatar_url: contributor.avatar_url,
            html_url: contributor.html_url,
            contributions: contributor.contributions,
            type: contributor.type,
          }))
      : [];
  }

  return {
    stars,
    starsFormatted: formatStars(stars),
    contributors,
  };
}

async function main() {
  await mkdir(path.dirname(outputPath), { recursive: true });

  try {
    const snapshot = await fetchRepoStats();
    await writeFile(outputPath, renderSnapshot(snapshot), 'utf8');
    console.log(
      `Generated GitHub snapshot: stars=${snapshot.stars}, contributors=${snapshot.contributors.length}`,
    );
  } catch (error) {
    await writeFile(outputPath, renderSnapshot(fallbackSnapshot), 'utf8');
    console.warn('GitHub snapshot fetch failed; wrote an empty fallback snapshot.');
    console.warn(error instanceof Error ? error.message : String(error));
  }
}

await main();

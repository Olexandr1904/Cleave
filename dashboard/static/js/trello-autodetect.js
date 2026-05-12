// Mirror of integrations/trello/list_autodetect.py
// Keep both in sync — same patterns, same tie-break rules.

const PATTERNS = {
  todo:        ['to do', 'todo', 'backlog', 'ready', 'inbox', 'queue'],
  in_progress: ['in progress', 'doing', 'wip', 'dev', 'development'],
  in_review:   ['in review', 'review', 'code review', 'pr', 'qa'],
  done:        ['done', 'shipped', 'complete', 'completed', 'closed', 'merged', 'released'],
};

function normalize(name) {
  return name.toLowerCase().trim().replace(/[-_/]+/g, ' ').replace(/\s+/g, ' ');
}

function wordIn(pattern, text) {
  // Word-boundary check: pattern surrounded by non-word characters (or string edges)
  const escaped = pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(`(?<!\\w)${escaped}(?!\\w)`);
  return re.test(text);
}

export function autodetectStatusMapping(lists) {
  const candidates = { todo: [], in_progress: [], in_review: [], done: [] };
  for (const lst of lists) {
    const name = lst.name;
    if (!name) continue;
    const norm = normalize(name);
    for (const [statusKey, patterns] of Object.entries(PATTERNS)) {
      let tier = null;
      for (const pat of patterns) {
        if (norm === pat) { tier = 0; break; }
        if (wordIn(pat, norm) && tier === null) tier = 1;
      }
      if (tier !== null) candidates[statusKey].push([tier, lst.pos || 0, lst.id]);
    }
  }
  const result = {};
  for (const [statusKey, hits] of Object.entries(candidates)) {
    if (!hits.length) continue;
    hits.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    result[statusKey] = hits[0][2];
  }
  return result;
}

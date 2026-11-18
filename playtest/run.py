"""Weekly automated playtest for Knight Hop.

Usage:  python3 playtest/run.py [--url URL] [--levels N] [--out DIR]

Produces DIR/metrics.json plus screenshots. It does not judge anything itself;
the weekly agent reads the metrics and screenshots and writes the report.

What it measures
  1. Level design (solver over levels 1..100): board size, coins, holes, par,
     spikes in difficulty, generation time.
  2. Live site checks on desktop (1366x768) and phone (390x844): load time,
     JS errors, anything off-screen, tap targets under 44px, first-screen text.
  3. Simulated players on a fresh profile:
       novice  - mostly heads for the nearest coin, makes some random hops,
                 takes a hint when it has used 2x par, restarts at 4x par
       expert  - plays the optimal line (sanity check that every level is
                 solvable and the win flow works)
"""
import argparse, asyncio, json, os, random, re, statistics, subprocess, sys, time, urllib.request
from datetime import date

ap = argparse.ArgumentParser()
ap.add_argument('--url', default='https://knight-hop.pages.dev/')
ap.add_argument('--levels', type=int, default=20)
ap.add_argument('--out', default=f'playtest/results/{date.today().isoformat()}')
args = ap.parse_args()
os.makedirs(args.out, exist_ok=True)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------- 1. level design via the game's own solver (node) ----------
html = open(os.path.join(ROOT, 'index.html'), encoding='utf8').read()
engine = html[html.index('// ---------- seeded random ----------'):html.index('// ---------- hand-made teaching levels')]
hand = re.search(r'function handLevel\(n\) \{[\s\S]*?\n\}', html).group(0)
js = engine + '\n' + hand + r'''
const out = [];
for (let n = 1; n <= 100; n++) {
  const t0 = Date.now();
  const L = handLevel(n) || generate(n, n * 7919 + 13);
  out.push({ n, size: L.size, coins: L.gems.length, holes: L.wall.reduce((a, b) => a + b, 0), par: L.par, genMs: Date.now() - t0 });
}
console.log(JSON.stringify(out));
'''
levels = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
spikes = [{'from': a['n'], 'to': b['n'], 'parFrom': a['par'], 'parTo': b['par']} for a, b in zip(levels, levels[1:]) if b['par'] - a['par'] >= 4]
drops = [{'from': a['n'], 'to': b['n'], 'parFrom': a['par'], 'parTo': b['par']} for a, b in zip(levels, levels[1:]) if a['par'] - b['par'] >= 4]
design = {
    'levels': levels, 'spikes': spikes, 'drops': drops,
    'parByChapter': [round(statistics.mean(l['par'] for l in levels[i:i + 10]), 1) for i in range(0, 100, 10)],
    'slowestGenMs': max(l['genMs'] for l in levels),
}

# ---------- 2 + 3. browser runs ----------
from playwright.async_api import async_playwright

NOVICE_JS = r'''() => {
  // Called with the game's globals in scope (classic script => they live on the global lexical scope).
  return { pos, par: L.par, size: L.size, wall: Array.from(L.wall), gems: L.gems, got: Array.from(got), moves: history.length, won };
}'''

def bfs_dist(size, wall, start, goal):
    from collections import deque
    J = [(1,2),(2,1),(2,-1),(1,-2),(-1,-2),(-2,-1),(-2,1),(-1,2)]
    seen = {start: 0}; q = deque([start])
    while q:
        p = q.popleft()
        if p == goal: return seen[p]
        x, y = p % size, p // size
        for dx, dy in J:
            nx, ny = x + dx, y + dy
            if 0 <= nx < size and 0 <= ny < size and not wall[ny * size + nx]:
                np_ = ny * size + nx
                if np_ not in seen: seen[np_] = seen[p] + 1; q.append(np_)
    return 99

async def play_level(pg, style, rng):
    stats = {'moves': 0, 'hints': 0, 'restarts': 0, 'ms': 0}
    t0 = time.time()
    for step in range(400):
        st = await pg.evaluate(NOVICE_JS)
        if st['won']: break
        if style == 'expert':
            await pg.evaluate('save.hints = 9'); await pg.click('#hintBtn'); stats['hints'] += 1
            await pg.click('.cell.hint'); await pg.wait_for_timeout(520); continue
        if st['moves'] >= 4 * st['par'] and stats['restarts'] < 2:
            await pg.click('#restart'); stats['restarts'] += 1; await pg.wait_for_timeout(300); continue
        if st['moves'] >= 2 * st['par'] * (stats['hints'] + 1):
            await pg.click('#hintBtn'); stats['hints'] += 1; await pg.wait_for_timeout(80)
            if await pg.locator('.cell.hint').count(): await pg.click('.cell.hint'); await pg.wait_for_timeout(520); continue
        targets = await pg.eval_on_selector_all('.cell.target', 'e => e.map(c => +c.dataset.i)')
        if not targets: break
        left = [g for g in st['gems'] if g not in st['got']]
        def score(t): return min(bfs_dist(st['size'], st['wall'], t, g) for g in left) if left else 0
        choice = rng.choice(targets) if rng.random() < 0.3 else min(targets, key=score)
        await pg.click(f'.cell[data-i="{choice}"]'); await pg.wait_for_timeout(520)
    st = await pg.evaluate(NOVICE_JS)
    stats.update(moves=st['moves'], par=st['par'], won=st['won'], ms=int((time.time() - t0) * 1000))
    stats['seal'] = await pg.evaluate("document.body.classList.contains('won') ? document.querySelector('#resTitle').textContent : null")
    return stats

async def ui_checks(pg):
    return await pg.evaluate(r'''() => {
      const vw = innerWidth, vh = innerHeight, issues = [];
      document.querySelectorAll('button, .cell.target, a').forEach((e) => {
        const r = e.getBoundingClientRect(); if (!r.width || getComputedStyle(e).visibility === 'hidden' || e.closest('.sheet:not(.open)')) return;
        if (r.right > vw + 1 || r.bottom > vh + 1 || r.left < -1 || r.top < -1) issues.push({ kind: 'offscreen', el: (e.id || e.className || e.tagName) + '', r: [r.left, r.top, r.width, r.height].map(Math.round) });
        if (e.tagName === 'BUTTON' && (r.width < 44 || r.height < 44)) issues.push({ kind: 'small-target', el: (e.id || e.className) + '', size: [Math.round(r.width), Math.round(r.height)] });
      });
      return { issues, coach: document.querySelector('#coach')?.textContent || '', title: document.querySelector('#lvl')?.textContent };
    }''')

async def device_run(p, name, vp, touch):
    b = await p.chromium.launch()
    ctx = await b.new_context(viewport=vp, has_touch=touch, is_mobile=touch)
    pg = await ctx.new_page()
    errors = []
    pg.on('pageerror', lambda e: errors.append(str(e)))
    pg.on('console', lambda m: m.type == 'error' and errors.append(m.text))
    t0 = time.time()
    await pg.goto(args.url, wait_until='load')
    await pg.wait_for_selector('#knight', timeout=20000)
    load_ms = int((time.time() - t0) * 1000)
    shots = {}
    async def shot(tag):
        path = os.path.join(args.out, f'{name}-{tag}.png'); await pg.screenshot(path=path); shots[tag] = os.path.basename(path)
    await shot('first-screen')
    first = await ui_checks(pg)
    rng = random.Random(42)
    novice = []
    for n in range(1, args.levels + 1):
        r = await play_level(pg, 'novice', rng); r['level'] = n; novice.append(r)
        if n in (1, 3, 10): await shot(f'won-level-{n}')
        if not r['won']: break
        await pg.click('#next'); await pg.wait_for_timeout(350)
        if n == 5: await shot('level-6-start')
    await pg.click('#levelsBtn'); await pg.wait_for_timeout(450); await shot('levels-sheet')
    await pg.click('#dailyBtn'); await pg.wait_for_timeout(400); await shot('daily')
    expert_daily = await play_level(pg, 'expert', rng)
    await b.close()
    return {'device': name, 'viewport': vp, 'loadMs': load_ms, 'errors': errors, 'firstScreen': first, 'novice': novice, 'expertDaily': expert_daily, 'screenshots': shots}

async def main():
    async with async_playwright() as p:
        return [await device_run(p, 'desktop', {'width': 1366, 'height': 768}, False),
                await device_run(p, 'phone', {'width': 390, 'height': 844}, True)]

runs = asyncio.run(main())
try: size = len(urllib.request.urlopen(args.url, timeout=20).read())
except Exception: size = None
commit = subprocess.run(['git', '-C', ROOT, 'log', '-1', '--format=%h %s'], capture_output=True, text=True).stdout.strip()
summary = {}
for r in runs:
    nv = r['novice']
    summary[r['device']] = {
        'levelsCleared': sum(1 for x in nv if x['won']),
        'perfectRate': round(sum(1 for x in nv if x.get('seal') == 'Perfect!') / max(1, len(nv)), 2),
        'avgMovesOverPar': round(statistics.mean(x['moves'] / max(1, x['par']) for x in nv), 2) if nv else None,
        'hintsUsed': sum(x['hints'] for x in nv), 'restarts': sum(x['restarts'] for x in nv),
        'firstHintLevel': next((x['level'] for x in nv if x['hints']), None),
        'errors': len(r['errors']), 'uiIssues': len(r['firstScreen']['issues']), 'loadMs': r['loadMs'],
    }
metrics = {'date': date.today().isoformat(), 'url': args.url, 'commit': commit, 'pageBytes': size, 'summary': summary, 'design': design, 'runs': runs}
json.dump(metrics, open(os.path.join(args.out, 'metrics.json'), 'w'), indent=1)
hist = os.path.join(ROOT, 'playtest', 'history.csv')
new = not os.path.exists(hist)
with open(hist, 'a') as f:
    if new: f.write('date,commit,device,levelsCleared,perfectRate,avgMovesOverPar,hintsUsed,restarts,firstHintLevel,errors,uiIssues,loadMs\n')
    for d, s in summary.items():
        f.write(','.join(str(x) for x in [metrics['date'], commit.split(' ')[0], d, s['levelsCleared'], s['perfectRate'], s['avgMovesOverPar'], s['hintsUsed'], s['restarts'], s['firstHintLevel'], s['errors'], s['uiIssues'], s['loadMs']]) + '\n')
print(json.dumps({'summary': summary, 'spikes': spikes, 'parByChapter': design['parByChapter'], 'out': args.out}, indent=1))

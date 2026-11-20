# Mimic Chess (repo: knight-hop)

A shapeshifting chess puzzle: you become what you land on. Start as a knight, step on badges to become a rook, bishop or king, take guards to steal their moves, and collect every coin in as few moves as possible. 28 curated levels across 5 chapters (Shapeshifter, Crumbling Floor, Guards, Patrols, Portals), plus Endless mode and a daily puzzle.

Single-file HTML5 game (`index.html`) with the CrazyGames SDK v3 (no-op elsewhere). Levels are generated and curated offline by a BFS solver, then embedded in the page; the same rules engine (`ENGINE`) runs in the game for hints.

Weekly playtest: see `playtest/AGENT.md` and `playtest/run.py`.

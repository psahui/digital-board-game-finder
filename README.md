# Digital Board Game Finder

The BoardGameGeek top 100, cross-referenced against Steam, Board Game Arena
and virtual tabletops (Tabletop Simulator / Tabletopia DLC): which top board
games you can play digitally, what's on sale right now, and what a group can
play together online tonight.

Built for [Matchsticks for my Eyes](https://www.matchstickeyes.com).

This is a personal project shared as-is: maintained for my own use, with no
support, roadmap, or response-time commitments implied. See the Licence
section below for reuse terms.

## How it works

- `fetch_data.py` (Python, standard library only, no API keys):
  - **BGG:** BGG's public browse pages and XML API sit behind Cloudflare / app
    tokens, so the ranks come from the geekdo JSON API that BGG's own site
    uses. There's no single "overall rank" list there, so the script pulls each
    subdomain (Strategy, Thematic, Family, …) sorted by overall rank and merges
    them. Player counts, play time and blurbs come from each game's geekitem
    record (cached for 30 days).
  - **Board Game Arena:** BGA's game list page embeds every game with its BGG
    id, so that match is exact. Also gives Premium vs free, supported player
    counts, average game length, and real-time / turn-based support. If BGA
    can't be reached, the previous run's BGA data is kept.
  - **Steam:** there's no BGG ↔ Steam link, so the script searches the store by
    name, keeps exact or edition-only matches ("Scythe" → "Scythe: Digital
    Edition"), and fetches price, discount, multiplayer modes and review
    summary for each. Prices are cached for 6 hours, reviews for a week.
- `index.html` is a single static page (vanilla JS). Every filter is mirrored
  into the URL, so **Copy link to this view** shares an exact filtered list.
  "Own it?" marks are kept in the viewer's own browser (localStorage).
- A GitHub Action (`.github/workflows/refresh.yml`) re-runs the fetch daily at
  about 04:20 AEST — just after Steam's sales roll over — and commits the
  updated data; GitHub Pages serves the result.

## Configuration

Everything editable lives in `config.json`:

- `top_n` — how deep into the BGG ranking to go (Steam lookups scale with
  this; 500 takes roughly ten minutes on a cold cache).
- `country` — Steam store region for prices (`au`, `us`, `gb`, …).
- `steam_overrides` — BGG id → list of Steam appids. Use it to fix a wrong
  match, add a renamed or DLC version, or (with `[]`) say a game has no Steam
  version. The BGG id is the number in the BGG URL
  (`boardgamegeek.com/boardgame/167791/...` → `"167791"`).
- `steam_notes` — BGG id → a one-line caveat shown on the card ("DLC — needs
  the base game", "adaptation of the sequel", …).
- `vtt_overrides` — BGG id → list of `[appid, name]` Tabletop Simulator /
  Tabletopia DLCs, when the automatic search misses one.

The fetch prints any doubtful matches at the end. To apply a config change
immediately, run the workflow from the **Actions** tab, or:

```bash
gh workflow run refresh.yml
```

Run locally with:

```bash
python fetch_data.py
python -m http.server 8000
```

then open http://localhost:8000.

## Licence

The code in this repository is released under the MIT Licence — see
[LICENCE](LICENCE).

Not covered by the licence:

- The **Matchsticks for my Eyes name and logo** (`assets/logo.png`), which
  remain the property of Peter Sahui and are not licensed for reuse.
- The contents of **`data/`**: rankings and game details from BoardGameGeek,
  store data from Steam and Board Game Arena. Game names and images belong to
  their respective owners.

#!/usr/bin/env python3
"""Cross-reference the BoardGameGeek top list against Steam and Board Game Arena.

Standard library only, no API keys. Writes data/games.json for index.html.

Sources:
  * BGG ranks come from the geekdo API that BGG's own site uses (the public
    browse pages and XML API sit behind Cloudflare / app tokens). The overall
    ranking isn't exposed as one list, so we pull each subdomain (Strategy,
    Thematic, ...) sorted by overall rank and merge them.
  * Board Game Arena's game list page embeds every game with its BGG id, so
    that match is exact.
  * Steam has no BGG link, so we search the store by name, score candidates,
    and let config.json's steam_overrides fix anything the scorer gets wrong.
"""

import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE_PATH = DATA / "cache.json"
OUT_PATH = DATA / "games.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 (board-game-finder; personal hobby project)"
}

GEEKDO = "https://api.geekdo.com/api"
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/"
STEAM_DETAILS = "https://store.steampowered.com/api/appdetails"
STEAM_REVIEWS = "https://store.steampowered.com/appreviews/"
BGA_LIST = "https://en.boardgamearena.com/gamelist?section=all"

# BGG subdomain family ids. Every ranked game sits in at least one, so the
# union of each subdomain's top-N-by-overall-rank contains the overall top N.
SUBDOMAINS = {
    5497: "Strategy", 5496: "Thematic", 5499: "Family", 4664: "Wargames",
    5498: "Party", 4666: "Abstract", 4667: "Customizable", 4665: "Children's",
}

# Steam categories worth surfacing for "can we play this together?"
MP_CATEGORIES = {
    "Online PvP": "online_pvp",
    "Online Co-op": "online_coop",
    "Shared/Split Screen PvP": "local_pvp",
    "Shared/Split Screen Co-op": "local_coop",
    "Cross-Platform Multiplayer": "crossplay",
    "Remote Play Together": "remote_play",
    "Steam Turn Notifications": "async",
}


# ---------------------------------------------------------------- http

def http_text(url, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - retry anything transient
            if attempt == retries - 1:
                raise
            wait = 5 * (attempt + 1) if "429" in str(exc) else 2 * (attempt + 1)
            print(f"  retry {attempt + 1} after {exc} ({url[:80]})", file=sys.stderr)
            time.sleep(wait)


def http_json(url, retries=4):
    return json.loads(http_text(url, retries))


# ---------------------------------------------------------------- cache

def load_cache():
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")


def fresh(entry, max_age_hours):
    if not entry:
        return False
    return time.time() - entry.get("_fetched", 0) < max_age_hours * 3600


# ---------------------------------------------------------------- BGG

def fetch_bgg_top(top_n):
    games = {}
    for fam_id, label in SUBDOMAINS.items():
        page = 1
        while True:
            params = {
                "ajax": 1, "linkdata_index": "boardgame", "nosession": 1,
                "objectid": fam_id, "objecttype": "family", "pageid": page,
                "showcount": 50, "sort": "rank", "subtype": "boardgamesubdomain",
            }
            items = http_json(f"{GEEKDO}/geekitem/linkeditems?" + urllib.parse.urlencode(params)).get("items", [])
            done = not items
            for it in items:
                try:
                    rank = int(it["rank"])
                except (TypeError, ValueError):
                    continue  # unranked
                if rank > top_n:
                    done = True
                    break
                gid = int(it["objectid"])
                g = games.setdefault(gid, {
                    "bgg_id": gid,
                    "name": it["name"],
                    "year": int(it["yearpublished"]) if it.get("yearpublished") else None,
                    "rank": rank,
                    "rating": round(float(it["average"]), 2) if it.get("average") else None,
                    "weight": round(float(it["avgweight"]), 2) if it.get("avgweight") else None,
                    "usersrated": int(it.get("usersrated") or 0),
                    "categories": [],
                })
                if label not in g["categories"]:
                    g["categories"].append(label)
            if done:
                break
            page += 1
            time.sleep(0.5)
        print(f"BGG {label}: {sum(1 for g in games.values() if label in g['categories'])} in top {top_n}")
    ranked = sorted(games.values(), key=lambda g: g["rank"])
    if len(ranked) < top_n * 0.95:
        print(f"WARNING: only {len(ranked)} BGG games found for top {top_n}", file=sys.stderr)
    return ranked[:top_n]


def fetch_bgg_details(games, cache):
    bucket = cache.setdefault("bgg", {})
    for g in games:
        key = str(g["bgg_id"])
        entry = bucket.get(key)
        if not fresh(entry, 24 * 30):
            item = http_json(f"{GEEKDO}/geekitems?objectid={key}&objecttype=thing")["item"]
            entry = {
                "_fetched": time.time(),
                "minplayers": int(item.get("minplayers") or 0),
                "maxplayers": int(item.get("maxplayers") or 0),
                "minplaytime": int(item.get("minplaytime") or 0),
                "maxplaytime": int(item.get("maxplaytime") or 0),
                "blurb": item.get("short_description") or "",
                "image": (item.get("images") or {}).get("square200") or item.get("imageurl") or "",
                "alternatenames": [a.get("name") for a in item.get("alternatenames", []) if a.get("name")][:30],
            }
            bucket[key] = entry
            time.sleep(0.4)
        g.update({k: v for k, v in entry.items() if not k.startswith("_") and k != "alternatenames"})
        g["_alternatenames"] = entry.get("alternatenames", [])


# ---------------------------------------------------------------- BGA

def fetch_bga():
    html = http_text(BGA_LIST)
    marker = '"game_list":'
    start = html.find(marker)
    if start < 0:
        raise RuntimeError("BGA game_list not found - page markup changed?")
    game_list, _ = json.JSONDecoder().raw_decode(html[start + len(marker):])
    by_bgg = {}
    for x in game_list:
        bid = x.get("bgg_id")
        if not bid:
            continue
        entry = {
            "slug": x["name"],
            "name": x.get("display_name_en") or x["name"],
            "url": f"https://boardgamearena.com/gamepanel?game={x['name']}",
            "premium": bool(x.get("premium")),
            "status": x.get("status"),  # public | beta
            "players": x.get("player_numbers") or [],
            "avg_minutes": x.get("average_duration"),
            "realtime": x.get("realtime") == "yes",
            "turnbased": x.get("turnbased") == "yes",
            "tutorial": bool(x.get("has_tutorial")),
            "plays": x.get("games_played") or 0,
        }
        prev = by_bgg.get(int(bid))
        if not prev or entry["plays"] > prev["plays"]:
            by_bgg[int(bid)] = entry
    print(f"BGA: {len(game_list)} games, {len(by_bgg)} with a BGG id")
    return by_bgg


# ---------------------------------------------------------------- Steam matching

EDITION_WORDS = re.compile(
    r"\b(digital|edition|board ?game|the board game|second|2nd|third|3rd|fourth|4th|"
    r"revised|deluxe|definitive|remastered|game)\b"
)


def norm(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^the ", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def core(name):
    """Name with edition qualifiers stripped: 'Scythe: Digital Edition' -> 'scythe'."""
    s = EDITION_WORDS.sub(" ", norm(name))
    return re.sub(r"\s+", " ", s).strip()


VTT_PREFIX = re.compile(r"^(tabletop simulator|tabletopia)\s*[-:–]\s*", re.I)
EXPANSION_HINT = re.compile(r"\s[-:]\s|\bexpansion\b|\bdlc\b|\bsoundtrack\b|\bpack\b", re.I)


def score_candidate(bgg_name, alt_names, cand_name):
    """0..1 confidence that a Steam result is a digital version of the BGG game."""
    if VTT_PREFIX.match(cand_name):
        return 0.0  # handled separately as virtual-tabletop DLC
    targets = {norm(bgg_name), core(bgg_name)}
    # Pre-colon title ('Twilight Imperium: Fourth Edition' -> 'Twilight Imperium')
    if ":" in bgg_name:
        targets.add(core(bgg_name.split(":")[0]))
    for alt in alt_names:
        if re.fullmatch(r"[\x00-\x7f]+", alt):  # Latin-script aliases only
            targets.add(norm(alt))
    targets.discard("")
    c_norm, c_core = norm(cand_name), core(cand_name)
    if c_norm == norm(bgg_name):
        return 1.0
    if c_core and c_core == core(bgg_name):
        return 0.95
    if c_norm in targets or c_core in targets:
        return 0.85
    # No looser tier: prefix matches ('Star Wars: Rebellion' -> 'STAR WARS: Galactic
    # Racer') were wrong every time. Renamed ports go in config steam_overrides.
    return 0.0


def steam_search(term, cc):
    url = STEAM_SEARCH + "?" + urllib.parse.urlencode({"term": term, "l": "english", "cc": cc})
    return http_json(url).get("items", [])


def find_steam(game, cache, cc):
    bucket = cache.setdefault("steam_search", {})
    key = norm(game["name"])
    entry = bucket.get(key)
    if not fresh(entry, 24 * 7):
        terms = [game["name"]]
        if ":" in game["name"]:
            terms.append(game["name"].split(":")[0])
        results = {}
        for t in terms:
            for it in steam_search(t, cc):
                results[it["id"]] = it["name"]
            time.sleep(0.4)
        entry = {"_fetched": time.time(), "results": results}
        bucket[key] = entry
    results = {int(k): v for k, v in entry["results"].items()}

    scored = sorted(
        ((score_candidate(game["name"], game.get("_alternatenames", []), n), appid, n)
         for appid, n in results.items()),
        reverse=True,
    )
    best = [s for s in scored if s[0] > 0]
    vtt = [(a, n) for a, n in results.items() if VTT_PREFIX.match(n)
           and score_candidate(game["name"], game.get("_alternatenames", []), VTT_PREFIX.sub("", n)) >= 0.85]
    return best, vtt


def fetch_steam_details(appid, cache, cc):
    bucket = cache.setdefault("steam_app", {})
    key = str(appid)
    entry = bucket.get(key)
    if fresh(entry, 6):  # prices move; keep this short
        return entry
    raw = http_json(f"{STEAM_DETAILS}?appids={appid}&cc={cc}&l=english").get(key, {})
    time.sleep(0.6)
    if not raw.get("success"):
        entry = {"_fetched": time.time(), "missing": True}
        bucket[key] = entry
        return entry
    d = raw["data"]
    cats = [c["description"] for c in d.get("categories", [])]
    entry = {
        "_fetched": time.time(),
        "name": d.get("name"),
        "type": d.get("type"),
        "is_free": d.get("is_free", False),
        "price": d.get("price_overview"),
        "mp": sorted({v for k, v in MP_CATEGORIES.items() if k in cats}),
        "multiplayer": "Multi-player" in cats,
        "developers": d.get("developers", []),
        "publishers": d.get("publishers", []),
        "release": (d.get("release_date") or {}).get("date"),
        "coming_soon": (d.get("release_date") or {}).get("coming_soon", False),
        "header": d.get("header_image"),
        "about": re.sub(r"<[^>]+>", " ", d.get("short_description") or "")[:300],
    }
    # Reviews change slowly; carry the old summary over unless it's a week stale.
    old = bucket.get(key) or {}
    if old.get("reviews") and time.time() - old.get("_reviews_fetched", 0) < 7 * 86400:
        entry["reviews"], entry["_reviews_fetched"] = old["reviews"], old["_reviews_fetched"]
    else:
        try:
            q = http_json(f"{STEAM_REVIEWS}{appid}?json=1&num_per_page=0&language=all&purchase_type=all")
            s = q.get("query_summary", {})
            entry["reviews"] = {"desc": s.get("review_score_desc"), "total": s.get("total_reviews", 0),
                                "pct": round(100 * s["total_positive"] / s["total_reviews"]) if s.get("total_reviews") else None}
            entry["_reviews_fetched"] = time.time()
            time.sleep(0.4)
        except Exception as exc:  # noqa: BLE001
            print(f"  reviews failed for {appid}: {exc}", file=sys.stderr)
    bucket[key] = entry
    return entry


def steam_record(appid, d, confidence, cc):
    p = d.get("price") or {}
    return {
        "appid": appid,
        "name": d.get("name"),
        "dlc": d.get("type") == "dlc",
        "url": f"https://store.steampowered.com/app/{appid}/",
        "confidence": confidence,
        "is_free": d.get("is_free", False),
        "price": p.get("final", 0) / 100 if p else (0 if d.get("is_free") else None),
        "price_initial": p.get("initial", 0) / 100 if p else None,
        "discount": p.get("discount_percent", 0) if p else 0,
        "currency": p.get("currency") or ("AUD" if cc == "au" else None),
        "mp": d.get("mp", []),
        "developers": d.get("developers", []),
        "publishers": d.get("publishers", []),
        "release": d.get("release"),
        "coming_soon": d.get("coming_soon", False),
        "header": d.get("header"),
        "reviews": d.get("reviews"),
    }


# ---------------------------------------------------------------- main

def main():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    top_n, cc = config.get("top_n", 100), config.get("country", "au")
    overrides = {str(k): v for k, v in config.get("steam_overrides", {}).items()}
    notes = {str(k): v for k, v in config.get("steam_notes", {}).items()}
    extra_vtt = {str(k): v for k, v in config.get("vtt_overrides", {}).items()}
    DATA.mkdir(exist_ok=True)
    cache = load_cache()

    games = fetch_bgg_top(top_n)
    fetch_bgg_details(games, cache)
    save_cache(cache)
    try:
        bga = fetch_bga()
    except Exception as exc:  # noqa: BLE001 - keep yesterday's BGA data rather than losing it
        print(f"WARNING: BGA fetch failed ({exc}); reusing previous BGA data", file=sys.stderr)
        prev = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() else {"games": []}
        bga = {g["bgg_id"]: g["bga"] for g in prev["games"] if g.get("bga")}

    review = []
    for i, g in enumerate(games, 1):
        key = str(g["bgg_id"])
        g["bga"] = bga.get(g["bgg_id"])
        g["steam"], g["vtt"] = [], []
        g["note"] = notes.get(key)

        best, vtt = find_steam(g, cache, cc)
        if key in overrides:
            picks = [(1.0, int(a)) for a in (overrides[key] or [])]
        else:
            # Keep the top-scoring match plus any equally strong alternative
            # (e.g. two separate digital editions of the same game).
            picks = [(s, a) for s, a, _ in best if s >= 0.85][:2] or [(s, a) for s, a, _ in best[:1]]
        for conf, appid in picks:
            d = fetch_steam_details(appid, cache, cc)
            # DLC only when hand-picked (e.g. an expansion sold inside another game's app)
            if d.get("missing") or d.get("type") not in ("game", None) and not (key in overrides and d.get("type") == "dlc"):
                continue
            g["steam"].append(steam_record(appid, d, conf, cc))
            if conf < 0.85:
                review.append(f"  ? #{g['rank']} {g['name']}  ->  {d.get('name')} ({appid}) conf {conf}")
        for appid, name in (extra_vtt.get(key) or vtt[:2]):
            platform = "Tabletopia" if name.lower().startswith("tabletopia") else "Tabletop Simulator"
            g["vtt"].append({"appid": int(appid), "platform": platform,
                             "url": f"https://store.steampowered.com/app/{appid}/"})

        g.pop("_alternatenames", None)
        if i % 20 == 0:
            save_cache(cache)
            print(f"  {i}/{len(games)} games matched")

    save_cache(cache)
    out = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "country": cc,
        "top_n": top_n,
        "games": games,
    }
    OUT_PATH.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")

    n_steam = sum(1 for g in games if g["steam"])
    n_bga = sum(1 for g in games if g["bga"])
    n_sale = sum(1 for g in games if any(s["discount"] for s in g["steam"]))
    print(f"\nDone: {len(games)} games | Steam {n_steam} ({n_sale} on sale) | BGA {n_bga} | "
          f"Virtual tabletop {sum(1 for g in games if g['vtt'])}")
    if review:
        print("Low-confidence Steam matches (add to steam_overrides to confirm or reject):")
        print("\n".join(review))


if __name__ == "__main__":
    main()

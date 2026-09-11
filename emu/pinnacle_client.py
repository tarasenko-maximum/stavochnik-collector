# -*- coding: utf-8 -*-
"""
Pinnacle — гостевой API (guest.api.arcadia.pinnacle.com), без аккаунта.
Проверено 06.09.2026 с домашнего IP (Сербия): работает; с дата-центра (Hetzner DE) — 403.

Эндпоинты (sportId=29 — футбол):
  GET /0.1/sports/29/matchups?withSpecials=false      — все матчи (~1200), поле isLive
  GET /0.1/sports/29/matchups/live                     — только live
  GET /0.1/sports/29/markets/straight[?primaryOnly=1]  — линии: moneyline/spread/total/team_total
Кэфы — американские. limits[maxRiskStake] — макс. риск на ставку (у.е. ≈ USD).
Ключ ниже — публичный гостевой ключ веб-клиента Pinnacle.
"""

import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from odds_common import Fixture, Market, Q, american_to_decimal

BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
GUEST_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"
SPORT_SOCCER = 29
# sportId Pinnacle и какие типы рынков period 0 сравнимы с другими источниками (см. docs/SPORTS-PROBE.md):
# у хоккея period 0 — только 2-way moneyline с ОТ и буллитами; спред/тотал основного времени лежат в period 6,
# а у SX/Cloudbet линии идут с ОТ — их не сводим (ложные вилки).
SPORTS = {
    "football":   (29, ("moneyline", "total", "spread")),
    "amfootball": (15, ("moneyline", "total", "spread")),
    "baseball":   (3,  ("moneyline", "total", "spread")),
    "basketball": (4,  ("moneyline", "total", "spread")),
    "hockey":     (19, ("moneyline",)),
}


class PinnacleError(Exception):
    pass


def _get(path, params=None, retries=3):
    url = f"{BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json", "X-API-Key": GUEST_KEY,
        "X-Device-UUID": "stavochnik-emu",
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise PinnacleError(f"HTTP {e.code}: {body}")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise PinnacleError(f"net error: {e}")
    raise PinnacleError("unreachable")


def _ts(iso):
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def fetch_soccer(primary_only=False):
    return fetch_sport("football", primary_only)


def fetch_sport(sport, primary_only=False):
    """Возвращает {matchupId: Fixture} по виду спорта с рынками period 0 (moneyline → 1x2/12, total, spread → ah)."""
    sport_id, allowed_types = SPORTS[sport]
    matchups = _get(f"sports/{sport_id}/matchups", {"withSpecials": "false"})
    try:
        live = _get(f"sports/{sport_id}/matchups/live")
    except PinnacleError:
        live = []
    live_ids = {m["id"] for m in live if m.get("type") == "matchup"}
    seen = {m["id"] for m in matchups}
    matchups = list(matchups) + [m for m in live if m["id"] not in seen]
    # Проверено 06.09.2026 (Арсенал–Челси 2:1): актуальные live-цены лежат в общем /markets/straight
    # под id дочернего live-matchup; эндпоинт /markets/live/straight отдаёт устаревшие/иные цены — НЕ используем.
    markets = _get(f"sports/{sport_id}/markets/straight",
                   {"primaryOnly": "true"} if primary_only else None)
    # у одной игры может быть два live-matchup (live_delay и danger_zone) — оставляем один, предпочитая не danger_zone
    by_parent = {}
    for m in matchups:
        if m.get("type") == "matchup" and m.get("parentId") and (m.get("isLive") or m["id"] in live_ids):
            names = " ".join(p.get("name") or "" for p in m.get("participants") or []).lower()
            if any(w in names for w in ("corner", "booking", "card", "yellow", "offside", "shots")):
                continue  # производные (угловые и т.п.) делят parentId с основным матчем
            by_parent.setdefault(m["parentId"], []).append(m)
    # из дублей выбираем ребёнка со свежайшими линиями (макс. version у его рынков); при равенстве — не danger_zone
    ver = {}
    for mk in markets:
        v = mk.get("version") or 0
        ver[mk.get("matchupId")] = max(ver.get(mk.get("matchupId"), 0), v)
    keep_live_ids = set()
    for pid, lst in by_parent.items():
        lst.sort(key=lambda m: (ver.get(m["id"], 0), m.get("liveMode") != "danger_zone"), reverse=True)
        keep_live_ids.add(lst[0]["id"])
    fx = {}
    for m in matchups:
        if m.get("type") != "matchup":
            continue
        # live-матч у Pinnacle — дочерний matchup с parent (прематчевый id); у родителя рынков нет
        if m.get("parent") and m["id"] not in keep_live_ids:
            continue
        parts = m.get("participants") or []
        league_name = (m.get("league") or {}).get("name") or ""
        # производные рынки (угловые, карточки и т.п.) — не матчи, пропускаем
        blob = (league_name + " " + " ".join(p.get("name") or "" for p in parts)).lower()
        if any(w in blob for w in ("corner", "booking", "cards", "(sets", "yellow", "offsides", "shots")):
            continue
        if (m.get("units") or "") in ("Kills", "Games"):
            continue
        home = next((p.get("name") for p in parts if p.get("alignment") == "home"), None)
        away = next((p.get("name") for p in parts if p.get("alignment") == "away"), None)
        if not home or not away:
            if len(parts) >= 2:
                home, away = parts[0].get("name"), parts[1].get("name")
            else:
                continue
        fx[m["id"]] = Fixture(
            src="pinnacle", ext_id=str(m["id"]), home=home, away=away,
            league=league_name, sport=sport,
            start_ts=_ts(m.get("startTime") or ""),
            is_live=bool(m.get("isLive")) or m["id"] in live_ids,
            raw={"liveMode": m.get("liveMode"), "status": m.get("status"),
                 "hasLive": m.get("hasLive"), "parentId": m.get("parentId"),
                 "score": [(p.get("state") or {}).get("score") for p in parts]},
        )
    for mk in markets:
        f = fx.get(mk.get("matchupId"))
        if f is None or mk.get("period") != 0 or (mk.get("status") or "open") != "open":
            continue
        # danger_zone — Pinnacle приостановил приём (после гола/опасного момента): цены заморожены, ставить нельзя
        if f.is_live and f.raw.get("liveMode") == "danger_zone":
            continue
        lim = 0.0
        for l in mk.get("limits") or []:
            if l.get("type") == "maxRiskStake":
                lim = float(l.get("amount") or 0)
        t = mk.get("type")
        if t not in allowed_types:
            continue
        prices = mk.get("prices") or []
        if t == "moneyline":
            sels = {}
            for p in prices:
                d = p.get("designation")
                if d in ("home", "draw", "away"):
                    k = american_to_decimal(p.get("price"))
                    if k:
                        sels[d] = Q(back=k, back_size=lim)
            if len(sels) == 3:
                f.markets[("1x2", None)] = Market(sels=sels, ext={"key": mk.get("key")})
            elif len(sels) == 2:
                f.markets[("12", None)] = Market(sels=sels, ext={"key": mk.get("key")})
        elif t == "total":
            sels = {}
            line = None
            for p in prices:
                d = p.get("designation")
                if d in ("over", "under"):
                    k = american_to_decimal(p.get("price"))
                    if k:
                        sels[d] = Q(back=k, back_size=lim)
                        line = float(p.get("points"))
            if len(sels) == 2 and line is not None:
                f.markets[("total", line)] = Market(sels=sels, ext={"key": mk.get("key"),
                                                                    "main": not mk.get("isAlternate")})
        elif t == "spread":
            sels = {}
            line = None
            for p in prices:
                d = p.get("designation")
                if d in ("home", "away"):
                    k = american_to_decimal(p.get("price"))
                    if k:
                        sels[d] = Q(back=k, back_size=lim)
                        if d == "home":
                            line = float(p.get("points"))
            if len(sels) == 2 and line is not None:
                f.markets[("ah", line)] = Market(sels=sels, ext={"key": mk.get("key"),
                                                                 "main": not mk.get("isAlternate")})
    return {k: v for k, v in fx.items() if v.markets}


if __name__ == "__main__":
    t = time.time()
    fx = fetch_soccer()
    live = [f for f in fx.values() if f.is_live]
    print(f"Pinnacle: {len(fx)} матчей с рынками, live: {len(live)}, {time.time()-t:.1f}s")
    for f in list(fx.values())[:3]:
        print(f.home, "-", f.away, f.league, f.start_ts, f.is_live, list(f.markets)[:6])
        m = f.markets.get(("1x2", None))
        if m:
            print("   1x2:", {k: (q.back, q.back_size) for k, q in m.sels.items()})

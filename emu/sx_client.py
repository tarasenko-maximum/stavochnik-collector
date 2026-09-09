# -*- coding: utf-8 -*-
"""
Клиент биржи SX.bet (api.sx.bet) — V3 (после миграции 25.08.2026). Публичные данные без ключа.
Проверено 06.09.2026 с домашнего IP (Сербия) и с сервера Hetzner DE: 200.

  GET /markets/active?sportIds=5&type=T&pageSize=100[&paginationKey=]   — активные рынки
      T: 1 = 1X2 (три бинарных рынка на матч: home / draw(Tie) / away), 2 = тоталы (line),
         3 = азиатский гандикап (line для teamOne), 52 = 12 (двухисходка)
  GET /orderbook-v3/snapshot?marketHash=…      — стакан рынка (публично)
  GET /orderbook-v3/snapshot/event?eventId=…   — стакан всех рынков события (нужен x-sx-api-key)
  GET /orders-v3/odds/best?marketHashes=…      — лучшие цены (нужен x-sx-api-key)
  V2 /orders — отключён (400 «OrderBook V2 is no longer supported»).

Семантика стакана (docs.sx.bet, get-orderbook-snapshot): outcomeOne/outcomeTwo — агрегированные
MAKER-ставки на этот исход; percentageOdds — подразумеваемая вероятность мейкера × 1e20;
size — ставка мейкера в USDC (6 знаков); индекс 0 — лучшая цена (макс. p мейкера).
Принимая мейкера, стоящего на исход X с вероятностью p и ставкой S:
  * я ставлю ПРОТИВ X (lay X): lay-кэф = 1/p, lay_size (ставка бэкера) = S,
    моё обязательство = S·(1/p − 1);
  * с точки зрения исхода не-X это бэк не-X с кэфом 1/(1−p) и моей ставкой S·(1−p)/p.
Комиссия V3: тейкер 1 % с прибыли, мейкер 0 %.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import config  # noqa: F401 — подхватывает emu/.env в os.environ (там SX_API_KEY)
from odds_common import Fixture, Market, Q, sanitize_exchange_market

BASE = "https://api.sx.bet"
FEE_TAKER = 0.01
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) stavochnik-emu/0.2"
API_KEY = os.environ.get("SX_API_KEY", "")
SPORT_SOCCER = 5
TYPES = {1: "1x2", 2: "total", 3: "ah", 52: "12"}


class SxError(Exception):
    pass


def _get(path, params, retries=3, timeout=30):
    url = f"{BASE}/{path}?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if API_KEY:
        headers["x-sx-api-key"] = API_KEY
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise SxError(f"HTTP {e.code}: {body}")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise SxError(f"net error: {e}")
    raise SxError("unreachable")


def pct_to_p(pct_odds_str):
    p = int(pct_odds_str) / 1e20
    return p if 0 < p < 1 else None


def size_to_usdc(size_str):
    return int(size_str) / 1e6


# ── рынки ────────────────────────────────────────────────────────────────────

def list_markets(types=(1, 2, 3), page_size=100, max_pages=60):
    """Все активные футбольные рынки заданных типов. Возвращает список сырых dict."""
    rows = []
    for t in types:
        key = None
        for _ in range(max_pages):
            params = {"sportIds": str(SPORT_SOCCER), "type": str(t), "pageSize": str(page_size)}
            if key:
                params["paginationKey"] = key
            d = _get("markets/active", params)
            if d.get("status") != "success":
                raise SxError(f"markets/active: {str(d)[:200]}")
            rows.extend(d["data"]["markets"])
            key = d["data"].get("nextKey")
            if not key:
                break
    return rows


def build_fixtures(rows):
    """Сырые рынки → {sportXeventId: Fixture} с рынками (без цен). В Market.ext:
    legs = {sel: (marketHash, side)} где side ∈ {"one","two"} — на какой стороне рынка стоит sel."""
    fx = {}
    for m in rows:
        if m.get("status") != "ACTIVE":
            continue
        t = m.get("type")
        mtype = TYPES.get(t)
        if not mtype:
            continue
        eid = m.get("sportXeventId")
        f = fx.get(eid)
        if f is None:
            f = Fixture(src="sx", ext_id=str(eid), home=m.get("teamOneName") or "",
                        away=m.get("teamTwoName") or "", league=m.get("leagueLabel") or "",
                        start_ts=int(m.get("gameTime") or 0), is_exchange=True, fee=FEE_TAKER,
                        raw={"league_id": m.get("leagueId"), "live_enabled": m.get("liveEnabled")})
            fx[eid] = f
        score = m.get("teamOneScore")
        if score is not None or (f.start_ts and f.start_ts < time.time()):
            f.is_live = True
        h = m["marketHash"]
        o1 = m.get("outcomeOneName") or ""
        live_ok = bool(m.get("liveEnabled"))
        if mtype == "1x2":
            mk = f.markets.setdefault(("1x2", None), Market(ext={"legs": {}}))
            if o1 == f.home:
                sel = "home"
            elif o1 == f.away:
                sel = "away"
            else:
                sel = "draw"
            mk.ext["legs"][sel] = (h, "one")
            mk.sels.setdefault(sel, Q())
        elif mtype == "12":
            mk = f.markets.setdefault(("12", None), Market(ext={"legs": {}}))
            mk.ext["legs"]["home"] = (h, "one")
            mk.ext["legs"]["away"] = (h, "two")
            mk.sels.setdefault("home", Q()); mk.sels.setdefault("away", Q())
        elif mtype == "total":
            line = m.get("line")
            if line is None:
                continue
            mk = f.markets.setdefault(("total", float(line)), Market(ext={"legs": {}, "main": bool(m.get("mainLine"))}))
            mk.ext["legs"]["over"] = (h, "one")
            mk.ext["legs"]["under"] = (h, "two")
            mk.sels.setdefault("over", Q()); mk.sels.setdefault("under", Q())
        elif mtype == "ah":
            line = m.get("line")
            if line is None:
                continue
            mk = f.markets.setdefault(("ah", float(line)), Market(ext={"legs": {}, "main": bool(m.get("mainLine"))}))
            mk.ext["legs"]["home"] = (h, "one")
            mk.ext["legs"]["away"] = (h, "two")
            mk.sels.setdefault("home", Q()); mk.sels.setdefault("away", Q())
    # 1x2 нужен полный (три ноги)
    for f in fx.values():
        mk = f.markets.get(("1x2", None))
        if mk and len(mk.ext["legs"]) < 3:
            del f.markets[("1x2", None)]
    return {k: v for k, v in fx.items() if v.markets}


def _mark_live_flags(rows, fx):
    for m in rows:
        f = fx.get(m.get("sportXeventId"))
        if not f:
            continue
        for mk in f.markets.values():
            for sel, (h, side) in mk.ext["legs"].items():
                if h == m["marketHash"]:
                    mk.ext.setdefault("live_ok", {})[h] = bool(m.get("liveEnabled"))


# ── стакан ───────────────────────────────────────────────────────────────────

def snapshot(market_hash):
    d = _get("orderbook-v3/snapshot", {"marketHash": market_hash}, timeout=20)
    return d.get("data") or {}


def event_snapshot(event_id):
    """Стаканы ВСЕХ рынков события одним запросом (нужен x-sx-api-key).
    Возвращает {marketHash: snapshot}. Экономит ~5 запросов на матч против поштучного snapshot."""
    out = {}
    key = None
    for _ in range(10):
        params = {"eventId": event_id}
        if key:
            params["nextKey"] = key
        d = _get("orderbook-v3/snapshot/event", params, timeout=25)
        data = d.get("data") or {}
        for m in data.get("markets") or []:
            out[m["marketHash"]] = m
        key = data.get("nextKey")
        if not key:
            break
    return out


def _best(levels):
    """Лучший уровень мейкеров: (p, size_usdc) или None. Индекс 0 — лучшая цена (макс. p)."""
    best = None
    for lvl in levels or []:
        p = pct_to_p(lvl["percentageOdds"])
        if p is None:
            continue
        s = size_to_usdc(lvl["size"])
        if best is None or p > best[0]:
            best = (p, s)
    return best


def taker_view(snap):
    """Стакан рынка → {"one": Q, "two": Q} с точки зрения тейкера по каждому исходу рынка."""
    one = _best(snap.get("outcomeOne"))
    two = _best(snap.get("outcomeTwo"))
    out = {}
    for side, same, opp in (("one", one, two), ("two", two, one)):
        q = Q()
        if opp:   # мейкер на противоположной стороне (p_opp, S_opp) → мой бэк на side
            p, s = opp
            q.back = 1.0 / (1.0 - p)
            q.back_size = s * (1.0 - p) / p
        if same:  # мейкер на моей стороне → мой lay против side
            p, s = same
            q.lay = 1.0 / p
            q.lay_size = s
        q.depth = {"one": [(round(pct_to_p(l["percentageOdds"]) or 0, 4), round(size_to_usdc(l["size"]), 1)) for l in (snap.get("outcomeOne") or [])[:3]],
                   "two": [(round(pct_to_p(l["percentageOdds"]) or 0, 4), round(size_to_usdc(l["size"]), 1)) for l in (snap.get("outcomeTwo") or [])[:3]]}
        out[side] = q
    return out


def refresh_prices(fixtures, workers=8, only_main=True):
    """Обновляет котировки рынков фикстур.
    С API-ключом — один запрос на СОБЫТИЕ (все рынки сразу); без ключа — по одному на рынок.
    only_main: для тоталов/гандикапов — только mainLine (экономим запросы)."""
    if API_KEY:
        return _refresh_by_event(fixtures, workers, only_main)
    jobs = []
    for f in fixtures:
        for mkey, mk in f.markets.items():
            if only_main and mkey[0] in ("total", "ah") and not mk.ext.get("main"):
                continue
            for h in {h for h, _ in mk.ext["legs"].values()}:
                if f.is_live and not mk.ext.get("live_ok", {}).get(h, True):
                    for sel, (hh, _) in mk.ext["legs"].items():
                        if hh == h:
                            mk.sels[sel] = Q()
                    continue
                jobs.append((f, mkey, h))
    results = {}

    def work(job):
        f, mkey, h = job
        try:
            results[h] = taker_view(snapshot(h))
        except SxError as e:
            results[h] = e

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, jobs))
    errors = 0
    for f, mkey, h in jobs:
        r = results.get(h)
        mk = f.markets[mkey]
        if isinstance(r, Exception) or r is None:
            errors += 1
            continue
        for sel, (hh, side) in mk.ext["legs"].items():
            if hh == h:
                mk.sels[sel] = r[side]
    for f in fixtures:
        for mk in f.markets.values():
            sanitize_exchange_market(mk)
    return len(jobs), errors


def _refresh_by_event(fixtures, workers=8, only_main=True):
    """Быстрый путь (с ключом): один запрос orderbook-v3/snapshot/event на матч."""
    results = {}

    def work(f):
        try:
            results[f.ext_id] = event_snapshot(f.ext_id)
        except SxError as e:
            results[f.ext_id] = e

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, fixtures))
    n = errors = 0
    for f in fixtures:
        snaps = results.get(f.ext_id)
        if isinstance(snaps, Exception) or snaps is None:
            errors += 1
            continue
        n += 1
        for mkey, mk in f.markets.items():
            if only_main and mkey[0] in ("total", "ah") and not mk.ext.get("main"):
                continue
            for sel, (h, side) in mk.ext["legs"].items():
                snap = snaps.get(h)
                if snap is None:
                    mk.sels[sel] = Q()
                    continue
                if f.is_live and not mk.ext.get("live_ok", {}).get(h, True):
                    mk.sels[sel] = Q()
                    continue
                mk.sels[sel] = taker_view(snap)[side]
            sanitize_exchange_market(mk)
    return n, errors


def fetch_soccer(types=(1, 2, 3), with_prices=False):
    rows = list_markets(types)
    fx = build_fixtures(rows)
    _mark_live_flags(rows, fx)
    if with_prices:
        refresh_prices(list(fx.values()))
    return fx


if __name__ == "__main__":
    t = time.time()
    fx = fetch_soccer()
    print(f"SX: {len(fx)} матчей, {time.time()-t:.1f}s; live: {sum(1 for f in fx.values() if f.is_live)}")
    now = time.time()
    soon = [f for f in fx.values() if f.is_live or 0 < f.start_ts - now < 3 * 3600][:3]
    n, err = refresh_prices(soon)
    print(f"snapshots: {n}, ошибок {err}")
    for f in soon:
        print(f.home, "-", f.away, f.league, "live" if f.is_live else "", list(f.markets)[:5])
        for mkey, mk in f.markets.items():
            if mkey[0] in ("total", "ah") and not mk.ext.get("main"):
                continue
            print("  ", mkey, {s: (q.back and round(q.back, 3), round(q.back_size, 1), q.lay and round(q.lay, 3), round(q.lay_size, 1)) for s, q in mk.sels.items()})

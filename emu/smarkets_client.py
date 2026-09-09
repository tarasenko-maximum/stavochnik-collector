# -*- coding: utf-8 -*-
"""
Smarkets — публичный REST API v3 (api.smarkets.com), без ключа для чтения котировок.
Проверено 06.09.2026 с домашнего IP (Сербия): 200; с сервера Hetzner DE: 403.

  GET /v3/events/?state=upcoming|live&type=football_match&limit=100&sort=start_datetime,id
      &start_datetime_min=...&start_datetime_max=...
  GET /v3/events/{id}/markets/           — рынки события (WINNER_3_WAY, OVER_UNDER param, ...)
  GET /v3/markets/{id}/contracts/         — исходы (contract_id, name)
  GET /v3/markets/{id1,id2,...}/quotes/   — стакан: {contract_id: {bids:[{price,quantity}], offers:[...]}}

Единицы (по документации Smarkets, проверить при первой сделке):
  price    — вероятность в базисных пунктах (3497 = 34.97 % → кэф 2.86)
  quantity — размер в 1/10000 GBP ВЫПЛАТЫ (payout); ставка = payout·price/10000
  bids     — заявки на ПОКУПКУ контракта (бэк) → в них можно продать (lay):
             lay-кэф = 10000/price, lay_size (ставка бэкера) = quantity·price/1e8
  offers   — заявки на ПРОДАЖУ (lay) → в них можно купить (бэк):
             back-кэф = 10000/price, back_size (моя ставка) = quantity·price/1e8
Комиссия: 2 % с чистой прибыли (стандартный тариф).
"""

import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from odds_common import Fixture, Market, Q, sanitize_exchange_market

BASE = "https://api.smarkets.com/v3"
FEE = 0.02
LAST_REQ = 0.0
MIN_INTERVAL = 0.5       # 429 при ~8 req/s (проверено 06.09)
QUOTES_BATCH = 80
STATES_EVERY_S = 90      # состояния live-рынков перечитываем не чаще раза в 90 с (open→live→halted→settled)
_last_states = {}        # ext_id -> ts последней проверки


class SmarketsError(Exception):
    pass


def _get(path, params=None, retries=3):
    global LAST_REQ
    url = f"{BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                                              "Accept": "application/json"})
    for attempt in range(retries):
        dt = time.time() - LAST_REQ
        if dt < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - dt)
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                LAST_REQ = time.time()
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            LAST_REQ = time.time()
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep((12 if e.code == 429 else 3) * (attempt + 1))
                continue
            raise SmarketsError(f"HTTP {e.code} {url}: {body}")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise SmarketsError(f"net error: {e}")
    raise SmarketsError("unreachable")


def _ts(iso):
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def list_events(t_min, t_max):
    """Футбольные события (upcoming + live) в окне unix [t_min, t_max]."""
    out = {}
    for state in ("live", "upcoming"):
        params = {"state": state, "type": "football_match", "limit": 100, "sort": "start_datetime,id",
                  "start_datetime_min": datetime.fromtimestamp(t_min, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "start_datetime_max": datetime.fromtimestamp(t_max, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        url = "events/"
        while True:
            d = _get(url, params) if params else _get(url)
            for e in d.get("events") or []:
                name = e.get("name") or ""
                if " vs " not in name:
                    continue
                home, away = name.split(" vs ", 1)
                out[e["id"]] = Fixture(
                    src="smarkets", ext_id=str(e["id"]), home=home.strip(), away=away.strip(),
                    league=(e.get("full_slug") or "").split("/")[3] if e.get("full_slug") else "",
                    start_ts=_ts(e.get("start_datetime") or ""),
                    is_live=(e.get("state") == "live"), is_exchange=True, fee=FEE,
                )
            nxt = (d.get("pagination") or {}).get("next_page")
            if not nxt:
                break
            # next_page — относительная строка запроса ("?state=...") к тому же ресурсу
            qs = nxt.split("?", 1)[1] if "?" in nxt else ""
            params = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
            url = "events/"
    return out


def load_markets(fixture: Fixture, want_totals=(1.5, 2.5, 3.5)):
    """Заполняет fixture.markets структурой (без цен): 1x2 + тоталы; хранит contract ids."""
    d = _get(f"events/{fixture.ext_id}/markets/")
    for m in d.get("markets") or []:
        mt = (m.get("market_type") or {})
        name = mt.get("name")
        if m.get("state") not in ("open", "live", None):
            continue
        if name == "WINNER_3_WAY":
            mkey = ("1x2", None)
        elif name == "OVER_UNDER":
            try:
                line = float(mt.get("param"))
            except (TypeError, ValueError):
                continue
            if line not in want_totals:
                continue
            mkey = ("total", line)
        else:
            continue
        c = _get(f"markets/{m['id']}/contracts/")
        sels = {}
        cmap = {}
        contracts = c.get("contracts") or []
        for ct in contracts:
            nm = (ct.get("name") or "").strip()
            low = nm.lower()
            if mkey[0] == "1x2":
                if low == "draw":
                    sel = "draw"
                elif nm == fixture.home or low == fixture.home.lower():
                    sel = "home"
                elif nm == fixture.away or low == fixture.away.lower():
                    sel = "away"
                else:
                    sel = None
            else:
                sel = "over" if low.startswith("over") else ("under" if low.startswith("under") else None)
            if sel:
                sels[sel] = Q()
                cmap[ct["id"]] = sel
        if mkey[0] == "1x2" and len(sels) < 3 and len(contracts) == 3:
            # fallback: порядок home, draw, away по display_order
            ordered = sorted(contracts, key=lambda x: x.get("display_order", 0))
            names = ["home", "draw", "away"]
            sels, cmap = {}, {}
            for ct, s in zip(ordered, names):
                if (ct.get("name") or "").lower() == "draw":
                    s = "draw"
                sels[s] = Q()
                cmap[ct["id"]] = s
        if sels:
            fixture.markets[mkey] = Market(sels=sels, ext={"market_id": m["id"], "contracts": cmap,
                                                            "inplay": m.get("inplay_enabled"),
                                                            "state": m.get("state"), "delay": m.get("bet_delay")})
    return fixture


def refresh_states(fixtures: list):
    """Для live-событий перечитывает состояние рынков (open → live/halted/settled).
    Состояния Smarkets: open (пре-матч), live (в игре, bet_delay ~8 с), halted, settled."""
    now = time.time()
    for f in fixtures:
        if not f.is_live:
            continue
        if now - _last_states.get(f.ext_id, 0) < STATES_EVERY_S:
            continue
        _last_states[f.ext_id] = now
        try:
            d = _get(f"events/{f.ext_id}/markets/")
        except SmarketsError as e:
            print(f"[smarkets] states {f.ext_id}: {e}")
            continue
        st = {m["id"]: (m.get("state"), m.get("bet_delay")) for m in d.get("markets") or []}
        for mk in f.markets.values():
            s = st.get(mk.ext["market_id"])
            if s:
                mk.ext["state"], mk.ext["delay"] = s


def _market_valid(f: Fixture, mk: Market):
    """Пре-матч: только state=open. Live: только state=live (иначе цены устаревшие/остановленные)."""
    st = mk.ext.get("state")
    return st == "live" if f.is_live else st == "open"


def refresh_quotes(fixtures: list):
    """Обновляет back/lay по всем рынкам списка фикстур (батчами market_id)."""
    refresh_states(fixtures)
    idx = {}
    for f in fixtures:
        for mkey, mk in f.markets.items():
            if not _market_valid(f, mk):
                for sel in mk.sels:
                    mk.sels[sel] = Q()
                continue
            idx[mk.ext["market_id"]] = (f, mkey)
    ids = list(idx)
    for i in range(0, len(ids), QUOTES_BATCH):
        chunk = ids[i:i + QUOTES_BATCH]
        d = _get(f"markets/{','.join(chunk)}/quotes/")
        for mid in chunk:
            f, mkey = idx[mid]
            mk = f.markets[mkey]
            for cid, sel in mk.ext["contracts"].items():
                qd = d.get(cid) or {}
                q = Q()
                bids = qd.get("bids") or []
                offers = qd.get("offers") or []
                if offers:
                    best = max(offers, key=lambda x: -x["price"])   # мин. цена = макс. кэф для бэка
                    best = min(offers, key=lambda x: x["price"])
                    p = best["price"]
                    if 0 < p < 10000:
                        q.back = 10000.0 / p
                        q.back_size = best["quantity"] * p / 1e8
                if bids:
                    best = max(bids, key=lambda x: x["price"])       # макс. цена = мин. lay-кэф
                    p = best["price"]
                    if 0 < p < 10000:
                        q.lay = 10000.0 / p
                        q.lay_size = best["quantity"] * p / 1e8
                q.depth = {"bids": bids[:3], "offers": offers[:3]}
                mk.sels[sel] = q
            sanitize_exchange_market(mk)
    return fixtures


import os
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "smarkets_markets_cache.json")


def _disk_cache_load():
    try:
        with open(CACHE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _disk_cache_save(d):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        print("[smarkets] cache save:", e)


def _markets_to_cache(f: Fixture):
    return {f"{k[0]}|{k[1]}": {"market_id": mk.ext["market_id"], "contracts": mk.ext["contracts"],
                               "inplay": mk.ext.get("inplay"), "state": mk.ext.get("state"), "delay": mk.ext.get("delay")}
            for k, mk in f.markets.items()}


def _markets_from_cache(f: Fixture, d):
    for k, v in d.items():
        mt, ln = k.split("|")
        mkey = (mt, None if ln == "None" else float(ln))
        f.markets[mkey] = Market(sels={sel: Q() for sel in v["contracts"].values()},
                                 ext={"market_id": v["market_id"], "contracts": v["contracts"], "inplay": v.get("inplay"),
                                      "state": v.get("state"), "delay": v.get("delay")})


def fetch_soccer(hours_back=3, hours_ahead=14, cache=None):
    """Полный цикл: события окна → рынки (кэш по ext_id, в памяти + на диске) → котировки. Возвращает {ext_id: Fixture}."""
    now = int(time.time())
    evs = list_events(now - hours_back * 3600, now + hours_ahead * 3600)
    cache = cache if cache is not None else {}
    disk = _disk_cache_load()
    disk_dirty = False
    out = {}
    for eid, f in evs.items():
        c = cache.get(eid)
        if c is None:
            if eid in disk:
                _markets_from_cache(f, disk[eid])
            else:
                try:
                    load_markets(f)
                except SmarketsError as e:
                    print(f"[smarkets] markets {eid}: {e}")
                    continue
                disk[eid] = _markets_to_cache(f)
                disk_dirty = True
            cache[eid] = f
            c = f
        else:
            c.is_live = f.is_live
        if c.markets:
            # событие, стартовавшее по времени, но не помеченное live, считаем live (цены open-рынков устарели)
            if not c.is_live and c.start_ts and c.start_ts < now:
                c.is_live = True
            out[eid] = c
    if disk_dirty:
        _disk_cache_save(disk)
    refresh_quotes(list(out.values()))
    return out


if __name__ == "__main__":
    t = time.time()
    fx = fetch_soccer(hours_back=1, hours_ahead=6)
    print(f"Smarkets: {len(fx)} событий, {time.time()-t:.1f}s, live: {sum(1 for f in fx.values() if f.is_live)}")
    for f in list(fx.values())[:3]:
        print(f.home, "-", f.away, f.league, f.is_live)
        for mkey, mk in f.markets.items():
            print("  ", mkey, {s: (q.back, round(q.back_size, 1), q.lay, round(q.lay_size, 1)) for s, q in mk.sels.items()})

# -*- coding: utf-8 -*-
"""
Клиент Cloudbet Feed API v2 (sports-api.cloudbet.com) — букмекерское плечо live-контура.

Проверено 07.09.2026: хост доступен и с ноутбука, и с сервера Hetzner DE (HTTP 401 без ключа —
значит блокировки по IP нет, в отличие от Pinnacle/Smarkets/Betfair, которые с дата-центра дают 403).
Это делает пару **Cloudbet × SX.bet** единственной, которая целиком работает на сервере 24/7.

База: https://sports-api.cloudbet.com/pub/v2/odds
  GET /sports                              — список видов спорта
  GET /events?sport=soccer&from=&to=&markets=...   — события окна (from/to — unix)
  GET /events?sport=soccer&live=true&markets=...   — события в игре
  GET /events/{id}?markets=...             — одно событие
Аутентификация: заголовок `X-API-Key: <JWT>`; Trading API Key = real-time (нужен для арбитража),
Affiliate-ключ кэширует до 1 минуты и НЕ подходит.

**Реальная схема, снята с живого API 08.09.2026 (документация врёт в двух местах):**
1. `/events` возвращает НЕ `{"events": [...]}`, а `{"competitions": [{name, key, sport, category,
   events: [...]}]}` — события вложены в турниры.
2. Ключи рынков в snake_case, а не camelCase, как в wiki:
   `soccer.match_odds` — 1X2,            selections: outcome home|draw|away, params ""
   `soccer.total_goals` — тоталы,        selections: outcome over|under,     params "total=2.5"
   `soccer.asian_handicap` — АГ,         selections: outcome home|away,      params "handicap=-1.5"
Субмаркеты: ключ вида "period=ft" (основное время); варианты с "ot" (овертайм) пропускаем.
Selection: {outcome, params, price (десятичный кэф), maxStake, minStake, status, side, probability}.
Приостановленные рынки приходят как `status: SELECTION_DISABLED` с `price: 0` — их отбрасываем.
Кроме основных, Cloudbet отдаёт и редкие рынки (угловые, карточки, точный счёт, обе забьют,
двойной шанс) — потенциал для будущих вилок, но SX.bet их не торгует, поэтому пока не берём.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import config  # noqa: F401 — подхватывает emu/.env в os.environ (там CLOUDBET_API_KEY)
from odds_common import Fixture, Market, Q

BASE = "https://sports-api.cloudbet.com/pub/v2/odds"
FOOTBALL = "soccer"
M_1X2 = "soccer.match_odds"
M_TOTAL = "soccer.total_goals"
M_AH = "soccer.asian_handicap"
MARKETS = f"{M_1X2},{M_TOTAL},{M_AH}"
LAST_REQ = 0.0
MIN_INTERVAL = 0.25


class CloudbetError(Exception):
    pass


def api_key():
    return (os.environ.get("CLOUDBET_API_KEY") or "").strip()


def _get(path, params=None, retries=3):
    global LAST_REQ
    key = api_key()
    if not key:
        raise CloudbetError(
            "CLOUDBET_API_KEY не задан. Аккаунт cloudbet.com (регистрация + депозит ~10 EUR) → "
            "My account → API (https://www.cloudbet.com/en/player/api) → Trading API Key (JWT) → "
            "положить в emu/.env строкой CLOUDBET_API_KEY=... Без ключа Feed API отдаёт HTTP 401."
        )
    url = f"{BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "X-API-Key": key,
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) stavochnik-emu/0.3"}
    for attempt in range(retries):
        dt = time.time() - LAST_REQ
        if dt < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - dt)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                LAST_REQ = time.time()
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            LAST_REQ = time.time()
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            if e.code == 401:
                raise CloudbetError(f"HTTP 401: невалидный или просроченный X-API-Key ({body!r})")
            raise CloudbetError(f"HTTP {e.code}: {body}")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise CloudbetError(f"net error: {e}")
    raise CloudbetError("unreachable")


def _ts(iso):
    from datetime import datetime
    try:
        return int(datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _param(s, name):
    """'total=2.5' -> 2.5 ; 'handicap=-0.5' -> -0.5 ; иначе None."""
    for part in (s or "").split("&"):
        if part.startswith(name + "="):
            try:
                return float(part.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _submarket(market):
    """Субмаркет основного времени (ключ без 'ot'); при отсутствии — первый попавшийся."""
    subs = (market or {}).get("submarkets") or {}
    if not isinstance(subs, dict):
        return None
    for k, v in subs.items():
        if "ot" not in k:
            return v
    return next(iter(subs.values()), None)


def _enabled(sel):
    """Ставка принимается: цена > 1 и статус не DISABLED (приостановленные приходят с price=0)."""
    try:
        price = float(sel.get("price") or 0)
    except (TypeError, ValueError):
        return False
    return price > 1.0 and sel.get("status") in (None, "SELECTION_ENABLED")


def parse_event(ev, league=""):
    """Событие Cloudbet → Fixture с рынками 1x2 / total / ah."""
    home = (ev.get("home") or {}).get("name") or ""
    away = (ev.get("away") or {}).get("name") or ""
    status = ev.get("status") or ""
    # у live-событий startTime может отсутствовать — берём cutoffTime как ориентир
    start = _ts(ev.get("startTime")) or _ts(ev.get("cutoffTime"))
    f = Fixture(src="cloudbet", ext_id=str(ev.get("id")), home=home, away=away,
                league=league,
                start_ts=start,
                is_live=status in ("TRADING_LIVE", "LIVE"),
                raw={"status": status, "cutoff": ev.get("cutoffTime")})
    markets = ev.get("markets") or {}

    sm = _submarket(markets.get(M_1X2))
    if sm:
        sels = {}
        for s in sm.get("selections") or []:
            if s.get("params"):
                continue
            o = s.get("outcome")
            if o in ("home", "draw", "away") and _enabled(s):
                sels[o] = Q(back=float(s["price"]), back_size=float(s.get("maxStake") or 0))
        if len(sels) == 3:
            f.markets[("1x2", None)] = Market(sels=sels, ext={"market": M_1X2})

    sm = _submarket(markets.get(M_TOTAL))
    if sm:
        by_line = {}
        for s in sm.get("selections") or []:
            line = _param(s.get("params"), "total")
            o = s.get("outcome")
            if line is None or o not in ("over", "under") or not _enabled(s):
                continue
            by_line.setdefault(line, {})[o] = Q(back=float(s["price"]), back_size=float(s.get("maxStake") or 0))
        for line, sels in by_line.items():
            if len(sels) == 2:
                f.markets[("total", line)] = Market(sels=sels, ext={"market": M_TOTAL, "main": False})

    sm = _submarket(markets.get(M_AH))
    if sm:
        by_line = {}
        for s in sm.get("selections") or []:
            line = _param(s.get("params"), "handicap")
            o = s.get("outcome")
            if line is None or o not in ("home", "away") or not _enabled(s):
                continue
            # линия в Cloudbet задана со стороны home (как и у нас)
            by_line.setdefault(line, {})[o] = Q(back=float(s["price"]), back_size=float(s.get("maxStake") or 0))
        for line, sels in by_line.items():
            if len(sels) == 2:
                f.markets[("ah", line)] = Market(sels=sels, ext={"market": M_AH, "main": False})
    return f


def fetch_soccer(hours_back=2, hours_ahead=6):
    """Футбольные события окна + live. Возвращает {ext_id: Fixture}.
    Ответ приходит как {"competitions": [{name, events: [...]}]} — разворачиваем."""
    now = int(time.time())
    out = {}
    # ⚠ Параметр `markets=` НЕ передаём: проверено 08.09.2026 — с ним API возвращает те же события,
    # но все selections приходят как SELECTION_DISABLED с price=0. Фильтруем рынки на своей стороне.
    for params in (
        {"sport": FOOTBALL, "live": "true"},
        {"sport": FOOTBALL, "from": now - int(hours_back * 3600), "to": now + int(hours_ahead * 3600),
         "limit": 500},
    ):
        try:
            d = _get("events", params)
        except CloudbetError as e:
            if "401" in str(e) or "не задан" in str(e):
                raise
            print(f"[cloudbet] {e}")
            continue
        for comp in d.get("competitions") or []:
            league = comp.get("name") or ""
            for ev in comp.get("events") or []:
                f = parse_event(ev, league)
                if not f.markets:
                    continue
                prev = out.get(f.ext_id)
                if prev is None or (f.is_live and not prev.is_live):
                    out[f.ext_id] = f
    return out


if __name__ == "__main__":
    # Диагностика после получения ключа: python cloudbet_client.py
    try:
        fx = fetch_soccer()
        live = [f for f in fx.values() if f.is_live]
        print(f"Cloudbet: {len(fx)} событий с рынками, live {len(live)}")
        for f in list(fx.values())[:3]:
            print(f.home, "-", f.away, "|", f.league, "| live" if f.is_live else "|", list(f.markets)[:6])
            for mkey, mk in list(f.markets.items())[:4]:
                print("   ", mkey, {s: (q.back, q.back_size) for s, q in mk.sels.items()})
    except CloudbetError as e:
        print("CloudbetError:", e)

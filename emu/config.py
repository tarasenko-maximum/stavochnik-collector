# -*- coding: utf-8 -*-
"""
«Ставочник» — фаза эмуляции. Конфигурация.
Все параметры — здесь. Ключи вставлять сюда или в файл .env рядом.
"""

import os

# ── .env рядом с config.py (KEY=value), не коммитится ─────────────────────────
_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_ENV):
    with open(_ENV, encoding="utf-8") as _fh:
        for _line in _fh:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ── API-ключи ────────────────────────────────────────────────────────────────
# OddsPapi: https://oddspapi.io/en/sign-up — бесплатно, 250 запросов/мес,
# покрывает крипто-БК (cloudbet, 1xbit, bcgame, stake, sx.bet...) + историю с 01.2026.
ODDSPAPI_KEY = os.environ.get("ODDSPAPI_KEY", "")
# The Odds API (опционально, эталон Pinnacle/Polymarket): https://the-odds-api.com
THEODDSAPI_KEY = os.environ.get("THEODDSAPI_KEY", "")
# Cloudbet Feed API (sports-api.cloudbet.com/pub/v2/odds): ключ из аккаунта Cloudbet
# «My account → API» (https://www.cloudbet.com/en/player/api); нужен депозит ~10 EUR.
# Без ключа API отдаёт HTTP 401. Trading-ключ = real-time; Affiliate-ключ = кэш до 1 мин.
CLOUDBET_API_KEY = os.environ.get("CLOUDBET_API_KEY", "")


# ── Пары для замера вилок (обе книги должны быть доступны из Сербии) ────────
# Slug'и книг — из каталога OddsPapi (/sportsbooks/), например cloudbet, 1xbit, bcgame.
PAIRS = [
    ("cloudbet", "bcgame"),
    ("cloudbet", "stake"),
    ("bcgame", "stake"),
    ("superbet.rs", "balkanbet.rs"),
]
# Острый эталон (контроль маржи; ставки не делаем)
SHARP_BOOK = "pinnacle"
# Сербские легальные БК — бонусное сравнение (выигрыш в них не облагается налогом)
SERBIAN_BOOKS = ["superbet.rs", "balkanbet.rs", "soccerbet.rs", "admiralbet.rs"]

# ── Турниры (sportId=10 — футбол) ────────────────────────────────────────────
# Числовые id из /v4/tournaments. Если пусто — при старте резолвим по slug ниже.
TOURNAMENT_IDS = []
TOURNAMENT_SLUGS = [
    "premier-league", "laliga", "bundesliga", "serie-a", "ligue-1",
    "championship", "eredivisie", "brazil-serie-a", "mls", "superliga",
]

# ── Маркеты ──────────────────────────────────────────────────────────────────
# 101 = 1X2 (home/draw/away), 104/105 = тоталы. Ключи outcome — из API OddsPapi.
MARKETS = ["101"]  # 1X2 — базовый замер (ключ в API — строка)

# ── Пороги и экономика ───────────────────────────────────────────────────────
MIN_EDGE_PCT = 1.0      # «сделка»: вилка с маржой ≤ −1% (после этого порога прибыльна с учётом издержек)
RECORD_ANY_ARB = True   # в статистику писать любые вилки (маржа < 0), не только ≥ порога
MIN_LIMIT_EUR = 20.0    # мин. лимит на плечо (в валюте книги) для сделки
COSTS_PCT = 1.0         # накладные издержки на сделку: вывод/ramp/комиссии, %
MIN_ARB_LIFETIME_MIN = 10  # мин. «время жизни» вилки (как долго Δ держался), чтобы считаться исполнимой

# ── Банкролл и размеры ───────────────────────────────────────────────────────
BANKROLL_EUR = 10000
STAKE_PER_TRADE_EUR = 50.0   # на одно плечо
MAX_STAKE_EUR = 200.0
TRADES_PER_DAY_CAP = 5       # лимит сделок/день (дисциплина)

# ── Поллинг (free-лимит OddsPapi 250 req/мес → экономим) ─────────────────────
LIVE_POLL_HOURS = 6          # live-снимок раз в N часов
HISTORY_LOOKBACK_DAYS = 21   # ретроспектива: сколько дней истории тянем
HISTORY_MAX_FIXTURES = 20    # потолок матчей за один ретро-прогон (запросов = матчи × 1)

# ── Хранилище ────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "state", "emu.db")
STATE_DIR = os.path.join(BASE_DIR, "state")

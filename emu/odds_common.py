# -*- coding: utf-8 -*-
"""
Общие структуры для live-контура «Ставочника»: нормализованное представление
фикстур/рынков/котировок из любого источника (букмекер или биржа), нормализация
названий команд и матчинг фикстур между источниками.

Соглашения:
  mkey  = (mtype, line):  mtype ∈ {"1x2", "12", "total", "ah"}; line — float или None.
  sel   ∈ {"home", "draw", "away", "over", "under"}; для "ah" line задана для home
          (away = −line).
  Котировка букмекера:  back (десятичный кэф), back_size (макс. ставка, у.е.).
  Котировка биржи:      back/back_size — цена и объём, доступные тейкеру НА исход;
                        lay/lay_size  — цена и объём для lay ПРОТИВ исхода
                        (lay_size = ставка бэкера, которую можно принять; обязательство
                        = lay_size·(lay−1)); fee — комиссия биржи с чистой прибыли.
"""

import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional


@dataclass
class Q:
    back: Optional[float] = None
    back_size: float = 0.0
    lay: Optional[float] = None
    lay_size: float = 0.0
    depth: dict = field(default_factory=dict)   # доп. уровни / сырые данные (для отладки)


@dataclass
class Market:
    sels: dict = field(default_factory=dict)     # sel -> Q
    ext: dict = field(default_factory=dict)      # внешние id (marketHash, id и т.п.)


@dataclass
class Fixture:
    src: str                      # "pinnacle" | "sx" | "smarkets" | "cloudbet"
    ext_id: str
    home: str
    away: str
    league: str
    start_ts: int                 # unix
    is_live: bool = False
    is_exchange: bool = False
    fee: float = 0.0              # комиссия биржи с прибыли (0.01 = 1 %)
    markets: dict = field(default_factory=dict)  # mkey -> Market
    raw: dict = field(default_factory=dict)

    def key(self):
        return f"{self.src}:{self.ext_id}"


# ── конверсии ────────────────────────────────────────────────────────────────

def american_to_decimal(a):
    if a is None:
        return None
    a = float(a)
    if a >= 100:
        return 1.0 + a / 100.0
    if a <= -100:
        return 1.0 + 100.0 / (-a)
    return None


def eff_back(k, fee):
    """Эффективный кэф бэка на бирже после комиссии с прибыли."""
    if not k or k <= 1.0:
        return None
    return 1.0 + (k - 1.0) * (1.0 - fee)


# ── нормализация названий ────────────────────────────────────────────────────

_STOP = {
    "fc", "cf", "sc", "ac", "as", "us", "ss", "sv", "fk", "nk", "hk", "kf", "ks", "bk", "if",
    "afc", "cfc", "club", "clube", "de", "del", "da", "do", "la", "le", "el", "the", "of",
    "cd", "ud", "sd", "ca", "cs", "csd", "csm", "asa", "fsv", "tsv", "vfb", "vfl", "vfr", "spvgg",
    "sk", "sp", "ssd", "usd", "asd", "pol", "polisportiva",
    "esporte", "clube", "futebol", "football",
    "calcio", "team", "fk", "ofk", "gks", "mks", "lks", "ks", "kks", "zks",
    "1", "01", "04", "05", "07", "08", "09", "96", "98", "99", "1899", "1900", "1901", "1903",
    "1904", "1905", "1906", "1907", "1908", "1909", "1910", "1911", "1912", "1913", "1919",
    "1920", "1921", "1923", "1925", "1926", "1927", "1929", "1930",
}
_KEEP_MARKERS = ("u17", "u18", "u19", "u20", "u21", "u23", "women", "w", "ii", "b", "reserves", "res", "youth")
_ALIAS = {"utd": "united", "man": "manchester", "atl": "atletico", "ath": "athletic", "dep": "deportivo",
          "wolves": "wolverhampton", "spurs": "tottenham", "psg": "paris", "inter": "internazionale", "st": "saint"}


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_name(s):
    """'SV Darmstadt 98' -> 'darmstadt'; 'Manchester Utd (W)' -> 'manchester utd w'."""
    s = strip_accents((s or "").lower())
    s = s.replace("&", " and ").replace("-", " ").replace("/", " ").replace(".", " ")
    s = re.sub(r"\(([^)]*)\)", r" \1 ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = [_ALIAS.get(t, t) for t in s.split() if t]
    out = []
    for t in toks:
        if t in _KEEP_MARKERS:
            out.append("w" if t == "women" else t)
            continue
        if t in _STOP:
            continue
        out.append(t)
    if not out:
        out = toks
    return " ".join(out)


def markers(s):
    return {t for t in norm_name(s).split() if t in _KEEP_MARKERS}


def _tok_match(x, y):
    """Совпадение двух токенов: равны, один — префикс другого (≥4 симв.) или очень близки (опечатка/транслит)."""
    if x == y:
        return 1.0
    if len(x) >= 4 and len(y) >= 4 and (x.startswith(y) or y.startswith(x)):
        return 0.9
    if len(x) >= 5 and len(y) >= 5 and SequenceMatcher(None, x, y).ratio() >= 0.87:
        return 0.85
    return 0.0


def name_sim(a, b):
    """Похожесть названий команд на уровне токенов. Суффиксные совпадения («-spor», «FC») не считаются:
    главное (самое длинное) слово одной команды обязано совпасть с каким-то словом другой."""
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    if markers(a) != markers(b):
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = na.split(), nb.split()
    sa, sb = set(ta), set(tb)
    if sa <= sb or sb <= sa:
        return 0.92
    # аббревиатура (OH Leuven = Oud-Heverlee Leuven): короткий токен = инициалы подряд идущих слов другой стороны
    def _acr(tok, other):
        if 2 <= len(tok) <= 3:
            for i in range(len(other) - len(tok) + 1):
                if "".join(w[0] for w in other[i:i + len(tok)]) == tok:
                    return 0.9
        return 0.0
    # главное слово каждой стороны должно найти пару
    def best(tok, other):
        return max(max((_tok_match(tok, o) for o in other), default=0.0), _acr(tok, other))
    main_a = max(ta, key=len)
    main_b = max(tb, key=len)
    if best(main_a, tb) == 0.0 and best(main_b, ta) == 0.0:
        return 0.0
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    score = sum(best(t, long_) for t in short) / max(len(short), 1)
    # штраф за лишние слова у длинной стороны (Waasland-Beveren vs Beveren — небольшой)
    score *= (len(short) / len(long_)) ** 0.25
    return score


def fixture_sim(fa: Fixture, fb: Fixture, max_dt=15 * 60):
    if abs(fa.start_ts - fb.start_ts) > max_dt:
        return 0.0
    h = name_sim(fa.home, fb.home)
    a = name_sim(fa.away, fb.away)
    if h == 0 or a == 0:
        return 0.0
    return (h + a) / 2.0


def match_fixtures(base: list, others: dict, threshold=0.72, margin=0.08):
    """base: список Fixture опорного источника; others: {src: [Fixture]}.
    Возвращает список групп: {"base": Fixture, src: Fixture, "scores": {src: score}}.
    Берём лучшего кандидата по каждому источнику; если два кандидата ближе, чем margin,
    матч считаем неоднозначным и пропускаем (важнее не поймать ложную вилку)."""
    groups = []
    used = {src: set() for src in others}
    for fb in base:
        g = {"base": fb, "scores": {}}
        for src, lst in others.items():
            cands = []
            for fo in lst:
                if fo.key() in used[src]:
                    continue
                s = fixture_sim(fb, fo)
                if s >= threshold:
                    cands.append((s, fo))
            if not cands:
                continue
            cands.sort(key=lambda x: -x[0])
            if len(cands) > 1 and cands[0][0] - cands[1][0] < margin and cands[0][0] < 0.95:
                continue
            s, fo = cands[0]
            g[src] = fo
            g["scores"][src] = round(s, 3)
            used[src].add(fo.key())
        if len(g["scores"]) > 0:
            groups.append(g)
    return groups


def now_ts():
    return int(time.time())


def sanitize_exchange_market(mk: Market):
    """Скрещённый стакан (back-кэф выше lay-кэфа) невозможен в живом матчинг-движке — это признак
    приостановленного/устаревшего рынка. Такие котировки обнуляем целиком."""
    for q in mk.sels.values():
        if q.back and q.lay and q.back > q.lay * 1.01:
            for qq in mk.sels.values():
                qq.back, qq.lay, qq.back_size, qq.lay_size = None, None, 0.0, 0.0
            mk.ext["crossed"] = True
            return False
    return True

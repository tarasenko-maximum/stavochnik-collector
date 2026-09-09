# -*- coding: utf-8 -*-
"""
Движок вилок live-контура: букмекер(и) × биржа(и) на нормализованных фикстурах.

Виды вилок (kind):
  BL  — back в букмекере на исход s + lay того же исхода на бирже.
        Ставка S в буке при кэфе Kb; lay-ставка L при кэфе Kl и комиссии f с прибыли.
        L = S·Kb/(Kl − f);  прибыль при любом исходе = S·(Kb·(1−f)/(Kl − f) − 1).
        edge = Kb·(1−f)/(Kl−f) − 1.   Ёмкость: S ≤ book_max, L ≤ lay_size.
  BB  — «крест»: по каждому исходу рынка берём лучший бэк среди источников
        (биржевой кэф — эффективный, 1+(K−1)(1−f)); Σ 1/K < 1. Обязательно ≥ 2 разных источника.
        X = min(cap_i·K_i), ставки X/K_i, прибыль X·(1 − Σ).
  EX  — то же, но все ноги на одной бирже (внутрибиржевая вилка) — для статистики.
  BKBK — букмекер × букмекер (когда подключён второй бук).
"""

from dataclasses import dataclass, field
from odds_common import eff_back


@dataclass
class Arb:
    kind: str
    mkey: tuple
    sel: str                 # исход (BL) или "*" (BB/EX)
    sources: tuple           # (book, exch) или набор источников
    edge: float              # доля (0.012 = 1.2 %)
    cap: float               # макс. суммарная ставка/ставка в буке, у.е.
    legs: dict = field(default_factory=dict)   # sel -> {src, odds, size, role}
    profit_at_cap: float = 0.0
    tied: float = 0.0        # капитал, замороженный на сделке при cap (ставки + обязательства)

    def key(self):
        return f"{self.kind}|{self.mkey[0]}|{self.mkey[1]}|{self.sel}|{'x'.join(self.sources)}"


def _norm_mkey(mkey):
    return (mkey[0], None if mkey[1] is None else round(float(mkey[1]), 2))


def back_lay(book, exch, mkey, sel, qb, qe, stake_cap):
    """BL-вилка между котировкой бука qb и биржи qe по исходу sel."""
    if not qb.back or not qe.lay or qe.lay <= 1.0:
        return None
    f = exch.fee
    kb, kl = qb.back, qe.lay
    edge = kb * (1.0 - f) / (kl - f) - 1.0
    if edge <= -0.05:
        return None
    # ёмкость: S ≤ book_max, S ≤ lay_size·(Kl−f)/Kb, S ≤ stake_cap
    caps = [stake_cap]
    if qb.back_size:
        caps.append(qb.back_size)
    if qe.lay_size:
        caps.append(qe.lay_size * (kl - f) / kb)
    S = max(0.0, min(caps))
    L = S * kb / (kl - f)
    return Arb(kind="BL", mkey=mkey, sel=sel, sources=(book.src, exch.src), edge=edge, cap=S,
               legs={"book": {"src": book.src, "odds": round(kb, 4), "size": round(qb.back_size, 1), "role": "back", "stake": round(S, 2)},
                     "exch": {"src": exch.src, "odds": round(kl, 4), "size": round(qe.lay_size, 1), "role": "lay", "stake": round(L, 2),
                              "liability": round(L * (kl - 1), 2)}},
               profit_at_cap=S * edge, tied=S + L * (kl - 1))


def cross_back(fixtures, mkey, stake_cap, same_only=None):
    """BB/EX по рынку mkey среди fixtures (список Fixture с этим рынком).
    same_only: если задан src — только ноги этого источника (EX)."""
    sels = None
    for f in fixtures:
        mk = f.markets.get(mkey)
        if not mk:
            continue
        s = set(mk.sels)
        sels = s if sels is None else (sels | s)
    if not sels:
        return None
    n_way = 3 if mkey[0] == "1x2" else 2
    if len(sels) < n_way:
        return None
    best = {}
    for sel in sels:
        cand = []
        for f in fixtures:
            if same_only and f.src != same_only:
                continue
            mk = f.markets.get(mkey)
            if not mk or sel not in mk.sels:
                continue
            q = mk.sels[sel]
            if not q.back or q.back <= 1.0:
                continue
            k = eff_back(q.back, f.fee) if f.is_exchange else q.back
            cand.append((k, f.src, q.back, q.back_size))
        if not cand:
            return None
        best[sel] = max(cand, key=lambda x: x[0])
    inv = sum(1.0 / k for k, _, _, _ in best.values())
    edge = 1.0 - inv
    if edge <= -0.05:
        return None
    srcs = {b[1] for b in best.values()}
    if not same_only and len(srcs) < 2:
        return None
    kind = "EX" if same_only else "BB"
    # X = min(cap_i·K_i): cap_i — ёмкость ноги (ставка), K_i — эффективный кэф
    X = None
    for sel, (k, src, raw, size) in best.items():
        cap_i = min(stake_cap, size) if size else stake_cap
        x = cap_i * k
        X = x if X is None else min(X, x)
    stakes = {sel: X / k for sel, (k, _, _, _) in best.items()}
    total = sum(stakes.values())
    return Arb(kind=kind, mkey=mkey, sel="*", sources=tuple(sorted(srcs)), edge=edge, cap=total,
               legs={sel: {"src": src, "odds": round(raw, 4), "eff": round(k, 4), "size": round(size, 1),
                           "role": "back", "stake": round(stakes[sel], 2)} for sel, (k, src, raw, size) in best.items()},
               profit_at_cap=X * edge, tied=total)


SUSPECT_EDGE = 0.30   # маржа выше — почти наверняка устаревшая котировка одной из сторон; не пишем как вилку


def find_arbs(group, stake_cap=100.0, min_record=-0.02):
    """group: {"base": Fixture, src: Fixture, ...} — одна и та же игра в разных источниках.
    Возвращает список Arb с edge > min_record (пишем и «почти вилки» для распределения)."""
    fixtures = [v for k, v in group.items() if k not in ("scores",) and hasattr(v, "markets")]
    # уникальные по src
    seen = {}
    for f in fixtures:
        seen[f.src] = f
    fixtures = list(seen.values())
    books = [f for f in fixtures if not f.is_exchange]
    exchs = [f for f in fixtures if f.is_exchange]
    out = []
    mkeys = set()
    for f in fixtures:
        for mkey in f.markets:
            mkeys.add(_norm_mkey(mkey))

    def get_mk(f, mkey):
        for k, v in f.markets.items():
            if _norm_mkey(k) == mkey:
                return v
        return None

    for mkey in mkeys:
        # BL: бэк (бук — сырой кэф; биржа — эффективный после комиссии) × lay на другой бирже × каждый исход
        for b in fixtures:
            mb = get_mk(b, mkey)
            if not mb:
                continue
            for e in exchs:
                if e.src == b.src:
                    continue
                me = get_mk(e, mkey)
                if not me:
                    continue
                for sel, qb in mb.sels.items():
                    qe = me.sels.get(sel)
                    if not qe or not qb.back:
                        continue
                    if b.is_exchange:
                        qb2 = type(qb)(back=eff_back(qb.back, b.fee), back_size=qb.back_size)
                    else:
                        qb2 = qb
                    a = back_lay(b, e, mkey, sel, qb2, qe, stake_cap)
                    if a and min_record < a.edge < SUSPECT_EDGE:
                        if b.is_exchange:
                            a.kind = "BLX"   # биржа(бэк) × биржа(lay)
                            a.legs["book"]["raw_odds"] = round(qb.back, 4)
                        out.append(a)
        # BB: все источники, у которых есть рынок
        have = [f for f in fixtures if get_mk(f, mkey)]
        if len(have) >= 2:
            tmp = []
            for f in have:
                # временно приведём ключи к нормализованным
                g = type(f)(src=f.src, ext_id=f.ext_id, home=f.home, away=f.away, league=f.league,
                            start_ts=f.start_ts, is_live=f.is_live, is_exchange=f.is_exchange, fee=f.fee,
                            markets={mkey: get_mk(f, mkey)})
                tmp.append(g)
            a = cross_back(tmp, mkey, stake_cap)
            if a and min_record < a.edge < SUSPECT_EDGE:
                out.append(a)
            for e in exchs:
                ge = [g for g in tmp if g.src == e.src]
                if ge:
                    a = cross_back(ge, mkey, stake_cap, same_only=e.src)
                    if a and min_record < a.edge < SUSPECT_EDGE:
                        out.append(a)
    return out


if __name__ == "__main__":
    from odds_common import Fixture, Market, Q
    b = Fixture(src="pinnacle", ext_id="1", home="A", away="B", league="L", start_ts=0,
                markets={("1x2", None): Market(sels={"home": Q(back=2.2, back_size=200), "draw": Q(back=3.4, back_size=200), "away": Q(back=3.2, back_size=200)})})
    e = Fixture(src="sx", ext_id="2", home="A", away="B", league="L", start_ts=0, is_exchange=True, fee=0.01,
                markets={("1x2", None): Market(sels={"home": Q(back=2.1, back_size=100, lay=2.15, lay_size=150),
                                                     "draw": Q(back=3.9, back_size=80, lay=4.1, lay_size=60),
                                                     "away": Q(back=3.6, back_size=90, lay=3.8, lay_size=70)})})
    for a in find_arbs({"base": b, "sx": e}, stake_cap=100):
        print(a.kind, a.mkey, a.sel, a.sources, f"edge={a.edge*100:.2f}% cap={a.cap:.1f} profit={a.profit_at_cap:.2f} tied={a.tied:.1f}", a.legs)
    # BL home: Kb=2.2, Kl=2.15, f=0.01 → edge = 2.2·0.99/2.14 − 1 = 1.78 %
    # BB: best home 2.2 (pin), draw eff 1+2.9·0.99=3.871, away eff 1+2.6·0.99=3.574 → Σ=0.4545+0.2583+0.2798=0.9926 → 0.74 %

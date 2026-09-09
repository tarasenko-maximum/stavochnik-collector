# -*- coding: utf-8 -*-
"""
Отчёт по live-замеру: частота вилок, маржа, время жизни, ёмкость, разрезы
(вид вилки / рынок / источники / live vs пре-матч), виртуальный P&L при задержке 0/30/60 с.

Запуск: python report_live.py [--since UNIX|YYYY-MM-DD] [--md path]
"""

import argparse
import json
import statistics
import time
from datetime import datetime, timezone, timedelta

import live_db as ldb

BELGRADE = timezone(timedelta(hours=2))


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, BELGRADE).strftime("%d.%m %H:%M")


def pct(x):
    return f"{x*100:.2f}%"


def q(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def build(since):
    con = ldb.connect()
    L = []
    polls = q(con, "SELECT COUNT(*), MIN(ts), MAX(ts), AVG(dur_s), SUM(n_groups), AVG(n_groups), AVG(n_live) FROM polls WHERE ts>=?", (since,))[0]
    if not polls[0]:
        return "Нет опросов за период."
    n_polls, t_min, t_max, avg_dur, _, avg_groups, avg_live = polls
    hours = max(1e-9, (t_max - t_min) / 3600.0) if n_polls > 1 else avg_dur / 3600.0
    L.append(f"# Live-замер «Ставочник» — отчёт {datetime.now(BELGRADE).strftime('%d.%m.%Y %H:%M')} (Белград)")
    L.append("")
    L.append(f"Опросов: **{n_polls}** с {fmt_ts(t_min)} по {fmt_ts(t_max)} ({hours:.1f} ч), средняя длительность опроса {avg_dur:.0f} с; "
             f"матчей в опросе в среднем {avg_groups:.0f}, из них live {avg_live:.0f}.")
    src_counts = q(con, "SELECT n_src FROM polls WHERE ts>=? ORDER BY id DESC LIMIT 1", (since,))[0][0]
    L.append(f"Источники (последний опрос): `{src_counts}`")
    errs = 0
    skews = []
    for (e,) in q(con, "SELECT errors FROM polls WHERE ts>=?", (since,)):
        try:
            d = json.loads(e or "{}")
        except Exception:
            d = {}
        skews.append(float(d.pop("skew_s", 0) or 0))
        if d:
            errs += 1
    L.append(f"Опросов с ошибками источников: {errs}; рассинхрон снимков источников: медиана {statistics.median(skews) if skews else 0:.0f} с, "
             f"макс {max(skews) if skews else 0:.0f} с (в live вилка засчитывается при рассинхроне ≤ 20 с).")
    L.append("")

    # Эпизоды (положительные вилки)
    eps = q(con, """SELECT e.id,e.gkey,e.kind,e.mtype,e.line,e.sel,e.sources,e.is_live,e.first_ts,e.last_ts,e.n_obs,
                    e.first_edge,e.max_edge,e.max_cap,e.min_cap,g.home,g.away,g.league
                    FROM episodes e JOIN groups g ON g.gkey=e.gkey WHERE e.first_ts>=? ORDER BY e.max_edge DESC""", (since,))
    L.append("## 1. Вилки с положительной маржой (эпизоды)")
    L.append("")
    L.append(f"Эпизод = непрерывная серия опросов, где вилка (edge > 0) держалась. Всего эпизодов: **{len(eps)}**; "
             f"в пересчёте на сутки: **{len(eps)/hours*24:.1f}/день**.")
    if eps:
        life = [e[9] - e[8] for e in eps]
        edges = [e[12] for e in eps]
        L.append("")
        L.append("| Порог | Эпизодов | /день | Медиана max-маржи | Медиана жизни, с | Живут ≥30 с | Живут ≥60 с |")
        L.append("|---|---|---|---|---|---|---|")
        for name, cond in (("edge>0", lambda e: True), ("edge≥0.5%", lambda e: e[12] >= 0.005),
                           ("edge≥1%", lambda e: e[12] >= 0.01), ("edge≥1% и cap≥50", lambda e: e[12] >= 0.01 and e[13] >= 50)):
            sub = [e for e in eps if cond(e)]
            if not sub:
                L.append(f"| {name} | 0 | 0 | – | – | – | – |")
                continue
            lf = [e[9] - e[8] for e in sub]
            L.append(f"| {name} | {len(sub)} | {len(sub)/hours*24:.1f} | {pct(statistics.median([e[12] for e in sub]))} | "
                     f"{statistics.median(lf):.0f} | {sum(1 for x in lf if x>=30)} | {sum(1 for x in lf if x>=60)} |")
        L.append("")
        L.append("Примечание: время жизни измеряется с шагом опроса (~30 с); эпизод из одного опроса имеет жизнь 0 с, "
                 "то есть «от 0 до одного шага».")
        # разрезы
        for title, idx in (("вид вилки", 2), ("рынок", 3), ("источники", 6), ("live/пре-матч", 7)):
            agg = {}
            for e in eps:
                k = e[idx]
                if idx == 7:
                    k = "live" if k else "пре-матч"
                a = agg.setdefault(k, [0, [], []])
                a[0] += 1; a[1].append(e[12]); a[2].append(e[9] - e[8])
            L.append("")
            L.append(f"**По {title}:**")
            L.append("")
            L.append("| Значение | Эпизодов | Медиана max-маржи | Макс. маржа | Медиана жизни, с |")
            L.append("|---|---|---|---|---|")
            for k, (n, ed, lf) in sorted(agg.items(), key=lambda x: -x[1][0]):
                L.append(f"| {k} | {n} | {pct(statistics.median(ed))} | {pct(max(ed))} | {statistics.median(lf):.0f} |")
        L.append("")
        L.append("**Топ-15 эпизодов по марже:**")
        L.append("")
        L.append("| Матч | Лига | Рынок | Исход | Вид | Источники | Live | Начало | Жизнь, с | Маржа max | Ёмкость max |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for e in eps[:15]:
            L.append(f"| {e[15]} – {e[16]} | {e[17]} | {e[3]} {e[4] if e[4] is not None else ''} | {e[5]} | {e[2]} | {e[6]} | "
                     f"{'да' if e[7] else ''} | {fmt_ts(e[8])} | {e[9]-e[8]} | {pct(e[12])} | {e[13]:.0f} |")
    L.append("")

    # Виртуальные сделки
    L.append("## 2. Виртуальные сделки (эмуляция исполнения)")
    L.append("")
    L.append("Сделка «исполняется» на текущих ценах, когда вилка прожила ≥ задержки и всё ещё жива. "
             "Ставка ≤ 100 у.е. на плечо в буке, ограничена лимитом бука и объёмом стакана биржи. "
             "Прибыль гарантирована при любом исходе (формулы BL/BB), комиссия биржи учтена.")
    L.append("")
    L.append("| Задержка | Сделок | Сделок edge≥1% | Σ ставок | Σ прибыль | Средняя маржа | Σ замороженного капитала |")
    L.append("|---|---|---|---|---|---|---|")
    for d in (0, 30, 60):
        r = q(con, "SELECT COUNT(*), SUM(CASE WHEN edge>=0.01 THEN 1 ELSE 0 END), COALESCE(SUM(stake),0), COALESCE(SUM(profit),0), COALESCE(AVG(edge),0), COALESCE(SUM(tied),0) FROM vtrades WHERE delay_s=? AND ts>=?", (d, since))[0]
        L.append(f"| {d} с | {r[0]} | {r[1] or 0} | {r[2]:.0f} | {r[3]:.2f} | {pct(r[4])} | {r[5]:.0f} |")
    L.append("")
    r = q(con, "SELECT kind, delay_s, COUNT(*), SUM(profit) FROM vtrades WHERE ts>=? GROUP BY kind, delay_s ORDER BY kind, delay_s", (since,))
    if r:
        L.append("| Вид | Задержка | Сделок | Σ прибыль |")
        L.append("|---|---|---|---|")
        for k, d, n, p in r:
            L.append(f"| {k} | {d} с | {n} | {p:.2f} |")
    L.append("")

    # Распределение маржи всех записей (включая «почти вилки»)
    L.append("## 3. Распределение маржи по всем наблюдениям (включая отрицательные)")
    L.append("")
    rows = q(con, "SELECT kind, edge FROM arbs WHERE ts>=?", (since,))
    if rows:
        bins = [(-0.02, -0.01), (-0.01, -0.005), (-0.005, 0.0), (0.0, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 9)]
        kinds = sorted({k for k, _ in rows})
        L.append("| Диапазон маржи | " + " | ".join(kinds) + " |")
        L.append("|---|" + "---|" * len(kinds))
        for lo, hi in bins:
            cells = []
            for k in kinds:
                cells.append(str(sum(1 for kk, e in rows if kk == k and lo <= e < hi)))
            L.append(f"| [{lo*100:+.1f}%, {hi*100:+.1f}%) | " + " | ".join(cells) + " |")
    L.append("")
    # средний спред бук vs биржа (для понимания «почему нет вилок»)
    L.append("## 4. Средний разрыв бук ↔ биржа по 1X2 (последние 200 тиков)")
    L.append("")
    ticks = q(con, "SELECT quotes FROM ticks WHERE ts>=? ORDER BY id DESC LIMIT 200", (since,))
    ratios = {}
    for (qs,) in ticks:
        d = json.loads(qs)
        pin = d.get("pinnacle", {}).get("1x2|None")
        if not pin:
            continue
        for src in ("sx", "smarkets"):
            m = d.get(src, {}).get("1x2|None")
            if not m:
                continue
            for sel in ("home", "draw", "away"):
                a, b = pin.get(sel), m.get(sel)
                if a and b and a[0] and b[2]:
                    ratios.setdefault(src, []).append(a[0] / b[2] - 1)   # back бука / lay биржи − 1
    for src, r in ratios.items():
        L.append(f"- {src}: медиана (K_back Pinnacle / K_lay биржи − 1) = {pct(statistics.median(r))}, "
                 f"90-й перцентиль {pct(sorted(r)[int(len(r)*0.9)])}, n={len(r)}. Вилка BL возникает, когда это > комиссии.")
    con.close()
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    ap.add_argument("--md", default=None)
    a = ap.parse_args()
    if a.since is None:
        since = int(datetime.now(BELGRADE).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    elif a.since.isdigit():
        since = int(a.since)
    else:
        since = int(datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=BELGRADE).timestamp())
    text = build(since)
    if a.md:
        with open(a.md, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("записано:", a.md)
    else:
        print(text)

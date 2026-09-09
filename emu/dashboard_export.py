# -*- coding: utf-8 -*-
"""
Экспорт состояния live-замера в JSON для дашборда (state/dashboard.json) + публикация на сервер.

Содержимое:
  meta        — время, номер опроса, счётчики источников, длительность
  live        — матчи в игре и ближайшие: котировки бука и бирж по 1X2 (+ главный тотал)
  arbs_now    — вилки, найденные в последнем опросе (плечи, маржа, ёмкость)
  episodes    — открытые и последние закрытые эпизоды (время жизни)
  trades      — виртуальные сделки (задержка 60 с — «реалистичный портфель»; 0 с — верхняя граница)
  ledger      — условные балансы: бук / биржи / заморожено / гарантированная прибыль (перелив)
  stats       — итоги дня: эпизоды/день, медианы, доля live
"""

import json
import os
import statistics
import subprocess
import time
from datetime import datetime, timezone, timedelta

import config
import live_db as ldb

OUT = os.environ.get("DASH_OUT") or os.path.join(config.STATE_DIR, "dashboard.json")
PUSH = os.environ.get("DASH_PUSH", "1") != "0"   # на сервере файл пишется на месте, scp не нужен
REMOTE = os.environ.get("DASH_REMOTE", "root@178.104.7.110:/var/www/stavochnik/dashboard.json")
BELGRADE = timezone(timedelta(hours=2))
START_BOOK = 3000.0      # условный стартовый банкролл на буке
START_EXCH = 5000.0      # условный стартовый банкролл на бирже(ах)


def _q(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def build(day_start=None):
    con = ldb.connect()
    now = int(time.time())
    if day_start is None:
        day_start = int(datetime.now(BELGRADE).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    p = _q(con, "SELECT id,ts,dur_s,n_src,n_groups,n_live,n_arbs,n_arbs_pos,errors FROM polls ORDER BY id DESC LIMIT 1")
    if not p:
        return {"meta": {"empty": True}}
    p = p[0]
    poll_id = p[0]
    meta = {"poll_id": poll_id, "ts": p[1], "time": datetime.fromtimestamp(p[1], BELGRADE).strftime("%H:%M:%S"),
            "date": datetime.fromtimestamp(p[1], BELGRADE).strftime("%d.%m.%Y"), "dur_s": p[2],
            "sources": json.loads(p[3] or "{}"), "groups": p[4], "live": p[5], "arbs": p[6], "arbs_pos": p[7],
            "errors": json.loads(p[8] or "{}"), "exported": now,
            "polls_today": _q(con, "SELECT COUNT(*) FROM polls WHERE ts>=?", (day_start,))[0][0]}
    # live-матчи и ближайшие: последние тики этого опроса
    live = []
    for gkey, is_live, quotes, home, away, league, start_ts in _q(con, """
            SELECT t.gkey,t.is_live,t.quotes,g.home,g.away,g.league,g.start_ts FROM ticks t JOIN groups g ON g.gkey=t.gkey
            WHERE t.poll_id=? ORDER BY t.is_live DESC, g.start_ts ASC""", (poll_id,)):
        q = json.loads(quotes)
        srcs = list(q.keys())
        if len(srcs) < 2:
            continue
        row = {"gkey": gkey, "home": home, "away": away, "league": league, "live": bool(is_live),
               "start": datetime.fromtimestamp(start_ts, BELGRADE).strftime("%H:%M"),
               "q": {}}
        for src, mk in q.items():
            m = mk.get("1x2|None")
            if m:
                row["q"][src] = {sel: m.get(sel) for sel in ("home", "draw", "away")}
            tot = [k for k in mk if k.startswith("total|")]
            if tot:
                k = sorted(tot, key=lambda x: abs(float(x.split("|")[1]) - 2.5))[0]
                row["q"].setdefault(src, {})["total"] = {"line": k.split("|")[1], **mk[k]}
        live.append(row)
    # вилки последнего опроса
    arbs_now = []
    for r in _q(con, """SELECT a.gkey,g.home,g.away,g.league,a.kind,a.mtype,a.line,a.sel,a.sources,a.edge,a.cap,a.profit_at_cap,a.tied,a.legs,a.is_live
                        FROM arbs a JOIN groups g ON g.gkey=a.gkey WHERE a.poll_id=? AND a.edge>0 ORDER BY a.edge DESC LIMIT 40""", (poll_id,)):
        arbs_now.append({"gkey": r[0], "home": r[1], "away": r[2], "league": r[3], "kind": r[4], "mtype": r[5], "line": r[6],
                         "sel": r[7], "sources": r[8], "edge": r[9], "cap": r[10], "profit": r[11], "tied": r[12],
                         "legs": json.loads(r[13]), "live": bool(r[14])})
    # эпизоды
    episodes = []
    for r in _q(con, """SELECT e.id,g.home,g.away,g.league,e.kind,e.mtype,e.line,e.sel,e.sources,e.is_live,e.first_ts,e.last_ts,e.n_obs,
                        e.max_edge,e.last_edge,e.max_cap,e.closed FROM episodes e JOIN groups g ON g.gkey=e.gkey
                        WHERE e.first_ts>=? ORDER BY e.closed ASC, e.last_ts DESC LIMIT 60""", (day_start,)):
        episodes.append({"id": r[0], "home": r[1], "away": r[2], "league": r[3], "kind": r[4], "mtype": r[5], "line": r[6],
                         "sel": r[7], "sources": r[8], "live": bool(r[9]),
                         "first": datetime.fromtimestamp(r[10], BELGRADE).strftime("%H:%M:%S"),
                         "last": datetime.fromtimestamp(r[11], BELGRADE).strftime("%H:%M:%S"),
                         "life_s": r[11] - r[10], "n_obs": r[12], "max_edge": r[13], "last_edge": r[14], "max_cap": r[15],
                         "open": not r[16]})
    # сделки (60 с — реалистичный портфель)
    trades = {}
    for d in (0, 30, 60):
        rows = _q(con, """SELECT v.id,g.home,g.away,g.league,v.kind,v.mtype,v.sel,v.sources,v.is_live,v.ts,v.edge,v.stake,v.profit,v.tied,v.legs
                          FROM vtrades v JOIN groups g ON g.gkey=v.gkey WHERE v.delay_s=? AND v.ts>=? ORDER BY v.ts DESC LIMIT 50""", (d, day_start))
        trades[str(d)] = [{"id": r[0], "home": r[1], "away": r[2], "league": r[3], "kind": r[4], "mtype": r[5], "sel": r[6],
                           "sources": r[7], "live": bool(r[8]), "time": datetime.fromtimestamp(r[9], BELGRADE).strftime("%H:%M:%S"),
                           "edge": r[10], "stake": r[11], "profit": r[12], "tied": r[13], "legs": json.loads(r[14])} for r in rows]
        agg = _q(con, "SELECT COUNT(*), COALESCE(SUM(stake),0), COALESCE(SUM(profit),0), COALESCE(SUM(tied),0), SUM(CASE WHEN edge>=0.01 THEN 1 ELSE 0 END) FROM vtrades WHERE delay_s=? AND ts>=?", (d, day_start))[0]
        trades[f"agg{d}"] = {"n": agg[0], "stake": agg[1], "profit": agg[2], "tied": agg[3], "n_ge1": agg[4] or 0}
    # леджер (перелив) по портфелю 60 с: ставки в буке, обязательства на биржах, гарантированная прибыль
    rows = _q(con, "SELECT kind, legs, profit, stake, tied FROM vtrades WHERE delay_s=60 AND ts>=?", (day_start,))
    book_staked = exch_liab = exch_staked = 0.0
    per_src = {}
    for kind, legs, profit, stake, tied in rows:
        L = json.loads(legs)
        if kind == "BL":
            book_staked += L["book"]["stake"]
            exch_liab += L["exch"]["liability"]
            per_src[L["exch"]["src"]] = per_src.get(L["exch"]["src"], 0.0) + L["exch"]["liability"]
        else:
            for sel, leg in L.items():
                if leg["src"] in ("pinnacle", "cloudbet"):
                    book_staked += leg["stake"]
                else:
                    exch_staked += leg["stake"]
                    per_src[leg["src"]] = per_src.get(leg["src"], 0.0) + leg["stake"]
    guaranteed = sum(r[2] for r in rows)
    ledger = {"start_book": START_BOOK, "start_exch": START_EXCH,
              "book_locked": round(book_staked, 2), "exch_locked": round(exch_liab + exch_staked, 2),
              "book_free": round(START_BOOK - book_staked, 2), "exch_free": round(START_EXCH - exch_liab - exch_staked, 2),
              "guaranteed": round(guaranteed, 2), "per_exchange": {k: round(v, 2) for k, v in per_src.items()},
              "n_trades": len(rows)}
    # статистика дня
    eps = _q(con, "SELECT max_edge, last_ts-first_ts, is_live, max_cap, sources, mtype FROM episodes WHERE first_ts>=?", (day_start,))
    first_poll = _q(con, "SELECT MIN(ts), MAX(ts) FROM polls WHERE ts>=?", (day_start,))[0]
    hours = max(0.25, ((first_poll[1] or now) - (first_poll[0] or now)) / 3600.0)
    ge1 = [e for e in eps if e[0] >= 0.01]
    stats = {"hours": round(hours, 2), "episodes": len(eps), "episodes_ge1": len(ge1),
             "per_day": round(len(eps) / hours * 24, 1), "per_day_ge1": round(len(ge1) / hours * 24, 1),
             "median_life": statistics.median([e[1] for e in eps]) if eps else 0,
             "life_ge30_ge1": sum(1 for e in ge1 if e[1] >= 30), "life_ge60_ge1": sum(1 for e in ge1 if e[1] >= 60),
             "live_share": round(sum(1 for e in eps if e[2]) / len(eps), 2) if eps else 0,
             "by_source": {}, "by_market": {}}
    for e in eps:
        stats["by_source"][e[4]] = stats["by_source"].get(e[4], 0) + 1
        stats["by_market"][e[5]] = stats["by_market"].get(e[5], 0) + 1
    con.close()
    return {"meta": meta, "live": live, "arbs_now": arbs_now, "episodes": episodes, "trades": trades, "ledger": ledger, "stats": stats}


def write_and_push(push=True):
    data = build()
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, OUT)
    if push and PUSH and REMOTE:
        # Windows OpenSSH scp падает на правах ~/.ssh/config; scp из Git for Windows работает
        scp = next((p for p in (r"C:\Program Files\Git\usr\bin\scp.exe",) if os.path.exists(p)), "scp")
        try:
            subprocess.run([scp, "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", OUT, REMOTE],
                           timeout=40, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("[dashboard] scp:", e)
    return data


if __name__ == "__main__":
    d = write_and_push(push=False)
    print(json.dumps(d.get("meta"), ensure_ascii=False), "| live:", len(d.get("live", [])), "arbs_now:", len(d.get("arbs_now", [])),
          "episodes:", len(d.get("episodes", [])), "| ledger:", d.get("ledger"))

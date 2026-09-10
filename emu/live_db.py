# -*- coding: utf-8 -*-
"""SQLite для live-контура: опросы, матчинг, тики котировок, вилки, эпизоды, виртуальные сделки."""

import json
import os
import sqlite3

import config

DB_PATH = os.path.join(config.STATE_DIR, "live.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    dur_s REAL,
    n_src TEXT,                 -- json {src: n_fixtures}
    n_groups INTEGER,
    n_live INTEGER,
    n_arbs INTEGER,
    n_arbs_pos INTEGER,
    errors TEXT
);
CREATE TABLE IF NOT EXISTS groups (
    gkey TEXT PRIMARY KEY,      -- base fixture key
    home TEXT, away TEXT, league TEXT, start_ts INTEGER,
    members TEXT,               -- json {src: {ext_id, home, away, score}}
    first_ts INTEGER, last_ts INTEGER
);
CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    gkey TEXT NOT NULL,
    is_live INTEGER,
    quotes TEXT NOT NULL        -- json {src: {mkey: {sel: [back, back_size, lay, lay_size]}}}
);
CREATE INDEX IF NOT EXISTS idx_ticks_g ON ticks(gkey, ts);
CREATE TABLE IF NOT EXISTS arbs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    gkey TEXT NOT NULL,
    akey TEXT NOT NULL,         -- kind|mtype|line|sel|srcs
    kind TEXT, mtype TEXT, line REAL, sel TEXT, sources TEXT,
    is_live INTEGER,
    edge REAL NOT NULL,         -- доля
    cap REAL, profit_at_cap REAL, tied REAL,
    legs TEXT
);
CREATE INDEX IF NOT EXISTS idx_arbs_g ON arbs(gkey, akey, ts);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gkey TEXT NOT NULL, akey TEXT NOT NULL,
    kind TEXT, mtype TEXT, line REAL, sel TEXT, sources TEXT,
    is_live INTEGER,
    first_ts INTEGER NOT NULL, last_ts INTEGER NOT NULL,
    n_obs INTEGER NOT NULL,
    first_edge REAL, max_edge REAL, min_edge REAL, last_edge REAL,
    max_cap REAL, min_cap REAL,
    closed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ep_open ON episodes(closed, gkey, akey);
CREATE TABLE IF NOT EXISTS vtrades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL,
    delay_s INTEGER NOT NULL,   -- 0 / 30 / 60: сколько секунд вилка уже жила при «исполнении»
    ts INTEGER NOT NULL,
    gkey TEXT, akey TEXT, kind TEXT, mtype TEXT, sel TEXT, sources TEXT, is_live INTEGER,
    edge REAL, stake REAL, profit REAL, tied REAL, legs TEXT,
    UNIQUE(episode_id, delay_s)
);
"""


def connect():
    os.makedirs(config.STATE_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def upsert_group(con, gkey, g, ts):
    base = g["base"]
    members = {}
    for src, f in g.items():
        if src in ("base", "scores"):
            continue
        members[src] = {"ext_id": f.ext_id, "home": f.home, "away": f.away, "score": g["scores"].get(src)}
    members[base.src] = {"ext_id": base.ext_id, "home": base.home, "away": base.away, "score": 1.0}
    con.execute("""INSERT INTO groups(gkey,home,away,league,start_ts,members,first_ts,last_ts)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(gkey) DO UPDATE SET members=excluded.members, last_ts=excluded.last_ts""",
                (gkey, base.home, base.away, base.league, base.start_ts, json.dumps(members, ensure_ascii=False), ts, ts))


def insert_poll(con, ts, dur, n_src, n_groups, n_live, n_arbs, n_pos, errors):
    cur = con.execute("INSERT INTO polls(ts,dur_s,n_src,n_groups,n_live,n_arbs,n_arbs_pos,errors) VALUES(?,?,?,?,?,?,?,?)",
                      (ts, dur, json.dumps(n_src), n_groups, n_live, n_arbs, n_pos, json.dumps(errors, ensure_ascii=False)))
    return cur.lastrowid


def insert_tick(con, poll_id, ts, gkey, is_live, quotes):
    con.execute("INSERT INTO ticks(poll_id,ts,gkey,is_live,quotes) VALUES(?,?,?,?,?)",
                (poll_id, ts, gkey, int(is_live), json.dumps(quotes)))


def insert_arb(con, poll_id, ts, gkey, a, is_live):
    con.execute("""INSERT INTO arbs(poll_id,ts,gkey,akey,kind,mtype,line,sel,sources,is_live,edge,cap,profit_at_cap,tied,legs)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (poll_id, ts, gkey, a.key(), a.kind, a.mkey[0], a.mkey[1], a.sel, "x".join(a.sources), int(is_live),
                 a.edge, a.cap, a.profit_at_cap, a.tied, json.dumps(a.legs, ensure_ascii=False)))


def open_episodes(con):
    rows = con.execute("SELECT id,gkey,akey,first_ts,last_ts,n_obs,max_edge,min_edge,max_cap,min_cap FROM episodes WHERE closed=0").fetchall()
    return {(r[1], r[2]): {"id": r[0], "first_ts": r[3], "last_ts": r[4], "n_obs": r[5], "max_edge": r[6],
                           "min_edge": r[7], "max_cap": r[8], "min_cap": r[9]} for r in rows}


def touch_episode(con, ep, gkey, a, ts, is_live):
    """Открыть/обновить эпизод для положительной вилки. Возвращает (episode_id, age_s)."""
    if ep is None:
        cur = con.execute("""INSERT INTO episodes(gkey,akey,kind,mtype,line,sel,sources,is_live,first_ts,last_ts,n_obs,
                             first_edge,max_edge,min_edge,last_edge,max_cap,min_cap) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)""",
                          (gkey, a.key(), a.kind, a.mkey[0], a.mkey[1], a.sel, "x".join(a.sources), int(is_live), ts, ts,
                           a.edge, a.edge, a.edge, a.edge, a.cap, a.cap))
        return cur.lastrowid, 0
    con.execute("""UPDATE episodes SET last_ts=?, n_obs=n_obs+1, max_edge=MAX(max_edge,?), min_edge=MIN(min_edge,?),
                   last_edge=?, max_cap=MAX(max_cap,?), min_cap=MIN(min_cap,?), is_live=MAX(is_live,?) WHERE id=?""",
                (ts, a.edge, a.edge, a.edge, a.cap, a.cap, int(is_live), ep["id"]))
    return ep["id"], ts - ep["first_ts"]


def close_episodes(con, ids):
    if ids:
        con.executemany("UPDATE episodes SET closed=1 WHERE id=?", [(i,) for i in ids])


def already_traded_recently(con, gkey, akey, before_ts, window_hours=24):
    """Была ли УЖЕ виртуальная сделка по этой ровно вилке (gkey+akey) в окне
    [before_ts−window_hours, before_ts) — независимо от episode_id. `before_ts` — момент начала
    ТЕКУЩЕГО эпизода (для новых эпизодов это ts опроса, для продолжающихся — их собственный
    first_ts): так проверка не видит сделки, которые сам этот эпизод уже вставил на delay 0/30/60
    (у них ts ≥ first_ts эпизода), но видит сделки от ПРЕДЫДУЩЕГО, уже закрытого эпизода той же
    вилки.

    Без этой проверки одна и та же возможность, которая на секунду пропадает из опроса и тут же
    появляется снова (обрыв данных источника, а не реальное закрытие вилки), открывает НОВЫЙ
    episode_id и эмулятор «переставляется» заново — иногда десятки раз в день на одном и том же
    матче, раздувая «гарантированную прибыль» далеко за пределы того, что реальный трейдер получил
    бы, войдя в позицию один раз. Обнаружено 10.09.2026: 51 сделка за день, из них ~40 — повторный
    вход в один и тот же Fenerbahce–Roma."""
    since_ts = before_ts - window_hours * 3600
    r = con.execute("SELECT 1 FROM vtrades WHERE gkey=? AND akey=? AND ts>=? AND ts<? LIMIT 1",
                    (gkey, akey, since_ts, before_ts)).fetchone()
    return r is not None


def insert_vtrade(con, episode_id, delay, ts, gkey, a, is_live, stake, profit, tied):
    con.execute("""INSERT OR IGNORE INTO vtrades(episode_id,delay_s,ts,gkey,akey,kind,mtype,sel,sources,is_live,edge,stake,profit,tied,legs)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (episode_id, delay, ts, gkey, a.key(), a.kind, a.mkey[0], a.sel, "x".join(a.sources), int(is_live),
                 a.edge, stake, profit, tied, json.dumps(a.legs, ensure_ascii=False)))


def q(sql, args=()):
    con = connect()
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()

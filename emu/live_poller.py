# -*- coding: utf-8 -*-
"""
Live-поллер «Ставочника»: каждые POLL_SEC секунд снимает котировки букмекеров (Pinnacle,
Cloudbet — при ключе) и бирж (SX.bet, Smarkets), матчит фикстуры, ищет вилки BL/BB/EX,
ведёт эпизоды (время жизни) и виртуальные сделки с задержкой исполнения 0/30/60 с.

Запуск:  python live_poller.py [--once] [--hours-ahead N] [--poll SEC]
Останов: Ctrl+C (состояние в state/live.db, эпизоды закрываются при следующем запуске).
"""

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone

import netfix  # noqa: F401  — обход DNS-подмены провайдера для публичных API (см. модуль)
import config
import live_db as ldb
import odds_common as oc
import arb_live
# клиенты подгружаются лениво: на сервере Hetzner нет смысла в Pinnacle/Smarkets (403 с дата-центра),
# и модулей может не быть в развёртывании
pinnacle_client = smarkets_client = None
try:
    import pinnacle_client
except ImportError:
    pass
try:
    import smarkets_client
except ImportError:
    pass
import sx_client

POLL_SEC = 30
STAKE_CAP = 1000.0            # потолок ставки на плечо, у.е. Реальным ограничителем должны быть
                              # лимит бука и объём стакана биржи, а не эта константа: при 100 замер
                              # 06–07.09 занижал ёмкость (Elche–Real Sociedad: стакан SX 4 998 USDC).
DELAYS = (0, 30, 60)          # «исполняем», когда вилка прожила ≥ delay с (и всё ещё жива)
MIN_EDGE_TRADE = 0.0          # виртуальная сделка при edge > 0 (порог ≥1 % считаем в отчёте)
RECORD_MIN_EDGE = -0.02       # пишем и «почти вилки» до −2 % для распределений
SX_REFRESH_MARKETS_SEC = 300  # список рынков SX обновляем раз в 5 мин
SM_REFRESH_EVENTS_SEC = 600   # список событий Smarkets — раз в 10 мин


def log(msg):
    print(datetime.now().strftime("%H:%M:%S"), msg, flush=True)


def keep_awake():
    """Windows: пока процесс жив, система не уходит в сон (экран гасить можно).
    Глобальные настройки питания НЕ меняются — флаг действует только для этого процесса и
    снимается при его завершении. Нужен потому, что замер 06–08.09 рвался на 4–11 ч из-за сна."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ok = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        return bool(ok)
    except Exception as e:
        log(f"keep_awake: {e}")
        return False


class Feeds:
    def __init__(self, hours_back, hours_ahead, sources=("pinnacle", "sx", "smarkets", "cloudbet")):
        self.hours_back, self.hours_ahead = hours_back, hours_ahead
        self.sources = set(sources)
        self.sx_fx, self.sx_ts = {}, 0
        self.sm_cache, self.sm_fx, self.sm_ts = {}, {}, 0
        self.errors = {}

    def window(self, f):
        now = time.time()
        return f.is_live or (now - self.hours_back * 3600 <= f.start_ts <= now + self.hours_ahead * 3600)

    def pinnacle(self):
        if "pinnacle" not in self.sources or pinnacle_client is None:
            return []
        try:
            fx = pinnacle_client.fetch_soccer(primary_only=True)
            return [f for f in fx.values() if self.window(f)]
        except Exception as e:
            self.errors["pinnacle"] = str(e)[:200]
            return []

    def sx(self):
        if "sx" not in self.sources:
            return []
        try:
            if time.time() - self.sx_ts > SX_REFRESH_MARKETS_SEC or not self.sx_fx:
                self.sx_fx = sx_client.fetch_soccer(types=(1, 2, 3))
                self.sx_ts = time.time()
            lst = [f for f in self.sx_fx.values() if self.window(f)]
            now = time.time()
            for f in lst:
                if f.start_ts and f.start_ts < now:
                    f.is_live = True
            n, err = sx_client.refresh_prices(lst, workers=16, only_main=True)
            if err:
                self.errors["sx_snap"] = f"{err}/{n}"
            return lst
        except Exception as e:
            self.errors["sx"] = str(e)[:200]
            return []

    def cloudbet(self):
        if "cloudbet" not in self.sources:
            return []
        try:
            import cloudbet_client
            fx = cloudbet_client.fetch_soccer(self.hours_back, self.hours_ahead)
            return [f for f in fx.values() if self.window(f)]
        except Exception as e:
            self.errors["cloudbet"] = str(e)[:200]
            return []

    def smarkets(self):
        if "smarkets" not in self.sources or smarkets_client is None:
            return []
        try:
            if time.time() - self.sm_ts > SM_REFRESH_EVENTS_SEC or not self.sm_fx:
                self.sm_fx = smarkets_client.fetch_soccer(self.hours_back, self.hours_ahead, cache=self.sm_cache)
                self.sm_ts = time.time()
            else:
                smarkets_client.refresh_quotes(list(self.sm_fx.values()))
            return list(self.sm_fx.values())
        except Exception as e:
            self.errors["smarkets"] = str(e)[:200]
            return []


def quotes_json(g):
    out = {}
    for src, f in g.items():
        if src in ("base", "scores"):
            continue
        d = {}
        for mkey, mk in f.markets.items():
            if mkey[0] in ("total", "ah") and f.src == "sx" and not mk.ext.get("main"):
                continue
            row = {}
            for sel, q in mk.sels.items():
                if q.back or q.lay:
                    row[sel] = [q.back and round(q.back, 4), round(q.back_size, 1), q.lay and round(q.lay, 4), round(q.lay_size, 1)]
            if row:
                d[f"{mkey[0]}|{mkey[1]}"] = row
        if d:
            out[f.src] = d
    b = g["base"]
    d = {}
    for mkey, mk in b.markets.items():
        row = {sel: [q.back and round(q.back, 4), round(q.back_size, 1), None, 0] for sel, q in mk.sels.items() if q.back}
        if row:
            d[f"{mkey[0]}|{mkey[1]}"] = row
    out[b.src] = d
    return out


RETENTION_HOURS = 30     # держим «сегодня» + запас на смену суток; старше — не нужно ни дашборду
                          # (все запросы фильтруют по day_start), ни отчёту
PRUNE_EVERY_POLLS = 80   # раз в ~20 мин при шаге 15 с
MAX_SKEW_S = 20          # макс. разница времени снимков источников, при которой вилка засчитывается
LIVE_EXCLUDE_MTYPES = {"ah"}   # в live у бука и бирж разная база гандикапа (от 0:0 vs от текущего счёта) — не сравниваем
# Проверено 06.09.2026: гостевой API Pinnacle в live НЕ обновляет цены (версии/кэфы заморожены минутами,
# Франкфурт–Аугсбург 1:2 на 90' — away 1.66). В live Pinnacle из расчёта вилок исключаем (тики пишем).
LIVE_STALE_SOURCES = {"pinnacle"}


def prune_old(con, poll_id):
    """Раз в ~20 мин удаляет тики и записи вилок старше RETENTION_HOURS + сжимает файл VACUUM'ом.
    Без этого `ticks` (полные JSON-снимки котировок каждого опроса) растёт неограниченно — на 4
    источниках это ≈6 МБ/час, и выгружаемая коллектором база за сутки перевалила бы за 100+ МБ
    (обнаружено 10.09: за 16.5 ч набежало 99 МБ, из них 57 МБ — один только `ticks`)."""
    if poll_id % PRUNE_EVERY_POLLS != 0:
        return
    cutoff = int(time.time()) - RETENTION_HOURS * 3600
    d1 = con.execute("DELETE FROM ticks WHERE ts<?", (cutoff,)).rowcount
    d2 = con.execute("DELETE FROM arbs WHERE ts<?", (cutoff,)).rowcount
    con.commit()
    if d1 or d2:
        con.execute("VACUUM")
        log(f"обрезка (>{RETENTION_HOURS} ч): ticks -{d1}, arbs -{d2}, база сжата VACUUM")


def run_poll(feeds: Feeds, con):
    t0 = time.time()
    ts = int(t0)
    feeds.errors = {}
    # три источника — параллельно, чтобы снимки были синхронны (в live цены меняются за секунды)
    from concurrent.futures import ThreadPoolExecutor
    done_ts = {}

    def _run(name, fn):
        res = fn()
        done_ts[name] = time.time()
        return res

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_sx = ex.submit(_run, "sx", feeds.sx)
        f_sm = ex.submit(_run, "smarkets", feeds.smarkets)
        f_cb = ex.submit(_run, "cloudbet", feeds.cloudbet)
        sx, sm, cb = f_sx.result(), f_sm.result(), f_cb.result()
    # Pinnacle — быстрый (≈5–10 с), снимаем ПОСЛЕ бирж, чтобы снимки были максимально близки по времени
    pin = _run("pinnacle", feeds.pinnacle)
    feeds.errors["skew_s"] = round(max(done_ts.values()) - min(done_ts.values()), 1) if len(done_ts) > 1 else 0
    n_src = {"pinnacle": len(pin), "sx": len(sx), "smarkets": len(sm), "cloudbet": len(cb)}
    # N-сторонний матчинг (без единого анкора) — см. odds_common.match_all: раньше матч целиком
    # выпадал из сопоставления, если его не было у «опорного» источника (обычно Cloudbet на
    # сервере), даже когда SX/Smarkets по нему давали полные живые котировки.
    groups = oc.match_all({"pinnacle": pin, "sx": sx, "smarkets": sm, "cloudbet": cb})
    open_eps = ldb.open_episodes(con)
    seen_eps = set()
    n_arbs = n_pos = n_live = 0
    poll_id = ldb.insert_poll(con, ts, 0, n_src, len(groups), 0, 0, 0, feeds.errors)
    for g in groups:
        # ключ группы — по именам команд, НЕ по source:id опорной фикстуры: раньше matching был
        # жёстко привязан к одному анкору, и gkey=base.key() был стабилен автоматически. Теперь
        # анкор в match_all может меняться от опроса к опросу (см. odds_common.match_all) — если
        # ключ группы будет зависеть от того, чья фикстура попала в "base" в конкретном опросе,
        # каждая смена анкора будет обрываться серию эпизода и открывать новую «группу» для того
        # же реального матча. Имя команд — единственное, что не меняется между источниками.
        gkey = f"pair:{oc.norm_name(g['base'].home)}|{oc.norm_name(g['base'].away)}"
        is_live = any(f.is_live for k, f in g.items() if k not in ("base", "scores")) or g["base"].is_live
        n_live += int(is_live)
        ldb.upsert_group(con, gkey, g, ts)
        ldb.insert_tick(con, poll_id, ts, gkey, is_live, quotes_json(g))
        if is_live:
            # в live: только синхронные снимки и без гандикапов
            srcs_in = [k for k in g if k not in ("base", "scores")] + [g["base"].src]
            skew = max(done_ts.get(s, 0) for s in srcs_in) - min(done_ts.get(s, 0) for s in srcs_in)
            if skew > MAX_SKEW_S:
                continue
            for f in [v for k, v in g.items() if k not in ("scores",)]:
                for mkey in [k for k in f.markets if k[0] in LIVE_EXCLUDE_MTYPES]:
                    f.markets.pop(mkey, None)
                if f.src in LIVE_STALE_SOURCES:
                    f.markets.clear()
        arbs = arb_live.find_arbs(g, stake_cap=STAKE_CAP, min_record=RECORD_MIN_EDGE)
        for a in arbs:
            ldb.insert_arb(con, poll_id, ts, gkey, a, is_live)
            n_arbs += 1
            if a.edge <= MIN_EDGE_TRADE:
                continue
            n_pos += 1
            ek = (gkey, a.key())
            ep = open_eps.get(ek)
            ep_id, age = ldb.touch_episode(con, ep, gkey, a, ts, is_live)
            seen_eps.add(ek)
            if ep is None:
                open_eps[ek] = {"id": ep_id, "first_ts": ts}
            for d in DELAYS:
                if age >= d:
                    ldb.insert_vtrade(con, ep_id, d, ts, gkey, a, is_live, a.cap, a.profit_at_cap, a.tied)
    # эпизоды, которых нет в этом опросе — закрываем
    ldb.close_episodes(con, [v["id"] for k, v in open_eps.items() if k not in seen_eps])
    dur = time.time() - t0
    con.execute("UPDATE polls SET dur_s=?, n_live=?, n_arbs=?, n_arbs_pos=?, errors=? WHERE id=?",
                (round(dur, 1), n_live, n_arbs, n_pos, json.dumps(feeds.errors, ensure_ascii=False), poll_id))
    con.commit()
    log(f"poll #{poll_id}: pin {len(pin)} cb {len(cb)} sx {len(sx)} sm {len(sm)} | групп {len(groups)} (live {n_live}) | "
        f"записей {n_arbs}, вилок>0 {n_pos} | {dur:.0f}s" + (f" | ошибки {feeds.errors}" if feeds.errors else ""))
    prune_old(con, poll_id)
    try:
        import dashboard_export
        dashboard_export.write_and_push(push=True)
    except Exception as e:
        log(f"dashboard export: {e}")
    return poll_id


class _Stop(BaseException):
    """Наследуем от BaseException, а не Exception: широкие `except Exception` в клиентах источников
    (сетевые ошибки одного фида не должны валить весь опрос) не должны глотать сигнал остановки."""
    pass


def _on_sigterm(signum, frame):
    # `timeout N cmd` (используется коллектором на GitHub Actions) убивает процесс через SIGTERM —
    # без этого обработчика WAL-файл (state/live.db-wal) не сливается в основной .db перед смертью
    # процесса, и выгруженная наружу база оказывается почти пустой (реальные данные остаются в -wal,
    # который никуда не отправляется). Поднимаем исключение, чтобы дойти до чистого closedown ниже.
    raise _Stop()


def main():
    import signal
    signal.signal(signal.SIGTERM, _on_sigterm)
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll", type=int, default=POLL_SEC)
    ap.add_argument("--hours-ahead", type=float, default=3.0)
    ap.add_argument("--hours-back", type=float, default=2.5)
    ap.add_argument("--sources", default="pinnacle,sx,smarkets,cloudbet",
                    help="через запятую: pinnacle,sx,smarkets,cloudbet. На сервере Hetzner доступны только sx,cloudbet "
                         "(Pinnacle/Smarkets/Betfair дают 403 с дата-центра)")
    args = ap.parse_args()
    feeds = Feeds(args.hours_back, args.hours_ahead, tuple(s.strip() for s in args.sources.split(",") if s.strip()))
    con = ldb.connect()
    # закрыть эпизоды прошлого запуска
    ldb.close_episodes(con, [v["id"] for v in ldb.open_episodes(con).values()])
    con.commit()
    awake = keep_awake()
    log(f"старт поллера: источники {','.join(sorted(feeds.sources))}, окно −{args.hours_back}h..+{args.hours_ahead}h, "
        f"шаг {args.poll}s, БД {ldb.DB_PATH}" + (" | сон системы заблокирован" if awake else ""))
    try:
        while True:
            t0 = time.time()
            try:
                run_poll(feeds, con)
            except (KeyboardInterrupt, _Stop):
                raise
            except Exception:
                log("ошибка опроса:\n" + traceback.format_exc())
            if args.once:
                break
            wait = args.poll - (time.time() - t0)
            if wait > 0:
                time.sleep(wait)
    except _Stop:
        log("получен SIGTERM — сливаю WAL и закрываю БД")
    finally:
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        con.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("остановлено")

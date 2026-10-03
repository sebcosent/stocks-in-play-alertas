"""
Servicio de alertas en segundo plano de Stocks in Play.

Funciona sin la app ni el navegador abiertos: cada pocos minutos revisa acciones,
divisas, oro, índices y el calendario macro, y manda los avisos por Telegram.
Usa los mismos ajustes (config.json) y las mismas reglas que la app.

Se arranca desde la app (Ajustes → Alertas) o con doble clic en Alertas_segundo_plano.bat.
El ordenador tiene que estar encendido (no en suspensión).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))

import pandas as pd  # noqa: E402

import core  # noqa: E402
import mercados  # noqa: E402
import motor  # noqa: E402

LOG_FILE = HERE / "alertas_servicio.log"
PID_FILE = HERE / "alertas.pid"
STOP_FILE = HERE / "alertas.stop"
LOCK_PORT = 47613

_mem: dict = {"econ": (0.0, None), "news": {}, "fund": None}


def log(msg: str) -> None:
    line = f"{datetime.now(motor.MADRID):%d/%m %H:%M:%S} {msg}\n"
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.write_text("", encoding="utf-8")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def write_status(**kw) -> None:
    data = motor.service_status()
    kw.setdefault("ts", datetime.now(timezone.utc).timestamp())
    kw.setdefault("pid", os.getpid())
    data.update(kw)
    try:
        motor.STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# Datos (con las mismas cachés en disco que la app)
# --------------------------------------------------------------------------------------
def get_daily(tickers: list[str]) -> dict:
    day = core.now_ny().date().isoformat()
    fname = f"diario_{day}.pkl"
    store = core.disk_load(fname, {})
    missing = [t for t in tickers if t not in store]
    for i in range(0, len(missing), 200):
        chunk = missing[i:i + 200]
        try:
            data = core.download_daily(chunk)
        except Exception:
            continue
        for t in chunk:
            d = core._split(data, t)
            if d is not None:
                d = d[[c for c in ("Open", "High", "Low", "Close", "Volume")
                       if c in d.columns]].dropna(subset=["Close"])
            store[t] = d if d is not None and len(d) else None
    if missing:
        core.disk_save(fname, store)
    return {t: store.get(t) for t in tickers}


def get_intraday(tickers: list[str]) -> pd.DataFrame:
    parts = []
    for i in range(0, len(tickers), 150):
        try:
            parts.append(core.download_intraday(tickers[i:i + 150]))
        except Exception:
            pass
    parts = [x for x in parts if x is not None and not x.empty]
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, axis=1) if len(parts) > 1 else parts[0]


def get_fundamentals(tickers: list[str]) -> dict:
    store = core.disk_load("fundamentales.pkl", {})
    now = time.time()
    missing = [t for t in tickers if t not in store or now - store[t][0] > 24 * 3600]
    if missing:
        df = core.fetch_fundamentals_many(missing)
        rows = {r["Ticker"]: r for r in df.to_dict("records")} if not df.empty else {}
        for t in missing:
            store[t] = (now, rows.get(t))
        core.disk_save("fundamentales.pkl", store)
    return {t: store[t][1] for t in tickers if t in store}


def get_news(tickers: list[str], key: str) -> dict:
    now = time.time()
    cache = _mem["news"]
    missing = [t for t in tickers if t not in cache or now - cache[t][0] > 600]
    if missing:
        fresh = core.news_many(missing, key)
        for t in missing:
            cache[t] = (now, fresh.get(t) or [])
    return {t: cache[t][1] for t in tickers if t in cache}


def get_econ():
    ts, cal = _mem["econ"]
    if cal is None or time.time() - ts > 1800:
        cal = core.economic_calendar()
        _mem["econ"] = (time.time(), cal)
    return cal


# --------------------------------------------------------------------------------------
# Escaneo de acciones (versión ligera del escáner de la app)
# --------------------------------------------------------------------------------------
def scan_stocks(p: dict):
    now = core.now_ny()
    frac = core.session_fraction(now)
    watch = [t.strip().upper() for t in p["watchlist"].split(",") if t.strip()]
    extra = [t.strip().upper() for t in (p.get("bingx_extra") or "").split(",") if t.strip()]
    bx = set(core.bingx_stock_tickers()) | set(extra)
    if p["only_bingx"] and bx:
        universe = list(dict.fromkeys(watch + sorted(bx)))
    else:
        info = []
        for s in p["sources"]:
            info += [q.get("symbol") for q in core.fetch_screener(s, 100) if q.get("symbol")]
        universe = list(dict.fromkeys(watch + info + core.BASE_UNIVERSE))[:320]
    universe = [t for t in universe if t and "." not in t and "^" not in t]

    daily = get_daily(universe + ["SPY"])
    keep = []
    for t in universe:
        d = daily.get(t)
        if t in watch or (d is not None and len(d) >= 15
                          and d["Close"].iloc[-1] >= p["min_price"] * 0.7
                          and d["Volume"].tail(63).mean() >= p["min_avg_vol"] * 0.7):
            keep.append(t)
    intra = get_intraday(keep + ["SPY"])
    spy = core.compute_metrics("SPY", daily.get("SPY"), core._split(intra, "SPY"), 1.0)
    spy_chg = spy["Cambio %"] if spy else float("nan")

    rows = []
    for t in keep:
        try:
            m = core.compute_metrics(t, daily.get(t), core._split(intra, t), frac)
        except Exception:
            m = None
        if m:
            rows.append(m)
    df = pd.DataFrame(rows)
    if df.empty:
        return df, {}, spy_chg

    earn = core.earnings_calendar(p["finnhub_key"], now.date())
    hora = dict(zip(earn["Ticker"], earn["Hora"])) if not earn.empty else {}
    df["Resultados"] = df["Ticker"].isin(hora)
    df["Hora resultados"] = df["Ticker"].map(lambda t: hora.get(t, ""))
    df["vs SPY"] = df["Cambio %"] - spy_chg if not pd.isna(spy_chg) else float("nan")
    move = pd.concat([df["Gap %"].abs(), df["Cambio %"].abs()], axis=1).max(axis=1)
    df = df[(df["Precio"] >= p["min_price"]) & (df["Vol. medio 3M"] >= p["min_avg_vol"])
            & ((move >= p["min_move"]) | df["Resultados"] | df["Ticker"].isin(watch))].copy()
    if df.empty:
        return df, {}, spy_chg

    df["Noticias"], df["Catalizadores"] = 0, [[] for _ in range(len(df))]
    df["Puntuación"] = df.apply(lambda r: core.score_row(r.to_dict()), axis=1)
    df = df.sort_values("Puntuación", ascending=False)
    top = list(df["Ticker"].head(int(p["top_n"])))
    fund = get_fundamentals(top)
    news = get_news(top, p["finnhub_key"])
    for col in ("Float", "Corto % float"):
        df[col] = df["Ticker"].map(lambda t, c=col: (fund.get(t) or {}).get(c))
    df["Noticias"] = df["Ticker"].map(lambda t: len(news.get(t) or []))
    df["Catalizadores"] = [
        sorted(set(core.tag_catalysts([n["title"] for n in news.get(t) or []]))
               | ({"Resultados"} if e else set()))
        for t, e in zip(df["Ticker"], df["Resultados"])]
    df["Puntuación"] = df.apply(lambda r: core.score_row(r.to_dict()), axis=1)
    df["Por qué"] = df.apply(lambda r: core.why(r.to_dict()), axis=1)
    return df.sort_values("Puntuación", ascending=False).reset_index(drop=True), news, spy_chg


def scan_fx() -> pd.DataFrame:
    tickers = list(mercados.INSTRUMENTS)
    return mercados.table(get_daily(tickers), get_intraday(tickers))


def stock_hours() -> bool:
    """Pre-market, sesión y after-hours de EE.UU. (4:00-20:00 NY, lunes a viernes)."""
    return core.market_status() in ("Pre-market", "Abierto", "After-hours")


# --------------------------------------------------------------------------------------
# Bucle principal
# --------------------------------------------------------------------------------------
def one_pass() -> int:
    p = motor.params_from_config(core.load_config())
    if not p["alerts_on"]:
        write_status(estado="Alertas desactivadas en Ajustes")
        return 0
    sent = core.load_sent()
    msgs: list[str] = []
    if stock_hours():
        df, news, spy = scan_stocks(p)
        msgs += motor.stock_messages(df, news, p, sent)
        try:
            core.record_picks(df, core.market_status(), spy)
        except Exception:
            pass
    if p["fx_alerts"] and p["fx_watch"]:
        msgs += motor.fx_messages(scan_fx(), p, sent)
        msgs += motor.macro_messages(get_econ(), p, sent)
    if msgs:
        core.save_sent(sent)
        core.log_alerts(msgs)
        # Si las alertas de Telegram las manda la nube (GitHub), el ordenador no las
        # repite; solo quedan en el historial de la app.
        cloud_sends = core.load_config().get("cloud_telegram") and not os.environ.get(
            "STOCKS_CLOUD")
        if p["tg_token"] and p["tg_chat"] and not cloud_sends:
            for i in range(0, len(msgs), 20):
                core.send_telegram(p["tg_token"], p["tg_chat"],
                                   "Stocks in Play\n\n" + "\n".join(msgs[i:i + 20]))
        log(f"{len(msgs)} alertas enviadas")
    write_status(estado="Funcionando", ultima_revision=datetime.now(motor.MADRID)
                 .strftime("%d/%m %H:%M"), ultimas_alertas=len(msgs))
    return int(p["refresh"]) or 120


def main() -> None:
    # Una sola copia a la vez
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        print("El servicio de alertas ya está funcionando.")
        return
    STOP_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    log("Servicio de alertas iniciado")
    write_status(estado="Iniciando")
    try:
        while True:
            try:
                wait = one_pass()
            except Exception:
                log("Error: " + traceback.format_exc(limit=3).replace("\n", " | "))
                write_status(estado="Error temporal, reintentando")
                wait = 120
            # Espera en pasos cortos para poder pararse enseguida
            for _ in range(max(60, wait) // 5):
                if STOP_FILE.exists():
                    raise SystemExit
                time.sleep(5)
    except (SystemExit, KeyboardInterrupt):
        pass
    finally:
        STOP_FILE.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        write_status(estado="Parado", ts=0)
        log("Servicio de alertas parado")


if __name__ == "__main__":
    main()

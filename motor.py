"""
Reglas de alertas compartidas por la app y por el servicio de alertas en segundo plano
(alertas.py), para que las dos den exactamente los mismos avisos.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

import core
import mercados

MADRID = ZoneInfo("Europe/Madrid")
STATUS_FILE = core.HERE / "alertas_estado.json"

DEFAULTS = {
    "sources": ["most_actives", "day_gainers", "day_losers", "small_cap_gainers"],
    "only_bingx": True, "bingx_extra": "",
    "watchlist": "", "min_price": 2.0, "min_avg_vol": 300_000, "min_move": 2.0, "top_n": 20,
    "refresh": 120, "alerts_on": True, "alert_score": 40, "alert_rvol": 3.0,
    "alert_gap": 8.0, "sound": True, "app_popups": False,
    "fx_alerts": True, "fx_prev": True, "fx_asia": True, "fx_atr": 1.0, "fx_macro": True,
    "fx_watch": ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "USDCHF=X",
                 "NZDUSD=X", "EURJPY=X", "GBPJPY=X", "GC=F"],
}


def params_from_config(cfg: dict) -> dict:
    p = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    p["fx_watch"] = [s for s in p["fx_watch"] if s in mercados.INSTRUMENTS]
    p["finnhub_key"] = cfg.get("finnhub_key", "")
    p["tg_token"] = cfg.get("telegram_token", "")
    p["tg_chat"] = cfg.get("telegram_chat_id", "")
    p["bingx_key"] = cfg.get("bingx_key", "")
    p["bingx_secret"] = cfg.get("bingx_secret", "")
    return p


def fmt_px(v, ref) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{v:,.{mercados.price_decimals(ref)}f}"


# --------------------------------------------------------------------------------------
# Reglas
# --------------------------------------------------------------------------------------
def stock_messages(df: pd.DataFrame | None, news: dict, p: dict, sent: set) -> list[str]:
    msgs = []
    if df is None or df.empty:
        return msgs
    for r in df.head(60).to_dict("records"):
        t = r["Ticker"]
        hit = (r["Puntuación"] >= p["alert_score"]
               or (not pd.isna(r.get("RVOL")) and r["RVOL"] >= p["alert_rvol"])
               or (not pd.isna(r.get("Gap %")) and abs(r["Gap %"]) >= p["alert_gap"]))
        if hit and t not in sent:
            sent.add(t)
            msgs.append(f"{t} {r['Cambio %']:+.1f}% · {r.get('Por qué', '')}")
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    for t, items in (news or {}).items():
        for n in items or []:
            k = f"{t}|{n['title'][:80]}"
            if n.get("time") and n["time"] >= cutoff and k not in sent:
                sent.add(k)
                msgs.append(f"Noticia {t}: {n['title']}")
    return msgs


def fx_messages(fx: pd.DataFrame | None, p: dict, sent: set) -> list[str]:
    if not p.get("fx_alerts") or not p.get("fx_watch") or fx is None or fx.empty:
        return []
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:      # fin de semana: mercado de divisas cerrado
        return []
    msgs = []
    for r in fx[fx["Símbolo"].isin(p["fx_watch"])].to_dict("records"):
        name, px, day = r["Nombre"], r["Precio"], r["Día"]
        f = lambda v: fmt_px(v, px)
        tail = f" · ahora {f(px)} · {r['Cambio %']:+.2f}% ({r['Mov. ATR']:.1f} ATR)"
        events = []
        if p.get("fx_prev"):
            if r["Máx hoy"] > r["Máx ayer"]:
                events.append(("maxayer", f"{name} rompe el MÁXIMO de ayer ({f(r['Máx ayer'])})"))
            if r["Mín hoy"] < r["Mín ayer"]:
                events.append(("minayer", f"{name} rompe el MÍNIMO de ayer ({f(r['Mín ayer'])})"))
        if p.get("fx_asia") and now_utc.hour >= 8 and not pd.isna(r.get("Asia máx")):
            highs = [v for v in (r.get("Londres máx"), r.get("Nueva York máx"))
                     if v is not None and not pd.isna(v)]
            lows = [v for v in (r.get("Londres mín"), r.get("Nueva York mín"))
                    if v is not None and not pd.isna(v)]
            if highs and max(highs) > r["Asia máx"]:
                events.append(("asiamax", f"{name} rompe el rango de ASIA al alza "
                                          f"({f(r['Asia máx'])})"))
            if lows and min(lows) < r["Asia mín"]:
                events.append(("asiamin", f"{name} rompe el rango de ASIA a la baja "
                                          f"({f(r['Asia mín'])})"))
        if not pd.isna(r["Mov. ATR"]) and r["Mov. ATR"] >= p.get("fx_atr", 1.0):
            events.append(("atr", f"{name} se mueve {r['Mov. ATR']:.1f} ATR hoy"))
        for code, text in events:
            k = f"FX|{r['Símbolo']}|{code}|{day}"
            if k not in sent:
                sent.add(k)
                msgs.append(text + tail)
    return msgs


def macro_messages(cal: pd.DataFrame | None, p: dict, sent: set) -> list[str]:
    if not p.get("fx_alerts") or not p.get("fx_macro") or cal is None or cal.empty:
        return []
    curr = set()
    for s in p.get("fx_watch") or []:
        curr |= core.currencies_of(s)
    now = pd.Timestamp.now(tz="UTC")
    soon = cal[(cal["Impacto"] == "Alto") & (cal["Divisa"].isin(curr))
               & (cal["Hora"] > now) & (cal["Hora"] <= now + pd.Timedelta(minutes=15))]
    msgs = []
    for r in soon.to_dict("records"):
        k = f"MACRO|{r['Divisa']}|{r['Evento']}|{r['Hora']:%Y%m%d%H%M}"
        if k not in sent:
            sent.add(k)
            mins = int((r["Hora"] - now).total_seconds() // 60)
            prev = f" · previsión {r['Previsión']}" if r.get("Previsión") else ""
            msgs.append(f"En {mins} min: dato de impacto ALTO {r['Divisa']} · "
                        f"{r['Evento']} ({r['Hora'].tz_convert(MADRID):%H:%M}){prev}")
    return msgs


# --------------------------------------------------------------------------------------
# Estado del servicio en segundo plano
# --------------------------------------------------------------------------------------
def service_status() -> dict:
    try:
        import json
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def service_running(max_age_s: int = 600) -> bool:
    st = service_status()
    try:
        return (datetime.now(timezone.utc).timestamp() - float(st.get("ts", 0))) < max_age_s
    except Exception:
        return False

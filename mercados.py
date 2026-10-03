"""
Divisas, metales e índices: qué está "en juego" hoy.

Como en divisas no hay volumen fiable, se mide la actividad con el ATR (rango medio
diario): cuánto se ha movido hoy comparado con un día normal, si ha roto el máximo o
mínimo de ayer, y los rangos de las sesiones de Asia, Londres y Nueva York.
"""

from __future__ import annotations

import math
from datetime import time

import pandas as pd

import core

INSTRUMENTS = {
    # Divisas principales
    "EURUSD=X": ("EUR/USD", "Divisas"),
    "GBPUSD=X": ("GBP/USD", "Divisas"),
    "USDJPY=X": ("USD/JPY", "Divisas"),
    "USDCHF=X": ("USD/CHF", "Divisas"),
    "AUDUSD=X": ("AUD/USD", "Divisas"),
    "USDCAD=X": ("USD/CAD", "Divisas"),
    "NZDUSD=X": ("NZD/USD", "Divisas"),
    # Cruces más negociados
    "EURJPY=X": ("EUR/JPY", "Divisas"),
    "GBPJPY=X": ("GBP/JPY", "Divisas"),
    "AUDJPY=X": ("AUD/JPY", "Divisas"),
    "EURGBP=X": ("EUR/GBP", "Divisas"),
    "EURCHF=X": ("EUR/CHF", "Divisas"),
    # Metales (futuros de COMEX/NYMEX, siguen de cerca al contado)
    "GC=F": ("Oro (XAU)", "Metales"),
    "SI=F": ("Plata (XAG)", "Metales"),
    "PL=F": ("Platino", "Metales"),
    "PA=F": ("Paladio", "Metales"),
    "HG=F": ("Cobre", "Metales"),
    # Índices (futuros, cotizan casi 24 h)
    "NQ=F": ("Nasdaq 100", "Índices"),
    "ES=F": ("S&P 500", "Índices"),
    "YM=F": ("Dow Jones", "Índices"),
    "RTY=F": ("Russell 2000", "Índices"),
}

# Sesiones en hora UTC
SESSIONS = {"Asia": (time(0, 0), time(8, 0)),
            "Londres": (time(7, 0), time(16, 0)),
            "Nueva York": (time(13, 0), time(22, 0))}


def _utc(df: pd.DataFrame) -> pd.DataFrame:
    idx = df.index
    idx = idx.tz_localize("UTC") if getattr(idx, "tz", None) is None else idx.tz_convert("UTC")
    df = df.copy()
    df.index = idx
    return df


def metrics(ticker: str, daily: pd.DataFrame | None, intra: pd.DataFrame | None) -> dict | None:
    if daily is None or intra is None:
        return None
    intra = intra.dropna(subset=["Close"])
    daily = daily.dropna(subset=["Close"])
    if intra.empty or len(daily) < 25:
        return None
    intra = _utc(intra)
    day = intra.index[-1].date()
    today = intra[intra.index.date == day]
    d_idx = pd.to_datetime(daily.index)
    d_dates = (d_idx.tz_convert("UTC") if d_idx.tz is not None else d_idx).date
    hist = daily[d_dates < day]
    if len(hist) < 21 or today.empty:
        return None

    prev = hist.iloc[-1]
    pc, ph, pl = float(prev["Close"]), float(prev["High"]), float(prev["Low"])
    h, l, c = hist["High"], hist["Low"], hist["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean())
    atr_pct = atr / pc * 100 if pc else float("nan")

    last = float(today["Close"].iloc[-1])
    hi, lo = float(today["High"].max()), float(today["Low"].min())
    chg = (last / pc - 1) * 100
    rng = (hi - lo) / pc * 100
    sma20, sma50 = float(c.tail(20).mean()), float(c.tail(50).mean())
    if last > sma20 > sma50:
        trend = "Alcista"
    elif last < sma20 < sma50:
        trend = "Bajista"
    else:
        trend = "Lateral"
    if hi > ph and lo < pl:
        brk = "Rompió ambos"
    elif hi > ph:
        brk = "Rompió máx. ayer"
    elif lo < pl:
        brk = "Rompió mín. ayer"
    else:
        brk = "Dentro de ayer"

    row = {
        "Símbolo": ticker,
        "Nombre": core_name(ticker),
        "Grupo": INSTRUMENTS.get(ticker, ("", ""))[1],
        "Día": day,
        "Precio": last,
        "Cierre ant.": pc,
        "Cambio %": chg,
        "Mov. ATR": abs(chg) / atr_pct if atr_pct > 0 else float("nan"),
        "Rango hoy %": rng,
        "Rango ATR": rng / atr_pct if atr_pct > 0 else float("nan"),
        "ATR %": atr_pct,
        "Posición en rango": (last - lo) / (hi - lo) * 100 if hi > lo else float("nan"),
        "Máx hoy": hi, "Mín hoy": lo, "Máx ayer": ph, "Mín ayer": pl,
        "Día anterior": brk,
        "Tendencia": trend,
        "SMA20": sma20,
        "Máx 52s": float(hist.tail(252)["High"].max()),
        "Mín 52s": float(hist.tail(252)["Low"].min()),
    }
    t = today.index.time
    for name, (a, b) in SESSIONS.items():
        s = today[(t >= a) & (t < b)]
        row[f"{name} máx"] = float(s["High"].max()) if not s.empty else float("nan")
        row[f"{name} mín"] = float(s["Low"].min()) if not s.empty else float("nan")
    row["Puntuación"] = score(row)
    row["Por qué"] = why(row)
    return row


def core_name(ticker: str) -> str:
    return INSTRUMENTS.get(ticker, (ticker, ""))[0]


def score(r: dict) -> float:
    def v(k, cap):
        x = r.get(k)
        return 0.0 if x is None or pd.isna(x) else min(abs(x), cap)
    s = v("Mov. ATR", 4) * 10 + v("Rango ATR", 4) * 6
    s += 6 if r["Día anterior"] in ("Rompió máx. ayer", "Rompió mín. ayer") else 0
    s += 3 if r["Día anterior"] == "Rompió ambos" else 0
    # movimiento a favor de la tendencia
    if (r["Tendencia"] == "Alcista" and r["Cambio %"] > 0) or \
            (r["Tendencia"] == "Bajista" and r["Cambio %"] < 0):
        s += 4
    return round(s, 1)


def why(r: dict) -> str:
    parts = [f"{r['Cambio %']:+.2f}% ({r['Mov. ATR']:.1f} ATR)"]
    if not pd.isna(r["Rango ATR"]):
        parts.append(f"rango {r['Rango ATR']:.1f} ATR")
    if r["Día anterior"] != "Dentro de ayer":
        parts.append(r["Día anterior"].lower())
    parts.append(f"tendencia {r['Tendencia'].lower()}")
    pos = r.get("Posición en rango")
    if pos is not None and not pd.isna(pos):
        parts.append("cerca del máx. del día" if pos >= 80 else
                     ("cerca del mín. del día" if pos <= 20 else "mitad del rango"))
    return " · ".join(parts)


def table(daily: dict, intra: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for t in INSTRUMENTS:
        try:
            m = metrics(t, daily.get(t), core._split(intra, t))
        except Exception:
            m = None
        if m:
            rows.append(m)
    df = pd.DataFrame(rows)
    return df.sort_values("Puntuación", ascending=False).reset_index(drop=True) \
        if not df.empty else df


def intraday_today(ticker: str, intra: pd.DataFrame) -> pd.DataFrame:
    df = core._split(intra, ticker)
    if df is None:
        return pd.DataFrame()
    df = _utc(df.dropna(subset=["Close"]))
    if df.empty:
        return df
    return df[df.index.date == df.index[-1].date()]


def price_decimals(price: float) -> int:
    if price is None or math.isnan(price):
        return 2
    return 5 if price < 5 else (3 if price < 200 else 2)

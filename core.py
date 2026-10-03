"""
Lógica de datos y cálculos de "Stocks in Play" (sin interfaz).

Fuentes:
  - Yahoo Finance (yfinance): listas de valores activos, histórico diario, velas de 5 min
    con pre/post-market, noticias, float y posiciones cortas.
  - Finnhub (opcional, clave gratuita en finnhub.io): calendario de resultados, noticias
    más completas, noticias generales del mercado y calendario de la FDA.
  - Telegram (opcional): envío de alertas.
"""

from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

NY = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
ALERTS_FILE = HERE / "alertas_enviadas.json"
CACHE_DIR = HERE / "cache"

PM_START, RTH_START, RTH_END, AH_END = time(4, 0), time(9, 30), time(16, 0), time(20, 0)

SCREENERS = {
    "most_actives": "Más activos",
    "day_gainers": "Mayores subidas",
    "day_losers": "Mayores bajadas",
    "small_cap_gainers": "Small caps al alza",
    "aggressive_small_caps": "Small caps agresivas",
    "most_shorted_stocks": "Más vendidas en corto",
}

CATALYSTS = {
    "Resultados": ["earnings", "results", "revenue", "guidance", "quarter", "eps",
                   "beats", "misses", "outlook", "forecast"],
    "FDA/Biotech": ["fda", "trial", "phase 1", "phase 2", "phase 3", "approval",
                    "approves", "clinical", "pdufa"],
    "Fusión/Adquisición": ["merger", "acquire", "acquisition", "buyout", "takeover",
                           "to buy", "deal", "bid for"],
    "Analistas": ["upgrade", "downgrade", "price target", "initiates", "rating",
                  "outperform", "underperform"],
    "Ampliación capital": ["offering", "dilution", "shelf", "private placement", "warrants"],
    "Contrato/Alianza": ["contract", "partnership", "agreement", "awarded", "collaboration"],
    "Legal/Regulatorio": ["lawsuit", "sec ", "investigation", "probe", "subpoena"],
    "Insiders/Recompra": ["buyback", "repurchase", "insider", "stake"],
}

# Valores líquidos y volátiles que siempre se vigilan (además de las listas de Yahoo)
BASE_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "NFLX", "AVGO",
    "PLTR", "SMCI", "COIN", "MSTR", "INTC", "MU", "BA", "DIS", "NKE", "SOFI",
    "RIVN", "LCID", "NIO", "MARA", "RIOT", "HOOD", "UBER", "SHOP", "SNOW", "CRWD",
    "ARM", "BABA", "PYPL", "XYZ", "F", "GM", "AAL", "CCL", "XOM", "JPM",
    "ORCL", "ADBE", "CRM", "QCOM", "TSM", "ASML", "LLY", "NVO", "UNH", "WMT",
    "COST", "TGT", "LULU", "DKNG", "RBLX", "U", "AFRM", "UPST", "CVNA", "GME",
    "AMC", "IONQ", "RGTI", "QUBT", "SOUN", "BBAI", "HIMS", "OKLO", "SMR", "RKLB",
    "ASTS", "ACHR", "JOBY", "APP", "CELH", "ENPH", "FSLR", "PDD", "JD", "SNAP",
]

DEFAULT_CONFIG = {
    "finnhub_key": "",
    "telegram_token": "",
    "telegram_chat_id": "",
    "watchlist": "",
}


# --------------------------------------------------------------------------------------
# Configuración y utilidades
# --------------------------------------------------------------------------------------
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def disk_load(name: str, default):
    """Lee un objeto guardado en la carpeta cache/ (o devuelve default)."""
    try:
        import pickle
        with open(CACHE_DIR / name, "rb") as f:
            return pickle.load(f)
    except Exception:
        return default


def disk_save(name: str, obj) -> None:
    try:
        import pickle
        CACHE_DIR.mkdir(exist_ok=True)
        tmp = CACHE_DIR / (name + ".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(obj, f)
        tmp.replace(CACHE_DIR / name)
        # borrar cachés diarias de otros días
        if name.startswith("diario_"):
            for old in CACHE_DIR.glob("diario_*.pkl"):
                if old.name != name:
                    old.unlink(missing_ok=True)
    except Exception:
        pass


def now_ny() -> datetime:
    return datetime.now(NY)


def market_status(now: datetime | None = None) -> str:
    now = (now or now_ny()).astimezone(NY)
    if now.weekday() >= 5:
        return "Cerrado (fin de semana)"
    t = now.time()
    if PM_START <= t < RTH_START:
        return "Pre-market"
    if RTH_START <= t < RTH_END:
        return "Abierto"
    if RTH_END <= t < AH_END:
        return "After-hours"
    return "Cerrado"


def session_fraction(now: datetime | None = None) -> float:
    """Fracción "de volumen" de la sesión regular ya transcurrida (1.0 si está cerrada).
    Se usa raíz cuadrada porque el volumen se concentra al principio de la sesión."""
    now = (now or now_ny()).astimezone(NY)
    if now.weekday() >= 5:
        return 1.0
    o = now.replace(hour=9, minute=30, second=0, microsecond=0)
    c = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now <= o or now >= c:
        return 1.0
    return max(0.05, math.sqrt((now - o).total_seconds() / (c - o).total_seconds()))


def _num(x, default=float("nan")):
    try:
        return default if x is None else float(x)
    except (TypeError, ValueError):
        return default


def human(n) -> str:
    if n is None or pd.isna(n):
        return "-"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def tag_catalysts(headlines: list[str]) -> list[str]:
    text = " ".join(headlines).lower()
    return [tag for tag, words in CATALYSTS.items() if any(w in text for w in words)]


def _split(data: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    """Saca el DataFrame de un ticker del resultado de yf.download (multi o simple)."""
    if data is None or data.empty:
        return None
    if isinstance(data.columns, pd.MultiIndex):
        lv0 = data.columns.get_level_values(0)
        lv1 = data.columns.get_level_values(1)
        if ticker in lv0:
            return data[ticker]
        if ticker in lv1:
            return data.xs(ticker, axis=1, level=1)
        return None
    return data


# --------------------------------------------------------------------------------------
# Universo de valores
# --------------------------------------------------------------------------------------
def fetch_screener(name: str, count: int = 100) -> list[dict]:
    try:
        res = yf.screen(name, count=count)
        return res.get("quotes", []) if isinstance(res, dict) else []
    except Exception:
        return []


def quotes_info(quotes: list[dict], source: str) -> dict[str, dict]:
    out = {}
    for q in quotes:
        s = q.get("symbol")
        if not s or "." in s or "^" in s:
            continue
        out[s] = {
            "Nombre": q.get("shortName") or q.get("longName") or "",
            "Cap. bursátil": _num(q.get("marketCap")),
            "Earnings ts": q.get("earningsTimestamp"),
            "Fuente": source,
        }
    return out


# --------------------------------------------------------------------------------------
# Precios
# --------------------------------------------------------------------------------------
def download_daily(tickers: list[str]) -> pd.DataFrame:
    return yf.download(tickers, period="1y", interval="1d", group_by="ticker",
                       auto_adjust=False, progress=False, threads=20)


def download_intraday(tickers: list[str]) -> pd.DataFrame:
    return yf.download(tickers, period="1d", interval="5m", prepost=True, group_by="ticker",
                       auto_adjust=False, progress=False, threads=20)


def _to_ny(df: pd.DataFrame) -> pd.DataFrame:
    idx = df.index
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")
    df = df.copy()
    df.index = idx.tz_convert(NY)
    return df


def compute_metrics(ticker: str, daily: pd.DataFrame | None, intra: pd.DataFrame | None,
                    frac: float) -> dict | None:
    """Métricas de un valor para la última sesión disponible (incluido el pre-market)."""
    if daily is None or intra is None:
        return None
    intra = intra.dropna(subset=["Close"])
    daily = daily.dropna(subset=["Close"])
    if intra.empty or len(daily) < 15:
        return None
    intra = _to_ny(intra)
    sess = intra.index[-1].date()
    today = intra[intra.index.date == sess]
    t = today.index.time
    pm = today[(t >= PM_START) & (t < RTH_START)]
    rth = today[(t >= RTH_START) & (t < RTH_END)]

    d_idx = pd.to_datetime(daily.index)
    d_dates = (d_idx.tz_convert(NY) if d_idx.tz is not None else d_idx).date
    hist = daily[d_dates < sess]
    if len(hist) < 15:
        return None
    prev_close = float(hist["Close"].iloc[-1])
    if prev_close <= 0:
        return None

    # ATR de 14 días
    h, l, c = hist["High"], hist["Low"], hist["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean())
    atr_pct = atr / prev_close * 100

    avg_vol = float(hist["Volume"].tail(63).mean())
    last = float(today["Close"].iloc[-1])
    pm_vol = float(pm["Volume"].sum()) if not pm.empty else 0.0
    rth_vol = float(rth["Volume"].sum()) if not rth.empty else 0.0
    open_px = float(rth["Open"].iloc[0]) if not rth.empty else (
        float(pm["Close"].iloc[-1]) if not pm.empty else float("nan"))

    gap = (open_px / prev_close - 1) * 100 if not math.isnan(open_px) else float("nan")
    chg = (last / prev_close - 1) * 100
    rvol = (rth_vol / frac) / avg_vol if avg_vol > 0 and rth_vol > 0 else float("nan")
    pm_pct = pm_vol / avg_vol * 100 if avg_vol > 0 else float("nan")

    # VWAP de la sesión regular (o del pre-market si aún no ha abierto)
    base = rth if not rth.empty else pm
    vwap = float("nan")
    if not base.empty and base["Volume"].sum() > 0:
        tp = (base["High"] + base["Low"] + base["Close"]) / 3
        vwap = float((tp * base["Volume"]).sum() / base["Volume"].sum())

    year = hist.tail(252)
    return {
        "Ticker": ticker,
        "Sesión": sess,
        "Precio": last,
        "Cierre ant.": prev_close,
        "Cambio %": chg,
        "Gap %": gap,
        "Gap ATR": abs(gap) / atr_pct if atr_pct > 0 and not math.isnan(gap) else float("nan"),
        "Mov. ATR": abs(chg) / atr_pct if atr_pct > 0 else float("nan"),
        "ATR %": atr_pct,
        "ATR $": atr,
        "RVOL": rvol,
        "Vol. hoy": rth_vol,
        "Vol. PM": pm_vol,
        "PM % vol. medio": pm_pct,
        "Vol. medio 3M": avg_vol,
        "PM máx": float(pm["High"].max()) if not pm.empty else float("nan"),
        "PM mín": float(pm["Low"].min()) if not pm.empty else float("nan"),
        "Máx día": float(rth["High"].max()) if not rth.empty else float("nan"),
        "Mín día": float(rth["Low"].min()) if not rth.empty else float("nan"),
        "Máx 52s": float(year["High"].max()),
        "Mín 52s": float(year["Low"].min()),
        "VWAP": vwap,
        "vs VWAP %": (last / vwap - 1) * 100 if vwap and not math.isnan(vwap) else float("nan"),
        "Apertura": open_px,
    }


def build_table(tickers: list[str], daily: pd.DataFrame, intra: pd.DataFrame,
                frac: float) -> pd.DataFrame:
    rows = []
    for t in tickers:
        try:
            m = compute_metrics(t, _split(daily, t), _split(intra, t), frac)
        except Exception:
            m = None
        if m:
            rows.append(m)
    return pd.DataFrame(rows)


def intraday_for(ticker: str, intra: pd.DataFrame) -> pd.DataFrame:
    """Velas de la última sesión con la columna VWAP (pre-market y luego sesión regular)."""
    df = _split(intra, ticker)
    if df is None:
        return pd.DataFrame()
    df = _to_ny(df.dropna(subset=["Close"]))
    if df.empty:
        return df
    sess = df.index[-1].date()
    df = df[df.index.date == sess].copy()
    t = df.index.time
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    pv = tp * df["Volume"]
    df["VWAP"] = float("nan")
    for mask in ((t < RTH_START), (t >= RTH_START) & (t < RTH_END)):
        if mask.any():
            cv = df.loc[mask, "Volume"].cumsum()
            df.loc[mask, "VWAP"] = (pv[mask].cumsum() / cv.where(cv > 0)).values
    return df


# --------------------------------------------------------------------------------------
# Float y cortos
# --------------------------------------------------------------------------------------
def fetch_fundamentals(ticker: str) -> dict:
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        info = {}
    spf = _num(info.get("shortPercentOfFloat"))
    return {
        "Ticker": ticker,
        "Nombre largo": info.get("longName") or info.get("shortName") or "",
        "Sector": info.get("sector") or "",
        "Float": _num(info.get("floatShares")),
        "Corto % float": spf * 100 if not math.isnan(spf) else float("nan"),
        "Días para cubrir": _num(info.get("shortRatio")),
        "Cap. bursátil info": _num(info.get("marketCap")),
    }


def fetch_fundamentals_many(tickers: list[str]) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame()
    with ThreadPoolExecutor(max_workers=8) as ex:
        return pd.DataFrame(list(ex.map(fetch_fundamentals, tickers)))


# --------------------------------------------------------------------------------------
# Noticias
# --------------------------------------------------------------------------------------
def parse_yahoo_news(item: dict) -> dict | None:
    c = item.get("content", item)
    title = c.get("title")
    if not title:
        return None
    ts = None
    if c.get("pubDate"):
        try:
            ts = datetime.fromisoformat(c["pubDate"].replace("Z", "+00:00"))
        except ValueError:
            ts = None
    elif c.get("providerPublishTime"):
        ts = datetime.fromtimestamp(int(c["providerPublishTime"]), tz=timezone.utc)
    link = ((c.get("clickThroughUrl") or {}).get("url")
            or (c.get("canonicalUrl") or {}).get("url") or c.get("link"))
    provider = (c.get("provider") or {}).get("displayName") or c.get("publisher") or ""
    return {"title": title, "time": ts, "link": link, "provider": provider}


def news_yahoo(ticker: str) -> list[dict]:
    try:
        raw = yf.Ticker(ticker).news or []
    except Exception:
        return []
    return [n for n in (parse_yahoo_news(i) for i in raw) if n]


def _finnhub(path: str, key: str, **params):
    params["token"] = key
    r = requests.get(f"https://finnhub.io/api/v1/{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def news_finnhub(ticker: str, key: str) -> list[dict]:
    d0 = (date.today() - timedelta(days=2)).isoformat()
    d1 = (date.today() + timedelta(days=1)).isoformat()
    try:
        raw = _finnhub("company-news", key, symbol=ticker, **{"from": d0, "to": d1})
    except Exception:
        return []
    return [{
        "title": n.get("headline", ""),
        "time": datetime.fromtimestamp(n["datetime"], tz=timezone.utc) if n.get("datetime") else None,
        "link": n.get("url"),
        "provider": n.get("source", ""),
    } for n in raw if n.get("headline")]


def recent_news(ticker: str, key: str = "", hours: int = 24) -> list[dict]:
    items = news_finnhub(ticker, key) if key else []
    if not items:
        items = news_yahoo(ticker)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    seen, out = set(), []
    for n in sorted(items, key=lambda n: n["time"] or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True):
        k = n["title"].strip().lower()
        if k in seen or (n["time"] is not None and n["time"] < cutoff):
            continue
        seen.add(k)
        out.append(n)
    return out


def news_many(tickers: list[str], key: str = "") -> dict[str, list[dict]]:
    with ThreadPoolExecutor(max_workers=6) as ex:
        return dict(zip(tickers, ex.map(lambda t: recent_news(t, key), tickers)))


def market_news(key: str, category: str = "general") -> list[dict]:
    """Noticias generales de Finnhub. Categorías: general, forex, merger, crypto."""
    if not key:
        return []
    try:
        raw = _finnhub("news", key, category=category)
    except Exception:
        return []
    return [{
        "title": n.get("headline", ""),
        "summary": n.get("summary", ""),
        "time": datetime.fromtimestamp(n["datetime"], tz=timezone.utc) if n.get("datetime") else None,
        "link": n.get("url"),
        "provider": n.get("source", ""),
        "related": n.get("related", ""),
        "category": category,
    } for n in raw if n.get("headline")]


def yahoo_market_news(tickers=("^GSPC", "^IXIC", "EURUSD=X", "GC=F", "CL=F")) -> list[dict]:
    """Noticias generales sin clave de Finnhub (titulares de Yahoo de índices, divisas...)."""
    out = []
    for t in tickers:
        for n in news_yahoo(t):
            n["category"] = "general"
            n["related"] = ""
            n["summary"] = ""
            out.append(n)
    return out


ECON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACT_ES = {"High": "Alto", "Medium": "Medio", "Low": "Bajo", "Holiday": "Festivo"}


def economic_calendar() -> pd.DataFrame:
    """Calendario económico de la semana (datos públicos de Forex Factory)."""
    try:
        r = requests.get(ECON_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        df = pd.DataFrame(r.json())
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    df["Hora"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df = df.rename(columns={"title": "Evento", "country": "Divisa", "forecast": "Previsión",
                            "previous": "Anterior"})
    df["Impacto"] = df["impact"].map(lambda x: IMPACT_ES.get(x, x))
    if "actual" in df:
        df = df.rename(columns={"actual": "Actual"})
    else:
        df["Actual"] = ""
    return df[["Hora", "Divisa", "Impacto", "Evento", "Actual", "Previsión", "Anterior"]] \
        .dropna(subset=["Hora"]).sort_values("Hora").reset_index(drop=True)


# Divisas afectadas por cada instrumento (para avisar de datos macro)
def currencies_of(symbol: str) -> set[str]:
    s = symbol.replace("=X", "")
    if len(s) == 6 and s.isalpha():
        return {s[:3], s[3:]}
    return {"USD"}   # oro, plata, índices de EE.UU.


# --------------------------------------------------------------------------------------
# Calendarios: resultados y FDA
# --------------------------------------------------------------------------------------
HOUR_ES = {"bmo": "Antes de apertura", "amc": "Tras el cierre", "dmh": "Durante la sesión"}


def earnings_calendar(key: str, day: date) -> pd.DataFrame:
    """Resultados de ayer y hoy. Finnhub si hay clave; si no, se intenta con Yahoo."""
    start, end = day - timedelta(days=3), day
    if key:
        try:
            raw = _finnhub("calendar/earnings", key, **{"from": start.isoformat(),
                                                         "to": end.isoformat()})
            df = pd.DataFrame(raw.get("earningsCalendar", []))
            if not df.empty:
                df = df.rename(columns={"symbol": "Ticker", "date": "Fecha", "hour": "Hora",
                                        "epsEstimate": "BPA estimado", "epsActual": "BPA real",
                                        "revenueEstimate": "Ventas est.",
                                        "revenueActual": "Ventas reales"})
                df["Fecha"] = pd.to_datetime(df["Fecha"]).dt.date
                df["Hora"] = df["Hora"].map(lambda h: HOUR_ES.get(h, h or ""))
                return _earnings_relevant(df, day)
        except Exception:
            pass
    try:  # yfinance >= 0.2.58
        cal = yf.Calendars(start=start, end=end + timedelta(days=1))
        df = cal.get_earnings_calendar(limit=200)
        if df is not None and not df.empty:
            df = df.reset_index()
            cols = {c: c for c in df.columns}
            for c in df.columns:
                lc = str(c).lower()
                if lc in ("symbol", "ticker"):
                    cols[c] = "Ticker"
                elif "date" in lc:
                    cols[c] = "FechaHora"
                elif "estimate" in lc:
                    cols[c] = "BPA estimado"
                elif "reported" in lc:
                    cols[c] = "BPA real"
                elif "timing" in lc:
                    cols[c] = "Hora"
            df = df.rename(columns=cols)
            if "FechaHora" in df:
                fh = pd.to_datetime(df["FechaHora"], errors="coerce", utc=True)
                df["Fecha"] = fh.dt.tz_convert(NY).dt.date
                if "Hora" not in df:
                    df["Hora"] = fh.dt.tz_convert(NY).dt.hour.map(
                        lambda h: "Antes de apertura" if h < 9 else
                        ("Tras el cierre" if h >= 16 else "Durante la sesión"))
                else:
                    df["Hora"] = df["Hora"].astype(str).str.lower().map(
                        lambda h: HOUR_ES.get(h, h))
            return _earnings_relevant(df, day)
    except Exception:
        pass
    return pd.DataFrame()


def _earnings_relevant(df: pd.DataFrame, day: date) -> pd.DataFrame:
    """Se queda con lo que mueve el día: hoy antes de apertura / durante, y la última
    sesión anterior tras el cierre."""
    if df.empty or "Fecha" not in df or "Ticker" not in df:
        return pd.DataFrame()
    if "Hora" not in df:
        df["Hora"] = ""
    prev_days = sorted(d for d in df["Fecha"].dropna().unique() if d < day)
    prev = prev_days[-1] if prev_days else None
    hoy = df["Fecha"] == day
    ayer_cierre = (df["Fecha"] == prev) & (df["Hora"] == "Tras el cierre")
    out = df[hoy | ayer_cierre].copy()
    if {"BPA real", "BPA estimado"} <= set(out.columns):
        est = pd.to_numeric(out["BPA estimado"], errors="coerce")
        real = pd.to_numeric(out["BPA real"], errors="coerce")
        out["Sorpresa %"] = (real - est) / est.abs() * 100
    out["Ticker"] = out["Ticker"].astype(str).str.upper()
    return out[~out["Ticker"].str.contains(r"[.^]", regex=True)]


def fda_calendar(key: str) -> pd.DataFrame:
    if not key:
        return pd.DataFrame()
    try:
        raw = _finnhub("fda-advisory-committee-calendar", key)
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        df["fromDate"] = pd.to_datetime(df["fromDate"], errors="coerce")
        today = pd.Timestamp(date.today())
        df = df[(df["fromDate"] >= today - pd.Timedelta(days=1))
                & (df["fromDate"] <= today + pd.Timedelta(days=30))]
        return df.rename(columns={"fromDate": "Fecha", "eventDescription": "Evento",
                                  "url": "Enlace"})[["Fecha", "Evento", "Enlace"]]
    except Exception:
        return pd.DataFrame()


# --------------------------------------------------------------------------------------
# Puntuación
# --------------------------------------------------------------------------------------
def score_row(r) -> float:
    def v(k, cap=None):
        x = r.get(k)
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return 0.0
        return min(abs(x), cap) if cap else abs(x)

    s = 0.0
    s += v("RVOL", 10) * 3                  # volumen relativo proyectado
    s += v("PM % vol. medio", 50) * 0.2     # actividad en pre-market
    s += v("Gap ATR", 5) * 4                # gap medido en ATRs
    s += v("Mov. ATR", 5) * 3               # movimiento medido en ATRs
    s += min(r.get("Noticias", 0) or 0, 5) * 1.5
    s += 3 * len(r.get("Catalizadores") or [])
    s += 8 if r.get("Resultados") else 0
    # Confirmación: el precio está del mismo lado del VWAP que el movimiento
    vw, ch = r.get("vs VWAP %"), r.get("Cambio %")
    if vw is not None and ch is not None and not pd.isna(vw) and not pd.isna(ch):
        if (ch > 0 and vw > 0) or (ch < 0 and vw < 0):
            s += 2
    # Fuerza relativa frente al SPY
    s += 2 if v("vs SPY") >= 3 else 0
    fl = r.get("Float")
    if fl is not None and not pd.isna(fl):
        s += 5 if fl < 20e6 else (3 if fl < 50e6 else 0)
    sp = r.get("Corto % float")
    if sp is not None and not pd.isna(sp):
        s += 4 if sp >= 20 else (2 if sp >= 10 else 0)
    return round(s, 1)


def why(r) -> str:
    parts = []
    if not pd.isna(r.get("Gap %")) and abs(r["Gap %"]) >= 2:
        atr = f" ({r['Gap ATR']:.1f} ATR)" if not pd.isna(r.get("Gap ATR")) else ""
        parts.append(f"Gap {r['Gap %']:+.1f}%{atr}")
    if not pd.isna(r.get("RVOL")) and r["RVOL"] >= 1.5:
        parts.append(f"RVOL {r['RVOL']:.1f}x")
    if not pd.isna(r.get("PM % vol. medio")) and r["PM % vol. medio"] >= 5:
        parts.append(f"PM {r['PM % vol. medio']:.0f}% del vol. medio")
    if r.get("Resultados"):
        parts.append(f"Resultados ({r.get('Hora resultados') or 'hoy'})")
    cats = [c for c in (r.get("Catalizadores") or []) if c != "Resultados"]
    if cats:
        parts.append(", ".join(cats))
    if r.get("Noticias"):
        parts.append(f"{r['Noticias']} noticias")
    vw = r.get("vs VWAP %")
    if vw is not None and not pd.isna(vw):
        parts.append("sobre VWAP" if vw > 0 else "bajo VWAP")
    rs = r.get("vs SPY")
    if rs is not None and not pd.isna(rs) and abs(rs) >= 3:
        parts.append(f"{rs:+.1f}% vs SPY")
    fl = r.get("Float")
    if fl is not None and not pd.isna(fl) and fl < 50e6:
        parts.append(f"float {human(fl)}")
    sp = r.get("Corto % float")
    if sp is not None and not pd.isna(sp) and sp >= 10:
        parts.append(f"corto {sp:.0f}%")
    return " · ".join(parts)


# --------------------------------------------------------------------------------------
# Acciones disponibles en BingX
# --------------------------------------------------------------------------------------
def _bingx_stock_ticker(code: str) -> str | None:
    """'NCSKTSLA2USD/USDT:USDT' o 'NCSKTSLA2USD-USDT' -> 'TSLA'."""
    base = re.split(r"[/:\-]", (code or "").upper())[0]
    if not base.startswith("NCSK"):
        return None
    base = re.sub(r"^NCSK", "", base)
    base = re.sub(r"2USD[T]?$", "", base)
    return base or None


def bingx_stock_tickers(force: bool = False) -> list[str]:
    """Acciones de EE.UU. que se pueden operar en BingX (contratos NCSK...).
    Usa la información pública de BingX (sin clave) y la guarda un día en cache/."""
    today = date.today().isoformat()
    cached = disk_load("bingx_acciones.pkl", None)
    if cached and cached.get("fecha") == today and cached.get("tickers") and not force:
        return cached["tickers"]
    tickers: set[str] = set()
    try:
        import ccxt
        ex = ccxt.bingx({"options": {"defaultType": "swap"}})
        for m in ex.load_markets().values():
            for code in (m.get("id"), m.get("symbol"), m.get("base")):
                t = _bingx_stock_ticker(code)
                if t:
                    tickers.add(t)
                    break
    except Exception:
        pass
    if not tickers:      # sin conexión: usar la última lista guardada
        return (cached or {}).get("tickers", [])
    out = sorted(tickers)
    disk_save("bingx_acciones.pkl", {"fecha": today, "tickers": out})
    return out


# --------------------------------------------------------------------------------------
# Mercado general
# --------------------------------------------------------------------------------------
INDICES = {"SPY": "S&P 500 (SPY)", "QQQ": "Nasdaq 100 (QQQ)", "IWM": "Small caps (IWM)",
           "^VIX": "Volatilidad (VIX)"}


def market_mood(spy_chg: float, vix_chg: float) -> str:
    if pd.isna(spy_chg):
        return ""
    if spy_chg <= -0.7 or (not pd.isna(vix_chg) and vix_chg >= 8):
        return "Mercado débil: los valores al alza fallan más a menudo"
    if spy_chg >= 0.7:
        return "Mercado fuerte: favorece los movimientos al alza"
    return "Mercado neutral"


# --------------------------------------------------------------------------------------
# Registro diario de candidatos y resultados
# --------------------------------------------------------------------------------------
JOURNAL_DIR = HERE / "registro"
JOURNAL_FILE = JOURNAL_DIR / "candidatos.csv"
JOURNAL_COLS = ["Fecha", "Hora detección", "Momento", "Ticker", "Puntuación", "Precio detección",
                "Cierre ant.", "Gap %", "Gap ATR", "RVOL", "PM % vol. medio", "ATR %",
                "vs VWAP %", "vs SPY", "Float", "Corto % float", "Resultados", "Noticias",
                "Catalizadores", "SPY %"]


def load_journal() -> pd.DataFrame:
    try:
        df = pd.read_csv(JOURNAL_FILE, encoding="utf-8-sig")
        df["Fecha"] = pd.to_datetime(df["Fecha"]).dt.date
        return df
    except Exception:
        return pd.DataFrame(columns=JOURNAL_COLS)


def record_picks(df: pd.DataFrame, status: str, spy_chg: float, n: int = 20,
                 min_score: float = 20) -> int:
    """Guarda los N primeros del ranking la primera vez que aparecen cada día.
    Solo en pre-market o con el mercado abierto. Devuelve cuántos se han añadido."""
    if status not in ("Pre-market", "Abierto") or df is None or df.empty:
        return 0
    now = now_ny()
    top = df[(df["Sesión"] == now.date()) & (df["Puntuación"] >= min_score)].head(n)
    if top.empty:
        return 0
    j = load_journal()
    done = set(j.loc[j["Fecha"] == now.date(), "Ticker"]) if not j.empty else set()
    new = top[~top["Ticker"].isin(done)]
    if new.empty:
        return 0
    rows = pd.DataFrame({
        "Fecha": now.date().isoformat(), "Hora detección": now.strftime("%H:%M"),
        "Momento": status, "Ticker": new["Ticker"], "Puntuación": new["Puntuación"],
        "Precio detección": new["Precio"].round(4),
    })
    for c in JOURNAL_COLS:
        if c not in rows and c in new:
            rows[c] = new[c].values
    rows["Catalizadores"] = new["Catalizadores"].map(lambda c: "|".join(c or [])).values
    rows["SPY %"] = spy_chg
    rows = rows.reindex(columns=JOURNAL_COLS)
    JOURNAL_DIR.mkdir(exist_ok=True)
    rows.to_csv(JOURNAL_FILE, mode="a", header=not JOURNAL_FILE.exists(), index=False,
                encoding="utf-8-sig")
    return len(rows)


def evaluate_journal(j: pd.DataFrame, daily: dict) -> pd.DataFrame:
    """Añade qué hizo cada candidato en su sesión (solo días ya cerrados)."""
    if j.empty:
        return j
    today = now_ny().date()
    out = []
    for r in j[j["Fecha"] < today].to_dict("records"):
        d = daily.get(r["Ticker"])
        if d is None or d.empty:
            continue
        idx = pd.to_datetime(d.index)
        row = d[idx.date == r["Fecha"]]
        if row.empty:
            continue
        o, h, l, c = (float(row[k].iloc[0]) for k in ("Open", "High", "Low", "Close"))
        pc = _num(r.get("Cierre ant."))
        det = _num(r.get("Precio detección"))
        atr_pct = _num(r.get("ATR %"))
        rng = (h - l) / pc * 100 if pc > 0 else float("nan")
        r.update({
            "Rango día %": rng,
            "Rango en ATR": rng / atr_pct if atr_pct and atr_pct > 0 else float("nan"),
            "Máx desde apertura %": (h / o - 1) * 100,
            "Mín desde apertura %": (l / o - 1) * 100,
            "Apertura→cierre %": (c / o - 1) * 100,
            "Detección→cierre %": (c / det - 1) * 100 if det > 0 else float("nan"),
        })
        out.append(r)
    return pd.DataFrame(out)


def factor_stats(ev: pd.DataFrame) -> pd.DataFrame:
    """Compara el movimiento medio de los candidatos con y sin cada factor."""
    if ev.empty:
        return pd.DataFrame()
    num = lambda c: pd.to_numeric(ev[c], errors="coerce")
    cats = ev["Catalizadores"].fillna("").astype(str)
    factors = {
        "Gap > 4 %": num("Gap %").abs() > 4,
        "Gap > 1 ATR": num("Gap ATR") > 1,
        "RVOL > 2": num("RVOL") > 2,
        "Vol. PM > 10 % del medio": num("PM % vol. medio") > 10,
        "Float < 50M": num("Float") < 50e6,
        "Corto > 15 % del float": num("Corto % float") > 15,
        "Resultados": ev["Resultados"].astype(str).str.lower().isin(["true", "1"]),
        "Con noticias": num("Noticias") > 0,
        "FDA/Biotech": cats.str.contains("FDA"),
        "Fusión/Adquisición": cats.str.contains("Fusión"),
        "Analistas": cats.str.contains("Analistas"),
        "Ampliación capital": cats.str.contains("Ampliación"),
        "Sobre VWAP al detectar": num("vs VWAP %") > 0,
        "Puntuación ≥ 50": num("Puntuación") >= 50,
    }
    rng = num("Rango en ATR")
    follow = num("Apertura→cierre %").abs()
    rows = []
    for name, m in factors.items():
        m = m.fillna(False)
        if m.sum() < 1:
            continue
        rows.append({
            "Factor": name, "Casos": int(m.sum()),
            "Rango en ATR (con)": rng[m].mean(), "Rango en ATR (sin)": rng[~m].mean(),
            "Diferencia": rng[m].mean() - rng[~m].mean(),
            "|Apertura→cierre| % (con)": follow[m].mean(),
        })
    return pd.DataFrame(rows).sort_values("Diferencia", ascending=False)


# --------------------------------------------------------------------------------------
# Alertas
# --------------------------------------------------------------------------------------
def load_sent() -> set[str]:
    try:
        d = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
        if d.get("fecha") == date.today().isoformat():
            return set(d.get("enviadas", []))
    except Exception:
        pass
    return set()


def save_sent(sent: set[str]) -> None:
    try:
        ALERTS_FILE.write_text(json.dumps({"fecha": date.today().isoformat(),
                                           "enviadas": sorted(sent)}), encoding="utf-8")
    except Exception:
        pass


def telegram_detect_chat(token: str) -> tuple[str, str]:
    """Busca el Chat ID en los últimos mensajes enviados al bot."""
    if not token:
        return "", "Primero pega el token del bot."
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
        data = r.json()
    except Exception as e:
        return "", f"No se pudo conectar con Telegram: {e}"
    if not data.get("ok"):
        return "", "Token incorrecto. Cópialo de nuevo desde @BotFather."
    for upd in reversed(data.get("result", [])):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            return str(chat["id"]), ""
    return "", ("No hay mensajes. Abre tu bot en Telegram, pulsa Iniciar (o escribe "
                "'hola') y vuelve a pulsar este botón.")


ALERT_LOG = HERE / "alertas_hoy.json"


def log_alerts(msgs: list[str]) -> None:
    log = load_alert_log()
    hora = datetime.now(ZoneInfo("Europe/Madrid")).strftime("%H:%M")
    log += [{"hora": hora, "texto": m} for m in msgs]
    try:
        ALERT_LOG.write_text(json.dumps({"fecha": date.today().isoformat(), "log": log[-200:]},
                                        ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def load_alert_log() -> list[dict]:
    try:
        d = json.loads(ALERT_LOG.read_text(encoding="utf-8"))
        return d.get("log", []) if d.get("fecha") == date.today().isoformat() else []
    except Exception:
        return []


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": text,
                                "disable_web_page_preview": True}, timeout=10)
        return r.ok
    except Exception:
        return False

# ======================================
# 암호화폐 섹터별 차트 — 웹페이지 빌드 (GitHub Actions 배치용)
#   · 데이터 수신 → 지표·추세선 계산 → 차트 PNG → site/ 폴더에 웹페이지 생성
#   · 시세: 바이낸스 현물 USDT 마켓(기본) → 크라켄 USD 마켓(대체)
#     바이낸스는 미국 IP를 막을 수 있어(GitHub 서버는 미국), 막히면 자동으로 크라켄만 사용
#   · 일봉은 UTC 00:00(한국 09:00)에 바뀜
#     자동 실행(매일 09:07): 막 끝난 어제 일봉까지 / 수동 실행: 진행 중인 오늘 일봉까지 포함
#   · 로컬 실행: python build_site.py  →  site/index.html 을 브라우저로 열기
# ======================================
import os, re, io, sys, json, math, time, shutil, threading
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests
import matplotlib as mpl
mpl.use("Agg")
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter
from matplotlib.lines import Line2D

from coins import (SECTIONS, AUTO_TOP_N, AUTO_SECTION, AUTO_AFTER, OUT_OF_LIST,
                   EXCLUDE_FROM_TOP, KRAKEN_ALIAS)

# --- 설정 ---
ROOT      = os.path.dirname(os.path.abspath(__file__))
SITE_DIR  = os.path.join(ROOT, "site")
WEB_DIR   = os.path.join(ROOT, "web")
KST       = dt.timezone(dt.timedelta(hours=9))

DISPLAY_BARS = 180                    # 차트에 보여줄 일수 (약 6개월)
MA_LIST      = [5, 20, 60, 120]       # 이동평균. 코인에서 흔한 [7, 25, 99] 등으로 바꿔도 됨
YEAR_BARS    = 365                    # 1년 고가 계산 구간
TREND_LONG   = dict(lookback=120, recent=5, min_span=20)   # 장기 추세선 (실선)
TREND_SHORT  = dict(lookback=45,  recent=3, min_span=8)    # 단기 추세선 (점선)
SHOW_SHORT_TREND = True
TREND_EXTEND = 8
BREAK_MARGIN = 0.01

FIG_SIZE, FIG_DPI = (10, 5.4), 90
MAX_WORKERS   = 6
RENDER_PROCS  = max(1, min(4, os.cpu_count() or 1))
MIN_SUCCESS   = 0.6                   # (상장된 코인 중) 수신 성공률이 이보다 낮으면 배포 안 함

UP_COLOR, DOWN_COLOR = "#E5383B", "#1D63D8"
MA_COLORS = {5: "#9B5DE5", 7: "#9B5DE5", 20: "#F4A100", 25: "#F4A100", 60: "#2A9D8F",
             99: "#7F8C8D", 120: "#7F8C8D", 50: "#2A9D8F", 200: "#7F8C8D", 10: "#C77DFF"}
SUPPORT_COLOR, RESIST_COLOR = "#00A6C8", "#E0479E"
FETCH_BARS = max(DISPLAY_BARS + max(MA_LIST), YEAR_BARS) + 30

BINANCE = "https://data-api.binance.vision"     # 바이낸스 공개 시세 전용 주소
KRAKEN  = "https://api.kraken.com"


# --- 한글 폰트 (Python 3.12에서도 동작하도록 폰트 파일을 직접 등록) ---
def _setup_korean_font():
    import importlib.util
    from matplotlib import font_manager as fm
    cands = []
    try:
        spec = importlib.util.find_spec("koreanize_matplotlib")
        for loc in (spec.submodule_search_locations or []) if spec else []:
            cands.append(os.path.join(loc, "fonts", "NanumGothic.ttf"))
    except Exception:
        pass
    cands += [
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/malgun.ttf",
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    ]
    for path in cands:
        if not os.path.exists(path):
            continue
        try:
            fm.fontManager.addfont(path)
            bold = os.path.join(os.path.dirname(path), "NanumGothicBold.ttf")   # 제목용 굵은 글꼴
            if os.path.exists(bold):
                fm.fontManager.addfont(bold)
            name = fm.FontProperties(fname=path).get_name()
            mpl.rcParams["font.family"] = name
            mpl.rcParams["axes.unicode_minus"] = False
            print(f"[폰트] {name} ({path})")
            return True
        except Exception:
            continue
    print("[경고] 한글 폰트를 찾지 못함 → 차트 글자를 영문·숫자로만 표시")
    return False

KFONT = _setup_korean_font()
TXT = ({"ma": "{}일", "sup": "지지 추세선", "res": "저항 추세선", "live": "진행 중"} if KFONT else
       {"ma": "MA{}", "sup": "Support", "res": "Resistance", "live": "live"})


# --- 데이터 수신 ---
_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; sector-charts/1.0)"})


class Unavailable(Exception):
    """그 거래소에 해당 코인 마켓이 없음 (오류가 아니라 '상장 안 됨')"""


class _Pacer:
    """여러 스레드가 동시에 불러도 요청 간격을 지키게 하는 간단한 속도 제한"""
    def __init__(self, gap):
        self.gap, self.t, self.lock = gap, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            w = self.t + self.gap - time.monotonic()
            if w > 0:
                time.sleep(w)
            self.t = time.monotonic()

BINANCE_PACE = _Pacer(0.06)      # 바이낸스: 넉넉한 가중치 한도
KRAKEN_PACE  = _Pacer(1.1)       # 크라켄 공개 API: 대략 초당 1회


def _clean(df):
    df = df.copy()
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    df = df.dropna(subset=["Close"])
    df = df[df["Close"] > 0].sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for col in ["Open", "High", "Low"]:
        bad = df[col].isna() | (df[col] <= 0)
        df.loc[bad, col] = df.loc[bad, "Close"]
    df["High"] = df[["Open", "High", "Low", "Close"]].max(axis=1)
    df["Low"] = df[["Open", "High", "Low", "Close"]].min(axis=1)
    df["Volume"] = df["Volume"].fillna(0)        # 코인은 Volume = 달러 거래대금
    return df


def _binance_get(path, params, timeout=12):
    BINANCE_PACE.wait()
    return _session.get(BINANCE + path, params=params, timeout=timeout)


def binance_probe():
    """바이낸스 접속 가능 여부 (미국 IP 차단이면 451/403)"""
    err = "응답 없음"
    for _ in range(2):
        try:
            r = _binance_get("/api/v3/ping", {})
            if r.status_code == 200:
                return True, ""
            if r.status_code in (403, 451):
                return False, f"접속 제한 (HTTP {r.status_code})"
            err = f"HTTP {r.status_code}"
        except Exception as e:
            err = str(e)[:120]
        time.sleep(1)
    return False, err


def fetch_binance(sym, limit=FETCH_BARS):
    r = None
    for attempt in range(3):
        r = _binance_get("/api/v3/klines", {"symbol": sym + "USDT", "interval": "1d", "limit": limit})
        if r.status_code == 200:
            rows = r.json()
            if not rows:
                raise Unavailable("바이낸스 데이터 없음")
            # [시작시각ms, 시가, 고가, 저가, 종가, 거래량, 종료시각, 거래대금(USDT), ...]
            df = pd.DataFrame([[x[0], x[1], x[2], x[3], x[4], x[7]] for x in rows],
                              columns=["t", "Open", "High", "Low", "Close", "Volume"])
            df.index = pd.to_datetime(df.pop("t"), unit="ms")
            return _clean(df)
        if r.status_code == 400:
            raise Unavailable("바이낸스 USDT 마켓 없음")
        if r.status_code in (403, 451):
            raise RuntimeError(f"바이낸스 접속 제한 (HTTP {r.status_code})")
        time.sleep(2 * (attempt + 1))                     # 418/429 등 → 잠시 후 재시도
    raise RuntimeError(f"바이낸스 HTTP {r.status_code if r is not None else '-'}")


def fetch_kraken(sym):
    pair = KRAKEN_ALIAS.get(sym, sym) + "USD"
    last = "응답 없음"
    for attempt in range(4):
        KRAKEN_PACE.wait()
        try:
            r = _session.get(KRAKEN + "/0/public/OHLC", params={"pair": pair, "interval": 1440}, timeout=15)
        except Exception as e:
            last = str(e)[:100]; time.sleep(2); continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}"; time.sleep(2 * (attempt + 1)); continue
        j = r.json()
        err = ";".join(j.get("error") or [])
        if err:
            if "Unknown asset pair" in err:
                raise Unavailable("크라켄 USD 마켓 없음")
            last = err[:100]
            time.sleep(3 * (attempt + 1)); continue           # Too many requests 등
        res = j.get("result") or {}
        key = next((k for k in res if k != "last"), None)
        rows = res.get(key) or []
        if not rows:
            raise Unavailable("크라켄 데이터 없음")
        # [시작시각s, 시가, 고가, 저가, 종가, VWAP, 거래량, 체결수] → 거래대금 ≈ VWAP × 거래량
        recs = []
        for x in rows:
            vwap, vol, close = float(x[5]), float(x[6]), float(x[4])
            recs.append([x[0], x[1], x[2], x[3], x[4], (vwap if vwap > 0 else close) * vol])
        df = pd.DataFrame(recs, columns=["t", "Open", "High", "Low", "Close", "Volume"])
        df.index = pd.to_datetime(df.pop("t"), unit="s")
        return _clean(df)
    raise RuntimeError(f"크라켄 {last}")


def fetch_one(sym, use_binance):
    notes, hard = [], False
    if use_binance:
        try:
            return dict(df=fetch_binance(sym), source="BINANCE")
        except Unavailable as e:
            notes.append(str(e))
        except Exception as e:
            notes.append(str(e)[:120]); hard = True
    try:
        return dict(df=fetch_kraken(sym), source="KRAKEN")
    except Unavailable as e:
        notes.append(str(e))
        if not hard:
            raise Unavailable(" / ".join(notes))
    except Exception as e:
        notes.append(str(e)[:120])
    raise RuntimeError(" / ".join(notes))


def binance_top(n):
    """바이낸스 USDT 마켓 24시간 거래대금 상위 n개 [(심볼, 거래대금$)]"""
    r = _binance_get("/api/v3/ticker/24hr", {"type": "MINI"}, timeout=20)
    r.raise_for_status()
    out = []
    for t in r.json():
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        qv = float(t.get("quoteVolume") or 0)
        if base in EXCLUDE_FROM_TOP or qv <= 0 or int(t.get("count") or 0) == 0:
            continue
        out.append((base, qv))
    out.sort(key=lambda x: -x[1])
    return out[:n]


# --- 지표 · 자동 추세선 ---
def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _hull(points, lower):
    h = []
    for p in points:
        while len(h) >= 2 and ((_cross(h[-2], h[-1], p) <= 0) if lower else (_cross(h[-2], h[-1], p) >= 0)):
            h.pop()
        h.append(p)
    return h


def trend_line(high, low, kind, lookback, recent, min_span):
    """저가(지지)/고가(저항)의 볼록껍질에서 두 접점 간격이 min_span 이상인 가장 최근 변 → (x1, y1, 기울기)"""
    n = len(low)
    start, stop = max(0, n - lookback), n - recent
    if stop - start < min_span + 1:
        return None
    arr = low if kind == "support" else high
    pts = [(i, float(arr[i])) for i in range(start, stop) if np.isfinite(arr[i])]
    if len(pts) < 2:
        return None
    hull = _hull(pts, lower=(kind == "support"))
    for a, b in reversed(list(zip(hull[:-1], hull[1:]))):
        if b[0] - a[0] >= min_span:
            return (a[0], a[1], (b[1] - a[1]) / (b[0] - a[0]))
    return None


def _line_at(line, x):
    return None if line is None else line[1] + line[2] * (x - line[0])


def calc_stats(df):
    c, v = df["Close"], df["Volume"]
    last = float(c.iloc[-1])
    pct = lambda k: (last / c.iloc[-1 - k] - 1) * 100 if len(c) > k else np.nan
    ma20 = c.rolling(20).mean().iloc[-1]
    vavg = v.iloc[-21:-1].mean() if len(v) > 21 else np.nan
    disp = df.iloc[-DISPLAY_BARS:]
    hi, lo = disp["High"].values, disp["Low"].values
    sup = trend_line(hi, lo, "support", **TREND_LONG)
    res = trend_line(hi, lo, "resistance", **TREND_LONG)
    xl = len(disp) - 1
    sup_v, res_v = _line_at(sup, xl), _line_at(res, xl)
    if res_v is not None and last > res_v * (1 + BREAK_MARGIN):
        trend = "저항선 돌파"
    elif sup_v is not None and last < sup_v * (1 - BREAK_MARGIN):
        trend = "지지선 이탈"
    else:
        trend = "추세선 안"
    return {
        "가격($)": last,
        "1일%": pct(1), "7일%": pct(7), "30일%": pct(30),
        "1년고가대비%": (last / df["High"].iloc[-YEAR_BARS:].max() - 1) * 100,
        "MA20이격%": (last / ma20 - 1) * 100 if pd.notna(ma20) else np.nan,
        "거래대금($M)": float(v.iloc[-1]) / 1e6,
        "거래량배수": float(v.iloc[-1]) / vavg if vavg and vavg > 0 else np.nan,
        "추세선": trend,
        "마지막일": df.index[-1].strftime("%Y-%m-%d"),
    }


# --- 차트 ---
def price_decimals(p):
    """유효숫자 약 5자리: 112,345 / 4,123.5 / 172.34 / 2.4512 / 0.000012340"""
    if not p or not np.isfinite(p) or p <= 0:
        return 2
    return int(min(10, max(0, 5 - math.floor(math.log10(p)) - 1)))


def fmt_price(p):
    return f"{p:,.{price_decimals(p)}f}"


def _fmt_usd(y, _=None):
    if y >= 1e9: return f"{y/1e9:.1f}B"
    if y >= 1e6: return f"{y/1e6:.0f}M"
    if y >= 1e3: return f"{y/1e3:.0f}K"
    return f"{y:.0f}"


def _compress_png(png):
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(png)).convert("RGB")
        q = im.quantize(colors=128, method=0, dither=0)
        out = io.BytesIO()
        q.save(out, "PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return png


def render_chart(sym, name, df_full, live_date=None):
    d = df_full.copy()
    for w in MA_LIST:
        d[f"MA{w}"] = d["Close"].rolling(w).mean()
    d = d.iloc[-DISPLAY_BARS:]
    n = len(d)
    x = np.arange(n)
    o, h, l, c, v = (d[k].values for k in ["Open", "High", "Low", "Close", "Volume"])
    colors = np.where(c >= o, UP_COLOR, DOWN_COLOR)

    fig = Figure(figsize=FIG_SIZE, dpi=FIG_DPI, facecolor="white")
    gs = fig.add_gridspec(2, 1, height_ratios=[3.3, 1], hspace=0.04)
    ax = fig.add_subplot(gs[0])
    axv = fig.add_subplot(gs[1], sharex=ax)

    pmin, pmax = float(np.nanmin(l)), float(np.nanmax(h))
    pad = (pmax - pmin) * 0.07 or pmax * 0.02
    ylo, yhi = pmin - pad, pmax + pad

    ax.vlines(x, l, h, colors=colors, linewidth=0.9, zorder=2)
    body_h = np.maximum(np.abs(c - o), (yhi - ylo) * 0.0015)
    ax.bar(x, body_h, bottom=np.minimum(o, c), width=0.62, color=colors,
           edgecolor=colors, linewidth=0.5, zorder=3)

    handles = []
    for w in MA_LIST:
        col = MA_COLORS.get(w, "#555555")
        ax.plot(x, d[f"MA{w}"].values, color=col, linewidth=1.1 if w > 7 else 0.9, zorder=4)
        handles.append(Line2D([0], [0], color=col, lw=1.5, label=TXT["ma"].format(w)))

    x_end = n - 1 + TREND_EXTEND
    sets = [(TREND_LONG, "-", 1.6, 0.95)]
    if SHOW_SHORT_TREND:
        sets.append((TREND_SHORT, "--", 1.1, 0.85))
    drew = set()
    for prm, ls, lw, alpha in sets:
        for kind, col in [("support", SUPPORT_COLOR), ("resistance", RESIST_COLOR)]:
            line = trend_line(h, l, kind, **prm)
            if line is None:
                continue
            ax.plot([line[0], x_end], [line[1], _line_at(line, x_end)], color=col,
                    linestyle=ls, linewidth=lw, alpha=alpha, zorder=5)
            drew.add(kind)
    if "support" in drew:
        handles.append(Line2D([0], [0], color=SUPPORT_COLOR, lw=1.6, label=TXT["sup"]))
    if "resistance" in drew:
        handles.append(Line2D([0], [0], color=RESIST_COLOR, lw=1.6, label=TXT["res"]))

    last, prev = c[-1], (df_full["Close"].iloc[-2] if len(df_full) > 1 else c[-1])
    chg = (last / prev - 1) * 100
    lc = UP_COLOR if chg > 0 else DOWN_COLOR if chg < 0 else "#555555"
    ax.axhline(last, color=lc, linestyle=":", linewidth=0.9, zorder=1)
    ax.annotate(fmt_price(last), xy=(1.0, last), xycoords=("axes fraction", "data"),
                xytext=(3, 0), textcoords="offset points", va="center", ha="left",
                fontsize=8.5, color="white", fontweight=600,
                bbox=dict(boxstyle="square,pad=0.25", fc=lc, ec=lc), annotation_clip=False)

    step = (yhi - ylo) / 6
    ydec = max(0, -math.floor(math.log10(step)) + 1) if step > 0 else 2
    ax.set_xlim(-1, x_end + 1)
    ax.set_ylim(ylo, yhi)
    ax.yaxis.tick_right()
    ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:,.{ydec}f}"))
    ax.tick_params(axis="x", labelbottom=False, length=0)
    ax.tick_params(axis="y", labelsize=8.5, length=0, pad=4)
    ax.grid(True, color="#E6E6E6", linewidth=0.6)
    for s in ["top", "left"]:
        ax.spines[s].set_visible(False)
    for s in ["right", "bottom"]:
        ax.spines[s].set_color("#CCCCCC")
    ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=7.5,
              ncol=len(handles), frameon=False, handlelength=1.6, columnspacing=1.0, borderaxespad=0.1)

    title = f"{name}  {sym}" if KFONT else sym
    day = f"{d.index[-1]:%Y-%m-%d}" + (f" {TXT['live']}" if live_date is not None and d.index[-1] >= live_date else "")
    ax.set_title(title, loc="left", fontsize=12, fontweight=600, pad=19)   # 나눔고딕 Bold = 600
    ax.set_title(f"${fmt_price(last)}  {chg:+.2f}%   ({day})", loc="right", fontsize=10.5, color=lc, pad=19)

    axv.bar(x, v, width=0.62, color=colors, alpha=0.55, zorder=2)
    if n >= 20:
        axv.plot(x, pd.Series(v).rolling(20).mean().values, color="#555555", linewidth=0.9, zorder=3)
    axv.yaxis.tick_right()
    axv.yaxis.set_major_formatter(FuncFormatter(_fmt_usd))
    axv.tick_params(axis="y", labelsize=7.5, length=0, pad=4)
    axv.tick_params(axis="x", labelsize=8.5, length=0)
    axv.set_ylim(0, max(float(np.nanmax(v)), 1) * 1.15)
    axv.grid(True, axis="x", color="#E6E6E6", linewidth=0.6)
    for s in ["top", "left"]:
        axv.spines[s].set_visible(False)
    for s in ["right", "bottom"]:
        axv.spines[s].set_color("#CCCCCC")

    per = d.index.to_period("M")
    ticks = [i for i in range(n) if i == 0 or per[i] != per[i - 1]]
    if len(ticks) > 1 and ticks[1] - ticks[0] < 8:
        ticks = ticks[1:]
    axv.set_xticks(ticks)
    axv.set_xticklabels([f"{d.index[i]:%y.%m}" for i in ticks])

    fig.subplots_adjust(left=0.015, right=0.885, top=0.875, bottom=0.07)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=FIG_DPI, facecolor="white")
    return _compress_png(buf.getvalue())


def _render_job(args):
    sym, name, df, out_path, live_date = args
    stats = calc_stats(df)
    with open(out_path, "wb") as f:
        f.write(render_chart(sym, name, df, live_date))
    return sym, stats


# --- 섹터 강도 ---
def sector_table(summary, curated):
    stk = summary[summary["섹션"].isin(curated)]
    if not len(stk):
        return []
    sec = (stk.groupby("섹션", sort=False)
              .agg(코인수=("심볼", "count"),
                   **{"평균 1일%": ("1일%", "mean"), "평균 7일%": ("7일%", "mean"),
                      "평균 30일%": ("30일%", "mean"), "거래대금 합($M)": ("거래대금($M)", "sum")},
                   MA20위=("MA20이격%", lambda s: int((s > 0).sum())),
                   저항돌파=("추세선", lambda s: int((s == "저항선 돌파").sum())),
                   지지이탈=("추세선", lambda s: int((s == "지지선 이탈").sum())))
              .reset_index()
              .sort_values("평균 7일%", ascending=False))
    return _records(sec)


def _records(df):
    out = []
    for r in df.to_dict("records"):
        rec = {}
        for k, v in r.items():
            if isinstance(v, (np.integer,)):
                v = int(v)
            elif isinstance(v, (float, np.floating)):
                v = None if pd.isna(v) else float(f"{float(v):.6g}")
            elif isinstance(v, (bool, np.bool_)):
                v = bool(v)
            rec[k] = v
        out.append(rec)
    return out


def _gh_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


# --- 메인 ---
def main():
    trigger = os.environ.get("TRIGGER") or os.environ.get("GITHUB_EVENT_NAME") or "local"
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    m = re.search(r"\.github/workflows/([^@]+)@", os.environ.get("GITHUB_WORKFLOW_REF", ""))
    workflow = m.group(1) if m else "daily.yml"
    t_start = time.time()

    info = {}                                         # 심볼 → (이름, 설명, 섹션)
    for sec, items in SECTIONS:
        for sym, nm, desc in items:
            info.setdefault(sym.upper(), (nm, desc, sec))
    curated = list(info)

    print("[0/3] 바이낸스 접속 확인")
    use_binance, why = binance_probe()
    print("      바이낸스 사용" if use_binance else f"      바이낸스 사용 불가 ({why}) → 크라켄만 사용")

    top, top_note = [], ""
    if AUTO_TOP_N > 0:
        if use_binance:
            try:
                top = binance_top(AUTO_TOP_N)
                print(f"      거래대금 상위 {len(top)}: " + ", ".join(s for s, _ in top))
            except Exception as e:
                top_note = f"거래대금 상위 목록을 받지 못함 ({str(e)[:80]})"
        else:
            top_note = "바이낸스 접속 제한으로 거래대금 상위 자동 섹션 생략"
    top_rank = {s: i + 1 for i, (s, _) in enumerate(top)}
    syms = curated + [s for s, _ in top if s not in info]

    print(f"[1/3] 일봉 데이터 수신: {len(syms)}개 (트리거: {trigger})")
    data, failed, missing = {}, {}, {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(fetch_one, s, use_binance): s for s in syms}
        for f in as_completed(futs):
            s = futs[f]
            try:
                data[s] = f.result()
            except Unavailable as e:
                missing[s] = str(e)
            except Exception as e:
                failed[s] = str(e)[:200]
    src_n = {k: sum(1 for d in data.values() if d["source"] == k) for k in ("BINANCE", "KRAKEN")}
    print(f"      성공 {len(data)} (바이낸스 {src_n['BINANCE']} · 크라켄 {src_n['KRAKEN']}) / "
          f"상장 없음 {len(missing)} / 실패 {len(failed)}")
    for s, e in missing.items():
        print(f"      상장 없음: {s} -> {e}")
    for s, e in failed.items():
        print(f"      실패: {s} -> {e}")
    listed = len(syms) - len(missing)
    if not data or len(data) < listed * MIN_SUCCESS:
        print(f"[중단] 수신 성공률이 {MIN_SUCCESS:.0%} 미만입니다. 배포하지 않습니다 (기존 페이지 유지).")
        sys.exit(1)

    # 진행 중인 오늘 일봉: 자동 실행이면 빼고(어제 마감 일봉 기준), 수동 실행이면 포함
    today_utc = pd.Timestamp(dt.datetime.now(dt.timezone.utc).date())
    live = trigger != "schedule"
    for s, d in data.items():
        if not live and len(d["df"]) > 1 and d["df"].index[-1] >= today_utc:
            d["df"] = d["df"].iloc[:-1]
    last_dates = pd.Series({s: d["df"].index[-1] for s, d in data.items()})
    candle_date = last_dates.mode().iloc[0]
    live = live and candle_date >= today_utc

    if os.path.isdir(SITE_DIR):
        shutil.rmtree(SITE_DIR)
    os.makedirs(os.path.join(SITE_DIR, "charts"))
    os.makedirs(os.path.join(SITE_DIR, "data"))

    names = {s: info[s][0] if s in info else s for s in data}
    print(f"[2/3] 차트 생성: {len(data)}개 (프로세스 {RENDER_PROCS}개)")
    jobs = [(s, names[s], data[s]["df"], os.path.join(SITE_DIR, "charts", f"{s}.png"),
             today_utc if live else None) for s in syms if s in data]
    stats = {}
    t0 = time.time()
    try:
        if RENDER_PROCS > 1:
            with ProcessPoolExecutor(max_workers=RENDER_PROCS) as ex:
                for s, st in ex.map(_render_job, jobs, chunksize=4):
                    stats[s] = st
        else:
            raise RuntimeError("single")
    except Exception as e:
        if str(e) != "single":
            print(f"      병렬 생성 실패 → 순차 생성 ({e})")
        for job in jobs:
            if job[0] in stats:
                continue
            try:
                s, st = _render_job(job)
                stats[s] = st
            except Exception as e2:
                failed[job[0]] = f"차트 오류: {e2}"[:200]
    print(f"      완료 {len(stats)}개 ({time.time() - t0:.0f}초)")

    # 결과표 행 (코인당 한 줄) + 차트 섹션
    rows = []
    for s in syms:
        if s not in stats:
            continue
        nm, desc, sec = info.get(s, (s, f"바이낸스 거래대금 {top_rank.get(s, '-')}위", OUT_OF_LIST))
        rows.append({"섹션": sec, "코인": nm, "심볼": s, "설명": desc, **stats[s],
                     "출처": data[s]["source"], "_top": top_rank.get(s)})
    summary = pd.DataFrame(rows)

    chart_sections = []
    for sec, items in SECTIONS:
        chart_sections.append({"name": sec, "auto": False, "codes": [x[0].upper() for x in items if x[0].upper() in stats]})
        if sec == AUTO_AFTER and top:
            chart_sections.append({"name": AUTO_SECTION, "auto": True, "codes": [t for t, _ in top if t in stats]})
    if top and not any(c["auto"] for c in chart_sections):
        chart_sections.insert(0, {"name": AUTO_SECTION, "auto": True, "codes": [t for t, _ in top if t in stats]})

    now = dt.datetime.now(KST)
    meta = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "updated_iso": now.isoformat(timespec="seconds"),
        "build_id": now.strftime("%Y%m%d%H%M%S"),
        "candle_date": candle_date.strftime("%Y-%m-%d"),
        "live": bool(live),
        "trigger": trigger,
        "repo": repo, "branch": branch, "workflow": workflow,
        "n_total": len(syms), "n_ok": len(stats), "sources": src_n,
        "binance": {"ok": use_binance, "why": why},
        "top_n": len(top), "top_note": top_note,
        "failed": [{"code": s, "name": names.get(s, info.get(s, (s,))[0]), "error": e} for s, e in failed.items()],
        "missing": [{"code": s, "name": info.get(s, (s,))[0], "error": e} for s, e in missing.items()],
        "ma": MA_LIST, "display_bars": DISPLAY_BARS,
        "build_seconds": round(time.time() - t_start),
    }
    payload = {
        "meta": meta,
        "sections": chart_sections,
        "sector_table": sector_table(summary, [s for s, _ in SECTIONS]) if len(summary) else [],
        "rows": _records(summary) if len(summary) else [],
    }
    js = json.dumps(payload, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")

    print("[3/3] 페이지 생성")
    with open(os.path.join(SITE_DIR, "data", "summary.js"), "w", encoding="utf-8") as f:
        f.write("window.__krsc_summary && window.__krsc_summary(" + js + ");\n")
    shutil.copy(os.path.join(WEB_DIR, "index.html"), os.path.join(SITE_DIR, "index.html"))
    open(os.path.join(SITE_DIR, ".nojekyll"), "w").close()

    size_mb = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(SITE_DIR) for f in fs) / 1e6
    print(f"      site/ {size_mb:.1f}MB · 업데이트 {meta['updated_at']} · 기준 일봉 {meta['candle_date']}"
          f"{' (진행 중)' if live else ''} · 총 {meta['build_seconds']}초")
    _gh_output(skip="false", ref_date=meta["candle_date"])


if __name__ == "__main__":
    main()

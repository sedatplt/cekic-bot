"""
STRATEJİ BOTU  -  Yön -> Geri çekilme -> Mum tetiği  (günde ~3 işlem hedefi)

MODE ortam değişkeni:
  backtest : stratejiyi geçmiş veride işlem işlem simüle eder, eşiği/çıkış tipini seçer,
             sonuçları Telegram'a yazar ve strategy_config.json'u günceller.
  scan     : her yeni 1 saatlik mum kapanışında sinyal arar (giriş/stop/hedef/boyut mesajı),
             sanal pozisyonları takip eder ve çıkışları bildirir.
  list     : Hyperliquid market adlarını Telegram'a gönderir.

Gizli anahtarlar: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.  İsteğe bağlı: ACCOUNT_USD (hesap büyüklüğü).
"""
import os
import json
import math
import time
from collections import defaultdict, deque

import numpy as np
import requests

# =====================================================================
#  AYARLAR
# =====================================================================
CRYPTO = ["BTC", "ETH", "SOL", "XRP", "BNB", "AVAX", "HYPE", "ZEC", "USELESS",
          "kPEPE", "PUMP", "UNI", "ARB", "PAXG", "JUP", "INJ"]
TRADFI = ["xyz:NVDA", "xyz:TSLA", "xyz:AAPL", "xyz:MSFT", "xyz:AMZN", "xyz:GOOGL",
          "xyz:META", "xyz:HOOD", "xyz:TSM", "xyz:AMD", "xyz:PLTR", "xyz:MU",
          "xyz:COIN", "xyz:MSTR", "xyz:AVGO", "xyz:CRCL", "xyz:INTC",
          "xyz:XYZ100", "xyz:SP500",
          "xyz:GOLD", "xyz:SILVER", "xyz:PALLADIUM", "xyz:COPPER",
          "xyz:CL", "xyz:BRENTOIL", "xyz:NATGAS", "xyz:HO"]
MANUAL = CRYPTO + TRADFI
AUTO_DEXES = ["", "xyz"]
AUTO_TOP = 10                  # hacme göre eklenecek ek market sayısı

# --- strateji ---
DON_N = (10, 20, 40, 80)       # 4 saatlik Donchian pencereleri (çoklu model)
TREND_MIN = 0.5                # |trend skoru| bu değerin altındaysa işlem yok
PB_LOOK, PB_MIN = 12, 1.0      # geri çekilme: son 12 mumun zirvesinden >= 1 ATR
STOP_MIN_ATR, STOP_MAX_ATR = 1.0, 3.0
TRAIL_ATR = 3.0                # iz süren stop mesafesi (ATR)
R_TARGET = 2.0                 # sabit hedef modunda hedef (R)
MAX_BARS = 48                  # en fazla 48 mum (saat) açık kalsın
COST = 0.001                   # gidiş-dönüş maliyet varsayımı (%0,10)
TARGET_PER_DAY = 3.0           # hedef ortalama işlem/gün
MAX_PER_DAY = 5                # günlük üst sınır
MAX_OPEN = 4                   # aynı anda en fazla açık pozisyon
DAILY_STOP_R = -3.0            # günlük zarar sınırı (R)

# --- test ---
DISC_FRAC = 0.60               # zaman çizelgesinin ilk %60'ı keşif, kalanı doğrulama
MIN_HOLD_TRADES = 60
THRESHOLDS = list(range(0, 101, 5))

# --- canlı ---
ACCOUNT_USD = float(os.environ.get("ACCOUNT_USD", "100"))
RISK_PCT = 1.0                 # işlem başına hesap riski (%)
MAX_LEV = 5.0
PAPER = os.environ.get("PAPER", "1") == "1"   # doğrulanmamışsa "kağıt işlem" olarak bildir

TF_SEC = {"1h": 3600, "4h": 14400}
WEIGHT_BUDGET = 1000
API_URL = "https://api.hyperliquid.xyz/info"
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CONFIG_FILE = "strategy_config.json"
STATE_FILE = "strateji_state.json"


# =====================================================================
#  TELEGRAM + HYPERLIQUID VERİSİ
# =====================================================================
def notify(text):
    print(text)
    if not (TOKEN and CHAT_ID):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text}, timeout=15)
    except requests.RequestException as e:
        print("Telegram hatası:", e)


def notify_long(text, limit=3500):
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > limit:
            notify(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        notify(chunk)


_calls = deque()


def throttle(weight):
    while True:
        now = time.time()
        while _calls and now - _calls[0][0] > 60:
            _calls.popleft()
        if sum(w for _, w in _calls) + weight <= WEIGHT_BUDGET:
            break
        time.sleep(max(0.5, 60 - (now - _calls[0][0]) + 0.2))
    _calls.append((time.time(), weight))


def hl_post(body, weight=20):
    for attempt in range(4):
        throttle(weight)
        r = requests.post(API_URL, json=body, timeout=30)
        if getattr(r, "status_code", 200) == 429:
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Hyperliquid hız sınırı (429) aşılamadı")


def get_dexes():
    try:
        return [""] + [d["name"] for d in hl_post({"type": "perpDexs"}) if d and d.get("name")]
    except Exception as e:
        print("Sağlayıcı listesi alınamadı:", e)
        return ["", "xyz"]


def fetch_universe(dex):
    body = {"type": "metaAndAssetCtxs"}
    if dex:
        body["dex"] = dex
    meta, ctxs = hl_post(body)
    out = []
    for u, c in zip(meta["universe"], ctxs):
        if u.get("isDelisted"):
            continue
        name = u["name"]
        if dex and ":" not in name:
            name = f"{dex}:{name}"
        out.append((float(c.get("dayNtlVlm") or 0), name))
    return out


def build_symbols(auto_top=AUTO_TOP):
    ranked, everything = [], set()
    for dex in get_dexes():
        try:
            items = fetch_universe(dex)
        except Exception as e:
            print("Market listesi alınamadı:", dex or "ana", e)
            continue
        everything.update(n for _, n in items)
        if dex in AUTO_DEXES:
            ranked += items
    ranked.sort(reverse=True)
    syms = list(MANUAL)
    if auto_top > 0:
        syms += [n for _, n in ranked[:auto_top] if n not in syms]
    if everything:
        for n in [s for s in syms if s not in everything]:
            alt = [x for x in everything if x.endswith(":" + n)]
            if len(alt) == 1:
                syms[syms.index(n)] = alt[0]
            else:
                print(f"UYARI: '{n}' Hyperliquid'de bulunamadı, atlandı.")
                syms.remove(n)
    return syms


def list_markets():
    lines = []
    for dex in get_dexes():
        try:
            items = sorted(fetch_universe(dex), reverse=True)
        except Exception as e:
            print("Alınamadı:", dex, e)
            continue
        if items:
            lines.append(f"[{dex or 'ana (kripto)'}] {len(items)} market:\n" + ", ".join(n for _, n in items))
    notify_long("\n\n".join(lines))


# =====================================================================
#  DİZİ YARDIMCILARI + MUM SERİSİ
# =====================================================================
def sh(x, k):
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[:len(x) - k]
    return out


def shb(x, k):
    out = np.zeros(len(x), bool)
    if k < len(x):
        out[k:] = x[:len(x) - k]
    return out


def rmean(x, n):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _roll(x, n, fn):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = fn(np.lib.stride_tricks.sliding_window_view(x, n), axis=1)
    return out


def rmin(x, n):
    return _roll(x, n, np.min)


def rmax(x, n):
    return _roll(x, n, np.max)


class Arr:
    def __init__(self, ts, o, h, l, c, v):
        self.ts, self.o, self.h, self.l, self.c, self.v = ts, o, h, l, c, v
        self.n = len(c)
        pc = sh(c, 1)
        with np.errstate(invalid="ignore"):
            tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
        tr[0] = h[0] - l[0]
        self.atr = rmean(tr, 14)
        self.ab = sh(rmean(np.abs(c - o), 10), 1)


def fetch_candles(coin, interval, bars):
    end = int(time.time() * 1000)
    body = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval,
        "startTime": end - bars * TF_SEC[interval] * 1000, "endTime": end}}
    rows = hl_post(body, weight=20 + bars // 60 + 1)
    if not rows or len(rows) < 60:
        return None
    ts = np.array([r["t"] for r in rows], float) / 1000.0
    cols = {k: np.array([float(r[k]) for r in rows], float) for k in "ohlcv"}
    keep = (ts + TF_SEC[interval] <= time.time()) & (cols["h"] >= cols["l"]) & (cols["c"] > 0) & (cols["o"] > 0)
    if not keep.any():
        return None
    A = Arr(ts[keep], cols["o"][keep], cols["h"][keep], cols["l"][keep], cols["c"][keep], cols["v"][keep])
    return A if A.n >= 60 else None


# =====================================================================
#  MUM FORMASYONLARI (sadece tetik olarak kullanılan çiftler)
# =====================================================================
TRIG_LONG = ["engulf_bull", "pin_lower", "outside_bull", "sweep_bull",
             "piercing", "morning_star", "three_out_up", "tweezer_bottom"]
TRIG_SHORT = ["engulf_bear", "pin_upper", "outside_bear", "sweep_bear",
              "dark_cloud", "evening_star", "three_out_down", "tweezer_top"]
TRIG_NAME = {
    "engulf_bull": "Yutan boğa", "engulf_bear": "Yutan ayı",
    "pin_lower": "Alt iğneli mum (çekiç)", "pin_upper": "Üst iğneli mum (kayan yıldız)",
    "outside_bull": "Dış mum (yükseliş)", "outside_bear": "Dış mum (düşüş)",
    "sweep_bull": "Likidite avı (dip)", "sweep_bear": "Likidite avı (zirve)",
    "piercing": "Delici mum", "dark_cloud": "Kara bulut",
    "morning_star": "Sabah yıldızı", "evening_star": "Akşam yıldızı",
    "three_out_up": "Üç dış yükseliş", "three_out_down": "Üç dış düşüş",
    "tweezer_bottom": "Cımbız dip", "tweezer_top": "Cımbız tepe",
}


def compute_triggers(A):
    O, H, L, C = A.o, A.h, A.l, A.c
    atr, ab = A.atr, A.ab
    with np.errstate(invalid="ignore", divide="ignore"):
        body = np.abs(C - O)
        rng = H - L
        up = H - np.maximum(O, C)
        lo = np.minimum(O, C) - L
        bull, bear = C > O, C < O
        Op, Cp, Hp, Lp = sh(O, 1), sh(C, 1), sh(H, 1), sh(L, 1)
        bodyp = np.abs(Cp - Op)
        bullp, bearp = shb(bull, 1), shb(bear, 1)
        midp = (Op + Cp) / 2
        O2, C2, L2, H2 = sh(O, 2), sh(C, 2), sh(L, 2), sh(H, 2)
        body2 = np.abs(C2 - O2)
        bull2, bear2 = shb(bull, 2), shb(bear, 2)
        ab2 = sh(ab, 2)
        pmin, pmax = sh(rmin(L, 20), 1), sh(rmax(H, 20), 1)
        P = {
            "engulf_bull": bearp & bull & (O <= Cp) & (C >= Op) & (body > bodyp) & (body >= ab),
            "engulf_bear": bullp & bear & (O >= Cp) & (C <= Op) & (body > bodyp) & (body >= ab),
            "pin_lower": (lo >= 2 * body) & (up <= 0.5 * body) & (rng >= 0.6 * atr) & (body >= 0.05 * rng),
            "pin_upper": (up >= 2 * body) & (lo <= 0.5 * body) & (rng >= 0.6 * atr) & (body >= 0.05 * rng),
            "outside_bull": (H > Hp) & (L < Lp) & bull & (C > Hp),
            "outside_bear": (H > Hp) & (L < Lp) & bear & (C < Lp),
            "sweep_bull": (L < pmin) & (C > pmin) & (lo >= 0.4 * rng),
            "sweep_bear": (H > pmax) & (C < pmax) & (up >= 0.4 * rng),
            "piercing": bearp & (bodyp >= ab) & bull & (O < Cp) & (C > midp) & (C < Op),
            "dark_cloud": bullp & (bodyp >= ab) & bear & (O > Cp) & (C < midp) & (C > Op),
            "morning_star": (bear2 & (body2 >= 1.2 * ab2) & (bodyp <= 0.5 * body2) & bull
                             & (C > (O2 + C2) / 2) & (Lp <= np.minimum(L2, L))),
            "evening_star": (bull2 & (body2 >= 1.2 * ab2) & (bodyp <= 0.5 * body2) & bear
                             & (C < (O2 + C2) / 2) & (Hp >= np.maximum(H2, H))),
            "three_out_up": bear2 & bullp & (Op <= C2) & (Cp >= O2) & (bodyp > body2) & bull & (C > Cp),
            "three_out_down": bull2 & bearp & (Op >= C2) & (Cp <= O2) & (bodyp > body2) & bear & (C < Cp),
            "tweezer_bottom": bearp & bull & (np.abs(L - Lp) <= 0.1 * atr),
            "tweezer_top": bullp & bear & (np.abs(H - Hp) <= 0.1 * atr),
        }
    return P


# =====================================================================
#  STRATEJİ: yön (4s) + geri çekilme (1s) + tetik + skor
# =====================================================================
def trend_score(A4):
    """4 saatlik çoklu Donchian orta-çizgi skoru, [-1, +1]."""
    s = np.zeros(A4.n)
    for N in DON_N:
        mid = (rmax(A4.h, N) + rmin(A4.l, N)) / 2
        with np.errstate(invalid="ignore"):
            s += np.sign(A4.c - mid)
    return s / len(DON_N)


def align_trend(A1, A4, s4):
    """Her 1 saatlik mumun kapanışında, o ana kadar KAPANMIŞ son 4 saatlik mumun skoru."""
    k = np.searchsorted(A4.ts + TF_SEC["4h"], A1.ts + TF_SEC["1h"], side="right") - 1
    out = np.where(k >= 0, s4[np.clip(k, 0, None)], np.nan)
    return out


DEFAULT_CFG = {"use_trend": True, "use_trigger": True, "flip": False}


def gen_signals(A1, s4_1, btc_1, cfg=DEFAULT_CFG):
    """Her 1 saatlik mum için: yön (+1/-1/0), skor, stop mesafesi (fiyat), tetik adı dizini."""
    n = A1.n
    P = compute_triggers(A1)
    tl = np.sum([P[k] for k in TRIG_LONG], axis=0)
    ts_ = np.sum([P[k] for k in TRIG_SHORT], axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        pbl = (rmax(A1.h, PB_LOOK) - A1.c) / A1.atr
        pbs = (A1.c - rmin(A1.l, PB_LOOK)) / A1.atr
        volavg = sh(rmean(A1.v, 20), 1)
        vol_spike = A1.v >= 1.5 * volavg
        okl, oks = pbl >= PB_MIN, pbs >= PB_MIN
        if cfg["use_trend"]:
            okl &= s4_1 >= TREND_MIN
            oks &= s4_1 <= -TREND_MIN
        trig_l = (tl > 0) if cfg["use_trigger"] else (A1.c > A1.o)
        trig_s = (ts_ > 0) if cfg["use_trigger"] else (A1.c < A1.o)
        sl, ss = okl & trig_l, oks & trig_s
        valid = ~np.isnan(A1.atr) & ~np.isnan(s4_1) & ~np.isnan(pbl)
        sl, ss = sl & valid & ~ss, ss & valid & ~sl
        d = np.where(sl, 1, np.where(ss, -1, 0))
        trend_pts = (np.clip((np.abs(s4_1) - 0.5) / 0.5, 0, 1) * 25 + 5) if cfg["use_trend"] else np.full(n, 15.0)
        pb = np.where(d == 1, pbl, pbs)
        pb_pts = np.clip((pb - PB_MIN) / 2.0, 0, 1) * 20 + 5
        ntrig = np.where(d == 1, tl, ts_)
        trig_pts = np.minimum(15 + 5 * (ntrig - 1), 25) if cfg["use_trigger"] else np.full(n, 15.0)
        vol_pts = 10.0 * vol_spike
        if btc_1 is None:
            btc_pts = np.full(n, 5.0)
        else:
            btc_pts = np.where(np.sign(btc_1) * d > 0, 10.0, 0.0) * (np.abs(btc_1) >= 0.5)
        score = np.nan_to_num(trend_pts + pb_pts + trig_pts + vol_pts + btc_pts)
        struct_long = np.minimum(A1.l, sh(A1.l, 1)) - 0.25 * A1.atr
        struct_short = np.maximum(A1.h, sh(A1.h, 1)) + 0.25 * A1.atr
        dist = np.where(d == 1, A1.c - struct_long, struct_short - A1.c)
        dist = np.clip(np.nan_to_num(dist, nan=0.0), STOP_MIN_ATR * np.nan_to_num(A1.atr),
                       STOP_MAX_ATR * np.nan_to_num(A1.atr))
    if cfg["flip"]:
        d = -d
    return d, score, dist, P


# =====================================================================
#  İŞLEM SİMÜLASYONU (backtest ve canlı sanal takip AYNI fonksiyonu kullanır)
# =====================================================================
def new_pos(d, entry, dist, mode):
    pos = {"d": d, "entry": entry, "dist": dist, "mode": mode, "bars": 0, "ext": entry}
    pos["stop"] = entry - d * dist
    pos["target"] = entry + d * R_TARGET * dist
    return pos


def advance(pos, o, h, l, c, atr, first=False):
    """Bir mum ilerlet. Çıkış varsa (fiyat, neden) döndürür, yoksa None."""
    d = pos["d"]
    pos["bars"] += 1
    if d == 1:
        if l <= pos["stop"]:
            return (pos["stop"] if first else min(pos["stop"], o)), "stop"
        if pos["mode"] == "r2" and h >= pos["target"]:
            return pos["target"], "hedef"
        pos["ext"] = max(pos["ext"], h)
        if pos["mode"] == "trail" and not math.isnan(atr):
            pos["stop"] = max(pos["stop"], pos["ext"] - TRAIL_ATR * atr)
    else:
        if h >= pos["stop"]:
            return (pos["stop"] if first else max(pos["stop"], o)), "stop"
        if pos["mode"] == "r2" and l <= pos["target"]:
            return pos["target"], "hedef"
        pos["ext"] = min(pos["ext"], l)
        if pos["mode"] == "trail" and not math.isnan(atr):
            pos["stop"] = min(pos["stop"], pos["ext"] + TRAIL_ATR * atr)
    if pos["bars"] >= MAX_BARS:
        return c, "süre"
    return None


def simulate(A, t, d, dist, mode):
    """t. mumun kapanışında verilen sinyali sonraki mumun açılışında gir, bitişe kadar yürüt."""
    if t + 1 >= A.n:
        return None
    entry = A.o[t + 1]
    pos = new_pos(d, entry, dist, mode)
    for j in range(t + 1, min(t + 1 + MAX_BARS, A.n)):
        res = advance(pos, A.o[j], A.h[j], A.l[j], A.c[j], A.atr[j], first=(j == t + 1))
        if res:
            px, why = res
            return j, (px - entry) * d / dist, dist / entry
    return None                         # veri bitti, sonuç belirsiz


def make_candidates(sym, A1, d, score, dist, mode):
    """Bir marketteki tüm sinyallerin işlem sonuçları."""
    out = []
    for t in np.nonzero(d != 0)[0]:
        if t + 1 >= A1.n or dist[t] <= 0:
            continue
        r = simulate(A1, int(t), int(d[t]), float(dist[t]), mode)
        if r is None:
            continue
        j, R_gross, risk_frac = r
        R_net = R_gross - COST / risk_frac
        out.append({"sym": sym, "entry_ts": float(A1.ts[t + 1]), "exit_ts": float(A1.ts[j] + TF_SEC["1h"]),
                    "score": float(score[t]), "R": float(R_net), "dir": int(d[t])})
    return out


def select_trades(cands, thr):
    """Zaman sırasıyla, gerçek zamanlı uygulanabilir kurallarla işlem seç (geleceğe bakmadan)."""
    import heapq
    cands = sorted((c for c in cands if c["score"] >= thr), key=lambda c: (c["entry_ts"], -c["score"]))
    taken, day_count, busy = [], defaultdict(int), {}
    open_heap, realized = [], defaultdict(float)       # (exit_ts, sıra, R)
    for c in cands:
        while open_heap and open_heap[0][0] <= c["entry_ts"]:    # kapanmış işlemlerin sonucunu işle
            ex, _, R = heapq.heappop(open_heap)
            realized[int(ex // 86400)] += R
        day = int(c["entry_ts"] // 86400)
        if day_count[day] >= MAX_PER_DAY:
            continue
        if busy.get(c["sym"], 0) > c["entry_ts"]:
            continue
        if len(open_heap) >= MAX_OPEN:
            continue
        if realized[day] <= DAILY_STOP_R:
            continue
        taken.append(c)
        day_count[day] += 1
        busy[c["sym"]] = c["exit_ts"]
        heapq.heappush(open_heap, (c["exit_ts"], len(taken), c["R"]))
    return taken


def stats(trades, t0, t1):
    """Belirli zaman aralığındaki işlemler için özet (gün bazlı t-istatistiği dahil)."""
    ts = [x for x in trades if t0 <= x["entry_ts"] < t1]
    days = max(1.0, (t1 - t0) / 86400)
    if not ts:
        return {"n": 0, "per_day": 0.0, "win": 0.0, "avg": 0.0, "pf": 0.0, "dd": 0.0, "t": 0.0,
                "series": [0.0] * int(days)}
    R = [x["R"] for x in ts]
    wins = [r for r in R if r > 0]
    losses = [-r for r in R if r <= 0]
    daily = defaultdict(float)
    for x in ts:
        daily[int(x["entry_ts"] // 86400)] += x["R"]
    nd = int(days)
    series = [daily.get(int(t0 // 86400) + i, 0.0) for i in range(nd)]
    mu = sum(series) / max(1, nd)
    sd = math.sqrt(sum((v - mu) ** 2 for v in series) / max(1, nd - 1)) or 1e-9
    cum, peak, dd = 0.0, 0.0, 0.0
    for x in sorted(ts, key=lambda x: x["exit_ts"]):
        cum += x["R"]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return {"n": len(ts), "per_day": len(ts) / days, "win": len(wins) / len(ts), "avg": sum(R) / len(R),
            "pf": (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else float("inf"),
            "dd": dd, "t": mu / (sd / math.sqrt(nd)), "series": series}


def paired_t(a, b):
    """İki stratejinin günlük R serileri arasındaki farkın eşleşik t-istatistiği."""
    n = min(len(a), len(b))
    if n < 10:
        return 0.0, 0.0
    d = [a[i] - b[i] for i in range(n)]
    mu = sum(d) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in d) / (n - 1)) or 1e-9
    return mu * 1.0, mu / (sd / math.sqrt(n))      # (günlük ortalama fark, t)


def fmt_stats(s):
    if s["n"] == 0:
        return "işlem yok"
    return (f"{s['n']} işlem ({s['per_day']:.1f}/gün) · kazanç %{s['win'] * 100:.0f} · ort. {s['avg']:+.2f}R · "
            f"PF {min(s['pf'], 99):.2f} · maks. düşüş {s['dd']:.1f}R · t={s['t']:.1f}")


# =====================================================================
#  BACKTEST
# =====================================================================
def load_all(symbols, bars1=5000):
    data = {}
    bars4 = bars1 // 4 + 150
    btc4 = fetch_candles("BTC", "4h", bars4)
    for sym in symbols:
        try:
            A1 = fetch_candles(sym, "1h", bars1)
            A4 = btc4 if sym == "BTC" else fetch_candles(sym, "4h", bars4)
        except Exception as e:
            print(f"{sym} verisi alınamadı: {e}")
            continue
        if A1 is None or A4 is None:
            continue
        data[sym] = (A1, A4)
    return data, btc4


def build_candidates(data, btc4, cfg, mode):
    cands = []
    for sym, (A1, A4) in data.items():
        s4 = align_trend(A1, A4, trend_score(A4))
        btc_1 = None
        if btc4 is not None and sym in CRYPTO and sym != "BTC":
            btc_1 = align_trend(A1, btc4, trend_score(btc4))
        d, score, dist, _ = gen_signals(A1, s4, btc_1, cfg)
        cands += make_candidates(sym, A1, d, score, dist, mode)
    return cands


def calibrate(cands, t_split, t_min):
    """Keşif döneminde günlük ~3 işlem veren eşiği bul."""
    best, best_gap = THRESHOLDS[-1], 1e9
    table = []
    for thr in THRESHOLDS:
        tr = select_trades(cands, thr)
        s = stats(tr, t_min, t_split)
        table.append((thr, s))
        gap = abs(s["per_day"] - TARGET_PER_DAY)
        if gap < best_gap:
            best, best_gap = thr, gap
    return best, table


def run_backtest(data=None, btc4=None, quiet=False):
    if data is None:
        syms = build_symbols()
        print(f"{len(syms)} market için veri çekiliyor...")
        data, btc4 = load_all(syms)
    if not data:
        notify("Backtest için veri alınamadı.")
        return None
    t_min = min(A1.ts[60] for A1, _ in data.values())
    t_max = max(A1.ts[-1] for A1, _ in data.values())
    t_split = t_min + DISC_FRAC * (t_max - t_min)
    ndays = (t_max - t_min) / 86400
    results = {}
    for mode in ("trail", "r2"):
        cands = build_candidates(data, btc4, DEFAULT_CFG, mode)
        thr, table = calibrate(cands, t_split, t_min)
        tr = select_trades(cands, thr)
        results[mode] = {"thr": thr, "table": table, "trades": tr,
                         "disc": stats(tr, t_min, t_split), "hold": stats(tr, t_split, t_max + 1)}
    mode = max(results, key=lambda m: results[m]["disc"]["avg"])          # seçim sadece keşif verisine göre
    chosen = results[mode]
    thr = chosen["thr"]
    # --- ablasyon: aynı sıklıkta, aynı çıkış tipiyle ---
    variants = {
        "tam strateji": DEFAULT_CFG,
        "mum tetiği yok (sadece trend+geri çekilme+yeşil/kırmızı mum)": {**DEFAULT_CFG, "use_trigger": False},
        "trend filtresi yok": {**DEFAULT_CFG, "use_trend": False},
        "TERS YÖN (aynı girişler, ters bahis)": {**DEFAULT_CFG, "flip": True},
    }
    abl = {}
    for name, cfg in variants.items():
        c = build_candidates(data, btc4, cfg, mode)
        vthr, _ = calibrate(c, t_split, t_min)
        tr = select_trades(c, vthr)
        abl[name] = (vthr, stats(tr, t_min, t_split), stats(tr, t_split, t_max + 1))
    NT = "mum tetiği yok (sadece trend+geri çekilme+yeşil/kırmızı mum)"
    FL = "TERS YÖN (aynı girişler, ters bahis)"
    NTR = "trend filtresi yok"
    full_hold = abl["tam strateji"][2]
    ok = (chosen["hold"]["n"] >= MIN_HOLD_TRADES and chosen["hold"]["avg"] > 0.05 and chosen["hold"]["t"] > 1.64
          and chosen["disc"]["avg"] > 0 and full_hold["avg"] > abl[FL][2]["avg"])

    def contrib(other):
        diff, tt = paired_t(full_hold["series"], abl[other][2]["series"])
        per_trade = full_hold["avg"] - abl[other][2]["avg"]
        lab = "anlamlı katkı" if tt > 1.64 else ("pozitif ama anlamlı değil" if per_trade > 0 else "katkı yok")
        return per_trade, tt, lab
    cfg_out = {"validated": bool(ok), "threshold": thr, "exit_mode": mode, "created": int(time.time()),
               "disc": {k: v for k, v in chosen["disc"].items() if k != "series"},
               "hold": {k: v for k, v in chosen["hold"].items() if k != "series"}}
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg_out, f, ensure_ascii=False, indent=1)
    rep = ["📈 STRATEJİ TESTİ: Yön → Geri çekilme → Mum tetiği",
           f"{len(data)} market · {ndays:.0f} gün · keşif %{int(DISC_FRAC * 100)} / bağımsız doğrulama %{100 - int(DISC_FRAC * 100)}",
           f"Hedef: günde ~{TARGET_PER_DAY:.0f} işlem · maliyet %{COST * 100:.2f} · 1R = stop mesafesi",
           "",
           f"SEÇİLEN: çıkış = {'iz süren stop' if mode == 'trail' else f'{R_TARGET:.0f}R hedef'}, skor eşiği = {thr}",
           f"  keşif     : {fmt_stats(chosen['disc'])}",
           f"  doğrulama : {fmt_stats(chosen['hold'])}",
           f"(diğer çıkış tipi: keşif {results['r2' if mode == 'trail' else 'trail']['disc']['avg']:+.2f}R, "
           f"doğrulama {results['r2' if mode == 'trail' else 'trail']['hold']['avg']:+.2f}R)",
           "", "KATKI ANALİZİ (her satırın eşiği ayrıca ~3 işlem/gün'e ayarlandı; doğrulama dönemi):"]
    for name, (vthr, ds, hs) in abl.items():
        rep.append(f"  • {name}: ort. {hs['avg']:+.2f}R ({hs['n']} işlem, t={hs['t']:.1f})")
    def by_class(t0, t1):
        out = []
        for cls, isfi in (("Kripto", False), ("TradFi (hisse/endeks/emtia)", True)):
            sub = [x for x in chosen["trades"] if (":" in x["sym"]) == isfi]
            out.append(f"  • {cls}: {fmt_stats(stats(sub, t0, t1))}")
        return out
    rep[7:7] = (["", "NEREDE İŞE YARADI? (doğrulama dönemi, seçilen ayarla)"] + by_class(t_split, t_max + 1)
                + ["  (tek tek market sonuçları küçük örnek olduğu için gösterilmez; sınıf bazında karar ver)"])
    pt, tt, lab = contrib(NT)
    rep.append(f"  → mum tetiğinin ek katkısı: {pt:+.2f}R/işlem (günlük eşleşik t={tt:.1f}): {lab}")
    pt2, tt2, lab2 = contrib(NTR)
    rep.append(f"  → trend filtresinin katkısı: {pt2:+.2f}R/işlem (t={tt2:.1f}): {lab2}")
    pt3, tt3, lab3 = contrib(FL)
    rep.append(f"  → ters yöne göre fark: {pt3:+.2f}R/işlem (t={tt3:.1f}): {lab3}")
    rep.append("")
    if ok:
        rep.append("✅ SONUÇ: doğrulama döneminde pozitif, istatistiksel olarak anlamlı ve ters yönden iyi. "
                   "Yine de önce küçük boyutla ileri test (kağıt işlem) yap.")
        if tt <= 1.64:
            rep.append("⚠️ Ama dikkat: kâr büyük olasılıkla trend + geri çekilmeden geliyor; mum tetiği ek katkısını kanıtlayamadı. "
                       "Mum formasyonuna 'sihirli' bir rol yükleme.")
    else:
        why = []
        if chosen["hold"]["n"] < MIN_HOLD_TRADES:
            why.append("doğrulamada yeterli işlem yok")
        if not chosen["hold"]["avg"] > 0.05:
            why.append("doğrulama ortalaması +0,05R'nin altında")
        if not chosen["hold"]["t"] > 1.64:
            why.append("istatistiksel olarak anlamlı değil")
        if not chosen["disc"]["avg"] > 0:
            why.append("keşif döneminde de kârlı değildi")
        if not full_hold["avg"] > abl[FL][2]["avg"]:
            why.append("ters yön kadar bile iyi değil")
        rep.append("⛔ SONUÇ: kanıt yeterli değil (" + ", ".join(why) + "). Gerçek parayla işlem açmayı önermem; "
                   "bot sadece kağıt işlem olarak takip eder.")
    rep.append("\nNot: geçmiş sonuç geleceği garanti etmez. İşlem kopyaları aynı gün/aynı yönde korelasyonlu olabilir; "
               "gerçek risk gösterilenden yüksek olabilir.")
    if not quiet:
        notify_long("\n".join(rep))
    return cfg_out, results, abl


# =====================================================================
#  CANLI TARAMA + SANAL POZİSYON TAKİBİ
# =====================================================================
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def sizing(entry, dist):
    risk_usd = ACCOUNT_USD * RISK_PCT / 100
    notional = risk_usd / (dist / entry)
    lev = notional / ACCOUNT_USD
    note = ""
    if lev > MAX_LEV:
        notional = MAX_LEV * ACCOUNT_USD
        risk_usd = notional * dist / entry
        lev = MAX_LEV
        note = " (kaldıraç sınırı nedeniyle risk düşürüldü)"
    return risk_usd, notional, lev, note


def fmt_px(x):
    return f"{x:.6g}"


def run_scan():
    cfg = load_json(CONFIG_FILE, {})
    state = load_json(STATE_FILE, {"open": [], "sent": [], "last": 0, "day": {}, "paper": {"n": 0, "R": 0.0}})
    validated = bool(cfg.get("validated"))
    if not validated and not PAPER:
        print("Strateji doğrulanmadı ve PAPER kapalı: bildirim yok.")
        return
    thr = cfg.get("threshold", 60)
    mode = cfg.get("exit_mode", "r2")
    now = int(time.time())
    boundary = now // 3600 * 3600
    if state.get("last", 0) >= boundary:
        return
    tag = "" if validated else "📝 KAĞIT İŞLEM (strateji doğrulanmadı, gerçek işlem açma) — "
    syms = build_symbols()
    btc4 = fetch_candles("BTC", "4h", 160)
    btc_s4 = trend_score(btc4) if btc4 is not None else None
    cands, books = [], {}
    for sym in syms:
        try:
            A1 = fetch_candles(sym, "1h", 150)
            A4 = btc4 if sym == "BTC" else fetch_candles(sym, "4h", 160)
        except Exception as e:
            print(f"{sym} hata: {e}")
            continue
        if A1 is None or A4 is None:
            continue
        books[sym] = A1
        s4 = align_trend(A1, A4, trend_score(A4))
        btc_1 = align_trend(A1, btc4, btc_s4) if (btc4 is not None and sym in CRYPTO and sym != "BTC") else None
        d, score, dist, P = gen_signals(A1, s4, btc_1)
        t = A1.n - 1
        if d[t] != 0 and score[t] >= thr and dist[t] > 0:
            names = [TRIG_NAME[k] for k in (TRIG_LONG if d[t] == 1 else TRIG_SHORT) if P[k][t]]
            cands.append((float(score[t]), sym, int(d[t]), float(dist[t]), float(A1.c[t]), names, int(A1.ts[t])))
    # --- açık sanal pozisyonları güncelle ---
    still = []
    for p in state["open"]:
        A1 = books.get(p["sym"])
        if A1 is None:
            still.append(p)
            continue
        pos = p["pos"]
        closed = False
        for j in range(A1.n):
            if A1.ts[j] <= p["signal_ts"] or A1.ts[j] <= p.get("done_ts", 0):
                continue
            first = not p.get("entered")
            if first:                                   # girişi bir sonraki mumun açılışına sabitle
                pos.update(new_pos(pos["d"], float(A1.o[j]), p["dist"], mode))
                pos["bars"] = 0
                p["entered"] = True
            res = advance(pos, float(A1.o[j]), float(A1.h[j]), float(A1.l[j]), float(A1.c[j]), float(A1.atr[j]), first)
            p["done_ts"] = float(A1.ts[j])
            if res:
                px, why = res
                R = (px - pos["entry"]) * pos["d"] / p["dist"] - COST / (p["dist"] / pos["entry"])
                state["paper"]["n"] += 1
                state["paper"]["R"] += R
                notify(f"🏁 {p['sym'].split(':')[-1]} {'LONG' if pos['d'] == 1 else 'SHORT'} kapandı ({why}): "
                       f"{R:+.2f}R (maliyet dahil)\nİleri test toplamı: {state['paper']['R']:+.1f}R / {state['paper']['n']} işlem")
                closed = True
                break
            if mode == "trail" and (pos["stop"] - p.get("sent_stop", pos["stop"])) * pos["d"] >= 0.5 * p["dist"]:
                p["sent_stop"] = pos["stop"]
                notify(f"🔧 STOP GÜNCELLE: {p['sym'].split(':')[-1]} yeni stop {fmt_px(pos['stop'])}")
        if not closed:
            still.append(p)
    state["open"] = still
    # --- yeni sinyaller (günlük sınır ve açık pozisyon sınırı ile) ---
    today = str(boundary // 86400)
    count = state["day"].get(today, 0)
    for score, sym, d, dist, close, names, sig_ts in sorted(cands, reverse=True):
        if count >= MAX_PER_DAY or len(state["open"]) >= MAX_OPEN:
            break
        if any(p["sym"] == sym for p in state["open"]):
            continue
        key = f"{sym}|{sig_ts}"
        if key in state["sent"]:
            continue
        state["sent"] = (state["sent"] + [key])[-300:]
        count += 1
        stop = close - d * dist
        risk_usd, notional, lev, note = sizing(close, dist)
        exit_txt = ("iz süren stop (bot stop güncellemelerini yazar)" if mode == "trail"
                    else f"hedef {fmt_px(close + d * R_TARGET * dist)} ({R_TARGET:.0f}R)")
        notify(f"{tag}{'🟢 LONG' if d == 1 else '🔴 SHORT'} — {sym.split(':')[-1]} (1 saat) · skor {score:.0f}\n"
               f"Tetik: {', '.join(names)}\n"
               f"Giriş: piyasa ≈ {fmt_px(close)} · Stop: {fmt_px(stop)} ({dist / close * 100:.1f}%)\n"
               f"Çıkış: {exit_txt} · en fazla {MAX_BARS} saat\n"
               f"Boyut: risk ${risk_usd:.2f} → pozisyon ≈ ${notional:.0f} ({lev:.1f}x){note}")
        state["open"].append({"sym": sym, "signal_ts": float(sig_ts), "dist": dist,
                              "pos": new_pos(d, close, dist, mode), "entered": False})
    state["day"] = {today: count}
    state["last"] = boundary
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


if __name__ == "__main__":
    mode_ = os.environ.get("MODE", "scan")
    if mode_ == "backtest":
        run_backtest()
    elif mode_ == "list":
        list_markets()
    else:
        run_scan()

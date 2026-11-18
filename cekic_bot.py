"""
Çekiç / Ters Çekiç tarayıcı (BIST + kripto) + Telegram bildirimi

Kural: Bir hissede, herhangi bir zaman diliminde, son LOOKBACK kapanmış mum
içinde en az HIT_COUNT adet çekiç veya ters çekiç varsa bildirim gönderir.

Kurulum:
    pip install yfinance requests
Ortam değişkenleri:
    TELEGRAM_TOKEN   (BotFather'dan alınır)
    TELEGRAM_CHAT_ID (botla konuşunca getUpdates ile öğrenilir)
Çalıştırma:
    python cekic_bot.py
"""
import os
import json
import time
import requests
import pandas as pd
import yfinance as yf

# ---------- Ayarlar ----------
# Hisseler (BIST): sadece kodu yaz, ".IS" otomatik eklenir
BIST = []   # şimdilik kapalı. Eklemek için: ["THYAO", "GARAN", ...]
# Kripto: Hyperliquid coin adı ile yaz (veri doğrudan Hyperliquid'den gelir)
CRYPTO = ["BTC", "ETH", "SOL", "XRP", "BNB",
          "AVAX", "HYPE", "ZEC", "USELESS", "kPEPE", "PUMP", "UNI", "ARB", "PAXG", "JUP", "INJ"]
# TradFi: Hyperliquid'deki hisse, endeks ve emtia kontratları.
# Çoğunun başında "xyz:" olur; bazı hisseler önek olmadan yazılır (GOOGL, HOOD, TSM).
# Adı yanlış/yok olanları bot kendisi eler ve uyarı yazar.
US_STOCKS = ["xyz:NVDA", "xyz:TSLA", "xyz:AAPL", "xyz:MSFT", "xyz:AMZN",
             "xyz:GOOGL", "xyz:META", "xyz:HOOD", "xyz:TSM", "xyz:AMD", "xyz:PLTR",
             "xyz:MU", "xyz:COIN", "xyz:MSTR", "xyz:AVGO", "xyz:CRCL", "xyz:INTC"]
INDICES = ["xyz:XYZ100", "xyz:SP500"]            # Nasdaq-100 ve S&P 500
# Emtia: altın, gümüş, paladyum, bakır, ham petrol, Brent, doğalgaz, ısıtma yağı
COMMODITIES = ["xyz:GOLD", "xyz:SILVER", "xyz:PALLADIUM", "xyz:COPPER",
               "xyz:CL", "xyz:BRENTOIL", "xyz:NATGAS", "xyz:HO"]
# Spot (anlık) marketler, USDC paritesi: sadece token adını yaz. Şimdilik boş.
SPOT = []
SPOT_LABEL = {}   # "@107" -> "PAXG/USDC" (bildirimde görünen ad)
HYPERLIQUID = CRYPTO + US_STOCKS + INDICES + COMMODITIES      # veri Hyperliquid'den gelir
# Hyperliquid'de en çok işlem gören marketlerden kaçı otomatik eklensin (0 = kapalı).
# Dikkat: yükselttikçe tarama uzar (Hyperliquid dakikada ~50 istek sınırı koyar).
AUTO_TOP = 5
AUTO_DEXES = ["", "xyz"]   # "" = kripto, "xyz" = hisse/emtia kontratları
HL_DELAY = 1.1             # istekler arası bekleme (saniye)
SYMBOLS = [f"{b}.IS" for b in BIST] + HYPERLIQUID
# ad: (Yahoo aralığı, geçmiş, birleştirme). 4h Yahoo'da yok, 1 saatlikten üretilir.
TIMEFRAMES = {
    "15m": ("15m", "5d", None),
    "1h": ("60m", "30d", None),
    "4h": ("60m", "60d", "4h"),
    "1d": ("1d", "6mo", None),
}
LOOKBACK = 5        # son kaç kapanmış muma bakılsın
HIT_COUNT = 2       # kaç çekiç görülünce bildirim gelsin
CHECK_EVERY = 300   # saniye
LONG_SHADOW = 2.0   # uzun gölge >= gövde x 2
SHORT_SHADOW = 0.5  # kısa gölge <= gövde x 0.5
MIN_BODY_PCT = 0.0005  # gövde en az fiyatın %0,05'i (doji'leri ele)

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = "sent.json"
RUN_ONCE = os.environ.get("RUN_ONCE") == "1"   # GitHub Actions modu
class OrderedSet:
    """Eklenme sırasını koruyan küme (eski kayıtları doğru silebilmek için)."""
    def __init__(self):
        self.d = {}

    def add(self, k):
        self.d[k] = 1

    def __contains__(self, k):
        return k in self.d

    def update(self, items):
        for k in items:
            self.d[k] = 1

    def latest(self, n=500):
        return list(self.d)[-n:]


sent = OrderedSet()   # aynı sinyali tekrar göndermemek için
last_scan = {}        # zaman dilimi -> en son taranan mum başlangıcı (saniye)
TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):          # eski biçim
            sent.update(data)
        else:
            sent.update(data.get("sent", []))
            last_scan.update(data.get("last", {}))
    except (FileNotFoundError, ValueError):
        pass


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({"sent": sent.latest(), "last": last_scan}, f)


def candle_type(o, h, l, c):
    """'hammer', 'inverted' veya None döner."""
    body = abs(c - o)
    if body < max(c, o) * MIN_BODY_PCT or h == l:
        return None
    upper = h - max(o, c)
    lower = min(o, c) - l
    if lower >= LONG_SHADOW * body and upper <= SHORT_SHADOW * body:
        return "hammer"
    if upper >= LONG_SHADOW * body and lower <= SHORT_SHADOW * body:
        return "inverted"
    return None


def notify(text):
    print(text)
    if not (TOKEN and CHAT_ID):
        print("(Telegram ayarlı değil, sadece ekrana yazıldı)")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=15,
        )
    except requests.RequestException as e:
        print("Telegram hatası:", e)


MIN_SCORE = 0   # 0-3: kalite puanı bundan düşük sinyaller gönderilmez (2 önerilir)
# Likidite avı (liquidity sweep): bu zaman dilimlerinde çekiç yerine likidite avı aranır
SWEEP_TFS = {"4h"}
SWEEP_LOOKBACK = 20   # son kaç mumun dip/zirvesi "likidite" sayılsın
SWEEP_WICK = 0.4      # avlanan iğne, mum boyunun en az bu oranı kadar olsun
SWEEP_CHECK = 2       # son kaç kapanmış muma bakılsın (geciken çalışmaları yakalamak için)
TREND_BARS = 6  # trendi anlamak için mumdan kaç mum geriye bakılsın
# Trend sayılması için gereken minimum hareket (zaman dilimine göre)
TREND_MIN = {"15m": 0.004, "1h": 0.008, "4h": 0.015, "1d": 0.03}

LABELS = {
    ("hammer", "down"): "🟢 Çekiç (yükseliş dönüşü olası)",
    ("inverted", "down"): "🟢 Ters çekiç (yükseliş dönüşü olası)",
    ("hammer", "up"): "🔴 Asılı adam (düşüş uyarısı)",
    ("inverted", "up"): "🔴 Kayan yıldız (düşüş uyarısı)",
    ("hammer", "flat"): "⚪ Çekiç şekli (trend belirsiz)",
    ("inverted", "flat"): "⚪ Ters çekiç şekli (trend belirsiz)",
}


def trend_before(closes, i, interval):
    """i. mumdan önceki trend: 'up', 'down' veya 'flat'."""
    if i - TREND_BARS < 0:
        return "flat"
    chg = closes[i - 1] / closes[i - 1 - TREND_BARS] - 1 if i - 1 - TREND_BARS >= 0 else 0
    lim = TREND_MIN.get(interval, 0.005)
    return "up" if chg > lim else "down" if chg < -lim else "flat"


HL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def hl_post(body):
    """Hyperliquid isteği: hız sınırına takılmamak için bekler, 429'da bir kez daha dener."""
    time.sleep(HL_DELAY)
    r = requests.post("https://api.hyperliquid.xyz/info", json=body, timeout=20)
    if getattr(r, "status_code", 200) == 429:
        time.sleep(10)
        r = requests.post("https://api.hyperliquid.xyz/info", json=body, timeout=20)
    r.raise_for_status()
    return r.json()


def get_dexes():
    """Hyperliquid'deki tüm market sağlayıcıları ('' = ana kripto marketleri)."""
    try:
        resp = hl_post({"type": "perpDexs"})
        return [""] + [d["name"] for d in resp if d and d.get("name")]
    except Exception as e:
        print("Sağlayıcı listesi alınamadı:", e)
        return ["", "xyz"]


def fetch_universe(dex):
    """Bir sağlayıcının marketleri: [(24s hacim, ad), ...]"""
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


def top_markets(n):
    """(hacmi en yüksek n market, Hyperliquid'deki tüm market adları) döner."""
    ranked, everything = [], set()
    for dex in get_dexes():
        try:
            items = fetch_universe(dex)
        except Exception as e:
            print("Market listesi alınamadı:", dex or "ana", e)
            continue
        everything.update(name for _, name in items)
        if dex in AUTO_DEXES:
            ranked += items
    ranked.sort(reverse=True)
    return [name for _, name in ranked[:n]], everything


def list_markets():
    """Tüm marketleri sağlayıcıya göre gruplayıp Telegram'a gönderir."""
    lines = []
    for dex in get_dexes():
        try:
            items = sorted(fetch_universe(dex), reverse=True)
        except Exception as e:
            print("Alınamadı:", dex, e)
            continue
        if items:
            lines.append(f"[{dex or 'ana (kripto)'}] {len(items)} market:\n"
                         + ", ".join(name for _, name in items))
    text = "\n\n".join(lines)
    with open("markets.txt", "w") as f:
        f.write(text)
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) > 3500:
            notify(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        notify(chunk)


def resolve_spot():
    """SPOT listesindeki token adlarını Hyperliquid'in spot market koduna çevirir."""
    if not SPOT:
        return
    try:
        meta = hl_post({"type": "spotMeta"})
    except Exception as e:
        print("Spot market listesi alınamadı:", e)
        return
    tokens = {t["name"]: t["index"] for t in meta["tokens"]}
    usdc = tokens.get("USDC")
    for name in SPOT:
        idx = tokens.get(name, tokens.get("U" + name))   # bazı tokenlar "U" öneklidir
        pair = next((u for u in meta["universe"] if u["tokens"] == [idx, usdc]), None)
        if idx is None or pair is None:
            print(f"UYARI: '{name}/USDC' spot marketi bulunamadı, atlandı.")
            continue
        if pair["name"] not in HYPERLIQUID:
            HYPERLIQUID.append(pair["name"])
        SPOT_LABEL[pair["name"]] = f"{name}/USDC"


def build_symbols():
    top, everything = top_markets(AUTO_TOP)
    if AUTO_TOP > 0:
        for name in top:
            if name not in HYPERLIQUID:
                HYPERLIQUID.append(name)
    if everything:                      # listede olmayan / yanlış yazılmış adları ayıkla
        for name in [n for n in HYPERLIQUID if n not in everything]:
            alt = [n for n in everything if n.endswith(":" + name)]
            if len(alt) == 1:               # ör. PAXG -> xyz:PAXG
                print(f"Bilgi: '{name}' yerine '{alt[0]}' kullanılıyor.")
                HYPERLIQUID[HYPERLIQUID.index(name)] = alt[0]
                continue
            print(f"UYARI: '{name}' Hyperliquid'de bulunamadı, atlandı.")
            HYPERLIQUID.remove(name)
    resolve_spot()
    return [f"{b}.IS" for b in BIST] + HYPERLIQUID


def fetch_hyperliquid(coin, interval, bars=120):
    """Hyperliquid'den mum verisi çeker (anahtar gerekmez)."""
    end = int(time.time() * 1000)
    body = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval,
        "startTime": end - bars * HL_MS[interval], "endTime": end}}
    rows = hl_post(body)
    if not rows:
        return None
    df = pd.DataFrame({
        "Open": [float(x["o"]) for x in rows], "High": [float(x["h"]) for x in rows],
        "Low": [float(x["l"]) for x in rows], "Close": [float(x["c"]) for x in rows],
        "Volume": [float(x.get("v", 0)) for x in rows],
    }, index=pd.to_datetime([x["t"] for x in rows], unit="ms"))
    return df


def rsi_series(closes, n=14):
    c = pd.Series(closes)
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn)).fillna(50).tolist()


def quality(df, closes, idx, bull):
    """Sinyal mumuna (idx) kalite kontrolleri. {ad: True/False} döner."""
    out = {}
    if "Volume" in df.columns and idx >= 20:
        avg = df["Volume"].iloc[idx - 20:idx].mean()
        out["Hacim"] = bool(avg > 0 and df["Volume"].iloc[idx] >= 1.5 * avg)
    else:
        out["Hacim"] = False
    r = rsi_series(closes)[idx]
    out["RSI"] = bool(r <= 35) if bull else bool(r >= 65)
    w = df.iloc[max(0, idx - 30):idx]
    if len(w):
        if bull:
            out["Dip seviyesi"] = bool(df["Low"].iloc[idx] <= w["Low"].min() * 1.002)
        else:
            out["Zirve seviyesi"] = bool(df["High"].iloc[idx] >= w["High"].max() * 0.998)
    return out


def scan_sweep(symbol, interval, df):
    """Likidite avı: önceki dip/zirveyi süpürüp geri içeri kapanan mum."""
    tf = {"15m": "15 dk", "1h": "1 saat", "4h": "4 saat", "1d": "Günlük"}
    name = f"{SPOT_LABEL.get(symbol) or symbol.replace('.IS', '').split(':')[-1]} - {tf.get(interval, interval)} grafik"
    closes = df["Close"].tolist()
    n = len(df)
    for i in range(max(n - SWEEP_CHECK, SWEEP_LOOKBACK), n):
        r = df.iloc[i]
        rng = r["High"] - r["Low"]
        if rng <= 0:
            continue
        w = df.iloc[i - SWEEP_LOOKBACK:i]
        prior_low, prior_high = w["Low"].min(), w["High"].max()
        body_low, body_high = min(r["Open"], r["Close"]), max(r["Open"], r["Close"])
        bull = (r["Low"] < prior_low and r["Close"] > prior_low
                and (body_low - r["Low"]) >= SWEEP_WICK * rng)
        bear = (r["High"] > prior_high and r["Close"] < prior_high
                and (r["High"] - body_high) >= SWEEP_WICK * rng)
        if bull == bear:                       # ikisi de ya da hiçbiri
            continue
        key = f"{symbol}|{interval}|sweep|{df.index[i]}|{'b' if bull else 's'}"
        if key not in sent:
            sent.add(key)
            vol = False
            if "Volume" in df.columns and i >= 20:
                avg = df["Volume"].iloc[i - 20:i].mean()
                vol = bool(avg > 0 and r["Volume"] >= 1.5 * avg)
            rsi = rsi_series(closes)[i]
            rsi_ok = bool(rsi <= 35) if bull else bool(rsi >= 65)
            head = "🟢 LİKİDİTE AVI - YÜKSELİŞ" if bull else "🔴 LİKİDİTE AVI - DÜŞÜŞ"
            side = "dibi süpürüldü, mum geri yukarı kapandı" if bull else "zirvesi süpürüldü, mum geri aşağı kapandı"
            notify(f"{head}\n{name}\nSon {SWEEP_LOOKBACK} mumun {side}.\n"
                   f"{'✅' if vol else '❌'} Hacim  {'✅' if rsi_ok else '❌'} RSI")
        if i + 1 < n and key + "|onay" not in sent:
            nxt = df.iloc[i + 1]
            if (bull and nxt["Close"] > r["High"]) or (not bull and nxt["Close"] < r["Low"]):
                sent.add(key + "|onay")
                notify(f"✅ ONAY MUMU GELDİ - {'yükseliş' if bull else 'düşüş'}\n{name}")


def scan(symbol, interval, yf_interval, period, resample):
    if symbol in HYPERLIQUID:
        df = fetch_hyperliquid(symbol, interval)
        resample = None
    else:
        df = yf.download(symbol, interval=yf_interval, period=period,
                         progress=False, auto_adjust=False)
    if df is None or len(df) < LOOKBACK + TREND_BARS + 2:
        return
    if hasattr(df.columns, "levels"):          # yfinance çok seviyeli sütun
        df.columns = df.columns.get_level_values(0)
    if resample:
        agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        if "Volume" in df.columns:
            agg["Volume"] = "sum"
        df = df.resample(resample).agg(agg).dropna()
    # Son mum henüz kapanmadıysa (açılışı + süresi şu andan sonraysa) çıkar
    tf_sec = TF_SECONDS.get(interval, 3600)
    last_open = df.index[-1]
    if getattr(last_open, "tzinfo", None) is not None:
        last_open = last_open.tz_convert("UTC").tz_localize(None)
    if last_open + pd.Timedelta(seconds=tf_sec) > pd.Timestamp.utcnow().tz_localize(None):
        df = df.iloc[:-1]
    if interval in SWEEP_TFS:
        if len(df) > SWEEP_LOOKBACK + 2:
            scan_sweep(symbol, interval, df)
        return
    closes = df["Close"].tolist()
    hits = []
    for i in range(len(df) - LOOKBACK, len(df)):
        r = df.iloc[i]
        t = candle_type(r["Open"], r["High"], r["Low"], r["Close"])
        if t:
            hits.append((df.index[i], t, trend_before(closes, i, interval), i))
    if len(hits) < HIT_COUNT:
        return
    key = f"{symbol}|{interval}|" + ",".join(str(h[0]) for h in hits)
    trends = {h[2] for h in hits}
    tf = {"15m": "15 dk", "1h": "1 saat", "4h": "4 saat", "1d": "Günlük"}
    name = f"{SPOT_LABEL.get(symbol) or symbol.replace('.IS', '').split(':')[-1]} - {tf.get(interval, interval)} grafik"
    last = hits[-1][3]
    if trends == {"flat"}:                          # yatay seyir: puanlama yok
        if key not in sent:
            sent.add(key)
            notify(f"⚪ KIRILIM SİNYALİ (yatay seyir)\n{name}")
        return
    if "up" in trends and "down" in trends:         # çelişkili: gönderme
        return
    bull = "down" in trends                         # düşüş(+yatay) -> yükseliş
    head = "🟢 YÜKSELİŞ SİNYALİ" if bull else "🔴 DÜŞÜŞ SİNYALİ"
    q = quality(df, closes, last, bull)
    score = sum(q.values())
    if score < MIN_SCORE:
        return
    if key not in sent:
        sent.add(key)
        stars = "⭐" * score + "☆" * (len(q) - score)
        checks = "  ".join(("✅ " if v else "❌ ") + k for k, v in q.items())
        notify(f"{head}  {stars}\n{name}\n{checks}")
    # onay mumu: sinyal mumundan sonraki mum, sinyal yönünde kapandı mı?
    if last + 1 < len(df) and key + "|onay" not in sent:
        nxt, sig = df.iloc[last + 1], df.iloc[last]
        if (bull and nxt["Close"] > sig["High"]) or (not bull and nxt["Close"] < sig["Low"]):
            sent.add(key + "|onay")
            notify(f"✅ ONAY MUMU GELDİ - {'yükseliş' if bull else 'düşüş'}\n{name}")


def run_scan(symbols):
    """Sadece yeni bir mumu kapanmış zaman dilimlerini tarar."""
    now = int(time.time())
    for iv, (yi, per, rs) in TIMEFRAMES.items():
        boundary = now // TF_SECONDS[iv] * TF_SECONDS[iv]
        if last_scan.get(iv, 0) >= boundary:
            continue                        # bu zaman diliminde yeni mum yok
        print(f"[{iv}] taranıyor...")
        for s in symbols:
            try:
                scan(s, iv, yi, per, rs)
            except Exception as e:
                print(f"{s} {iv} hata: {e}")
        last_scan[iv] = boundary


def main():
    if os.environ.get("LIST_MARKETS") == "1":
        list_markets()
        return
    load_state()
    symbols = build_symbols()
    print(f"{len(symbols)} sembol taranıyor.")
    if RUN_ONCE:
        run_scan(symbols)
        save_state()
        return
    notify("Çekiç tarayıcı başladı.")
    while True:
        run_scan(symbols)
        save_state()
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()

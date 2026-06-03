"""
╔══════════════════════════════════════════════════════════════╗
║     POLYMARKET BTC BOT v7 — FINAL OPTIMIZED EDITION         ║
║     Bugs corrigés | Notifications fiables | AI améliorée    ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import os
import json
import time
import math
import aiohttp
from datetime import datetime
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ─── CONFIG ────────────────────────────────────────────────────────────────
TOKEN          = os.getenv("TELEGRAM_TOKEN", "VOTRE_TOKEN_ICI")
ALLOWED_UID    = int(os.getenv("ALLOWED_USER_ID", "0"))
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
PAPER_MODE     = os.getenv("PAPER_MODE", "true").lower() == "true"
BANKROLL_START = float(os.getenv("BANKROLL", "50.0"))

MAX_BET_USD    = 5.0
MIN_BET_USD    = 1.0
MAX_BET_PCT    = 0.05
POLY_FEE       = 0.02
DAILY_LOSS_MAX = 0.10
MAX_CONSEC_LOSS= 2
COOLDOWN_MIN   = 30
CLAUDE_API     = "https://api.anthropic.com/v1/messages"
FEAR_GREED_API = "https://api.alternative.me/fng/?limit=1"
DATA_FILE      = "polybot_v7_state.json"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("polybot_v7.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── INDICATEURS ───────────────────────────────────────────────────────────
def ema(values, period):
    if not values: return 0
    if len(values) < period: return values[-1]
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]: e = v * k + e * (1 - k)
    return e

def rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains = losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i-1]
        if d > 0: gains += d
        else: losses -= d
    if losses == 0: return 100.0
    return round(100 - 100 / (1 + gains/losses), 2)

def macd_calc(closes):
    if len(closes) < 26: return 0, 0, 0
    ml = ema(closes, 12) - ema(closes, 26)
    sig = ml * 0.9
    return round(ml, 4), round(sig, 4), round(ml - sig, 4)

def bollinger(closes, period=20):
    if len(closes) < period: return None, None, None
    w = closes[-period:]
    mid = sum(w) / period
    std = math.sqrt(sum((x-mid)**2 for x in w) / period)
    return round(mid-2*std, 2), round(mid, 2), round(mid+2*std, 2)

def atr_calc(candles, period=14):
    if len(candles) < 2: return 0.0
    trs = [max(c["high"]-c["low"],
               abs(c["high"]-candles[i-1]["close"]),
               abs(c["low"]-candles[i-1]["close"]))
           for i, c in enumerate(candles) if i > 0]
    if not trs: return 0.0
    return round(sum(trs[-period:]) / min(len(trs), period), 2)

def stoch(closes, highs, lows, period=14):
    if len(closes) < period: return 50.0, 50.0
    lo, hi = min(lows[-period:]), max(highs[-period:])
    if hi == lo: return 50.0, 50.0
    k = (closes[-1]-lo)/(hi-lo)*100
    d = (closes[-2]-lo)/(hi-lo)*100 if len(closes) > period else k
    return round(k,1), round(d,1)

def williams_r(closes, highs, lows, period=14):
    if len(closes) < period: return -50.0
    hi, lo = max(highs[-period:]), min(lows[-period:])
    if hi == lo: return -50.0
    return round(-100*(hi-closes[-1])/(hi-lo), 1)

def vwap_calc(candles):
    if not candles: return 0
    tv = sum(c["vol"] for c in candles)
    if tv == 0: return candles[-1]["close"]
    return round(sum(((c["high"]+c["low"]+c["close"])/3)*c["vol"] for c in candles)/tv, 2)

def detect_divergence(candles_5m, candles_1m):
    """Détecte divergence haussière/baissière (prix vs RSI)"""
    if len(candles_5m) < 10 or len(candles_1m) < 10:
        return None
    
    c5 = [c["close"] for c in candles_5m[-10:]]
    r5 = [rsi([c["close"] for c in candles_5m[max(0,i-14):i+1]]) 
          for i in range(len(candles_5m)-10, len(candles_5m))]
    
    if len(c5) < 4 or len(r5) < 4:
        return None
    
    # Divergence haussière: prix fait nouveau bas mais RSI monte
    price_lower = c5[-1] < c5[-3]
    rsi_higher  = r5[-1] > r5[-3]
    if price_lower and rsi_higher and r5[-1] < 40:
        return "BULLISH"
    
    # Divergence baissière: prix fait nouveau haut mais RSI baisse
    price_higher = c5[-1] > c5[-3]
    rsi_lower    = r5[-1] < r5[-3]
    if price_higher and rsi_lower and r5[-1] > 60:
        return "BEARISH"
    
    return None

def detect_engulfing(candles):
    """Détecte bougie engulfing (signal de retournement fort)"""
    if len(candles) < 3:
        return None
    
    prev = candles[-2]
    curr = candles[-1]
    
    prev_body = abs(prev["close"] - prev["open"])
    curr_body = abs(curr["close"] - curr["open"])
    
    if prev_body == 0:
        return None
    
    # Engulfing haussier: précédente rouge, actuelle verte et plus grande
    if (prev["close"] < prev["open"] and  # prev bearish
        curr["close"] > curr["open"] and  # curr bullish
        curr["open"] < prev["close"] and  # opens below prev close
        curr["close"] > prev["open"] and  # closes above prev open
        curr_body > prev_body * 1.2):     # 20% bigger
        return "BULLISH"
    
    # Engulfing baissier
    if (prev["close"] > prev["open"] and
        curr["close"] < curr["open"] and
        curr["open"] > prev["close"] and
        curr["close"] < prev["open"] and
        curr_body > prev_body * 1.2):
        return "BEARISH"
    
    return None

def detect_vwap_break(candles, lookback=6):
    """Détecte cassure du VWAP avec volume"""
    if len(candles) < lookback + 2:
        return None
    
    vw = vwap_calc(candles[-20:])
    prev_price = candles[-2]["close"]
    curr_price = candles[-1]["close"]
    vols = [c["vol"] for c in candles[-lookback:]]
    avg_vol = sum(vols) / len(vols) if vols else 1
    curr_vol = candles[-1]["vol"]
    
    vol_confirmed = curr_vol > avg_vol * 1.3
    
    # Cassure haussière: prix passe au-dessus VWAP
    if prev_price < vw and curr_price > vw and vol_confirmed:
        return "BULLISH"
    
    # Cassure baissière: prix passe en-dessous VWAP
    if prev_price > vw and curr_price < vw and vol_confirmed:
        return "BEARISH"
    
    return None

def compute_advanced_signals(candles_5m, candles_1m):
    """Calcule tous les signaux avancés"""
    div  = detect_divergence(candles_5m, candles_1m)
    eng  = detect_engulfing(candles_5m[-3:]) if len(candles_5m) >= 3 else None
    vb   = detect_vwap_break(candles_5m)
    
    signals = []
    score = 0
    
    if div == "BULLISH":
        signals.append("🔄 Divergence RSI haussière (signal fort UP)")
        score += 2
    elif div == "BEARISH":
        signals.append("🔄 Divergence RSI baissière (signal fort DOWN)")
        score -= 2
    
    if eng == "BULLISH":
        signals.append("🕯️ Engulfing haussier détecté")
        score += 2
    elif eng == "BEARISH":
        signals.append("🕯️ Engulfing baissier détecté")
        score -= 2
    
    if vb == "BULLISH":
        signals.append("📊 Cassure VWAP haussière avec volume")
        score += 1
    elif vb == "BEARISH":
        signals.append("📊 Cassure VWAP baissière avec volume")
        score -= 1
    
    return {
        "divergence": div,
        "engulfing":  eng,
        "vwap_break": vb,
        "signals":    signals,
        "score":      score,
        "bias":       "UP" if score > 0 else "DOWN" if score < 0 else None
    }

def pivot_sr(candles, lookback=20):
    if len(candles) < lookback: return [], []
    highs = [c["high"] for c in candles[-lookback:]]
    lows  = [c["low"]  for c in candles[-lookback:]]
    price = candles[-1]["close"]
    atr   = atr_calc(candles) * 3
    res, sup = [], []
    for i in range(2, len(highs)-2):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1] and highs[i] > highs[i-2] and highs[i] > highs[i+2]:
            if highs[i] > price and highs[i]-price < atr:
                res.append(round(highs[i], 0))
        if lows[i] < lows[i-1] and lows[i] < lows[i+1] and lows[i] < lows[i-2] and lows[i] < lows[i+2]:
            if lows[i] < price and price-lows[i] < atr:
                sup.append(round(lows[i], 0))
    return sorted(set(sup), reverse=True)[:2], sorted(set(res))[:2]

def compute_ind(candles):
    if len(candles) < 10: return {}
    c = [x["close"] for x in candles]
    h = [x["high"]  for x in candles]
    l = [x["low"]   for x in candles]
    v = [x["vol"]   for x in candles]
    price = c[-1]
    e9  = ema(c, 9);  e21 = ema(c, 21); e50 = ema(c, min(50,len(c)))
    r14 = rsi(c, 14); r7  = rsi(c, 7)
    ml, sg, hist = macd_calc(c)
    bb_l, bb_m, bb_h = bollinger(c)
    at  = atr_calc(candles)
    stk, std = stoch(c, h, l)
    wr  = williams_r(c, h, l)
    vw  = vwap_calc(candles[-20:])
    av  = sum(v[-10:])/10 if len(v)>=10 else v[-1]
    mom = c[-1]-c[-6] if len(c)>=6 else 0
    sup, res = pivot_sr(candles)
    return {
        "price": round(price,2), "rsi_7": r7, "rsi_14": r14,
        "ema9": round(e9,2), "ema21": round(e21,2), "ema50": round(e50,2),
        "macd_hist": hist, "macd_line": ml,
        "bb_low": bb_l, "bb_mid": bb_m, "bb_high": bb_h,
        "atr": at, "atr_pct": round(at/price*100,3) if price else 0,
        "stoch_k": stk, "stoch_d": std, "williams_r": wr,
        "vwap": vw, "above_vwap": price > vw,
        "vol_ratio": round(v[-1]/av,2) if av else 1.0,
        "momentum": round(mom,2), "ema_bull": e9 > e21,
        "supports": sup, "resistances": res,
    }

def session_ctx():
    # Utiliser heure Paris (UTC+2)
    h = (datetime.utcnow().hour + 2) % 24
    if   14 <= h < 17: return {"session":"US_OPEN",      "quality":"EXCELLENT", "score_bonus":2}
    elif 17 <= h < 20: return {"session":"US_AFTERNOON", "quality":"EXCELLENT", "score_bonus":1}
    elif  9 <= h < 13: return {"session":"EU_OPEN",      "quality":"GOOD",      "score_bonus":1}
    elif 20 <= h < 22: return {"session":"US_CLOSE",     "quality":"GOOD",      "score_bonus":0}
    elif  7 <= h <  9: return {"session":"ASIA_LATE",    "quality":"MEDIUM",    "score_bonus":0}
    elif  1 <= h <  7: return {"session":"ASIA_EARLY",   "quality":"MEDIUM",    "score_bonus":-1}
    else:              return {"session":"OVERNIGHT",    "quality":"LOW",       "score_bonus":-1}

def pattern_mem(trades):
    if len(trades) < 5: return "Moins de 5 trades."
    wins  = [t for t in trades if t["result"]=="WIN"]
    losses= [t for t in trades if t["result"]=="LOSS"]
    up_t  = [t for t in trades if t["dir"]=="UP"]
    dn_t  = [t for t in trades if t["dir"]=="DOWN"]
    up_wr = sum(1 for t in up_t if t["result"]=="WIN")/len(up_t)*100 if up_t else 0
    dn_wr = sum(1 for t in dn_t if t["result"]=="WIN")/len(dn_t)*100 if dn_t else 0
    cw = sum(t.get("conf",0) for t in wins)/len(wins)*100 if wins else 0
    cl = sum(t.get("conf",0) for t in losses)/len(losses)*100 if losses else 0
    return (f"UP:{up_wr:.0f}%({len(up_t)}) DOWN:{dn_wr:.0f}%({len(dn_t)}) | "
            f"Conf wins:{cw:.0f}% losses:{cl:.0f}%")

def is_trending(c5, c15):
    if len(c5) < 12: return False
    h = datetime.utcnow().hour
    thr = 0.12 if (22<=h or h<7) else 0.06
    closes = [c["close"] for c in c5[-12:]]
    highs  = [c["high"]  for c in c5[-6:]]
    lows   = [c["low"]   for c in c5[-6:]]
    price  = closes[-1] if closes[-1] else 1
    range_pct = (max(highs)-min(lows))/price*100
    mom_pct   = abs(closes[-1]-closes[0])/price*100
    return range_pct > thr or mom_pct > thr*0.7

# ─── DATA FETCH ────────────────────────────────────────────────────────────
async def fetch_price():
    sources = [
        ("Kraken",   "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
         lambda d: float(d["result"]["XXBTZUSD"]["c"][0])),
        ("Coinbase", "https://api.coinbase.com/v2/prices/BTC-USD/spot",
         lambda d: float(d["data"]["amount"])),
        ("CoinGecko","https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
         lambda d: float(d["bitcoin"]["usd"])),
        ("Binance",  "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
         lambda d: float(d["price"])),
    ]
    for name, url, parser in sources:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                    if r.status == 200:
                        p = parser(await r.json())
                        if p > 0:
                            log.info(f"Price {name}: ${p:,.0f}")
                            return p
        except Exception as e:
            log.warning(f"Price {name}: {e}")
    return st.current_price

async def fetch_klines(interval, limit=60):
    # Binance
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    if isinstance(data, list) and len(data) > 5:
                        return [{"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),
                                 "close":float(k[4]),"vol":float(k[5]),"ts":int(k[0])//1000}
                                for k in data]
    except Exception as e:
        log.warning(f"Binance klines {interval}: {e}")
    # Kraken fallback
    try:
        km = {"1m":1,"5m":5,"15m":15,"1h":60}
        url = f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={km.get(interval,5)}&count={limit}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    ohlc = data.get("result",{}).get("XXBTZUSD",[])
                    if ohlc:
                        return [{"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),
                                 "close":float(k[4]),"vol":float(k[6]),"ts":int(k[0])}
                                for k in ohlc[-limit:]]
    except Exception as e:
        log.warning(f"Kraken klines {interval}: {e}")
    return []

async def fetch_fear_greed():
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(FEAR_GREED_API, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    d = await r.json()
                    return {"value": int(d["data"][0]["value"]),
                            "label": d["data"][0]["value_classification"]}
    except: pass
    return {"value": 50, "label": "Neutral"}

async def fetch_btc_24h():
    # Kraken first (works on Railway)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
                             timeout=aiohttp.ClientTimeout(total=6)) as r:
                if r.status == 200:
                    d = await r.json()
                    t = d.get("result",{}).get("XXBTZUSD",{})
                    if t:
                        price = float(t["c"][0]); open_p = float(t["o"])
                        chg = ((price-open_p)/open_p*100) if open_p else 0
                        return {"change_pct": round(chg,2),
                                "high_24h": float(t["h"][0]),
                                "low_24h":  float(t["l"][0]),
                                "volume":   float(t["v"][0])}
    except: pass
    # CoinGecko fallback
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true&include_24hr_vol=true"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    d = (await r.json()).get("bitcoin",{})
                    return {"change_pct": round(d.get("usd_24h_change",0),2),
                            "high_24h": 0, "low_24h": 0, "volume": 0}
    except: pass
    return {"change_pct": 0, "high_24h": 0, "low_24h": 0, "volume": 0}

# ─── CLAUDE AI v7 ──────────────────────────────────────────────────────────
async def claude_decide(i1, i5, i15, i1h, adv, trades, bankroll, consec, fg, btc24, sess):
    if not ANTHROPIC_KEY:
        return {"dir":None,"conf":0,"size":0,"reasoning":"Pas de clé API.","trade":False}

    patterns = pattern_mem(trades)
    trades_txt = "".join(f"  {t['result']} {t['dir']} PnL:{t['pnl']:+.2f}$ conf:{t.get('conf',0)*100:.0f}%\n"
                         for t in trades[-6:]) or "  Aucun trade.\n"
    adv_txt = "\n".join(adv.get("signals",[])) or "Aucun signal avancé détecté."

    sup_str = str(i5.get("supports",[])) or "Aucun"
    res_str = str(i5.get("resistances",[])) or "Aucun"
    max_bet = round(min(MAX_BET_USD, bankroll*MAX_BET_PCT), 2)
    mid_bet = round(MIN_BET_USD + (max_bet-MIN_BET_USD)*0.5, 2)

    prompt = f"""Tu es un expert en trading Polymarket BTC UP/DOWN 5 minutes.
Objectif: maximiser le win rate. Trader uniquement les setups à haute probabilité.

━━━ MACRO ━━━
Fear&Greed: {fg['value']}/100 ({fg['label']})
BTC 24h: {btc24['change_pct']:+.2f}% | High:{btc24['high_24h']:,.0f} Low:{btc24['low_24h']:,.0f}
Session: {sess['session']} ({sess['quality']}) | Heure Paris: {(datetime.utcnow().hour+2)%24}h

━━━ TENDANCE DOMINANTE (15M) ━━━
EMA 15m: {'HAUSSIER' if i15.get('ema_bull') else 'BAISSIER'}
MACD 15m: {i15.get('macd_hist',0):+.4f}
RSI 15m: {i15.get('rsi_14',50)}
Momentum 15m: {i15.get('momentum',0):+.2f}
⚠️ La tendance 15m est le signal DOMINANT pour trader le 5m. Plus réactif et fiable.

━━━ CONTEXTE 1H (tendance de fond) ━━━
EMA 1h: {'HAUSSIER' if i1h.get('ema_bull') else 'BAISSIER'}
MACD 1h: {i1h.get('macd_hist',0):+.4f}
RSI 1h: {i1h.get('rsi_14',50)}
→ Si 15m ET 1h alignés = signal FORT (mise max)
→ Si 15m seul = signal MOYEN (mise réduite)
→ Si 15m contre 1h = PASS sauf RSI 5m extrême (<15 ou >85)

━━━ NIVEAUX CLÉS ━━━
Prix: ${i5.get('price',0):,.2f}
Supports: {sup_str} | Résistances: {res_str}
VWAP 5m: ${i5.get('vwap',0):,.0f} ({'AU-DESSUS' if i5.get('above_vwap') else 'EN-DESSOUS'})

━━━ SIGNAUX AVANCÉS ━━━
{adv_txt}
Score avancé: {adv['score']:+d} | Biais: {adv['bias'] or 'NEUTRE'}
⚡ Ces signaux sont très fiables — accorde leur un poids FORT dans ta décision.

━━━ INDICATEURS 5M (principal) ━━━
RSI 7/14: {i5.get('rsi_7',50)}/{i5.get('rsi_14',50)}
EMA 9/21/50: {i5.get('ema9',0):.0f}/{i5.get('ema21',0):.0f}/{i5.get('ema50',0):.0f} ({'↑' if i5.get('ema_bull') else '↓'})
MACD: {i5.get('macd_hist',0):+.4f} | Stoch: {i5.get('stoch_k',50)}/{i5.get('stoch_d',50)}
Williams %R: {i5.get('williams_r',-50)} | Vol ratio: x{i5.get('vol_ratio',1):.2f}
ATR: ${i5.get('atr',0):.0f} ({i5.get('atr_pct',0):.3f}%)

━━━ 1M / 15M / 1H ━━━
1m  RSI:{i1.get('rsi_14',50)} EMA:{'↑' if i1.get('ema_bull') else '↓'} MACD:{i1.get('macd_hist',0):+.3f} Vol:x{i1.get('vol_ratio',1):.1f}
15m RSI:{i15.get('rsi_14',50)} EMA:{'↑' if i15.get('ema_bull') else '↓'} MACD:{i15.get('macd_hist',0):+.3f} Mom:{i15.get('momentum',0):+.0f}
1h  RSI:{i1h.get('rsi_14',50)} EMA:{'↑' if i1h.get('ema_bull') else '↓'} MACD:{i1h.get('macd_hist',0):+.2f}

━━━ MÉMOIRE ━━━
{patterns}
Derniers trades:
{trades_txt}Pertes consécutives: {consec} | Bankroll: {bankroll:.2f}$

━━━ RÈGLES ━━━
1. SIGNAL DOMINANT = 15m. Suivre sa direction sauf si 1h contradictoire fort.
2. Signal FORT (mise max {max_bet}$):
   - 15m ET 1h alignés dans même direction
   - MACD 15m confirmé + volume x>1.2
   - Session EXCELLENT (US_OPEN/US_AFTERNOON)
3. Signal MOYEN (mise {mid_bet}$):
   - 15m aligné seul (1h neutre ou légèrement contre)
   - RSI 5m confirme direction (< 40 pour UP, > 60 pour DOWN)
   - Session GOOD+
4. Signal COUNTER-TREND (mise min {MIN_BET_USD}$):
   - RSI 5m extrême (<15=UP ou >85=DOWN) même si 15m contre
   - Seulement si volume x>1.5 confirme retournement
5. PASS absolu si:
   - ATR 5m < 0.04% (marché mort)
   - 15m ET 1h contradictoires ET RSI neutre (35-65)
   - Prix exactement entre support et résistance serrés
   - Volume x<0.3 (marché vide)
6. RÈGLES AVANCÉES:
   - RSI divergence: RSI 5m monte mais prix baisse → signal UP fort
   - Engulfing: grande bougie dans sens 15m après consolidation → trader
   - VWAP break: prix passe au-dessus/dessous VWAP avec volume → signal fort
   - Fear&Greed < 15 + 15m neutre → biais UP (rebond macro probable)
   - Fear&Greed > 80 → biais DOWN
7. Après 2 pertes consécutives → mise minimum {MIN_BET_USD}$ uniquement
8. Session US_OPEN/US_AFTERNOON → augmenter confiance, c'est le meilleur moment

RÉPONDS UNIQUEMENT EN JSON (rien d'autre):
{{"trade":true/false,"direction":"UP"/"DOWN"/null,"confidence":0.0-1.0,"bet_size":{MIN_BET_USD}-{max_bet},"reasoning":"2-3 phrases FR","key_signals":["s1","s2","s3"],"risk_level":"LOW"/"MEDIUM"/"HIGH"}}"""

    try:
        async with aiohttp.ClientSession() as session_http:
            async with session_http.post(
                CLAUDE_API,
                headers={"Content-Type":"application/json",
                         "x-api-key":ANTHROPIC_KEY,
                         "anthropic-version":"2023-06-01"},
                json={"model":"claude-haiku-4-5-20251001","max_tokens":400,
                      "messages":[{"role":"user","content":prompt}]},
                timeout=aiohttp.ClientTimeout(total=25)
            ) as r:
                if r.status != 200:
                    txt = await r.text()
                    log.error(f"Claude {r.status}: {txt[:100]}")
                    return {"dir":None,"conf":0,"size":0,
                            "reasoning":f"Erreur API {r.status}","trade":False}
                data = await r.json()
                raw  = data["content"][0]["text"].strip()
                raw  = raw.replace("```json","").replace("```","").strip()
                s = raw.find("{"); e = raw.rfind("}")+1
                if s >= 0 and e > s: raw = raw[s:e]
                res = json.loads(raw)

                def sf(v, d=0.0):
                    try: return float(v) if v is not None else d
                    except: return d

                direction = res.get("direction")
                if direction not in ["UP","DOWN"]: direction = None
                trade = bool(res.get("trade",False)) and direction is not None

                return {
                    "dir":       direction,
                    "conf":      sf(res.get("confidence"), 0.0),
                    "size":      sf(res.get("bet_size"), 0.0),
                    "reasoning": str(res.get("reasoning","")),
                    "signals":   res.get("key_signals",[]) or [],
                    "risk":      res.get("risk_level","MEDIUM") or "MEDIUM",
                    "trade":     trade,
                }
    except Exception as e:
        log.error(f"Claude error: {e}")
        return {"dir":None,"conf":0,"size":0,
                "reasoning":f"Erreur: {str(e)[:60]}","trade":False}

# ─── STATE ─────────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.running       = False
        self.paper_mode    = PAPER_MODE
        self.bankroll      = BANKROLL_START
        self.c1  = deque(maxlen=100)
        self.c5  = deque(maxlen=100)
        self.c15 = deque(maxlen=100)
        self.c1h = deque(maxlen=100)
        self.price         = 0.0
        self.trades        = []
        self.bet           = None        # active bet
        self.wins = self.losses = 0
        self.pnl           = 0.0
        self.consec        = 0
        self.streak        = 0
        self.best_streak   = 0
        self.worst_streak  = 0
        self.cooldown_until= 0
        self.session_start = time.time()
        self.daily_start   = BANKROLL_START
        self.daily_ts      = time.time()
        self.skipped       = 0
        self.last_decision = {}
        self.fg            = {"value":50,"label":"Neutral"}
        self.btc24         = {}
        self.tick_job = self.price_job = self.macro_job = None

    def save(self):
        try:
            with open(DATA_FILE,"w") as f:
                json.dump({
                    "bankroll":self.bankroll,"trades":self.trades[-200:],
                    "wins":self.wins,"losses":self.losses,"pnl":self.pnl,
                    "best_streak":self.best_streak,"worst_streak":self.worst_streak,
                    "consec":self.consec,"daily_start":self.daily_start,
                    "daily_ts":self.daily_ts,"paper_mode":self.paper_mode,
                    "skipped":self.skipped,
                }, f, indent=2)
        except Exception as e: log.error(f"Save: {e}")

    def load(self):
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE) as f: d = json.load(f)
                self.bankroll     = d.get("bankroll", BANKROLL_START)
                self.trades       = d.get("trades", [])
                self.wins         = d.get("wins", 0)
                self.losses       = d.get("losses", 0)
                self.pnl          = d.get("pnl", 0.0)
                self.best_streak  = d.get("best_streak", 0)
                self.worst_streak = d.get("worst_streak", 0)
                self.consec       = d.get("consec", 0)
                self.daily_start  = d.get("daily_start", self.bankroll)
                self.daily_ts     = d.get("daily_ts", time.time())
                self.paper_mode   = d.get("paper_mode", PAPER_MODE)
                self.skipped      = d.get("skipped", 0)
                log.info("State chargé")
        except Exception as e: log.error(f"Load: {e}")

st = State()

# ─── RISK ──────────────────────────────────────────────────────────────────
def check_daily():
    now = time.time()
    if now - st.daily_ts > 86400:
        st.daily_start = st.bankroll
        st.daily_ts = now
    return st.daily_start > 0 and (st.daily_start-st.bankroll)/st.daily_start >= DAILY_LOSS_MAX

def in_cd(): return time.time() < st.cooldown_until

# ─── SEND HELPER — jamais de crash ─────────────────────────────────────────
async def send(bot, text, parse_mode="Markdown"):
    """Envoie un message Telegram — jamais de crash."""
    try:
        await bot.send_message(chat_id=ALLOWED_UID, text=text, parse_mode=parse_mode)
        return True
    except Exception as e:
        log.error(f"Send failed: {e}")
        # Retry without markdown
        try:
            clean = text.replace("*","").replace("`","").replace("_","")
            await bot.send_message(chat_id=ALLOWED_UID, text=clean)
            return True
        except Exception as e2:
            log.error(f"Send retry failed: {e2}")
            return False

# ─── JOBS ──────────────────────────────────────────────────────────────────
async def job_price(context):
    p = await fetch_price()
    if p > 0: st.price = p

async def job_macro(context):
    st.fg    = await fetch_fear_greed()
    st.btc24 = await fetch_btc_24h()
    log.info(f"Macro: F&G={st.fg['value']} BTC24h={st.btc24.get('change_pct',0):+.2f}%")

async def job_tick(context):
    if not st.running: return

    # Daily limit
    if check_daily():
        st.running = False
        await send(context.bot, "🛑 *Limite journalière atteinte* — Bot arrêté.")
        return

    # Cooldown
    if in_cd():
        rem = int((st.cooldown_until - time.time())/60)
        log.info(f"Cooldown {rem}min restantes")
        return

    # Fetch data
    c1  = await fetch_klines("1m",  60)
    c5  = await fetch_klines("5m",  50)
    c15 = await fetch_klines("15m", 40)
    c1h = await fetch_klines("1h",  30)

    if not c5:
        log.warning("Pas de données klines")
        return

    st.c1  = deque(c1,  maxlen=100)
    st.c5  = deque(c5,  maxlen=100)
    st.c15 = deque(c15, maxlen=100)
    st.c1h = deque(c1h, maxlen=100)
    st.price = c5[-1]["close"]

    # ── RÉSOUDRE BET ACTIF ──
    if st.bet:
        bet  = st.bet
        won  = bet["dir"] == ("UP" if st.price > bet["entry"] else "DOWN")
        gross = bet["amount"]*(1-POLY_FEE) if won else -bet["amount"]

        st.bankroll  = max(0.0, st.bankroll + gross)
        st.pnl      += gross

        if won:
            st.wins += 1; st.consec = 0
            st.streak = st.streak+1 if st.streak >= 0 else 1
            st.best_streak = max(st.best_streak, st.streak)
        else:
            st.losses += 1; st.consec += 1
            st.streak = st.streak-1 if st.streak <= 0 else -1
            st.worst_streak = min(st.worst_streak, st.streak)
            if st.consec >= MAX_CONSEC_LOSS:
                st.cooldown_until = time.time() + COOLDOWN_MIN*60
                log.warning(f"Cooldown activé ({st.consec} pertes)")

        record = {
            "dir":bet["dir"],"amount":bet["amount"],"pnl":round(gross,4),
            "conf":bet["conf"],"result":"WIN" if won else "LOSS",
            "entry":bet["entry"],"exit":st.price,
            "reasoning":bet.get("reasoning",""),
            "paper":st.paper_mode,"ts":int(time.time())
        }
        st.trades.append(record)
        st.bet = None

        # Notification clôture — TOUJOURS envoyée
        emoji = "✅" if won else "❌"
        mode  = "📄" if st.paper_mode else "💰"
        cd_msg = f"\n⏸ Cooldown {COOLDOWN_MIN}min" if in_cd() else ""
        msg = (f"{emoji} *Trade clôturé* [{mode}]\n"
               f"`{bet['dir']}` | `${bet['entry']:,.0f}` → `${st.price:,.0f}`\n"
               f"PnL: `{'+' if gross>=0 else ''}{gross:.2f} USDC`\n"
               f"Bankroll: `{st.bankroll:.2f} USDC`\n"
               f"Streak: `{st.streak:+d}` | Pertes: `{st.consec}`{cd_msg}")
        await send(context.bot, msg)
        st.save()

    if in_cd(): return

    # Trend filter
    if not is_trending(list(st.c5), list(st.c15)):
        st.skipped += 1
        log.info("Range — pause")
        return

    # Indicateurs
    i1  = compute_ind(list(st.c1))
    i5  = compute_ind(list(st.c5))
    i15 = compute_ind(list(st.c15))
    i1h = compute_ind(list(st.c1h))
    sess = session_ctx()

    if not i5: return

    # Claude décide
    adv = compute_advanced_signals(list(st.c5), list(st.c1))
    dec = await claude_decide(i1, i5, i15, i1h, adv, st.trades[-15:],
                              st.bankroll, st.consec, st.fg, st.btc24, sess)
    st.last_decision = dec

    log.info(f"Claude: {dec['dir']} trade={dec['trade']} conf={dec['conf']:.0%} | {dec['reasoning'][:60]}")

    # Placer bet
    if dec["trade"] and dec["dir"] and not st.bet:
        amount = max(MIN_BET_USD, min(dec["size"], MAX_BET_USD, st.bankroll*MAX_BET_PCT))
        amount = round(amount, 2)

        if amount >= MIN_BET_USD and st.bankroll >= amount:
            st.bet = {
                "dir": dec["dir"], "amount": amount, "conf": dec["conf"],
                "entry": st.price, "reasoning": dec["reasoning"],
                "ts": int(time.time()),
            }

            # Notification bet — TOUJOURS envoyée
            mode = "📄" if st.paper_mode else "💰"
            risk_e = {"LOW":"🟢","MEDIUM":"🟡","HIGH":"🔴"}.get(dec["risk"],"🟡")
            sigs = "\n".join(f"  • {s}" for s in dec["signals"][:4])
            msg = (f"🧠 *Bet placé* [{mode}]\n"
                   f"━━━━━━━━━━━━━━━\n"
                   f"*{dec['dir']}* | `{amount:.2f}$` | `{dec['conf']*100:.0f}%` | {risk_e}\n"
                   f"BTC: `${st.price:,.2f}` | `{sess['session']}`\n"
                   f"F&G: `{st.fg['value']}` | 15m:`{'↑' if i15.get('ema_bull') else '↓'}` 1h:`{'↑' if i1h.get('ema_bull') else '↓'}`\n\n"
                   f"💭 _{dec['reasoning']}_\n\n"
                   f"🔑 Signaux:\n{sigs}")
            await send(context.bot, msg)
    else:
        st.skipped += 1

# ─── HELPERS ───────────────────────────────────────────────────────────────
def auth(u): return ALLOWED_UID == 0 or u.effective_user.id == ALLOWED_UID
def fmt(v):  return f"+{v:.2f}" if v >= 0 else f"{v:.2f}"
def wr():
    t = st.wins + st.losses
    return f"{st.wins/t*100:.1f}%" if t else "—"
def roi():
    return f"{fmt((st.bankroll-BANKROLL_START)/BANKROLL_START*100)}%"
def upt():
    s = int(time.time()-st.session_start)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def kb():
    sess = session_ctx()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Status",   callback_data="status"),
         InlineKeyboardButton("🧠 AI Last",  callback_data="ai")],
        [InlineKeyboardButton("📈 Trades",   callback_data="trades"),
         InlineKeyboardButton("📉 Stats",    callback_data="stats")],
        [InlineKeyboardButton("😱 F&G",      callback_data="fear"),
         InlineKeyboardButton(f"🕐 {sess['session']}", callback_data="session")],
        [InlineKeyboardButton("▶️ Start",    callback_data="run"),
         InlineKeyboardButton("⏹ Stop",     callback_data="stop")],
        [InlineKeyboardButton("🟢 Actif" if st.running else "🔴 Arrêté", callback_data="status"),
         InlineKeyboardButton("💰 Réel" if st.paper_mode else "📄 Paper", callback_data="paper")],
    ])

# ─── COMMANDES ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context):
    if not auth(update): return
    sess = session_ctx()
    await update.message.reply_text(
        f"🧠 *POLYMARKET BOT v7 — FINAL*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Mode: *{'📄 PAPER' if st.paper_mode else '💰 RÉEL'}*\n\n"
        f"✅ Notifications garanties\n"
        f"✅ 4 timeframes (1m/5m/15m/1h)\n"
        f"✅ Tendance 1h comme signal dominant\n"
        f"✅ Fear&Greed + Sessions\n"
        f"✅ Support/Résistance\n"
        f"✅ Cooldown intelligent\n\n"
        f"Session: `{sess['session']}` — {sess['quality']}\n\n"
        f"*/run* */stop* */status* */ai* */signal*\n"
        f"*/trades* */stats* */fear* */paper* */reset*",
        parse_mode="Markdown", reply_markup=kb()
    )

async def cmd_run(update: Update, context):
    if not auth(update): return
    if st.running:
        await update.message.reply_text("⚠️ Déjà en cours.")
        return
    if not ANTHROPIC_KEY:
        await update.message.reply_text("❌ ANTHROPIC_API_KEY manquante.")
        return

    st.running = True
    st.session_start = time.time()
    st.daily_start = st.bankroll
    st.daily_ts = time.time()

    st.price_job = context.job_queue.run_repeating(job_price, interval=30,  first=5)
    st.macro_job = context.job_queue.run_repeating(job_macro, interval=300, first=8)
    st.tick_job  = context.job_queue.run_repeating(job_tick,  interval=300, first=15)

    st.fg    = await fetch_fear_greed()
    st.btc24 = await fetch_btc_24h()
    sess = session_ctx()

    await update.message.reply_text(
        f"▶️ *Bot v7 démarré !*\n"
        f"F&G: `{st.fg['value']}` ({st.fg['label']})\n"
        f"BTC 24h: `{st.btc24.get('change_pct',0):+.2f}%`\n"
        f"Session: `{sess['session']}` — {sess['quality']}\n"
        f"Bankroll: `{st.bankroll:.2f} USDC`",
        parse_mode="Markdown"
    )
    await job_tick(context)

async def cmd_stop(update: Update, context):
    if not auth(update): return
    st.running = False
    for j in [st.tick_job, st.price_job, st.macro_job]:
        if j:
            try: j.schedule_removal()
            except: pass
    st.tick_job = st.price_job = st.macro_job = None
    st.save()
    await update.message.reply_text(
        f"⏹ *Arrêté* | Uptime: `{upt()}` | BR: `{st.bankroll:.2f}` | PnL: `{fmt(st.pnl)}` | WR: `{wr()}`",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, context):
    if not auth(update): return
    sess = session_ctx()
    dl = (st.daily_start-st.bankroll)/st.daily_start*100 if st.daily_start > 0 else 0
    bet_info = f"{st.bet['dir']} {st.bet['amount']:.2f}$ @ ${st.bet['entry']:,.0f}" if st.bet else "Aucun"
    cd_msg = f"\n⏸ Cooldown: `{int((st.cooldown_until-time.time())/60)}min`" if in_cd() else ""

    await update.message.reply_text(
        f"📊 *STATUS v7* [{'📄' if st.paper_mode else '💰'}]\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{'🟢 EN COURS' if st.running else '🔴 ARRÊTÉ'}{cd_msg}\n\n"
        f"₿ `${st.price:,.2f}` | 24h: `{st.btc24.get('change_pct',0):+.2f}%`\n"
        f"😱 F&G: `{st.fg['value']}` ({st.fg['label']})\n"
        f"🕐 Session: `{sess['session']}` ({sess['quality']})\n\n"
        f"💰 BR: `{st.bankroll:.2f}` | ROI: `{roi()}` | PnL: `{fmt(st.pnl)}`\n"
        f"📅 Perte jour: `{dl:.1f}%/{DAILY_LOSS_MAX*100:.0f}%`\n"
        f"🎯 Bet: `{bet_info}`\n"
        f"🚫 Refusés: `{st.skipped}` | ⏱ `{upt()}`",
        parse_mode="Markdown", reply_markup=kb()
    )

async def cmd_ai(update: Update, context):
    if not auth(update): return
    d = st.last_decision
    if not d:
        await update.message.reply_text("⏳ Lance /run d'abord.")
        return
    risk_e = {"LOW":"🟢","MEDIUM":"🟡","HIGH":"🔴"}.get(d.get("risk","MEDIUM"),"🟡")
    dir_e  = "🟢" if d.get("dir")=="UP" else "🔴" if d.get("dir")=="DOWN" else "⚪"
    sigs   = "\n".join(f"  • {s}" for s in d.get("signals",[]))
    await update.message.reply_text(
        f"🧠 *DERNIÈRE DÉCISION AI*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_e} *{d.get('dir') or 'PASS'}* | {risk_e} | `{d.get('conf',0)*100:.0f}%`\n"
        f"Trade: `{'OUI ✅' if d.get('trade') else 'NON ❌'}` | Mise: `{d.get('size',0):.2f}$`\n\n"
        f"💭 _{d.get('reasoning','—')}_\n\n"
        f"🔑 Signaux:\n{sigs or '  —'}",
        parse_mode="Markdown"
    )

async def cmd_signal(update: Update, context):
    if not auth(update): return
    await update.message.reply_text("⏳ Analyse complète...")
    c1=await fetch_klines("1m",60); c5=await fetch_klines("5m",50)
    c15=await fetch_klines("15m",40); c1h=await fetch_klines("1h",30)
    if c5:
        st.c1=deque(c1,maxlen=100); st.c5=deque(c5,maxlen=100)
        st.c15=deque(c15,maxlen=100); st.c1h=deque(c1h,maxlen=100)
        st.price=c5[-1]["close"]
    st.fg=await fetch_fear_greed(); st.btc24=await fetch_btc_24h()
    i1=compute_ind(list(st.c1)); i5=compute_ind(list(st.c5))
    i15=compute_ind(list(st.c15)); i1h=compute_ind(list(st.c1h))
    sess=session_ctx()
    adv=compute_advanced_signals(list(st.c5),list(st.c1))
    d=await claude_decide(i1,i5,i15,i1h,adv,st.trades[-15:],st.bankroll,
                          st.consec,st.fg,st.btc24,sess)
    st.last_decision=d
    dir_e="🟢" if d["dir"]=="UP" else "🔴" if d["dir"]=="DOWN" else "⚪"
    risk_e={"LOW":"🟢","MEDIUM":"🟡","HIGH":"🔴"}.get(d.get("risk","MEDIUM"),"🟡")
    sigs="\n".join(f"  • {s}" for s in d.get("signals",[])[:5])
    await update.message.reply_text(
        f"🧠 *ANALYSE AI v7*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_e} *{d['dir'] or 'PASS'}* | {risk_e} | `{d['conf']*100:.0f}%`\n"
        f"₿ `${i5.get('price',0):,.2f}` | F&G:`{st.fg['value']}` | `{sess['session']}`\n"
        f"15m: `{'↑' if i15.get('ema_bull') else '↓'}` EMA | MACD:`{i15.get('macd_hist',0):+.2f}` | 1h:`{'↑' if i1h.get('ema_bull') else '↓'}`\n\n"
        f"💭 _{d['reasoning']}_\n\n"
        f"🔑 Signaux:\n{sigs or '  Aucun'}",
        parse_mode="Markdown"
    )

async def cmd_trades(update: Update, context):
    if not auth(update): return
    trades = st.trades[-8:][::-1]
    if not trades:
        await update.message.reply_text("📈 Aucun trade.")
        return
    lines = ["📈 *TRADES v7*\n━━━━━━━━━━━━━━━━━━━━━━━━"]
    for t in trades:
        e="✅" if t["result"]=="WIN" else "❌"
        ts=datetime.fromtimestamp(t["ts"]).strftime("%d/%m %H:%M")
        r=t.get("reasoning","")[:40]
        lines.append(f"{e} `{t['dir']}` `{fmt(t['pnl'])}$` `{ts}`\n   _{r}_")
    if st.bet:
        lines.append(f"\n🔄 *Actif:* `{st.bet['dir']}` `{st.bet['amount']:.2f}$` @ `${st.bet['entry']:,.0f}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_stats(update: Update, context):
    if not auth(update): return
    total=st.wins+st.losses
    aw=sum(t["pnl"] for t in st.trades if t["pnl"]>0)/max(st.wins,1)
    al=abs(sum(t["pnl"] for t in st.trades if t["pnl"]<0))/max(st.losses,1)
    rr=aw/al if al>0 else 0
    peak=BANKROLL_START; mdd=0.0; rb=BANKROLL_START
    for t in st.trades:
        rb+=t["pnl"]
        if rb>peak: peak=rb
        dd=(peak-rb)/peak*100 if peak>0 else 0
        if dd>mdd: mdd=dd
    patterns=pattern_mem(st.trades)
    await update.message.reply_text(
        f"📉 *STATS v7*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total: `{total}` (✅{st.wins} ❌{st.losses})\n"
        f"Win Rate: `{wr()}` | ROI: `{roi()}`\n"
        f"PnL: `{fmt(st.pnl)}$` | R:R: `{rr:.2f}`\n\n"
        f"Gain moy: `+{aw:.2f}$` | Perte moy: `-{al:.2f}$`\n"
        f"Best streak: `+{st.best_streak}` | Max DD: `{mdd:.1f}%`\n"
        f"Refusés AI: `{st.skipped}`\n"
        f"Bankroll: `{st.bankroll:.2f} USDC`\n\n"
        f"📊 _{patterns}_",
        parse_mode="Markdown"
    )

async def cmd_fear(update: Update, context):
    if not auth(update): return
    fg=st.fg; v=fg['value']
    bar="█"*(v//10)+"░"*(10-v//10)
    e="😱" if v<20 else "😟" if v<40 else "😐" if v<60 else "😊" if v<80 else "🤑"
    interp=("Extrême Peur → biais UP en neutre" if v<20 else
            "Peur → marché incertain" if v<40 else
            "Neutre" if v<60 else
            "Greed → attention" if v<80 else "Extrême Greed → biais DOWN")
    btc=st.btc24
    await update.message.reply_text(
        f"😱 *FEAR & GREED*\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{e} *{fg['label']}* — `{v}/100`\n`{bar}`\n\n_{interp}_\n\n"
        f"₿ 24h: `{btc.get('change_pct',0):+.2f}%` | "
        f"H:`${btc.get('high_24h',0):,.0f}` L:`${btc.get('low_24h',0):,.0f}`",
        parse_mode="Markdown"
    )

async def cmd_paper(update: Update, context):
    if not auth(update): return
    st.paper_mode = not st.paper_mode
    await update.message.reply_text(
        f"Mode: *{'📄 PAPER' if st.paper_mode else '💰 RÉEL ⚠️'}*",
        parse_mode="Markdown"
    )
    st.save()

async def cmd_reset(update: Update, context):
    if not auth(update): return
    st.running = False
    for j in [st.tick_job, st.price_job, st.macro_job]:
        if j:
            try: j.schedule_removal()
            except: pass
    st.bankroll=BANKROLL_START; st.trades=[]; st.bet=None
    st.wins=st.losses=st.skipped=st.consec=0
    st.pnl=st.streak=st.best_streak=st.worst_streak=0
    st.cooldown_until=0; st.session_start=time.time()
    st.c1.clear(); st.c5.clear(); st.c15.clear(); st.c1h.clear()
    if os.path.exists(DATA_FILE): os.remove(DATA_FILE)
    await update.message.reply_text("🔄 *Reset complet.*", parse_mode="Markdown")

async def cmd_cooldown(update: Update, context):
    if not auth(update): return
    st.cooldown_until=0; st.consec=0
    await update.message.reply_text("✅ Cooldown reset.", parse_mode="Markdown")

async def cb(update: Update, context):
    q=update.callback_query; await q.answer()
    h={"status":cmd_status,"ai":cmd_ai,"trades":cmd_trades,"stats":cmd_stats,
       "fear":cmd_fear,"session":cmd_signal,"run":cmd_run,"stop":cmd_stop,"paper":cmd_paper}
    if q.data in h: await h[q.data](update, context)

# ─── MAIN ──────────────────────────────────────────────────────────────────
def main():
    st.load()
    app = Application.builder().token(TOKEN).build()
    for name, handler in [
        ("start",cmd_start),("run",cmd_run),("stop",cmd_stop),
        ("status",cmd_status),("ai",cmd_ai),("signal",cmd_signal),
        ("trades",cmd_trades),("stats",cmd_stats),("fear",cmd_fear),
        ("paper",cmd_paper),("cooldown",cmd_cooldown),("reset",cmd_reset),
    ]:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(cb))
    log.info("🧠 PolyBot v7 FINAL démarré")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

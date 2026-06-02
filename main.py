"""
╔══════════════════════════════════════════════════════════════╗
║     POLYMARKET BTC BOT v5 — CLAUDE AI BRAIN EDITION         ║
║     IA décisionnelle | Multi-TF | Risk management avancé    ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import os
import json
import time
import math
import aiohttp
from datetime import datetime, timedelta
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ─── CONFIG ────────────────────────────────────────────────────────────────
TOKEN           = os.getenv("TELEGRAM_TOKEN", "VOTRE_TOKEN_ICI")
ALLOWED_UID     = int(os.getenv("ALLOWED_USER_ID", "0"))
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
PAPER_MODE      = os.getenv("PAPER_MODE", "true").lower() == "true"
BANKROLL_START  = float(os.getenv("BANKROLL", "50.0"))

# Risk params
MAX_BET_USD     = 5.0
MIN_BET_USD     = 1.0
MAX_BET_PCT     = 0.05
POLY_FEE        = 0.02
DAILY_LOSS_MAX  = 0.10
MAX_CONSEC_LOSS = 3      # pause après N pertes consécutives
COOLDOWN_MIN    = 30     # minutes de pause après max pertes

# APIs
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
KRAKEN_PRICE   = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
COINBASE_PRICE = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
COINGECKO_URL  = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
CLAUDE_API     = "https://api.anthropic.com/v1/messages"
POLY_MARKETS   = "https://clob.polymarket.com/markets"

DATA_FILE = "polybot_v5_state.json"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("polybot_v5.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── INDICATEURS ───────────────────────────────────────────────────────────
def ema(values, period):
    if len(values) < period:
        return values[-1] if values else 0
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i-1]
        if d > 0: gains += d
        else: losses -= d
    if losses == 0: return 100.0
    return round(100 - 100 / (1 + gains/losses), 2)

def macd_calc(closes):
    if len(closes) < 26: return 0, 0, 0
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    ml  = e12 - e26
    sig = ml * 0.9
    return round(ml, 4), round(sig, 4), round(ml - sig, 4)

def bollinger(closes, period=20):
    if len(closes) < period: return None, None, None
    w = closes[-period:]
    mid = sum(w) / period
    std = math.sqrt(sum((x-mid)**2 for x in w) / period)
    return round(mid-2*std, 2), round(mid, 2), round(mid+2*std, 2)

def atr_calc(candles, period=14):
    if len(candles) < period+1: return 0.0
    trs = [max(c["high"]-c["low"], abs(c["high"]-candles[i-1]["close"]),
               abs(c["low"]-candles[i-1]["close"]))
           for i, c in enumerate(candles) if i > 0]
    return round(sum(trs[-period:]) / min(len(trs), period), 2)

def stoch(closes, highs, lows, period=14):
    if len(closes) < period: return 50.0, 50.0
    lo, hi = min(lows[-period:]), max(highs[-period:])
    if hi == lo: return 50.0, 50.0
    k = (closes[-1]-lo)/(hi-lo)*100
    d = (closes[-2]-lo)/(hi-lo)*100 if len(closes) >= period+1 else k
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

def compute_indicators(candles):
    """Calcule tous les indicateurs sur un set de bougies"""
    if len(candles) < 10:
        return {}
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    vols   = [c["vol"]   for c in candles]

    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    e50 = ema(closes, min(50, len(closes)))
    r14 = rsi(closes, 14)
    r7  = rsi(closes, 7)
    ml, sig, hist = macd_calc(closes)
    bb_l, bb_m, bb_h = bollinger(closes)
    atr = atr_calc(candles)
    stk, std = stoch(closes, highs, lows)
    wr  = williams_r(closes, highs, lows)
    vw  = vwap_calc(candles[-20:])
    avg_vol = sum(vols[-10:])/10 if len(vols) >= 10 else vols[-1]
    price = closes[-1]
    mom   = closes[-1] - closes[-6] if len(closes) >= 6 else 0
    atr_pct = (atr/price*100) if price > 0 else 0

    return {
        "price": round(price, 2),
        "rsi_7": r7, "rsi_14": r14,
        "ema9": round(e9,2), "ema21": round(e21,2), "ema50": round(e50,2),
        "macd_line": ml, "macd_signal": sig, "macd_hist": hist,
        "bb_low": bb_l, "bb_mid": bb_m, "bb_high": bb_h,
        "atr": atr, "atr_pct": round(atr_pct, 3),
        "stoch_k": stk, "stoch_d": std,
        "williams_r": wr,
        "vwap": vw, "above_vwap": price > vw,
        "vol_ratio": round(vols[-1]/avg_vol, 2) if avg_vol > 0 else 1.0,
        "momentum": round(mom, 2),
        "ema_bull": e9 > e21,
        "candles_count": len(candles),
    }

# ─── CLAUDE AI BRAIN ───────────────────────────────────────────────────────
async def claude_decide(ind_1m, ind_5m, ind_15m, recent_trades, bankroll, consec_losses):
    """
    Envoie le contexte complet à Claude qui prend la décision de trading.
    Retourne: { dir, confidence, bet_size, reasoning, trade }
    """
    if not ANTHROPIC_KEY:
        return {"dir": None, "confidence": 0, "bet_size": 0,
                "reasoning": "Pas de clé API Claude configurée.", "trade": False}

    # Résumé des derniers trades
    trades_summary = ""
    if recent_trades:
        for t in recent_trades[-5:]:
            trades_summary += f"- {t['result']} {t['dir']} | PnL: {t['pnl']:+.2f}$ | Score: {t.get('score',0)}\n"
    else:
        trades_summary = "Aucun trade récent."

    prompt = f"""Tu es un expert en trading de prédiction binaire sur Polymarket (marchés UP/DOWN Bitcoin 5 minutes).

TON RÔLE: Analyser les données de marché et décider si on doit parier UP, DOWN, ou ne pas trader du tout.

DONNÉES ACTUELLES:
Prix BTC: ${ind_5m.get('price', 0):,.2f}

INDICATEURS 1 MINUTE:
- RSI: {ind_1m.get('rsi_14', 50)} | Stoch K/D: {ind_1m.get('stoch_k',50)}/{ind_1m.get('stoch_d',50)}
- EMA9/21: {ind_1m.get('ema9',0):.0f}/{ind_1m.get('ema21',0):.0f} | Croisement: {'HAUSSIER' if ind_1m.get('ema_bull') else 'BAISSIER'}
- MACD hist: {ind_1m.get('macd_hist',0):.4f} | Williams %R: {ind_1m.get('williams_r',-50)}
- Volume ratio: x{ind_1m.get('vol_ratio',1):.2f} | Momentum: {ind_1m.get('momentum',0):+.2f}

INDICATEURS 5 MINUTES (principal):
- RSI 7/14: {ind_5m.get('rsi_7',50)}/{ind_5m.get('rsi_14',50)}
- EMA9/21/50: {ind_5m.get('ema9',0):.0f}/{ind_5m.get('ema21',0):.0f}/{ind_5m.get('ema50',0):.0f}
- Croisement EMA: {'HAUSSIER' if ind_5m.get('ema_bull') else 'BAISSIER'}
- MACD hist: {ind_5m.get('macd_hist',0):.4f}
- Bollinger: bas={ind_5m.get('bb_low',0):.0f} mid={ind_5m.get('bb_mid',0):.0f} haut={ind_5m.get('bb_high',0):.0f}
- Stoch K/D: {ind_5m.get('stoch_k',50)}/{ind_5m.get('stoch_d',50)}
- Williams %R: {ind_5m.get('williams_r',-50)}
- VWAP: {ind_5m.get('vwap',0):.0f} | Prix {'AU-DESSUS' if ind_5m.get('above_vwap') else 'EN-DESSOUS'} du VWAP
- ATR: {ind_5m.get('atr',0):.0f} ({ind_5m.get('atr_pct',0):.3f}% du prix)
- Volume ratio: x{ind_5m.get('vol_ratio',1):.2f}

INDICATEURS 15 MINUTES (tendance):
- RSI 14: {ind_15m.get('rsi_14',50)}
- EMA9/21: {ind_15m.get('ema9',0):.0f}/{ind_15m.get('ema21',0):.0f} | Croisement: {'HAUSSIER' if ind_15m.get('ema_bull') else 'BAISSIER'}
- MACD hist: {ind_15m.get('macd_hist',0):.4f}
- Momentum 15m: {ind_15m.get('momentum',0):+.2f}

CONTEXTE RISK MANAGEMENT:
- Bankroll actuelle: {bankroll:.2f} USDC
- Pertes consécutives: {consec_losses}
- Derniers trades:
{trades_summary}

RÈGLES IMPORTANTES:
1. Si pertes consécutives >= 3 sur le même setup → NE PAS TRADER
2. Si ATR < 0.05% → marché trop calme → NE PAS TRADER  
3. Si les 3 timeframes ne sont pas alignés → NE PAS TRADER sauf signal TRÈS fort
4. Mise maximale: {min(MAX_BET_USD, bankroll*MAX_BET_PCT):.2f} USDC | Minimale: {MIN_BET_USD} USDC
5. Sur Polymarket UP/DOWN 5min: on prédit si BTC sera plus haut ou plus bas dans 5 minutes

RÉPONDS UNIQUEMENT EN JSON (rien d'autre, pas de markdown):
{{
  "trade": true/false,
  "direction": "UP" ou "DOWN" ou null,
  "confidence": 0.0 à 1.0,
  "bet_size": montant en USDC (entre {MIN_BET_USD} et {min(MAX_BET_USD, bankroll*MAX_BET_PCT):.2f}),
  "reasoning": "Explication courte en français (2-3 phrases max)",
  "key_signals": ["signal1", "signal2", "signal3"],
  "risk_level": "LOW" ou "MEDIUM" ou "HIGH"
}}"""

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                CLAUDE_API,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01"
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                if r.status != 200:
                    log.error(f"Claude API error: {r.status}")
                    return {"dir": None, "confidence": 0, "bet_size": 0,
                            "reasoning": f"Erreur API Claude ({r.status})", "trade": False}

                data = await r.json()
                raw = data["content"][0]["text"].strip()
                # Clean JSON
                raw = raw.replace("```json", "").replace("```", "").strip()
                result = json.loads(raw)

                return {
                    "dir":       result.get("direction"),
                    "confidence": float(result.get("confidence", 0)),
                    "bet_size":  float(result.get("bet_size", 0)),
                    "reasoning": result.get("reasoning", ""),
                    "key_signals": result.get("key_signals", []),
                    "risk_level": result.get("risk_level", "MEDIUM"),
                    "trade":     bool(result.get("trade", False)),
                }
    except json.JSONDecodeError as e:
        log.error(f"Claude JSON parse error: {e} | raw: {raw[:200]}")
        return {"dir": None, "confidence": 0, "bet_size": 0,
                "reasoning": "Erreur parsing réponse Claude.", "trade": False}
    except Exception as e:
        log.error(f"Claude API call failed: {e}")
        return {"dir": None, "confidence": 0, "bet_size": 0,
                "reasoning": f"Erreur: {str(e)[:100]}", "trade": False}

# ─── STATE ─────────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.running          = False
        self.paper_mode       = PAPER_MODE
        self.bankroll         = BANKROLL_START
        self.candles_1m       = deque(maxlen=100)
        self.candles_5m       = deque(maxlen=100)
        self.candles_15m      = deque(maxlen=100)
        self.current_price    = 0.0
        self.trades           = []
        self.active_bet       = None
        self.wins             = 0
        self.losses           = 0
        self.total_pnl        = 0.0
        self.alerts           = True
        self.daily_start_br   = BANKROLL_START
        self.daily_reset_ts   = time.time()
        self.streak           = 0
        self.best_streak      = 0
        self.worst_streak     = 0
        self.consec_losses    = 0
        self.cooldown_until   = 0
        self.session_start    = time.time()
        self.last_signal      = {}
        self.last_ai_decision = {}
        self.skipped_trades   = 0
        self.tick_job         = None
        self.price_job        = None

    def save(self):
        try:
            with open(DATA_FILE, "w") as f:
                json.dump({
                    "bankroll": self.bankroll,
                    "trades": self.trades[-200:],
                    "wins": self.wins, "losses": self.losses,
                    "total_pnl": self.total_pnl,
                    "best_streak": self.best_streak,
                    "worst_streak": self.worst_streak,
                    "consec_losses": self.consec_losses,
                    "daily_start_br": self.daily_start_br,
                    "daily_reset_ts": self.daily_reset_ts,
                    "paper_mode": self.paper_mode,
                    "skipped_trades": self.skipped_trades,
                }, f, indent=2)
        except Exception as e:
            log.error(f"Save: {e}")

    def load(self):
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE) as f:
                    d = json.load(f)
                self.bankroll       = d.get("bankroll", BANKROLL_START)
                self.trades         = d.get("trades", [])
                self.wins           = d.get("wins", 0)
                self.losses         = d.get("losses", 0)
                self.total_pnl      = d.get("total_pnl", 0.0)
                self.best_streak    = d.get("best_streak", 0)
                self.worst_streak   = d.get("worst_streak", 0)
                self.consec_losses  = d.get("consec_losses", 0)
                self.daily_start_br = d.get("daily_start_br", self.bankroll)
                self.daily_reset_ts = d.get("daily_reset_ts", time.time())
                self.paper_mode     = d.get("paper_mode", PAPER_MODE)
                self.skipped_trades = d.get("skipped_trades", 0)
                log.info("State chargé")
        except Exception as e:
            log.error(f"Load: {e}")

st = State()

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
                            log.info(f"Price {name}: ${p:,.2f}")
                            return p
        except Exception as e:
            log.warning(f"{name}: {e}")
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

# ─── RISK ──────────────────────────────────────────────────────────────────
def check_daily_limit():
    now = time.time()
    if now - st.daily_reset_ts > 86400:
        st.daily_start_br = st.bankroll
        st.daily_reset_ts = now
    if st.daily_start_br == 0: return False
    return (st.daily_start_br - st.bankroll) / st.daily_start_br >= DAILY_LOSS_MAX

def in_cooldown():
    return time.time() < st.cooldown_until

# ─── TICK ──────────────────────────────────────────────────────────────────
async def price_update(context):
    p = await fetch_price()
    if p > 0: st.current_price = p

async def tick(context: ContextTypes.DEFAULT_TYPE):
    if not st.running: return

    # Daily limit check
    if check_daily_limit():
        st.running = False
        await context.bot.send_message(chat_id=ALLOWED_UID,
            text="🛑 *Limite journalière atteinte* — Bot arrêté.", parse_mode="Markdown")
        return

    # Cooldown check
    if in_cooldown():
        remaining = int((st.cooldown_until - time.time()) / 60)
        log.info(f"En cooldown — {remaining} min restantes")
        return

    # Fetch données
    c1  = await fetch_klines("1m",  60)
    c5  = await fetch_klines("5m",  50)
    c15 = await fetch_klines("15m", 40)

    if not c5:
        log.warning("Pas de données klines")
        return

    st.candles_1m  = deque(c1,  maxlen=100)
    st.candles_5m  = deque(c5,  maxlen=100)
    st.candles_15m = deque(c15, maxlen=100)
    st.current_price = c5[-1]["close"]

    # Résoudre bet actif
    if st.active_bet:
        bet    = st.active_bet
        entry  = bet["entry_price"]
        current = st.current_price
        won    = bet["dir"] == ("UP" if current > entry else "DOWN")
        gross  = bet["amount"] * (1 - POLY_FEE) if won else -bet["amount"]

        st.bankroll   = max(0.0, st.bankroll + gross)
        st.total_pnl += gross

        if won:
            st.wins += 1
            st.consec_losses = 0
            st.streak = st.streak + 1 if st.streak >= 0 else 1
            st.best_streak = max(st.best_streak, st.streak)
        else:
            st.losses += 1
            st.consec_losses += 1
            st.streak = st.streak - 1 if st.streak <= 0 else -1
            st.worst_streak = min(st.worst_streak, st.streak)

            # Cooldown après trop de pertes consécutives
            if st.consec_losses >= MAX_CONSEC_LOSS:
                st.cooldown_until = time.time() + COOLDOWN_MIN * 60
                log.warning(f"{st.consec_losses} pertes consécutives — cooldown {COOLDOWN_MIN}min")

        record = {
            "dir": bet["dir"], "amount": bet["amount"],
            "pnl": round(gross, 4), "conf": bet["conf"],
            "result": "WIN" if won else "LOSS",
            "entry": entry, "exit": current,
            "ai_reasoning": bet.get("ai_reasoning", ""),
            "paper": st.paper_mode, "ts": int(time.time()),
        }
        st.trades.append(record)
        st.active_bet = None

        if st.alerts:
            emoji = "✅" if won else "❌"
            cooldown_msg = f"\n⏸ *Cooldown {COOLDOWN_MIN}min activé* ({st.consec_losses} pertes)" if in_cooldown() else ""
            await context.bot.send_message(chat_id=ALLOWED_UID,
                text=(
                    f"{emoji} *Trade clôturé* [{'📄 PAPER' if st.paper_mode else '💰 RÉEL'}]\n"
                    f"Direction: `{bet['dir']}`\n"
                    f"Entrée: `${entry:,.2f}` → Sortie: `${current:,.2f}`\n"
                    f"PnL: `{'+' if gross >= 0 else ''}{gross:.2f} USDC`\n"
                    f"Bankroll: `{st.bankroll:.2f} USDC`\n"
                    f"Streak: `{st.streak:+d}`{cooldown_msg}"
                ), parse_mode="Markdown")
        st.save()

    # Si en cooldown, stop ici
    if in_cooldown():
        return

    # Calcul indicateurs
    ind_1m  = compute_indicators(list(st.candles_1m))
    ind_5m  = compute_indicators(list(st.candles_5m))
    ind_15m = compute_indicators(list(st.candles_15m))

    if not ind_5m:
        return

    # 🧠 CLAUDE AI DECISION
    decision = await claude_decide(
        ind_1m, ind_5m, ind_15m,
        st.trades[-10:],
        st.bankroll,
        st.consec_losses
    )
    st.last_ai_decision = decision

    log.info(f"Claude: trade={decision['trade']} dir={decision['dir']} conf={decision['confidence']:.0%} | {decision['reasoning'][:80]}")

    # Placer bet si Claude dit oui
    if decision["trade"] and decision["dir"] and not st.active_bet:
        amount = max(MIN_BET_USD, min(decision["bet_size"], MAX_BET_USD, st.bankroll * MAX_BET_PCT))
        amount = round(amount, 2)

        if amount >= MIN_BET_USD and st.bankroll >= amount:
            st.active_bet = {
                "dir": decision["dir"],
                "amount": amount,
                "conf": decision["confidence"],
                "entry_price": st.current_price,
                "ai_reasoning": decision["reasoning"],
                "ts": int(time.time()),
            }

            if st.alerts:
                signals_text = "\n".join(f"  • {s}" for s in decision.get("key_signals", [])[:4])
                risk_emoji = {"LOW":"🟢","MEDIUM":"🟡","HIGH":"🔴"}.get(decision.get("risk_level","MEDIUM"),"🟡")
                await context.bot.send_message(chat_id=ALLOWED_UID,
                    text=(
                        f"🧠 *Claude AI — Bet placé* [{'📄 PAPER' if st.paper_mode else '💰 RÉEL'}]\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Direction: *{decision['dir']}*\n"
                        f"Mise: `{amount:.2f} USDC`\n"
                        f"Confiance: `{decision['confidence']*100:.0f}%`\n"
                        f"Risque: {risk_emoji} `{decision.get('risk_level','?')}`\n"
                        f"Prix BTC: `${st.current_price:,.2f}`\n\n"
                        f"💭 *Raisonnement:*\n_{decision['reasoning']}_\n\n"
                        f"🔑 *Signaux clés:*\n{signals_text}"
                    ), parse_mode="Markdown")
    else:
        st.skipped_trades += 1
        log.info(f"Trade refusé par Claude: {decision['reasoning'][:100]}")

# ─── HELPERS ───────────────────────────────────────────────────────────────
def auth(u): return ALLOWED_UID == 0 or u.effective_user.id == ALLOWED_UID
def fmt(v): return f"+{v:.2f}" if v >= 0 else f"{v:.2f}"
def uptime():
    s = int(time.time() - st.session_start)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"
def win_rate():
    t = st.wins + st.losses
    return f"{st.wins/t*100:.1f}%" if t > 0 else "—"
def roi():
    r = (st.bankroll - BANKROLL_START) / BANKROLL_START * 100
    return f"{fmt(r)}%"

def main_kb():
    cd_label = f"⏸ Cooldown {int((st.cooldown_until-time.time())/60)}min" if in_cooldown() else "🟢 Actif"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Status",   callback_data="status"),
         InlineKeyboardButton("🧠 AI Last",  callback_data="ai_last")],
        [InlineKeyboardButton("📈 Trades",   callback_data="trades"),
         InlineKeyboardButton("📉 Stats",    callback_data="stats")],
        [InlineKeyboardButton("🔬 Indicateurs", callback_data="indicators"),
         InlineKeyboardButton("🌐 Marchés", callback_data="markets")],
        [InlineKeyboardButton("▶️ Start",    callback_data="run"),
         InlineKeyboardButton("⏹ Stop",     callback_data="stop")],
        [InlineKeyboardButton(cd_label,      callback_data="cooldown_reset"),
         InlineKeyboardButton("💰 Passer Réel" if st.paper_mode else "📄 Paper", callback_data="toggle_paper")],
    ])

# ─── COMMANDES ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    has_ai = "✅ Configurée" if ANTHROPIC_KEY else "❌ Manquante (ajouter ANTHROPIC_API_KEY)"
    await update.message.reply_text(
        f"🧠 *POLYMARKET BTC BOT v5 — AI EDITION*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Mode: *{'📄 PAPER' if st.paper_mode else '💰 RÉEL'}*\n"
        f"🤖 Claude AI: {has_ai}\n\n"
        f"Claude analyse chaque setup et décide si trader ou non.\n"
        f"Il explique son raisonnement à chaque trade.\n\n"
        f"*/run* — Démarrer | */stop* — Arrêter\n"
        f"*/status* — État | */ai* — Dernière décision AI\n"
        f"*/signal* — Forcer analyse | */trades* — Historique\n"
        f"*/stats* — Statistiques | */paper* — Toggle mode\n"
        f"*/cooldown* — Reset cooldown | */reset* — Reset complet",
        parse_mode="Markdown", reply_markup=main_kb()
    )

async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    if st.running:
        await update.message.reply_text("⚠️ Déjà en cours.")
        return
    if not ANTHROPIC_KEY:
        await update.message.reply_text(
            "❌ *Clé API Claude manquante !*\n"
            "Ajoute la variable `ANTHROPIC_API_KEY` dans Railway → Variables.\n"
            "Obtiens-la sur: console.anthropic.com",
            parse_mode="Markdown"
        )
        return

    st.running = True
    st.session_start = time.time()
    st.daily_start_br = st.bankroll
    st.price_job = context.job_queue.run_repeating(price_update, interval=30, first=5)
    st.tick_job  = context.job_queue.run_repeating(tick, interval=300, first=10)

    await update.message.reply_text(
        f"▶️ *Bot AI démarré !*\n"
        f"🧠 Claude analyse toutes les 5 minutes\n"
        f"Prix BTC: `${st.current_price:,.2f}`\n"
        f"Bankroll: `{st.bankroll:.2f} USDC`",
        parse_mode="Markdown"
    )
    await tick(context)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    st.running = False
    for j in [st.tick_job, st.price_job]:
        if j: j.schedule_removal()
    st.tick_job = st.price_job = None
    st.save()
    await update.message.reply_text(
        f"⏹ *Bot arrêté*\n"
        f"Uptime: `{uptime()}`\n"
        f"Bankroll: `{st.bankroll:.2f} USDC`\n"
        f"PnL: `{fmt(st.total_pnl)} USDC`\n"
        f"Win Rate: `{win_rate()}`",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    daily_loss = (st.daily_start_br - st.bankroll) / st.daily_start_br * 100 if st.daily_start_br > 0 else 0
    cd_msg = f"\n⏸ Cooldown: `{int((st.cooldown_until-time.time())/60)} min`" if in_cooldown() else ""
    bet_info = f"{st.active_bet['dir']} {st.active_bet['amount']:.2f}$ @ ${st.active_bet['entry_price']:,.0f}" if st.active_bet else "Aucun"

    await update.message.reply_text(
        f"📊 *STATUS* [{'📄 PAPER' if st.paper_mode else '💰 RÉEL'}]\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{'🟢 EN COURS' if st.running else '🔴 ARRÊTÉ'}{cd_msg}\n\n"
        f"₿ BTC: `${st.current_price:,.2f}`\n"
        f"💰 Bankroll: `{st.bankroll:.2f} USDC`\n"
        f"📈 ROI: `{roi()}`\n"
        f"💹 PnL: `{fmt(st.total_pnl)} USDC`\n"
        f"📅 Perte jour: `{daily_loss:.1f}%` / `{DAILY_LOSS_MAX*100:.0f}%`\n\n"
        f"🎯 Bet actif: `{bet_info}`\n"
        f"🔴 Pertes consécutives: `{st.consec_losses}`\n"
        f"🚫 Trades refusés par AI: `{st.skipped_trades}`\n"
        f"⏱ Uptime: `{uptime()}`",
        parse_mode="Markdown", reply_markup=main_kb()
    )

async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    d = st.last_ai_decision
    if not d:
        await update.message.reply_text("⏳ Pas encore de décision AI. Lance /run d'abord.")
        return

    signals = "\n".join(f"  • {s}" for s in d.get("key_signals", []))
    risk_emoji = {"LOW":"🟢","MEDIUM":"🟡","HIGH":"🔴"}.get(d.get("risk_level","MEDIUM"),"🟡")
    dir_emoji = "🟢" if d.get("dir") == "UP" else "🔴" if d.get("dir") == "DOWN" else "⚪"

    await update.message.reply_text(
        f"🧠 *DERNIÈRE DÉCISION CLAUDE AI*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_emoji} Direction: *{d.get('dir') or 'PASS'}*\n"
        f"📊 Confiance: `{d.get('confidence',0)*100:.0f}%`\n"
        f"💰 Mise suggérée: `{d.get('bet_size',0):.2f} USDC`\n"
        f"{risk_emoji} Risque: `{d.get('risk_level','?')}`\n"
        f"🤝 Trade: `{'OUI' if d.get('trade') else 'NON'}`\n\n"
        f"💭 *Raisonnement:*\n_{d.get('reasoning','—')}_\n\n"
        f"🔑 *Signaux clés:*\n{signals or '  —'}",
        parse_mode="Markdown"
    )

async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text("⏳ Fetching données + analyse Claude AI...")

    c1  = await fetch_klines("1m",  60)
    c5  = await fetch_klines("5m",  50)
    c15 = await fetch_klines("15m", 40)

    if c5:
        st.candles_1m  = deque(c1,  maxlen=100)
        st.candles_5m  = deque(c5,  maxlen=100)
        st.candles_15m = deque(c15, maxlen=100)
        st.current_price = c5[-1]["close"]

    ind_1m  = compute_indicators(list(st.candles_1m))
    ind_5m  = compute_indicators(list(st.candles_5m))
    ind_15m = compute_indicators(list(st.candles_15m))

    decision = await claude_decide(ind_1m, ind_5m, ind_15m, st.trades[-10:], st.bankroll, st.consec_losses)
    st.last_ai_decision = decision

    dir_emoji = "🟢" if decision["dir"] == "UP" else "🔴" if decision["dir"] == "DOWN" else "⚪"
    signals_text = "\n".join(f"  • {s}" for s in decision.get("key_signals",[])[:5])
    risk_emoji = {"LOW":"🟢","MEDIUM":"🟡","HIGH":"🔴"}.get(decision.get("risk_level","MEDIUM"),"🟡")

    await update.message.reply_text(
        f"🧠 *ANALYSE CLAUDE AI*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{dir_emoji} Direction: *{decision['dir'] or 'PASS — Ne pas trader'}*\n"
        f"📊 Confiance: `{decision['confidence']*100:.0f}%`\n"
        f"💰 Mise: `{decision['bet_size']:.2f} USDC`\n"
        f"{risk_emoji} Risque: `{decision.get('risk_level','?')}`\n\n"
        f"₿ BTC: `${ind_5m.get('price',0):,.2f}`\n"
        f"RSI 5m: `{ind_5m.get('rsi_14',50)}` | EMA: `{'🟢' if ind_5m.get('ema_bull') else '🔴'}`\n"
        f"VWAP: `{'AU-DESSUS' if ind_5m.get('above_vwap') else 'EN-DESSOUS'}`\n\n"
        f"💭 *Raisonnement Claude:*\n_{decision['reasoning']}_\n\n"
        f"🔑 *Signaux:*\n{signals_text or '  Aucun signal fort'}",
        parse_mode="Markdown"
    )

async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    trades = st.trades[-8:][::-1]
    if not trades:
        await update.message.reply_text("📈 Aucun trade.")
        return
    lines = ["📈 *DERNIERS TRADES (AI)*\n━━━━━━━━━━━━━━━━━━━━━━━━"]
    for t in trades:
        emoji = "✅" if t["result"] == "WIN" else "❌"
        ts    = datetime.fromtimestamp(t["ts"]).strftime("%d/%m %H:%M")
        reason = t.get("ai_reasoning","")[:40]
        lines.append(f"{emoji} `{t['dir']}` | `{fmt(t['pnl'])}$` | `{ts}`\n   _{reason}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    total    = st.wins + st.losses
    avg_win  = sum(t["pnl"] for t in st.trades if t["pnl"] > 0) / max(st.wins,1)
    avg_loss = abs(sum(t["pnl"] for t in st.trades if t["pnl"] < 0)) / max(st.losses,1)
    rr = avg_win / avg_loss if avg_loss > 0 else 0

    peak, max_dd, rbr = BANKROLL_START, 0.0, BANKROLL_START
    for t in st.trades:
        rbr += t["pnl"]
        if rbr > peak: peak = rbr
        dd = (peak-rbr)/peak*100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd

    await update.message.reply_text(
        f"📉 *STATISTIQUES AI BOT*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Total: `{total}` (✅{st.wins} ❌{st.losses})\n"
        f"🎯 Win Rate: `{win_rate()}`\n"
        f"💰 PnL: `{fmt(st.total_pnl)} USDC`\n"
        f"📈 ROI: `{roi()}`\n"
        f"⚖️ R:R: `{rr:.2f}`\n\n"
        f"💚 Gain moyen: `+{avg_win:.2f}$`\n"
        f"🔴 Perte moyenne: `-{avg_loss:.2f}$`\n\n"
        f"🔥 Best streak: `+{st.best_streak}`\n"
        f"💀 Worst streak: `{st.worst_streak}`\n"
        f"📉 Max Drawdown: `{max_dd:.1f}%`\n\n"
        f"🚫 Trades refusés (AI): `{st.skipped_trades}`\n"
        f"💼 Bankroll: `{st.bankroll:.2f} USDC`",
        parse_mode="Markdown"
    )

async def cmd_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    st.cooldown_until = 0
    st.consec_losses  = 0
    await update.message.reply_text("✅ Cooldown reset — Bot peut trader à nouveau.", parse_mode="Markdown")

async def cmd_paper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    st.paper_mode = not st.paper_mode
    mode = "📄 PAPER" if st.paper_mode else "💰 RÉEL ⚠️"
    await update.message.reply_text(f"Mode: *{mode}*", parse_mode="Markdown")
    st.save()

async def cmd_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text("🌐 Fetching marchés Polymarket...")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(POLY_MARKETS, params={"active":"true"}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    markets = data.get("data",[]) if isinstance(data,dict) else data
                    btc = [m for m in markets if isinstance(m,dict) and
                           ("btc" in m.get("question","").lower() or "bitcoin" in m.get("question","").lower())]
                    if btc:
                        lines = ["🌐 *MARCHÉS BTC POLYMARKET*\n━━━━━━━━━━━━━━━━━━━━━━━━"]
                        for m in btc[:5]:
                            lines.append(f"• `{m.get('question','?')[:50]}`")
                        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
                        return
    except Exception as e:
        log.error(f"Markets: {e}")
    await update.message.reply_text("⚠️ Marchés non disponibles.")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    st.running = False
    for j in [st.tick_job, st.price_job]:
        if j: j.schedule_removal()
    st.bankroll = BANKROLL_START
    st.candles_1m.clear(); st.candles_5m.clear(); st.candles_15m.clear()
    st.trades=[]; st.active_bet=None
    st.wins=st.losses=st.skipped_trades=st.consec_losses=0
    st.total_pnl=st.streak=st.best_streak=st.worst_streak=0
    st.cooldown_until=0; st.session_start=time.time()
    if os.path.exists(DATA_FILE): os.remove(DATA_FILE)
    await update.message.reply_text("🔄 *Reset complet.*", parse_mode="Markdown")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    handlers = {
        "status": cmd_status, "ai_last": cmd_ai,
        "trades": cmd_trades, "stats": cmd_stats,
        "indicators": cmd_signal, "markets": cmd_markets,
        "run": cmd_run, "stop": cmd_stop,
        "toggle_paper": cmd_paper, "cooldown_reset": cmd_cooldown,
    }
    if q.data in handlers:
        await handlers[q.data](update, context)

def main():
    st.load()
    app = Application.builder().token(TOKEN).build()
    for name, handler in [
        ("start",cmd_start),("run",cmd_run),("stop",cmd_stop),
        ("status",cmd_status),("ai",cmd_ai),("signal",cmd_signal),
        ("trades",cmd_trades),("stats",cmd_stats),("markets",cmd_markets),
        ("paper",cmd_paper),("cooldown",cmd_cooldown),("reset",cmd_reset),
    ]:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    log.info(f"🧠 PolyBot v5 AI démarré — {'PAPER' if st.paper_mode else 'RÉEL'}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

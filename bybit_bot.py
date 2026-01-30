# ======================================================
# BYBIT SMART BOT (FULL SYSTEM)
# PART 1 – CORE SYSTEM & CONFIG
# ======================================================

import os
import time
import math
import json
import threading
import requests
from datetime import datetime

from flask import Flask, request, send_from_directory
from pybit.unified_trading import HTTP

# ======================================================
# MODE CONFIG (DEMO / REAL)
# ======================================================

MODE = os.getenv("MODE", "DEMO")  # DEMO or REAL

BYBIT_DEMO_KEY = os.getenv("BYBIT_DEMO_KEY")
BYBIT_DEMO_SECRET = os.getenv("BYBIT_DEMO_SECRET")

BYBIT_REAL_KEY = os.getenv("BYBIT_REAL_KEY")
BYBIT_REAL_SECRET = os.getenv("BYBIT_REAL_SECRET")

TG_TOKEN = os.getenv("TG_TOKEN")
TG_ADMIN = int(os.getenv("TG_ADMIN", "0"))

if MODE == "REAL":
    API_KEY = BYBIT_REAL_KEY
    API_SECRET = BYBIT_REAL_SECRET
    TESTNET = False
else:
    API_KEY = BYBIT_DEMO_KEY
    API_SECRET = BYBIT_DEMO_SECRET
    TESTNET = True

# ======================================================
# GLOBAL BOT STATE
# ======================================================

BOT_ACTIVE = True
KILL_SWITCH = False

START_DAY_BALANCE = None
TRADES_TODAY = 0

OPEN_POSITIONS = {}   # symbol -> position data
SYMBOL_COOLDOWN = {}  # symbol -> last trade time

# ======================================================
# RISK SETTINGS (BASE – SAFE DEFAULTS)
# ======================================================

LEVERAGE = 20
RISK_PER_TRADE = 0.20      # 20% of available balance
MAX_DAILY_LOSS = 0.10     # 10%
MAX_DAILY_PROFIT = 0.25   # 25%
MAX_TRADES_PER_DAY = 5
COOLDOWN_SECONDS = 300    # 5 minutes per symbol

# ======================================================
# CONNECT TO BYBIT
# ======================================================

print("🔌 Connecting to Bybit...")

session = HTTP(
    api_key=API_KEY,
    api_secret=API_SECRET,
    testnet=TESTNET
)

# ======================================================
# TELEGRAM CORE
# ======================================================

def tg(message: str):
    if not TG_TOKEN or TG_ADMIN == 0:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={
                "chat_id": TG_ADMIN,
                "text": message
            },
            timeout=5
        )
    except:
        pass

# ======================================================
# WALLET / BALANCE
# ======================================================

def get_balance():
    try:
        r = session.get_wallet_balance(accountType="UNIFIED")
        return float(r["result"]["list"][0]["totalWalletBalance"])
    except:
        return 0.0

# ======================================================
# DAILY INIT
# ======================================================

def init_day():
    global START_DAY_BALANCE, TRADES_TODAY, KILL_SWITCH

    START_DAY_BALANCE = get_balance()
    TRADES_TODAY = 0
    KILL_SWITCH = False

    tg(
        f"🚀 BYBIT BOT STARTED ({MODE})\n"
        f"Balance: {START_DAY_BALANCE}"
    )

# ======================================================
# DAILY RISK CHECK
# ======================================================

def daily_risk_check():
    global KILL_SWITCH

    if START_DAY_BALANCE is None:
        return

    current_balance = get_balance()
    pnl_ratio = (current_balance - START_DAY_BALANCE) / START_DAY_BALANCE

    if pnl_ratio <= -MAX_DAILY_LOSS:
        KILL_SWITCH = True
        tg("🛑 DAILY LOSS LIMIT HIT")

    if pnl_ratio >= MAX_DAILY_PROFIT:
        KILL_SWITCH = True
        tg("🎯 DAILY PROFIT TARGET HIT")

  # ======================================================
# PART 2 – MARKET DATA & INDICATORS
# ======================================================

# ===============================
# SYMBOL LIST (100+ PAIRS SAFE)
# ===============================

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "MATICUSDT", "DOTUSDT",
    "LTCUSDT", "LINKUSDT", "ATOMUSDT", "OPUSDT", "ARBUSDT",
    "SUIUSDT", "INJUSDT", "APTUSDT", "FILUSDT", "NEARUSDT"
]

# ===============================
# FETCH CANDLES
# ===============================

def get_klines(symbol, interval="5", limit=100):
    try:
        r = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=interval,
            limit=limit
        )
        return r["result"]["list"]
    except:
        return []

# ===============================
# PRICE HELPERS
# ===============================

def get_last_price(symbol):
    try:
        r = session.get_tickers(
            category="linear",
            symbol=symbol
        )
        return float(r["result"]["list"][0]["lastPrice"])
    except:
        return None

# ===============================
# INDICATORS
# ===============================

def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))

    if not losses:
        return 100

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None

    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)

    return sum(trs[-period:]) / period


# ===============================
# MARKET SNAPSHOT
# ===============================

def get_market_snapshot(symbol):
    klines = get_klines(symbol)
    if not klines or len(klines) < 50:
        return None

    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]

    snapshot = {
        "price": closes[-1],
        "sma_fast": sma(closes, 9),
        "sma_slow": sma(closes, 21),
        "rsi": rsi(closes),
        "atr": atr(highs, lows, closes)
    }

    return snapshot

# ======================================================
# PART 3 – AI TRADE FILTER
# ======================================================

# ===============================
# AI FILTER SETTINGS
# ===============================

AI_MIN_ATR_RATIO = 0.001      # avoid dead markets
AI_MAX_RSI_BUY = 70
AI_MIN_RSI_BUY = 35

AI_MIN_RSI_SELL = 30
AI_MAX_RSI_SELL = 65


# ===============================
# AI DECISION ENGINE
# ===============================

def ai_trade_filter(symbol, snapshot):
    """
    Returns:
        "LONG" | "SHORT" | None
    """

    if snapshot is None:
        return None

    price = snapshot["price"]
    sma_fast = snapshot["sma_fast"]
    sma_slow = snapshot["sma_slow"]
    rsi_val = snapshot["rsi"]
    atr_val = snapshot["atr"]

    if not all([price, sma_fast, sma_slow, rsi_val, atr_val]):
        return None

    # -------------------------------
    # Volatility filter (ATR)
    # -------------------------------
    if atr_val / price < AI_MIN_ATR_RATIO:
        return None

    # -------------------------------
    # LONG conditions
    # -------------------------------
    if (
        sma_fast > sma_slow and
        AI_MIN_RSI_BUY <= rsi_val <= AI_MAX_RSI_BUY
    ):
        return "LONG"

    # -------------------------------
    # SHORT conditions
    # -------------------------------
    if (
        sma_fast < sma_slow and
        AI_MIN_RSI_SELL <= rsi_val <= AI_MAX_RSI_SELL
    ):
        return "SHORT"

    return None


# ===============================
# COOLDOWN CHECK
# ===============================

def can_trade_symbol(symbol):
    now = time.time()
    last_time = SYMBOL_COOLDOWN.get(symbol, 0)

    if now - last_time < COOLDOWN_SECONDS:
        return False

    return True


def mark_symbol_traded(symbol):
    SYMBOL_COOLDOWN[symbol] = time.time()

# ======================================================
# PART 4 – POSITION SIZING & ORDER EXECUTION
# ======================================================

# ===============================
# ORDER SETTINGS
# ===============================

MIN_QTY_USDT = 5          # Bybit minimum
SL_ATR_MULTIPLIER = 1.5
TP_ATR_MULTIPLIER = 3.0


# ===============================
# POSITION SIZE CALC
# ===============================

def calculate_position_size(balance, price):
    """
    Risk-based position sizing
    """
    risk_amount = balance * RISK_PER_TRADE
    qty = (risk_amount * LEVERAGE) / price

    # Safety minimum
    notional = qty * price
    if notional < MIN_QTY_USDT:
        return None

    return round(qty, 3)


# ===============================
# PLACE MARKET ORDER
# ===============================

def place_order(symbol, side, snapshot):
    global TRADES_TODAY, OPEN_TRADES

    if KILL_SWITCH:
        return

    if TRADES_TODAY >= MAX_TRADES:
        return

    balance = get_balance()
    price = snapshot["price"]
    atr_val = snapshot["atr"]

    qty = calculate_position_size(balance, price)
    if qty is None:
        return

    if side == "LONG":
        order_side = "Buy"
        sl_price = price - (atr_val * SL_ATR_MULTIPLIER)
        tp_price = price + (atr_val * TP_ATR_MULTIPLIER)
    else:
        order_side = "Sell"
        sl_price = price + (atr_val * SL_ATR_MULTIPLIER)
        tp_price = price - (atr_val * TP_ATR_MULTIPLIER)

    try:
        session.place_order(
            category="linear",
            symbol=symbol,
            side=order_side,
            orderType="Market",
            qty=qty,
            takeProfit=round(tp_price, 4),
            stopLoss=round(sl_price, 4),
            timeInForce="GoodTillCancel",
            reduceOnly=False,
            closeOnTrigger=False
        )

        OPEN_TRADES[symbol] = {
            "side": side,
            "entry": price,
            "qty": qty,
            "sl": sl_price,
            "tp": tp_price
        }

        TRADES_TODAY += 1
        mark_symbol_traded(symbol)

        tg(f"📈 {side} OPENED\n{symbol}\nQty: {qty}")

    except Exception as e:
        tg(f"❌ ORDER FAILED {symbol}\n{e}")

  # ======================================================
# PART 5 – TRAILING STOP ENGINE
# ======================================================

# ===============================
# TRAILING SETTINGS
# ===============================

TRAIL_START_ATR = 1.2     # start trailing after this profit (ATR)
TRAIL_STEP_ATR = 0.6      # move SL every step


# ===============================
# UPDATE STOP LOSS
# ===============================

def update_stop_loss(symbol, new_sl):
    try:
        session.set_trading_stop(
            category="linear",
            symbol=symbol,
            stopLoss=round(new_sl, 4)
        )
        return True
    except:
        return False


# ===============================
# TRAILING LOGIC
# ===============================

def manage_trailing():
    while True:
        for symbol, trade in list(OPEN_TRADES.items()):
            try:
                price = get_last_price(symbol)
                if price is None:
                    continue

                side = trade["side"]
                entry = trade["entry"]
                sl = trade["sl"]
                atr_val = abs(trade["tp"] - trade["entry"]) / TP_ATR_MULTIPLIER

                # ---------------------------
                # LONG trailing
                # ---------------------------
                if side == "LONG":
                    profit = price - entry

                    if profit >= atr_val * TRAIL_START_ATR:
                        new_sl = price - (atr_val * TRAIL_STEP_ATR)
                        if new_sl > sl:
                            if update_stop_loss(symbol, new_sl):
                                OPEN_TRADES[symbol]["sl"] = new_sl
                                tg(f"🔁 TRAIL SL ↑ {symbol}\nSL: {round(new_sl,4)}")

                # ---------------------------
                # SHORT trailing
                # ---------------------------
                if side == "SHORT":
                    profit = entry - price

                    if profit >= atr_val * TRAIL_START_ATR:
                        new_sl = price + (atr_val * TRAIL_STEP_ATR)
                        if new_sl < sl:
                            if update_stop_loss(symbol, new_sl):
                                OPEN_TRADES[symbol]["sl"] = new_sl
                                tg(f"🔁 TRAIL SL ↓ {symbol}\nSL: {round(new_sl,4)}")

            except:
                pass

        time.sleep(5)

  # ======================================================
# PART 6 – MARKET SCAN & SIGNAL ENGINE
# ======================================================

# ===============================
# SYMBOL SETTINGS
# ===============================

TRADE_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT"
]

SCAN_INTERVAL = 15   # seconds


# ===============================
# MARKET DATA HELPERS
# ===============================

def get_last_price(symbol):
    try:
        r = session.get_tickers(
            category="linear",
            symbol=symbol
        )
        return float(r["result"]["list"][0]["lastPrice"])
    except:
        return None


def get_klines(symbol, interval="5", limit=50):
    try:
        r = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=interval,
            limit=limit
        )
        return r["result"]["list"]
    except:
        return None


# ===============================
# INDICATOR CALCULATIONS
# ===============================

def calculate_sma(data, length):
    if len(data) < length:
        return None
    return sum(data[-length:]) / length


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))

    if not losses:
        return 100

    rs = (sum(gains) / period) / (sum(losses) / period)
    return 100 - (100 / (1 + rs))


def calculate_atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, period + 1):
        tr = max(
            highs[-i] - lows[-i],
            abs(highs[-i] - closes[-i - 1]),
            abs(lows[-i] - closes[-i - 1])
        )
        trs.append(tr)

    return sum(trs) / period if trs else None


# ===============================
# SNAPSHOT BUILDER
# ===============================

def build_snapshot(symbol):
    klines = get_klines(symbol)
    if not klines:
        return None

    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]

    price = closes[-1]

    sma_fast = calculate_sma(closes, 9)
    sma_slow = calculate_sma(closes, 21)
    rsi_val = calculate_rsi(closes)
    atr_val = calculate_atr(highs, lows, closes)

    if not all([sma_fast, sma_slow, rsi_val, atr_val]):
        return None

    return {
        "price": price,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "rsi": rsi_val,
        "atr": atr_val
    }


# ===============================
# MAIN SCAN LOOP
# ===============================

def scan_markets():
    tg("🧠 Market scan started")

    while True:
        if not BOT_ACTIVE or KILL_SWITCH:
            time.sleep(5)
            continue

        daily_risk_check()

        for symbol in TRADE_SYMBOLS:
            if symbol in OPEN_TRADES:
                continue

            if not can_trade_symbol(symbol):
                continue

            snapshot = build_snapshot(symbol)
            if snapshot is None:
                continue

            decision = ai_trade_filter(symbol, snapshot)

            if decision in ["LONG", "SHORT"]:
                place_order(symbol, decision, snapshot)

        time.sleep(SCAN_INTERVAL)

      # ======================================================
# PART 7 – TELEGRAM CONTROL & COMMANDS
# ======================================================

# ===============================
# TELEGRAM COMMAND HANDLER
# ===============================

def handle_command(text):
    global BOT_ACTIVE, KILL_SWITCH

    cmd = text.lower().strip()

    if cmd == "/start":
        BOT_ACTIVE = True
        KILL_SWITCH = False
        tg("✅ BOT ACTIVATED")

    elif cmd == "/stop":
        BOT_ACTIVE = False
        tg("⛔ BOT PAUSED")

    elif cmd == "/kill":
        KILL_SWITCH = True
        tg("🛑 KILL SWITCH ENABLED")

    elif cmd == "/status":
        bal = get_balance()
        tg(
            f"📊 STATUS\n"
            f"Mode: {MODE}\n"
            f"Balance: {bal}\n"
            f"Trades today: {TRADES_TODAY}\n"
            f"Open trades: {len(OPEN_TRADES)}"
        )

    elif cmd == "/reset":
        init_day()
        tg("🔄 DAILY RESET DONE")

    else:
        tg("❓ Unknown command")


# ===============================
# TELEGRAM POLLING LOOP
# ===============================

def start_telegram():
    if not TG_TOKEN or TG_ADMIN == 0:
        print("Telegram disabled")
        return

    tg("🤖 Telegram control started")

    offset = 0
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 30}
            ).json()

            for update in r.get("result", []):
                offset = update["update_id"] + 1

                if "message" not in update:
                    continue

                chat_id = update["message"]["chat"]["id"]
                if chat_id != TG_ADMIN:
                    continue

                text = update["message"].get("text", "")
                if text:
                    handle_command(text)

        except Exception as e:
            time.sleep(3)

      # ======================================================
# PART 8 – MINI WEB UI (DASHBOARD)
# ======================================================

from flask import Flask, jsonify

app = Flask(__name__)


# ===============================
# API – BOT STATUS
# ===============================

@app.route("/status")
def api_status():
    return jsonify({
        "mode": MODE,
        "bot_active": BOT_ACTIVE,
        "kill_switch": KILL_SWITCH,
        "balance": get_balance(),
        "trades_today": TRADES_TODAY,
        "open_trades": OPEN_TRADES
    })


# ===============================
# API – CONTROL
# ===============================

@app.route("/start")
def api_start():
    global BOT_ACTIVE, KILL_SWITCH
    BOT_ACTIVE = True
    KILL_SWITCH = False
    return jsonify({"status": "BOT STARTED"})


@app.route("/stop")
def api_stop():
    global BOT_ACTIVE
    BOT_ACTIVE = False
    return jsonify({"status": "BOT STOPPED"})


@app.route("/kill")
def api_kill():
    global KILL_SWITCH
    KILL_SWITCH = True
    return jsonify({"status": "KILL SWITCH ON"})


# ===============================
# RUN WEB SERVER
# ===============================

def start_web():
    app.run(host="0.0.0.0", port=10000)

# ======================================================
# PART 9 – THREADS & MAIN RUNNER
# ======================================================

if __name__ == "__main__":
    # ---- INIT DAY ----
    init_day()

    # ---- TELEGRAM THREAD ----
    threading.Thread(
        target=start_telegram,
        daemon=True
    ).start()

    # ---- MARKET SCAN THREAD ----
    threading.Thread(
        target=scan_markets,
        daemon=True
    ).start()

    # ---- TRAILING STOP THREAD ----
    threading.Thread(
        target=manage_trailing,
        daemon=True
    ).start()

    # ---- WEB UI (BLOCKING) ----
    start_web()

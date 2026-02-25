#!/usr/bin/env python3
"""
MEWC/USDT Asymmetric Market Maker for NonKYC.io
Strategy: Liberal buy pressure + conservative sell wall.

Buy side  — frequent, aggressive limit buys just above the ask.
Sell side — infrequent, wide-spread limit sells well above the bid (off by default).

Features:
  - REST API with HMAC-SHA256 authentication
  - Persistent settings saved to nonkyc_settings.json
  - Configurable buy/sell intervals, order sizes, spreads
  - Real-time market data (price, spread, volume)
  - Live order tracking and trade history
  - Tkinter GUI with dark theme
  - Safety controls: budget limit, price caps, MEWC reserve, emergency stop
"""

import json
import hashlib
import hmac
import time
import threading
import os
import sys
import uuid
import logging
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from urllib.parse import urlencode
import http.client
import ssl
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# When frozen by PyInstaller --onefile, __file__ points to a temp dir.
# Use the exe's real directory instead so settings persist next to the exe.
if getattr(sys, 'frozen', False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(_APP_DIR, "nonkyc_settings.json")
BOT_STATE_FILE = os.path.join(_APP_DIR, "mewc_trader_state.json")
LOG_FILE = os.path.join(_APP_DIR, "mewc_trader.log")

API_BASE = "api.nonkyc.io"
API_PATH = "/api/v2"
SYMBOL = "MEWC/USDT"
SYMBOL_UNDERSCORE = "MEWC_USDT"

# --- Buy side defaults (liberal — tight to market, frequent) ---
DEFAULT_BUY_INTERVAL = 45          # seconds between buys
DEFAULT_ORDER_USDT = 1.50          # USDT per buy order
DEFAULT_BUDGET_USDT = 5000.0       # total max USDT to spend
DEFAULT_MAX_PRICE = 0.00015        # max price willing to pay (safety)
DEFAULT_BUY_MODE = "limit"         # "limit" or "market"
DEFAULT_PRICE_BUMP = 1.0           # % above best ask for aggressive limit buys

# --- Sell side defaults (very conservative — wide spread, infrequent) ---
DEFAULT_SELL_ENABLED = False        # sell side OFF by default
DEFAULT_SELL_INTERVAL = 300         # seconds between sells (5 min)
DEFAULT_SELL_ORDER_MEWC = 5000.0   # MEWC per sell order (small)
DEFAULT_MIN_SELL_PRICE = 0.00005   # floor — never sell below this
DEFAULT_SELL_SPREAD_PCT = 10.0     # % above best bid for sell price (very wide)
DEFAULT_MIN_MEWC_RESERVE = 50000.0 # keep at least this much MEWC

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
)
logger = logging.getLogger("mewc_trader")


# ---------------------------------------------------------------------------
# NonKYC REST API Client
# ---------------------------------------------------------------------------

class NonKYCApi:
    """Synchronous REST API client for NonKYC.io with HMAC-SHA256 auth."""

    def __init__(self, access_key: str, secret_key: str):
        self.access_key = access_key
        self.secret_key = secret_key
        self.base = API_BASE
        self.path = API_PATH

    def _sign(self, payload: str) -> str:
        return hmac.new(
            self.secret_key.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

    def _auth_headers_get(self, full_url: str) -> dict:
        nonce = str(int(time.time() * 1000))
        data_to_sign = f"{self.access_key}{full_url}{nonce}"
        signature = self._sign(data_to_sign)
        return {
            "X-API-KEY": self.access_key,
            "X-API-NONCE": nonce,
            "X-API-SIGN": signature,
            "Content-Type": "application/json",
        }

    def _auth_headers_post(self, full_url: str, body_str: str) -> dict:
        nonce = str(int(time.time() * 1000))
        data_to_sign = f"{self.access_key}{full_url}{body_str}{nonce}"
        signature = self._sign(data_to_sign)
        return {
            "X-API-KEY": self.access_key,
            "X-API-NONCE": nonce,
            "X-API-SIGN": signature,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, params: dict = None,
                 body: dict = None, auth: bool = False) -> dict:
        """Make an HTTPS request to the NonKYC API."""
        url_path = self.path + endpoint
        full_url = f"https://{self.base}{url_path}"

        if params:
            qs = urlencode(params)
            url_path += f"?{qs}"
            full_url += f"?{qs}"

        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(self.base, 443, timeout=30, context=ctx)

        body_str = None
        headers = {"Content-Type": "application/json"}

        if method == "POST" and body is not None:
            body_str = json.dumps(body, separators=(",", ":"))

        if auth:
            if method == "GET":
                headers = self._auth_headers_get(full_url)
            else:
                headers = self._auth_headers_post(full_url, body_str or "")

        conn.request(method, url_path, body=body_str, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()

        if resp.status >= 400:
            raise Exception(f"HTTP {resp.status}: {data[:500]}")

        try:
            return json.loads(data)
        except json.JSONDecodeError:
            raise Exception(f"Invalid JSON response: {data[:500]}")

    # -- Public endpoints --

    def get_market_info(self, symbol: str = SYMBOL) -> dict:
        return self._request("GET", "/market/info", params={"symbol": symbol})

    def get_orderbook(self, symbol: str = SYMBOL, limit: int = 10) -> dict:
        return self._request("GET", "/market/orderbook",
                             params={"symbol": symbol, "limit": limit})

    def get_ticker(self, symbol: str = SYMBOL_UNDERSCORE) -> dict:
        return self._request("GET", f"/ticker/{symbol}")

    def get_recent_trades(self, symbol: str = SYMBOL, limit: int = 20) -> list:
        return self._request("GET", "/market/trades",
                             params={"symbol": symbol, "limit": limit})

    def get_server_time(self) -> dict:
        return self._request("GET", "/time")

    # -- Private endpoints --

    def get_balances(self) -> list:
        return self._request("GET", "/balances", auth=True)

    def get_balance(self, ticker: str) -> dict:
        """Get balance for a specific ticker."""
        balances = self.get_balances()
        for b in balances:
            if b.get("asset", "").upper() == ticker.upper():
                return b
        return {"asset": ticker, "available": "0", "held": "0", "pending": "0"}

    def create_order(self, symbol: str, side: str, quantity: str,
                     price: str = None, order_type: str = "limit",
                     user_provided_id: str = None) -> dict:
        body = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity,
        }
        if price is not None:
            body["price"] = price
        if user_provided_id:
            body["userProvidedId"] = user_provided_id
        return self._request("POST", "/createorder", body=body, auth=True)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("POST", "/cancelorder",
                             body={"id": order_id}, auth=True)

    def cancel_all_orders(self, symbol: str = SYMBOL, side: str = "all") -> dict:
        return self._request("POST", "/cancelallorders",
                             body={"symbol": symbol, "side": side}, auth=True)

    def get_my_orders(self, symbol: str = None, status: str = "active",
                      limit: int = 100, skip: int = 0) -> list:
        params = {"status": status, "limit": limit, "skip": skip}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/account/orders", params=params, auth=True)

    def get_my_trades(self, symbol: str = None, limit: int = 50,
                      skip: int = 0) -> list:
        params = {"limit": limit, "skip": skip}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/account/trades", params=params, auth=True)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/getorder/{order_id}", auth=True)


# ---------------------------------------------------------------------------
# Bot Engine
# ---------------------------------------------------------------------------

class TradingBot:
    """Asymmetric Market Maker: liberal buys + conservative sells."""

    def __init__(self, api: NonKYCApi, log_callback=None):
        self.api = api
        self.log_cb = log_callback or (lambda msg: print(msg))

        # Buy config (liberal defaults)
        self.buy_interval = DEFAULT_BUY_INTERVAL
        self.order_usdt = DEFAULT_ORDER_USDT
        self.budget_usdt = DEFAULT_BUDGET_USDT
        self.max_price = DEFAULT_MAX_PRICE
        self.buy_mode = DEFAULT_BUY_MODE
        self.price_bump_pct = DEFAULT_PRICE_BUMP

        # Sell config (very conservative defaults)
        self.sell_enabled = DEFAULT_SELL_ENABLED
        self.sell_interval = DEFAULT_SELL_INTERVAL
        self.sell_order_mewc = DEFAULT_SELL_ORDER_MEWC
        self.min_sell_price = DEFAULT_MIN_SELL_PRICE
        self.sell_spread_pct = DEFAULT_SELL_SPREAD_PCT
        self.min_mewc_reserve = DEFAULT_MIN_MEWC_RESERVE

        # Buy state
        self.total_usdt_spent = Decimal("0")
        self.total_mewc_bought = Decimal("0")
        self.total_orders = 0
        self.total_fills = 0
        self.buy_history = []

        # Sell state
        self.total_mewc_sold = Decimal("0")
        self.total_usdt_received = Decimal("0")
        self.total_sell_orders = 0
        self.total_sell_fills = 0
        self.sell_history = []

        # Market data (updated by polling)
        self.last_price = Decimal("0")
        self.best_bid = Decimal("0")
        self.best_ask = Decimal("0")
        self.volume_24h = ""
        self.change_pct = ""
        self.usdt_balance = Decimal("0")
        self.mewc_balance = Decimal("0")

        # Market info
        self.price_decimals = 8
        self.quantity_decimals = 4

        # Thread control
        self.running = False
        self._thread = None
        self._stop_event = threading.Event()

        # Load persisted state + settings
        self._load_state()
        self.load_settings()

    def _log(self, msg):
        logger.info(msg)
        self.log_cb(msg)

    # ---- Settings persistence (nonkyc_settings.json) ----

    def save_settings(self):
        """Save all bot settings into nonkyc_settings.json alongside API keys."""
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}

        data["bot_settings"] = {
            "buy_interval": self.buy_interval,
            "order_usdt": self.order_usdt,
            "budget_usdt": self.budget_usdt,
            "max_price": self.max_price,
            "buy_mode": self.buy_mode,
            "price_bump_pct": self.price_bump_pct,
            "sell_enabled": self.sell_enabled,
            "sell_interval": self.sell_interval,
            "sell_order_mewc": self.sell_order_mewc,
            "min_sell_price": self.min_sell_price,
            "sell_spread_pct": self.sell_spread_pct,
            "min_mewc_reserve": self.min_mewc_reserve,
        }

        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(data, f, indent=4)
            self._log("Settings saved to nonkyc_settings.json")
        except Exception as e:
            self._log(f"Failed to save settings: {e}")

    def load_settings(self):
        """Load bot settings from nonkyc_settings.json if present."""
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
            s = data.get("bot_settings")
            if not s:
                return False

            self.buy_interval = s.get("buy_interval", DEFAULT_BUY_INTERVAL)
            self.order_usdt = s.get("order_usdt", DEFAULT_ORDER_USDT)
            self.budget_usdt = s.get("budget_usdt", DEFAULT_BUDGET_USDT)
            self.max_price = s.get("max_price", DEFAULT_MAX_PRICE)
            self.buy_mode = s.get("buy_mode", DEFAULT_BUY_MODE)
            self.price_bump_pct = s.get("price_bump_pct", DEFAULT_PRICE_BUMP)

            self.sell_enabled = s.get("sell_enabled", DEFAULT_SELL_ENABLED)
            self.sell_interval = s.get("sell_interval", DEFAULT_SELL_INTERVAL)
            self.sell_order_mewc = s.get("sell_order_mewc", DEFAULT_SELL_ORDER_MEWC)
            self.min_sell_price = s.get("min_sell_price", DEFAULT_MIN_SELL_PRICE)
            self.sell_spread_pct = s.get("sell_spread_pct", DEFAULT_SELL_SPREAD_PCT)
            self.min_mewc_reserve = s.get("min_mewc_reserve", DEFAULT_MIN_MEWC_RESERVE)

            self._log("Loaded saved settings from nonkyc_settings.json")
            return True
        except Exception:
            return False

    # ---- State persistence (mewc_trader_state.json) ----

    def _save_state(self):
        """Persist cumulative session data."""
        state = {
            "total_usdt_spent": str(self.total_usdt_spent),
            "total_mewc_bought": str(self.total_mewc_bought),
            "total_orders": self.total_orders,
            "total_fills": self.total_fills,
            "buy_history": self.buy_history[-200:],
            "total_mewc_sold": str(self.total_mewc_sold),
            "total_usdt_received": str(self.total_usdt_received),
            "total_sell_orders": self.total_sell_orders,
            "total_sell_fills": self.total_sell_fills,
            "sell_history": self.sell_history[-200:],
        }
        try:
            with open(BOT_STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def _load_state(self):
        """Restore cumulative session data."""
        if not os.path.exists(BOT_STATE_FILE):
            return
        try:
            with open(BOT_STATE_FILE, "r") as f:
                state = json.load(f)
            self.total_usdt_spent = Decimal(state.get("total_usdt_spent", "0"))
            self.total_mewc_bought = Decimal(state.get("total_mewc_bought", "0"))
            self.total_orders = state.get("total_orders", 0)
            self.total_fills = state.get("total_fills", 0)
            self.buy_history = state.get("buy_history", [])
            self.total_mewc_sold = Decimal(state.get("total_mewc_sold", "0"))
            self.total_usdt_received = Decimal(state.get("total_usdt_received", "0"))
            self.total_sell_orders = state.get("total_sell_orders", 0)
            self.total_sell_fills = state.get("total_sell_fills", 0)
            self.sell_history = state.get("sell_history", [])
            self._log(f"Restored state: {self.total_usdt_spent} USDT spent, "
                      f"{self.total_mewc_bought} MEWC bought over {self.total_orders} orders | "
                      f"{self.total_mewc_sold} MEWC sold over {self.total_sell_orders} orders")
        except Exception as e:
            self._log(f"Could not load state: {e}")

    # ---- Market data ----

    def fetch_market_info(self):
        """Fetch market metadata (decimals, etc.)."""
        try:
            info = self.api.get_market_info(SYMBOL)
            self.price_decimals = info.get("priceDecimals", 8)
            self.quantity_decimals = info.get("quantityDecimals", 4)
            self._log(f"Market: {SYMBOL} | priceDecimals={self.price_decimals} "
                      f"quantityDecimals={self.quantity_decimals}")
            return info
        except Exception as e:
            self._log(f"Error fetching market info: {e}")
            return None

    def fetch_market_data(self):
        """Fetch current price, orderbook, and balances."""
        try:
            ticker = self.api.get_ticker(SYMBOL_UNDERSCORE)
            self.last_price = Decimal(str(ticker.get("last_price", "0")))
            self.best_bid = Decimal(str(ticker.get("bid", "0")))
            self.best_ask = Decimal(str(ticker.get("ask", "0")))
            self.volume_24h = ticker.get("base_volume", "0")
            self.change_pct = ticker.get("previous_day_price", "")

            prev = Decimal(str(self.change_pct)) if self.change_pct else Decimal("0")
            if prev > 0 and self.last_price > 0:
                change = ((self.last_price - prev) / prev * 100).quantize(Decimal("0.01"))
                self.change_pct = f"{'+' if change >= 0 else ''}{change}%"
            else:
                self.change_pct = "N/A"

        except Exception as e:
            self._log(f"Error fetching ticker: {e}")

        try:
            usdt_bal = self.api.get_balance("USDT")
            self.usdt_balance = Decimal(str(usdt_bal.get("available", "0")))
            mewc_bal = self.api.get_balance("MEWC")
            self.mewc_balance = Decimal(str(mewc_bal.get("available", "0")))
        except Exception as e:
            self._log(f"Error fetching balances: {e}")

    # ---- Buy side (liberal) ----

    def _calculate_buy_price(self) -> Decimal:
        """Calculate the buy price: slightly above best ask to push price up."""
        if self.best_ask <= 0:
            return Decimal("0")
        bump = Decimal(str(self.price_bump_pct)) / Decimal("100")
        price = self.best_ask * (Decimal("1") + bump)
        quant = Decimal("1") / Decimal(10 ** self.price_decimals)
        return price.quantize(quant, rounding=ROUND_UP)

    def _calculate_buy_quantity(self, price: Decimal) -> Decimal:
        """Calculate MEWC quantity for a given USDT amount."""
        if price <= 0:
            return Decimal("0")
        qty = Decimal(str(self.order_usdt)) / price
        quant = Decimal("1") / Decimal(10 ** self.quantity_decimals)
        return qty.quantize(quant, rounding=ROUND_DOWN)

    def place_buy_order(self) -> dict:
        """Place a single buy order."""
        # Budget check
        remaining = Decimal(str(self.budget_usdt)) - self.total_usdt_spent
        if remaining < Decimal(str(self.order_usdt)):
            self._log(f"Budget exhausted: spent {self.total_usdt_spent} / {self.budget_usdt} USDT")
            self.stop()
            return None

        # Balance check
        if self.usdt_balance < Decimal(str(self.order_usdt)):
            self._log(f"Insufficient USDT balance: {self.usdt_balance} < {self.order_usdt}")
            return None

        # Price check
        price = self._calculate_buy_price()
        if price <= 0:
            self._log("Could not determine buy price (ask is 0)")
            return None

        if price > Decimal(str(self.max_price)):
            self._log(f"Price {price} exceeds max price {self.max_price} — skipping")
            return None

        qty = self._calculate_buy_quantity(price)
        if qty <= 0:
            self._log("Calculated quantity is 0 — order too small")
            return None

        cost_usdt = (price * qty).quantize(Decimal("0.00000001"), rounding=ROUND_UP)
        order_id = str(uuid.uuid4()).replace("-", "")[:24]

        self._log(f"Placing BUY: {qty} MEWC @ {price} USDT "
                  f"(~{cost_usdt} USDT) [mode={self.buy_mode}]")

        try:
            if self.buy_mode == "market":
                result = self.api.create_order(
                    symbol=SYMBOL,
                    side="buy",
                    quantity=str(qty),
                    order_type="market",
                    user_provided_id=order_id,
                )
            else:
                result = self.api.create_order(
                    symbol=SYMBOL,
                    side="buy",
                    quantity=str(qty),
                    price=str(price),
                    order_type="limit",
                    user_provided_id=order_id,
                )

            self.total_orders += 1

            status = result.get("status", "").lower()
            exec_qty = Decimal(str(result.get("executedQuantity", "0")))

            if exec_qty > 0:
                self.total_fills += 1
                actual_cost = exec_qty * price
                self.total_usdt_spent += actual_cost
                self.total_mewc_bought += exec_qty
                self.buy_history.append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "price": str(price),
                    "qty": str(exec_qty),
                    "cost": str(actual_cost.quantize(Decimal("0.0001"))),
                })
                self._log(f"  -> Filled {exec_qty} MEWC (status: {status})")
            else:
                self._log(f"  -> Order placed (status: {status}, "
                          f"id: {result.get('id', 'unknown')[:12]}...)")

            self._save_state()
            return result

        except Exception as e:
            self._log(f"  -> Order FAILED: {e}")
            return None

    # ---- Sell side (very conservative) ----

    def _calculate_sell_price(self) -> Decimal:
        """Calculate sell price: well above best bid (wide spread, conservative)."""
        if self.best_bid <= 0:
            return Decimal("0")
        spread = Decimal(str(self.sell_spread_pct)) / Decimal("100")
        price = self.best_bid * (Decimal("1") + spread)
        quant = Decimal("1") / Decimal(10 ** self.price_decimals)
        return price.quantize(quant, rounding=ROUND_UP)

    def _calculate_sell_quantity(self) -> Decimal:
        """Calculate sell quantity, respecting the MEWC reserve."""
        available = self.mewc_balance - Decimal(str(self.min_mewc_reserve))
        if available <= 0:
            return Decimal("0")
        qty = min(available, Decimal(str(self.sell_order_mewc)))
        quant = Decimal("1") / Decimal(10 ** self.quantity_decimals)
        return qty.quantize(quant, rounding=ROUND_DOWN)

    def place_sell_order(self) -> dict:
        """Place a single conservative sell order."""
        if not self.sell_enabled:
            return None

        # Reserve check
        available = self.mewc_balance - Decimal(str(self.min_mewc_reserve))
        if available <= 0:
            self._log(f"SELL skip: MEWC balance {self.mewc_balance} "
                      f"below reserve {self.min_mewc_reserve}")
            return None

        price = self._calculate_sell_price()
        if price <= 0:
            self._log("SELL skip: could not determine sell price (bid is 0)")
            return None

        if price < Decimal(str(self.min_sell_price)):
            self._log(f"SELL skip: price {price} below min sell floor {self.min_sell_price}")
            return None

        qty = self._calculate_sell_quantity()
        if qty <= 0:
            self._log("SELL skip: quantity is 0 after reserve")
            return None

        revenue = (price * qty).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        order_id = str(uuid.uuid4()).replace("-", "")[:24]

        self._log(f"Placing SELL: {qty} MEWC @ {price} USDT (~{revenue} USDT)")

        try:
            result = self.api.create_order(
                symbol=SYMBOL,
                side="sell",
                quantity=str(qty),
                price=str(price),
                order_type="limit",
                user_provided_id=order_id,
            )

            self.total_sell_orders += 1
            status = result.get("status", "").lower()
            exec_qty = Decimal(str(result.get("executedQuantity", "0")))

            if exec_qty > 0:
                self.total_sell_fills += 1
                actual_rev = exec_qty * price
                self.total_mewc_sold += exec_qty
                self.total_usdt_received += actual_rev
                self.sell_history.append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "price": str(price),
                    "qty": str(exec_qty),
                    "revenue": str(actual_rev.quantize(Decimal("0.0001"))),
                })
                self._log(f"  -> SELL Filled {exec_qty} MEWC (status: {status})")
            else:
                self._log(f"  -> SELL Order placed (status: {status}, "
                          f"id: {result.get('id', 'unknown')[:12]}...)")

            self._save_state()
            return result

        except Exception as e:
            self._log(f"  -> SELL FAILED: {e}")
            return None

    # ---- Main loop ----

    def _run_loop(self):
        """Main bot loop running in a thread."""
        self._log("Bot started — Asymmetric Market Maker active")
        self._log(f"  BUY : every {self.buy_interval}s | {self.order_usdt} USDT | "
                  f"bump {self.price_bump_pct}% | max {self.max_price}")
        if self.sell_enabled:
            self._log(f"  SELL: every {self.sell_interval}s | {self.sell_order_mewc} MEWC | "
                      f"spread {self.sell_spread_pct}% | min {self.min_sell_price} | "
                      f"reserve {self.min_mewc_reserve}")
        else:
            self._log("  SELL: disabled")

        # Initial market info
        self.fetch_market_info()
        self.fetch_market_data()

        cycle = 0
        last_sell_time = 0.0

        while not self._stop_event.is_set():
            cycle += 1
            now = time.time()
            try:
                # Refresh market data every cycle
                self.fetch_market_data()

                # Place buy order every cycle
                self.place_buy_order()

                # Place sell order on its own (slower) timer
                if self.sell_enabled and (now - last_sell_time) >= self.sell_interval:
                    self.place_sell_order()
                    last_sell_time = now

                # Log periodic summary
                if cycle % 10 == 0:
                    avg = (self.total_usdt_spent / self.total_mewc_bought
                           if self.total_mewc_bought > 0 else Decimal("0"))
                    parts = [
                        f"BUY: {self.total_orders} orders, {self.total_fills} fills, "
                        f"{self.total_usdt_spent:.4f} USDT spent",
                    ]
                    if self.sell_enabled:
                        parts.append(
                            f"SELL: {self.total_sell_orders} orders, "
                            f"{self.total_sell_fills} fills, "
                            f"{self.total_usdt_received:.4f} USDT received"
                        )
                    self._log(f"[Summary] {' | '.join(parts)}")

            except Exception as e:
                self._log(f"Loop error: {e}")

            # Wait for next buy cycle (interruptible)
            self._stop_event.wait(self.buy_interval)

        self._log("Bot stopped.")

    def start(self):
        if self.running:
            return
        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self._stop_event.set()
        self._save_state()
        self._log("Stopping bot...")

    def reset_stats(self):
        """Reset cumulative stats."""
        self.total_usdt_spent = Decimal("0")
        self.total_mewc_bought = Decimal("0")
        self.total_orders = 0
        self.total_fills = 0
        self.buy_history = []
        self.total_mewc_sold = Decimal("0")
        self.total_usdt_received = Decimal("0")
        self.total_sell_orders = 0
        self.total_sell_fills = 0
        self.sell_history = []
        self._save_state()
        self._log("Stats reset.")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

DARK_BG = "#1a1a2e"
DARKER_BG = "#16213e"
ACCENT = "#0f3460"
HIGHLIGHT = "#e94560"
TEXT_COLOR = "#eaeaea"
GREEN = "#00e676"
RED = "#ff5252"
GOLD = "#ffd700"
MUTED = "#888888"


class TradingBotGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MEWC/USDT Market Maker — NonKYC.io")
        self.root.geometry("1060x820")
        self.root.configure(bg=DARK_BG)
        self.root.resizable(True, True)

        # Placeholders (loaded after UI is built)
        self.api = None
        self.bot = None

        # Style
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TFrame", background=DARK_BG)
        style.configure("Card.TFrame", background=DARKER_BG)
        style.configure("Dark.TLabel", background=DARK_BG, foreground=TEXT_COLOR,
                        font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=DARKER_BG, foreground=TEXT_COLOR,
                        font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=DARK_BG, foreground=GOLD,
                        font=("Segoe UI", 14, "bold"))
        style.configure("Big.TLabel", background=DARKER_BG, foreground=GREEN,
                        font=("Consolas", 18, "bold"))
        style.configure("Stat.TLabel", background=DARKER_BG, foreground=TEXT_COLOR,
                        font=("Consolas", 11))
        style.configure("Green.TButton", background=GREEN, foreground="#000",
                        font=("Segoe UI", 10, "bold"))
        style.configure("Red.TButton", background=RED, foreground="#fff",
                        font=("Segoe UI", 10, "bold"))
        style.configure("Dark.TButton", background=ACCENT, foreground=TEXT_COLOR,
                        font=("Segoe UI", 9))
        style.map("Green.TButton", background=[("active", "#00c853")])
        style.map("Red.TButton", background=[("active", "#d32f2f")])
        style.map("Dark.TButton", background=[("active", "#1a4a7a")])

        self._build_ui()
        self._load_api()
        self._populate_settings_from_bot()
        self._append_log("MEWC/USDT Market Maker initialized")
        if self.api:
            self._append_log(f"API key loaded: {self.api.access_key[:8]}...")
        self._update_display()

    def _load_api(self):
        try:
            with open(SETTINGS_FILE) as f:
                settings = json.load(f)
            self.api = NonKYCApi(settings["access_key"], settings["secret_key"])
            self.bot = TradingBot(self.api, log_callback=self._append_log)
        except Exception as e:
            msg = (f"Could not load settings:\n{e}\n\n"
                   f"Place nonkyc_settings.json next to the exe/script with:\n"
                   f'  {{"access_key": "...", "secret_key": "..."}}\n\n'
                   f"Expected path:\n{SETTINGS_FILE}")
            _write_crash_log(f"_load_api failed: {e}")
            self.root.destroy()           # close the half-built main window
            _show_error_window("Config Error", msg)
            sys.exit(1)

    def _populate_settings_from_bot(self):
        """Push bot's current settings (loaded from file or defaults) into GUI fields."""
        if not self.bot:
            return

        def _set(entry, value):
            entry.delete(0, tk.END)
            entry.insert(0, str(value))

        _set(self.interval_entry, self.bot.buy_interval)
        _set(self.order_size_entry, self.bot.order_usdt)
        _set(self.budget_entry, self.bot.budget_usdt)
        _set(self.max_price_entry, self.bot.max_price)
        self.mode_var.set(self.bot.buy_mode)
        _set(self.bump_entry, self.bot.price_bump_pct)

        self.sell_enabled_var.set(self.bot.sell_enabled)
        _set(self.sell_interval_entry, self.bot.sell_interval)
        _set(self.sell_order_entry, self.bot.sell_order_mewc)
        _set(self.min_sell_price_entry, self.bot.min_sell_price)
        _set(self.sell_spread_entry, self.bot.sell_spread_pct)
        _set(self.reserve_entry, self.bot.min_mewc_reserve)

    def _build_ui(self):
        # Top bar
        top = ttk.Frame(self.root, style="Dark.TFrame")
        top.pack(fill=tk.X, padx=10, pady=(10, 5))

        ttk.Label(top, text="\U0001f431 MEWC/USDT Market Maker", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(top, text="NonKYC.io", style="Dark.TLabel",
                  font=("Segoe UI", 10, "italic")).pack(side=tk.LEFT, padx=(10, 0))

        # Status indicator
        self.status_label = ttk.Label(top, text="\u25cf STOPPED", style="Dark.TLabel",
                                      foreground=RED, font=("Segoe UI", 10, "bold"))
        self.status_label.pack(side=tk.RIGHT)

        # Main paned layout
        main = ttk.Frame(self.root, style="Dark.TFrame")
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Left column — market data + stats
        left = ttk.Frame(main, style="Dark.TFrame", width=400)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        # Market data card
        market_card = ttk.LabelFrame(left, text=" Market Data ",
                                     style="Card.TFrame")
        market_card.configure(labelwidget=self._card_label("\U0001f4ca Market Data"))
        market_card.pack(fill=tk.X, pady=(0, 5))
        market_card.configure(style="Card.TFrame")

        mf = ttk.Frame(market_card, style="Card.TFrame")
        mf.pack(fill=tk.X, padx=10, pady=5)

        self.price_var = tk.StringVar(value="--")
        self.bid_var = tk.StringVar(value="--")
        self.ask_var = tk.StringVar(value="--")
        self.spread_var = tk.StringVar(value="--")
        self.vol_var = tk.StringVar(value="--")
        self.change_var = tk.StringVar(value="--")

        ttk.Label(mf, text="Last Price:", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(mf, textvariable=self.price_var, style="Big.TLabel").grid(row=0, column=1, sticky="e", padx=(10, 0))

        labels = [("Best Bid:", self.bid_var), ("Best Ask:", self.ask_var),
                  ("Spread:", self.spread_var), ("24h Volume:", self.vol_var),
                  ("24h Change:", self.change_var)]
        for i, (lbl, var) in enumerate(labels, start=1):
            ttk.Label(mf, text=lbl, style="Card.TLabel").grid(row=i, column=0, sticky="w", pady=1)
            ttk.Label(mf, textvariable=var, style="Stat.TLabel").grid(row=i, column=1, sticky="e", pady=1)

        mf.columnconfigure(1, weight=1)

        # Balance card
        bal_card = ttk.Frame(left, style="Card.TFrame")
        bal_card.pack(fill=tk.X, pady=(0, 5))

        bf = ttk.Frame(bal_card, style="Card.TFrame")
        bf.pack(fill=tk.X, padx=10, pady=5)

        self.usdt_bal_var = tk.StringVar(value="--")
        self.mewc_bal_var = tk.StringVar(value="--")

        ttk.Label(bf, text="\U0001f4b0 Balances", style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(bf, text="USDT:", style="Card.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Label(bf, textvariable=self.usdt_bal_var, style="Stat.TLabel").grid(row=1, column=1, sticky="e")
        ttk.Label(bf, text="MEWC:", style="Card.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Label(bf, textvariable=self.mewc_bal_var, style="Stat.TLabel").grid(row=2, column=1, sticky="e")
        bf.columnconfigure(1, weight=1)

        # Bot stats card — Buy
        stats_card = ttk.Frame(left, style="Card.TFrame")
        stats_card.pack(fill=tk.X, pady=(0, 5))

        sf = ttk.Frame(stats_card, style="Card.TFrame")
        sf.pack(fill=tk.X, padx=10, pady=5)

        self.orders_var = tk.StringVar(value="0")
        self.fills_var = tk.StringVar(value="0")
        self.spent_var = tk.StringVar(value="0.0000")
        self.bought_var = tk.StringVar(value="0")
        self.avg_var = tk.StringVar(value="--")
        self.budget_remain_var = tk.StringVar(value="--")

        ttk.Label(sf, text="\U0001f4c8 Buy Stats", style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")

        stat_labels = [("Orders Placed:", self.orders_var),
                       ("Orders Filled:", self.fills_var),
                       ("USDT Spent:", self.spent_var),
                       ("MEWC Bought:", self.bought_var),
                       ("Avg Buy Price:", self.avg_var),
                       ("Budget Left:", self.budget_remain_var)]
        for i, (lbl, var) in enumerate(stat_labels, start=1):
            ttk.Label(sf, text=lbl, style="Card.TLabel").grid(row=i, column=0, sticky="w", pady=1)
            ttk.Label(sf, textvariable=var, style="Stat.TLabel").grid(row=i, column=1, sticky="e", pady=1)
        sf.columnconfigure(1, weight=1)

        # Bot stats card — Sell
        sell_stats_card = ttk.Frame(left, style="Card.TFrame")
        sell_stats_card.pack(fill=tk.X, pady=(0, 5))

        ssf = ttk.Frame(sell_stats_card, style="Card.TFrame")
        ssf.pack(fill=tk.X, padx=10, pady=5)

        self.sell_orders_var = tk.StringVar(value="0")
        self.sell_fills_var = tk.StringVar(value="0")
        self.sell_received_var = tk.StringVar(value="0.0000")
        self.sell_sold_var = tk.StringVar(value="0")

        ttk.Label(ssf, text="\U0001f4c9 Sell Stats", style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")

        sell_stat_labels = [("Sell Orders:", self.sell_orders_var),
                            ("Sell Fills:", self.sell_fills_var),
                            ("USDT Received:", self.sell_received_var),
                            ("MEWC Sold:", self.sell_sold_var)]
        for i, (lbl, var) in enumerate(sell_stat_labels, start=1):
            ttk.Label(ssf, text=lbl, style="Card.TLabel").grid(row=i, column=0, sticky="w", pady=1)
            ttk.Label(ssf, textvariable=var, style="Stat.TLabel").grid(row=i, column=1, sticky="e", pady=1)
        ssf.columnconfigure(1, weight=1)

        # Right column — controls + log
        right = ttk.Frame(main, style="Dark.TFrame", width=550)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))

        # ---- Buy Settings card ----
        buy_ctrl_card = ttk.Frame(right, style="Card.TFrame")
        buy_ctrl_card.pack(fill=tk.X, pady=(0, 5))

        bcf = ttk.Frame(buy_ctrl_card, style="Card.TFrame")
        bcf.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(bcf, text="\u2699\ufe0f Buy Settings (liberal)", style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=4, sticky="w")

        # Row 1: interval + order size
        ttk.Label(bcf, text="Buy Interval (s):", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        self.interval_entry = ttk.Entry(bcf, width=8)
        self.interval_entry.insert(0, str(DEFAULT_BUY_INTERVAL))
        self.interval_entry.grid(row=1, column=1, padx=(5, 15), pady=2)

        ttk.Label(bcf, text="Order Size (USDT):", style="Card.TLabel").grid(row=1, column=2, sticky="w", pady=2)
        self.order_size_entry = ttk.Entry(bcf, width=8)
        self.order_size_entry.insert(0, str(DEFAULT_ORDER_USDT))
        self.order_size_entry.grid(row=1, column=3, padx=(5, 0), pady=2)

        # Row 2: budget + max price
        ttk.Label(bcf, text="Total Budget (USDT):", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        self.budget_entry = ttk.Entry(bcf, width=8)
        self.budget_entry.insert(0, str(DEFAULT_BUDGET_USDT))
        self.budget_entry.grid(row=2, column=1, padx=(5, 15), pady=2)

        ttk.Label(bcf, text="Max Price (USDT):", style="Card.TLabel").grid(row=2, column=2, sticky="w", pady=2)
        self.max_price_entry = ttk.Entry(bcf, width=10)
        self.max_price_entry.insert(0, str(DEFAULT_MAX_PRICE))
        self.max_price_entry.grid(row=2, column=3, padx=(5, 0), pady=2)

        # Row 3: buy mode + price bump
        ttk.Label(bcf, text="Buy Mode:", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=2)
        self.mode_var = tk.StringVar(value=DEFAULT_BUY_MODE)
        mode_frame = ttk.Frame(bcf, style="Card.TFrame")
        mode_frame.grid(row=3, column=1, sticky="w", padx=(5, 15), pady=2)
        ttk.Radiobutton(mode_frame, text="Limit", variable=self.mode_var, value="limit").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_frame, text="Market", variable=self.mode_var, value="market").pack(side=tk.LEFT, padx=(5, 0))

        ttk.Label(bcf, text="Price Bump %:", style="Card.TLabel").grid(row=3, column=2, sticky="w", pady=2)
        self.bump_entry = ttk.Entry(bcf, width=8)
        self.bump_entry.insert(0, str(DEFAULT_PRICE_BUMP))
        self.bump_entry.grid(row=3, column=3, padx=(5, 0), pady=2)

        # ---- Sell Settings card ----
        sell_ctrl_card = ttk.Frame(right, style="Card.TFrame")
        sell_ctrl_card.pack(fill=tk.X, pady=(0, 5))

        scf = ttk.Frame(sell_ctrl_card, style="Card.TFrame")
        scf.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(scf, text="\U0001f6e1\ufe0f Sell Settings (conservative)", style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")

        self.sell_enabled_var = tk.BooleanVar(value=DEFAULT_SELL_ENABLED)
        sell_chk = tk.Checkbutton(scf, text="Enable Sell Side", variable=self.sell_enabled_var,
                                  bg=DARKER_BG, fg=TEXT_COLOR, selectcolor=ACCENT,
                                  activebackground=DARKER_BG, activeforeground=TEXT_COLOR,
                                  font=("Segoe UI", 9))
        sell_chk.grid(row=0, column=3, sticky="e", pady=2)

        # Row 1: sell interval + order size
        ttk.Label(scf, text="Sell Interval (s):", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        self.sell_interval_entry = ttk.Entry(scf, width=8)
        self.sell_interval_entry.insert(0, str(DEFAULT_SELL_INTERVAL))
        self.sell_interval_entry.grid(row=1, column=1, padx=(5, 15), pady=2)

        ttk.Label(scf, text="MEWC per Sell:", style="Card.TLabel").grid(row=1, column=2, sticky="w", pady=2)
        self.sell_order_entry = ttk.Entry(scf, width=10)
        self.sell_order_entry.insert(0, str(DEFAULT_SELL_ORDER_MEWC))
        self.sell_order_entry.grid(row=1, column=3, padx=(5, 0), pady=2)

        # Row 2: min price + spread
        ttk.Label(scf, text="Min Sell Price:", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        self.min_sell_price_entry = ttk.Entry(scf, width=10)
        self.min_sell_price_entry.insert(0, str(DEFAULT_MIN_SELL_PRICE))
        self.min_sell_price_entry.grid(row=2, column=1, padx=(5, 15), pady=2)

        ttk.Label(scf, text="Sell Spread %:", style="Card.TLabel").grid(row=2, column=2, sticky="w", pady=2)
        self.sell_spread_entry = ttk.Entry(scf, width=8)
        self.sell_spread_entry.insert(0, str(DEFAULT_SELL_SPREAD_PCT))
        self.sell_spread_entry.grid(row=2, column=3, padx=(5, 0), pady=2)

        # Row 3: MEWC reserve
        ttk.Label(scf, text="MEWC Reserve:", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=2)
        self.reserve_entry = ttk.Entry(scf, width=10)
        self.reserve_entry.insert(0, str(DEFAULT_MIN_MEWC_RESERVE))
        self.reserve_entry.grid(row=3, column=1, padx=(5, 15), pady=2)

        # Buttons
        btn_frame = ttk.Frame(right, style="Dark.TFrame")
        btn_frame.pack(fill=tk.X, pady=(5, 5))

        self.start_btn = tk.Button(btn_frame, text="\u25b6  START", bg=GREEN, fg="#000",
                                   font=("Segoe UI", 11, "bold"), width=12,
                                   command=self._start_bot, relief=tk.FLAT)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.stop_btn = tk.Button(btn_frame, text="\u23f9  STOP", bg=RED, fg="#fff",
                                  font=("Segoe UI", 11, "bold"), width=12,
                                  command=self._stop_bot, relief=tk.FLAT, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 5))

        tk.Button(btn_frame, text="\U0001f4be Save Settings", bg="#1976d2", fg="#fff",
                  font=("Segoe UI", 9, "bold"), command=self._save_settings,
                  relief=tk.FLAT).pack(side=tk.LEFT, padx=(0, 5))

        tk.Button(btn_frame, text="Refresh", bg=ACCENT, fg=TEXT_COLOR,
                  font=("Segoe UI", 9), command=self._manual_refresh,
                  relief=tk.FLAT).pack(side=tk.LEFT, padx=(0, 5))

        tk.Button(btn_frame, text="Cancel Orders", bg="#ff9800", fg="#000",
                  font=("Segoe UI", 9), command=self._cancel_all,
                  relief=tk.FLAT).pack(side=tk.LEFT, padx=(0, 5))

        tk.Button(btn_frame, text="Reset Stats", bg=MUTED, fg="#fff",
                  font=("Segoe UI", 9), command=self._reset_stats,
                  relief=tk.FLAT).pack(side=tk.RIGHT)

        # Log
        log_frame = ttk.Frame(right, style="Dark.TFrame")
        log_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(log_frame, text="\U0001f4cb Activity Log", style="Dark.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=18, bg="#0d1117", fg="#c9d1d9",
            insertbackground=TEXT_COLOR, font=("Consolas", 9),
            relief=tk.FLAT, wrap=tk.WORD, state=tk.DISABLED
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(3, 0))

        # Configure log tags
        self.log_text.tag_configure("buy", foreground=GREEN)
        self.log_text.tag_configure("sell", foreground="#ff9800")
        self.log_text.tag_configure("error", foreground=RED)
        self.log_text.tag_configure("info", foreground=TEXT_COLOR)
        self.log_text.tag_configure("summary", foreground=GOLD)
        self.log_text.tag_configure("save", foreground="#64b5f6")

        # Bottom status bar
        status_bar = ttk.Frame(self.root, style="Dark.TFrame")
        status_bar.pack(fill=tk.X, padx=10, pady=(5, 10))
        self.bottom_status = ttk.Label(status_bar, text="Ready", style="Dark.TLabel",
                                       foreground=MUTED, font=("Segoe UI", 8))
        self.bottom_status.pack(side=tk.LEFT)

    def _card_label(self, text):
        lbl = ttk.Label(self.root, text=text, style="Card.TLabel",
                        font=("Segoe UI", 10, "bold"))
        return lbl

    def _append_log(self, msg, tag="info"):
        """Thread-safe log append."""
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"

        # Determine tag from content
        if "SELL" in msg and ("Placing" in msg or "Filled" in msg):
            tag = "sell"
        elif "BUY" in msg or "Filled" in msg or "bought" in msg.lower():
            tag = "buy"
        elif "error" in msg.lower() or "FAILED" in msg:
            tag = "error"
        elif "Summary" in msg:
            tag = "summary"
        elif "saved" in msg.lower() or "Settings" in msg:
            tag = "save"

        def _insert():
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, line, tag)
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)

        if threading.current_thread() is threading.main_thread():
            _insert()
        else:
            self.root.after(0, _insert)

    def _apply_settings(self):
        """Read GUI settings into bot."""
        try:
            # Buy settings
            self.bot.buy_interval = max(10, int(self.interval_entry.get()))
            self.bot.order_usdt = max(0.01, float(self.order_size_entry.get()))
            self.bot.budget_usdt = max(1, float(self.budget_entry.get()))
            self.bot.max_price = float(self.max_price_entry.get())
            self.bot.buy_mode = self.mode_var.get()
            self.bot.price_bump_pct = max(0, float(self.bump_entry.get()))

            # Sell settings
            self.bot.sell_enabled = self.sell_enabled_var.get()
            self.bot.sell_interval = max(30, int(self.sell_interval_entry.get()))
            self.bot.sell_order_mewc = max(1, float(self.sell_order_entry.get()))
            self.bot.min_sell_price = float(self.min_sell_price_entry.get())
            self.bot.sell_spread_pct = max(0.1, float(self.sell_spread_entry.get()))
            self.bot.min_mewc_reserve = max(0, float(self.reserve_entry.get()))
        except ValueError as e:
            messagebox.showerror("Settings Error", f"Invalid setting value: {e}")
            return False
        return True

    def _save_settings(self):
        """Save current GUI settings to nonkyc_settings.json."""
        if not self._apply_settings():
            return
        self.bot.save_settings()

    def _start_bot(self):
        if self.bot.running:
            return
        if not self._apply_settings():
            return
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.status_label.configure(text="\u25cf RUNNING", foreground=GREEN)
        self.bot.start()

    def _stop_bot(self):
        self.bot.stop()
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.status_label.configure(text="\u25cf STOPPED", foreground=RED)

    def _manual_refresh(self):
        """Manual refresh in background thread."""
        def _do():
            self._append_log("Refreshing market data...")
            self.bot.fetch_market_data()
        threading.Thread(target=_do, daemon=True).start()

    def _cancel_all(self):
        if not messagebox.askyesno("Confirm", "Cancel ALL open MEWC/USDT orders?"):
            return
        def _do():
            try:
                result = self.api.cancel_all_orders(SYMBOL, "all")
                self._append_log(f"Cancel all orders: {result}")
            except Exception as e:
                self._append_log(f"Cancel error: {e}")
        threading.Thread(target=_do, daemon=True).start()

    def _reset_stats(self):
        if not messagebox.askyesno("Confirm", "Reset all cumulative bot stats?"):
            return
        self.bot.reset_stats()

    def _update_display(self):
        """Periodically update GUI with bot state (runs on main thread)."""
        if self.bot:
            # Market data
            if self.bot.last_price > 0:
                self.price_var.set(f"{self.bot.last_price}")
            self.bid_var.set(f"{self.bot.best_bid}")
            self.ask_var.set(f"{self.bot.best_ask}")
            if self.bot.best_ask > 0 and self.bot.best_bid > 0:
                spread = self.bot.best_ask - self.bot.best_bid
                spread_pct = (spread / self.bot.best_ask * 100).quantize(Decimal("0.01"))
                self.spread_var.set(f"{spread} ({spread_pct}%)")
            self.vol_var.set(f"{self.bot.volume_24h} MEWC")
            self.change_var.set(str(self.bot.change_pct))

            # Balances
            self.usdt_bal_var.set(f"{self.bot.usdt_balance}")
            self.mewc_bal_var.set(f"{self.bot.mewc_balance}")

            # Buy stats
            self.orders_var.set(str(self.bot.total_orders))
            self.fills_var.set(str(self.bot.total_fills))
            self.spent_var.set(f"{self.bot.total_usdt_spent:.4f}")
            self.bought_var.set(f"{self.bot.total_mewc_bought:.0f}")

            if self.bot.total_mewc_bought > 0:
                avg = self.bot.total_usdt_spent / self.bot.total_mewc_bought
                self.avg_var.set(f"{avg:.10f}")
            else:
                self.avg_var.set("--")

            remaining = Decimal(str(self.bot.budget_usdt)) - self.bot.total_usdt_spent
            self.budget_remain_var.set(f"{remaining:.4f} USDT")

            # Sell stats
            self.sell_orders_var.set(str(self.bot.total_sell_orders))
            self.sell_fills_var.set(str(self.bot.total_sell_fills))
            self.sell_received_var.set(f"{self.bot.total_usdt_received:.4f}")
            self.sell_sold_var.set(f"{self.bot.total_mewc_sold:.0f}")

            # Status check
            if self.bot.running:
                self.status_label.configure(text="\u25cf RUNNING", foreground=GREEN)
                self.start_btn.configure(state=tk.DISABLED)
                self.stop_btn.configure(state=tk.NORMAL)
            else:
                self.status_label.configure(text="\u25cf STOPPED", foreground=RED)
                self.start_btn.configure(state=tk.NORMAL)
                self.stop_btn.configure(state=tk.DISABLED)

            # Bottom status
            sell_tag = " | SELL: ON" if self.bot.sell_enabled else ""
            self.bottom_status.configure(
                text=f"Last update: {datetime.now().strftime('%H:%M:%S')} | "
                     f"Price: {self.bot.last_price} | "
                     f"Buy Orders: {self.bot.total_orders} | "
                     f"Sell Orders: {self.bot.total_sell_orders}{sell_tag}")

        # Schedule next update
        self.root.after(2000, self._update_display)


# ---------------------------------------------------------------------------
# Crash log helper
# ---------------------------------------------------------------------------

def _write_crash_log(msg: str):
    """Write a crash report next to the exe/script for debugging."""
    try:
        crash_path = os.path.join(_APP_DIR, "mewc_trader_crash.log")
        with open(crash_path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} — {msg}\n")
    except Exception:
        pass  # if we can't write the crash log, nothing we can do


def _show_error_window(title: str, message: str):
    """Show a standalone Tk error window that works in frozen / --windowed mode."""
    err = tk.Tk()
    err.title(title)
    err.configure(bg="#1e1e2e")
    err.geometry("560x260")
    err.resizable(False, False)
    tk.Label(err, text=title, fg="#ff5252", bg="#1e1e2e",
             font=("Segoe UI", 13, "bold")).pack(pady=(14, 4))
    txt = tk.Text(err, wrap="word", bg="#2a2a3c", fg="#eaeaea",
                  font=("Consolas", 10), relief="flat", height=8)
    txt.pack(padx=14, pady=6, fill="both", expand=True)
    txt.insert("1.0", message)
    txt.configure(state="disabled")
    tk.Button(err, text="OK", width=10, command=err.destroy,
              bg="#3a5a8c", fg="#eaeaea", font=("Segoe UI", 10, "bold")).pack(pady=(0, 10))
    err.mainloop()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()
    app = TradingBotGUI(root)

    # Initial market data fetch in background
    def _initial_fetch():
        try:
            app.bot.fetch_market_info()
            app.bot.fetch_market_data()
            app._append_log(f"Market loaded: {SYMBOL} | "
                            f"Price: {app.bot.last_price} | "
                            f"Bid: {app.bot.best_bid} | Ask: {app.bot.best_ask}")
        except Exception as e:
            app._append_log(f"Initial fetch error: {e}")

    threading.Thread(target=_initial_fetch, daemon=True).start()

    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        _write_crash_log(tb)
        try:
            _show_error_window("MEWC Trader — Fatal Error", tb)
        except Exception:
            pass
        sys.exit(1)

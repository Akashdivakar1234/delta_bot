import os
import sys
import time
import hmac
import hashlib
import json
import math
import requests
from datetime import datetime, timezone, timedelta

# --- COLOR PRINTING UTILITIES ---
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

def log_info(msg):
    print(f"{Colors.BLUE}[INFO]{Colors.RESET} {msg}")

def log_success(msg):
    print(f"{Colors.GREEN}{Colors.BOLD}[SUCCESS] {msg}{Colors.RESET}")

def log_warning(msg):
    print(f"{Colors.YELLOW}[WARNING]{Colors.RESET} {msg}")

def log_error(msg):
    print(f"{Colors.RED}{Colors.BOLD}[ERROR] {msg}{Colors.RESET}")

def log_setup(msg):
    print(f"{Colors.CYAN}{Colors.BOLD}[SETUP ALERT] {msg}{Colors.RESET}")


# --- DELTA EXCHANGE API CLIENT ---
class DeltaExchangeAPI:
    def __init__(self, api_key, api_secret, is_testnet=True, use_india_delta=True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.is_testnet = is_testnet
        self.use_india_delta = use_india_delta
        
        # Determine Base URL
        if self.is_testnet:
            self.base_url = "https://cdn-ind.testnet.deltaex.org" if use_india_delta else "https://testnet-api.delta.exchange"
        else:
            self.base_url = "https://api.india.delta.exchange" if use_india_delta else "https://api.delta.exchange"
            
        log_info(f"Initialized Delta Client on: {self.base_url}")
        
    def _generate_signature(self, method, path, timestamp, body=""):
        # Pre-hash string: METHOD + TIMESTAMP + PATH + BODY
        message = method + str(timestamp) + path + body
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    def request(self, method, endpoint, params=None, payload=None, auth=True):
        url = f"{self.base_url}{endpoint}"
        
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "python-requests"
        }
        
        if auth:
            timestamp = int(time.time())
            
            # Serialize body to JSON string if present
            body_str = ""
            if payload:
                body_str = json.dumps(payload)
                
            # Build path with query params for signature if needed
            sig_path = endpoint
            if params:
                query_str = "&".join([f"{k}={v}" for k, v in params.items()])
                sig_path = f"{endpoint}?{query_str}"
                
            # Generate signature
            signature = self._generate_signature(method, sig_path, timestamp, body_str)
            
            headers.update({
                "api-key": self.api_key,
                "signature": signature,
                "timestamp": str(timestamp)
            })
        
        max_retries = 3
        backoff_factor = 2
        
        for attempt in range(1, max_retries + 1):
            try:
                if method == "GET":
                    response = requests.get(url, headers=headers, params=params, timeout=10)
                elif method == "POST":
                    response = requests.post(url, headers=headers, json=payload, timeout=10)
                elif method == "PATCH":
                    response = requests.patch(url, headers=headers, json=payload, timeout=10)
                elif method == "DELETE":
                    response = requests.delete(url, headers=headers, json=payload, timeout=10)
                else:
                    raise ValueError(f"Unsupported method: {method}")
                    
                if response.status_code in [200, 201, 204]:
                    if not response.text.strip():
                        return {"success": True}
                    return response.json()
                elif response.status_code >= 500:
                    raise requests.exceptions.HTTPError(f"Server Error {response.status_code}")
                else:
                    try:
                        err_json = response.json()
                        return {"success": False, "error": err_json.get("error", {}).get("message", response.text)}
                    except:
                        return {"success": False, "error": f"HTTP {response.status_code}: {response.text}"}
                        
            except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout) as e:
                log_warning(f"Connection attempt {attempt}/{max_retries} failed for {method} {endpoint}: {e}")
                if attempt == max_retries:
                    log_error(f"Failed after {max_retries} connection attempts: {e}")
                    return {"success": False, "error": str(e)}
                time.sleep(backoff_factor ** attempt)
                
            except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout) as e:
                if method == "GET":
                    log_warning(f"Read timeout attempt {attempt}/{max_retries} failed for GET {endpoint}: {e}")
                    if attempt == max_retries:
                        log_error(f"GET failed after {max_retries} read timeout attempts: {e}")
                        return {"success": False, "error": str(e)}
                    time.sleep(backoff_factor ** attempt)
                else:
                    log_error(f"Read timeout on non-idempotent {method} request to {endpoint}. Aborting to prevent duplicate side-effects. Error: {e}")
                    return {"success": False, "error": f"Read timeout: {e}"}
                    
            except requests.exceptions.HTTPError as e:
                log_warning(f"HTTP Server error attempt {attempt}/{max_retries} for {method} {endpoint}: {e}")
                if attempt == max_retries:
                    log_error(f"Failed after {max_retries} HTTP Server error attempts: {e}")
                    return {"success": False, "error": str(e)}
                time.sleep(backoff_factor ** attempt)
                
            except Exception as e:
                log_error(f"HTTP Request failed with unexpected error: {e}")
                return {"success": False, "error": str(e)}

    def get_products(self):
        # Fetch list of all live products
        res = self.request("GET", "/v2/products", auth=False)
        if res.get("success") or isinstance(res, list):
            # API can return direct list or {success: true, result: [...]}
            return res.get("result", res) if isinstance(res, dict) else res
        return []

    def get_ticker(self, symbol):
        params = {"symbol": symbol}
        res = self.request("GET", "/v2/tickers", params=params, auth=False)
        if res.get("success"):
            # Can return a list or dict
            result = res.get("result", {})
            if isinstance(result, list) and len(result) > 0:
                return result[0]
            return result
        return {}

    def get_candles(self, symbol, resolution, limit=200):
        # Endpoint: /v2/history/candles
        end_time = int(time.time())
        # Subtract seconds based on resolution to get appropriate start time
        res_seconds = {
            "1m": 60,
            "5m": 300,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400
        }
        seconds = res_seconds.get(resolution, 60) * limit
        start_time = end_time - seconds
        
        params = {
            "symbol": symbol,
            "resolution": resolution,
            "start": start_time,
            "end": end_time
        }
        
        res = self.request("GET", "/v2/history/candles", params=params, auth=False)
        if isinstance(res, dict) and res.get("success"):
            candles = res.get("result", [])
            # Reversing candles to chronological order (oldest first)
            return candles[::-1]
        return []

    def set_margin_mode_isolated(self):
        # Set account-wide margin mode to isolated
        payload = {"margin_mode": "isolated"}
        return self.request("PATCH", "/v2/margin_mode", payload=payload)

    def set_leverage(self, product_id, leverage):
        # Set leverage for a specific product
        payload = {
            "product_id": int(product_id),
            "leverage": str(leverage)
        }
        return self.request("POST", "/v2/products/leverage", payload=payload)

    def place_bracket_order(self, product_id, size, side, limit_price, stop_loss, take_profit):
        payload = {
            "product_id": int(product_id),
            "size": int(size),
            "side": side.lower(),
            "order_type": "limit_order",
            "limit_price": str(limit_price),
            "bracket_stop_loss_price": str(stop_loss),
            "bracket_take_profit_price": str(take_profit),
            "post_only": False
        }
        return self.request("POST", "/v2/orders", payload=payload)


# --- STRATEGY HELPER FUNCTIONS ---
def get_usd_inr_rate():
    url = "https://open.er-api.com/v6/latest/USD"
    max_retries = 3
    backoff_factor = 2
    for attempt in range(1, max_retries + 1):
        try:
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                data = res.json()
                rate = data["rates"]["INR"]
                return rate
            else:
                log_warning(f"USD/INR rate attempt {attempt}/{max_retries} failed with status {res.status_code}")
        except Exception as e:
            log_warning(f"USD/INR rate attempt {attempt}/{max_retries} failed: {e}")
        if attempt < max_retries:
            time.sleep(backoff_factor ** attempt)
    return None

def is_within_killzone():
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_tz)
    
    # Check Weekend (5 = Saturday, 6 = Sunday)
    if now.weekday() in [5, 6]:
        return False, "Weekend restriction active (Saturday/Sunday)"
        
    time_str = now.strftime("%H:%M")
    
    # Killzones
    # London Killzone: 12:30 PM – 3:30 PM IST (12:30 to 15:30)
    london_active = "12:30" <= time_str <= "15:30"
    
    # New York Killzone: 5:30 PM – 8:30 PM IST (17:30 to 20:30)
    ny_active = "17:30" <= time_str <= "20:30"
    
    if london_active:
        return True, "London Killzone Active"
    if ny_active:
        return True, "New York Killzone Active"
        
    return False, f"Outside Permitted Killzone Windows (Current IST: {time_str})"

def calculate_ema(prices, period=20):
    if len(prices) < period:
        return None
    multiplier = 2 / (period + 1)
    # Start with SMA as initial EMA value
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))
    return ema

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high = float(candles[i]['high'])
        low = float(candles[i]['low'])
        prev_close = float(candles[i-1]['close'])
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        trs.append(tr)
    return sum(trs[-period:]) / period

def detect_swings(candles, n=2):
    """
    Finds swing highs and lows using a 2n+1 candle window (fractals).
    Excludes the active candle (index -1).
    """
    swing_highs = []
    swing_lows = []
    
    for i in range(n, len(candles) - n):
        # We check high/low against n candles on both sides
        high_val = float(candles[i]['high'])
        low_val = float(candles[i]['low'])
        
        is_high = True
        is_low = True
        
        for j in range(1, n + 1):
            if high_val < float(candles[i-j]['high']) or high_val < float(candles[i+j]['high']):
                is_high = False
            if low_val > float(candles[i-j]['low']) or low_val > float(candles[i+j]['low']):
                is_low = False
                
        if is_high:
            swing_highs.append({'index': i, 'price': high_val, 'time': candles[i]['time']})
        if is_low:
            swing_lows.append({'index': i, 'price': low_val, 'time': candles[i]['time']})
            
    return swing_highs, swing_lows

def round_step(value, step):
    """Rounds a value to the nearest step size."""
    if step == 0:
        return value
    return round(value / step) * step

def floor_step(value, step):
    """Rounds a value down to the nearest step size."""
    if step == 0:
        return value
    return math.floor((value + 1e-9) / step) * step


# --- CORE BOT SCANNER CLASS ---
class DeltaBot:
    def __init__(self, config_path="config.json"):
        with open(config_path, "r") as f:
            self.config = json.load(f)
            
        self.api_key = self.config["api_key"]
        self.api_secret = self.config["api_secret"]
        self.is_testnet = self.config["is_testnet"]
        self.use_india_delta = self.config["use_india_delta"]
        self.symbol = self.config["symbol"]
        self.risk_percent = self.config.get("risk_percent", 2.0)
        self.capital_mode = self.config.get("capital_mode", "auto")
        self.static_capital_inr = self.config.get("static_capital_inr", 350.0)
        self.rr_ratio = self.config["target_rr_ratio"]
        self.leverage = self.config["leverage"]
        self.usd_inr_fallback = self.config["usd_inr_rate_fallback"]
        self.poll_interval = self.config["poll_interval_seconds"]
        
        # Check if dummy credentials
        self.is_mock_mode = (self.api_key == "YOUR_DELTA_API_KEY" or self.api_secret == "YOUR_DELTA_API_SECRET")
        
        self.api = DeltaExchangeAPI(
            self.api_key, 
            self.api_secret, 
            is_testnet=self.is_testnet, 
            use_india_delta=self.use_india_delta
        )
        
        self.product_info = {}
        self.last_processed_candle_time = 0
        
    def setup_account(self):
        if self.is_mock_mode:
            log_warning("Bot is running in MOCK mode (No API Keys provided). Order placement simulated.")
            return True
            
        # Get product metadata
        log_info(f"Fetching product metadata for {self.symbol}...")
        products = self.api.get_products()
        match = next((p for p in products if p.get('symbol') == self.symbol), None)
        if not match:
            log_error(f"Symbol {self.symbol} not found on Delta Exchange.")
            return False
            
        self.product_info = {
            "id": match["id"],
            "symbol": match["symbol"],
            "tick_size": float(match["tick_size"]),
            "contract_value": float(match.get("contract_value", 1.0)),
            "step_size": float(match.get("step_size", 1.0))
        }
        log_success(f"Product Loaded: ID={self.product_info['id']}, Tick Size={self.product_info['tick_size']}, Contract Value={self.product_info['contract_value']}")
        
        # Set account margin mode to isolated
        log_info("Setting Account Margin Mode to ISOLATED...")
        res = self.api.set_margin_mode_isolated()
        if res.get("success"):
            log_success("Margin mode set to ISOLATED.")
        else:
            log_warning(f"Could not set margin mode automatically (this is normal on Testnet — set it manually). Reason: {res.get('error', 'unknown')}")
        
        # Set leverage
        log_info(f"Setting leverage to {self.leverage}x...")
        res = self.api.set_leverage(self.product_info["id"], self.leverage)
        if res.get("success"):
            log_success(f"Leverage set to {self.leverage}x.")
        else:
            log_warning(f"Could not set leverage automatically (this is normal on Testnet — set it manually in the Delta Exchange UI). Reason: {res.get('error', 'unknown')}")
        return True

    def scan_market(self):
        # 1. Time / Day Rule check
        allowed, reason = is_within_killzone()
        if not allowed:
            log_info(f"Scanning paused: {reason}")
            return
            
        # 2. Get current USDINR conversion rate
        usd_inr = get_usd_inr_rate()
        if not usd_inr:
            usd_inr = self.usd_inr_fallback
        log_info(f"Active Session. Current USD/INR Rate: {usd_inr:.2f}")
        
        # 3. Get HTF trend direction (1H resolution)
        candles_1h = self.api.get_candles(self.symbol, "1h", limit=50)
        if len(candles_1h) < 25:
            log_warning("Insufficient 1H candles to determine trend.")
            return
            
        close_prices_1h = [float(c['close']) for c in candles_1h]
        current_close_1h = close_prices_1h[-1]
        ema_20_1h = calculate_ema(close_prices_1h, 20)
        
        if not ema_20_1h:
            log_warning("Failed to calculate EMA 20.")
            return
            
        htf_bullish = current_close_1h > ema_20_1h
        trend_str = "BULLISH (Buyside Draw)" if htf_bullish else "BEARISH (Sellside Draw)"
        log_info(f"HTF Trend: {trend_str} (Price: {current_close_1h:.5f} vs 20EMA: {ema_20_1h:.5f})")

        # 4. Get LFT execution candles (1m resolution)
        candles_1m = self.api.get_candles(self.symbol, "1m", limit=100)
        if len(candles_1m) < 20:
            log_warning("Insufficient 1m candles for setup check.")
            return
            
        latest_1m_close = float(candles_1m[-1]['close'])
        log_info(f"Scanning 1m chart. Latest price: {latest_1m_close:.5f}")
            
        # Exclude active forming candle (index -1)
        completed_candles = candles_1m[:-1]
        latest_completed_time = completed_candles[-1]['time']
        
        # Skip if already analyzed this candle
        if latest_completed_time <= self.last_processed_candle_time:
            return
            
        self.last_processed_candle_time = latest_completed_time
        
        # 5. Detect Swings (using N=2 fractal)
        swing_highs, swing_lows = detect_swings(completed_candles, n=2)
        if not swing_highs or not swing_lows:
            return
            
        # Calculate 14-period ATR for displacement check
        atr = calculate_atr(completed_candles, 14)
        
        # Find the most recent Swing High and Swing Low
        latest_sh = swing_highs[-1]
        latest_sl = swing_lows[-1]
        
        # Let's inspect the last few completed candles for Setup
        # We scan the last 5 completed candles for a Sweep + MSS + FVG
        for i in range(len(completed_candles) - 3, len(completed_candles) - 1):
            # Define Candle 1, Candle 2 (Displacement), Candle 3
            c1 = completed_candles[i-1]
            c2 = completed_candles[i]  # Candidate displacement candle
            c3 = completed_candles[i+1]
            
            c1_high, c1_low = float(c1['high']), float(c1['low'])
            c2_high, c2_low = float(c2['high']), float(c2['low'])
            c2_open, c2_close = float(c2['open']), float(c2['close'])
            c3_high, c3_low = float(c3['high']), float(c3['low'])
            
            # --- CASE A: BULLISH SETUP (LONG) ---
            # 1. Sweep of Swing Low: Low of candle drops below previous swing low, close is above it.
            # (We look at any of the preceding candles to check for the sweep)
            swept_low = False
            sweep_price_low = 0.0
            
            # Did c1 or c2 sweep a previous swing low?
            for sl in swing_lows:
                if sl['index'] < i - 1:  # Must be a historical swing low
                    # If candle swept it
                    if c2_low < sl['price'] and c2_close > sl['price']:
                        swept_low = True
                        sweep_price_low = c2_low
                        break
                    elif c1_low < sl['price'] and float(c1['close']) > sl['price']:
                        swept_low = True
                        sweep_price_low = c1_low
                        break
            
            # 2. HTF must align (if we want strict draw on liquidity, we buy only when HTF is bullish)
            if swept_low and htf_bullish:
                # 3. MSS (Market Structure Shift) on Candle 2 (Displacement)
                # Candle 2 body closes above the recent swing high (internal structure)
                is_mss_up = c2_close > latest_sh['price']
                
                # 4. Displacement Check (Candle 2 body must be >= 1.5x ATR)
                body_size = c2_close - c2_open
                is_displacement_up = body_size >= 1.5 * atr
                
                if is_mss_up and is_displacement_up:
                    # 5. Bullish FVG check (Candle 1 Low > Candle 3 High)
                    if c1_low > c3_high:
                        # Setup Detected!
                        entry_price = c1_low  # Top of FVG
                        stop_loss = sweep_price_low - (self.product_info.get("tick_size", 0.01) * 2) # Buffer
                        take_profit = entry_price + (self.rr_ratio * (entry_price - stop_loss))
                        
                        log_setup(f"A+ BULLISH SETUP DETECTED!")
                        log_info(f"Entry: {entry_price:.5f} | SL: {stop_loss:.5f} | TP: {take_profit:.5f}")
                        self.execute_trade("BUY", entry_price, stop_loss, take_profit, usd_inr)
                        return
            
            # --- CASE B: BEARISH SETUP (SHORT) ---
            # 1. Sweep of Swing High: High of candle pushes above previous swing high, close is below it.
            swept_high = False
            sweep_price_high = 0.0
            
            for sh in swing_highs:
                if sh['index'] < i - 1:
                    if c2_high > sh['price'] and c2_close < sh['price']:
                        swept_high = True
                        sweep_price_high = c2_high
                        break
                    elif c1_high > sh['price'] and float(c1['close']) < sh['price']:
                        swept_high = True
                        sweep_price_high = c1_high
                        break
                        
            if swept_high and not htf_bullish:
                # 2. MSS: Candle 2 closes below recent swing low
                is_mss_down = c2_close < latest_sl['price']
                
                # 3. Displacement check
                body_size = c2_open - c2_close
                is_displacement_down = body_size >= 1.5 * atr
                
                if is_mss_down and is_displacement_down:
                    # 4. Bearish FVG check (Candle 1 High < Candle 3 Low)
                    if c1_high < c3_low:
                        # Setup Detected!
                        entry_price = c1_high  # Bottom of FVG
                        stop_loss = sweep_price_high + (self.product_info.get("tick_size", 0.01) * 2) # Buffer
                        take_profit = entry_price - (self.rr_ratio * (stop_loss - entry_price))
                        
                        log_setup(f"A+ BEARISH SETUP DETECTED!")
                        log_info(f"Entry: {entry_price:.5f} | SL: {stop_loss:.5f} | TP: {take_profit:.5f}")
                        self.execute_trade("SELL", entry_price, stop_loss, take_profit, usd_inr)
                        return

    def get_current_risk_inr(self, usd_inr):
        if self.is_mock_mode or self.capital_mode == "static":
            capital = self.static_capital_inr
            log_info(f"Using STATIC capital base: {capital:.2f} INR")
        else:
            try:
                res = self.api.request("GET", "/v2/wallet/balances")
                capital = 0.0
                if res.get("success") or isinstance(res, list):
                    balances = res.get("result", res) if isinstance(res, dict) else res
                    for bal in balances:
                        symbol = bal.get("symbol", "").upper()
                        val = float(bal.get("available_balance", bal.get("balance", 0.0)))
                        
                        if symbol == "INR":
                            capital += val
                        elif symbol in ["USD", "USDT", "USDC"]:
                            capital += val * usd_inr
                            
                if capital <= 0:
                    capital = self.static_capital_inr
                    log_warning(f"No wallet balance found or balance is 0. Falling back to static capital: {capital:.2f} INR")
                else:
                    log_info(f"Retrieved dynamic account balance: {capital:.2f} INR")
            except Exception as e:
                capital = self.static_capital_inr
                log_warning(f"Failed to fetch balances: {e}. Falling back to static capital: {capital:.2f} INR")
                
        # Risk = capital * percentage
        risk_inr = capital * (self.risk_percent / 100.0)
        return risk_inr

    def execute_trade(self, side, entry, stop_loss, take_profit, usd_inr):
        # Enforce structural rules
        price_distance = abs(entry - stop_loss)
        
        # Round prices to correct tick size
        tick_size = self.product_info.get("tick_size", 0.01)
        entry_rounded = round_step(entry, tick_size)
        sl_rounded = round_step(stop_loss, tick_size)
        tp_rounded = round_step(take_profit, tick_size)
        
        # Dynamic risk calculation
        current_risk_inr = self.get_current_risk_inr(usd_inr)
        risk_usd = current_risk_inr / usd_inr
        contract_val = self.product_info.get("contract_value", 1.0)
        
        # Calculate raw size: size = risk_usd / (distance * contract_val)
        raw_size = risk_usd / (price_distance * contract_val)
        
        # Round size down to step size
        step_size = self.product_info.get("step_size", 1.0)
        final_size = floor_step(raw_size, step_size)
        
        # PRO-GOVERNANCE MANDATE CHECK
        # Calculate actual cash risk at this size
        actual_risk_inr = (final_size * contract_val * price_distance) * usd_inr
        
        log_info(f"Sizing details: Target Risk={current_risk_inr:.2f} INR ({risk_usd:.4f} USD)")
        log_info(f"Raw Size calculated: {raw_size:.4f} contracts, Rounded Size: {final_size} contracts")
        log_info(f"Actual Risk on execution: {actual_risk_inr:.2f} INR")
        
        # If order size is 0, or actual risk exceeds calculated dynamic risk (with 0.05 tolerance for rounding)
        if final_size <= 0:
            log_warning("Calculated position size is 0. Trade skipped.")
            return
            
        if actual_risk_inr > (current_risk_inr + 0.05):
            log_error(f"PRO-GOVERNANCE MANDATE TRIGGERED: Cash risk ({actual_risk_inr:.2f} INR) exceeds risk ceiling of {current_risk_inr:.2f} INR. Trade AUTO-INVALIDATED.")
            return
            
        # Place Order
        if self.is_mock_mode:
            log_success(f"[MOCK] Placed isolated margin bracket order:")
            log_success(f"[MOCK] Symbol: {self.symbol} | Side: {side} | Size: {final_size} contracts")
            log_success(f"[MOCK] Limit Entry: {entry_rounded:.5f} | Stop Loss: {sl_rounded:.5f} | Take Profit: {tp_rounded:.5f}")
        else:
            log_info("Submitting bracket order to Delta Exchange API...")
            product_id = self.product_info["id"]
            res = self.api.place_bracket_order(
                product_id=product_id,
                size=final_size,
                side=side,
                limit_price=entry_rounded,
                stop_loss=sl_rounded,
                take_profit=tp_rounded
            )
            
            if res.get("success"):
                log_success(f"Order successfully filled on Exchange: {res}")
            else:
                err_msg = res.get("error", str(res))
                if "bracket_order_position_exists" in err_msg or "bracket_order_position_exists" in str(res):
                    log_info(f"Bracket order or position already exists for {self.symbol}. Skipping trade.")
                else:
                    log_error(f"Exchange rejected order: {res}")

    def start(self):
        log_info("Starting Delta Exchange Bot...")
        if not self.setup_account():
            log_error("Setup failed. Terminating.")
            return
            
        log_success("Account and Product specifications configured successfully. Bot is now active.")
        log_info(f"Polling intervals set to: {self.poll_interval}s. Scanning...")
        
        while True:
            try:
                self.scan_market()
            except Exception as e:
                log_error(f"Error in scan loop: {e}")
            time.sleep(self.poll_interval)


# --- BOT EXECUTION ENTRY POINT ---
if __name__ == "__main__":
    bot = DeltaBot()
    bot.start()

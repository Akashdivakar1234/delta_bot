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


# --- DNS CACHING ADAPTER ---
from urllib3.util.connection import create_connection as _orig_create_connection
import socket

class DNSCacheAdapter(requests.adapters.HTTPAdapter):
    """HTTPAdapter that caches DNS lookups to avoid repeated resolution."""
    _dns_cache = {}
    
    def send(self, request, **kwargs):
        from urllib.parse import urlparse
        parsed = urlparse(request.url)
        hostname = parsed.hostname
        if hostname and hostname not in self._dns_cache:
            try:
                self._dns_cache[hostname] = socket.gethostbyname(hostname)
            except Exception:
                pass  # Let it fall through to normal resolution
        return super().send(request, **kwargs)
    
    def init_poolmanager(self, *args, **kwargs):
        import urllib3
        # Monkey-patch connection creation to use cached IPs
        original_create_connection = urllib3.util.connection.create_connection
        dns_cache = self._dns_cache
        
        def patched_create_connection(address, *args, **kwargs):
            host, port = address
            if host in dns_cache:
                address = (dns_cache[host], port)
            return original_create_connection(address, *args, **kwargs)
        
        urllib3.util.connection.create_connection = patched_create_connection
        super().init_poolmanager(*args, **kwargs)


# --- DELTA EXCHANGE API CLIENT ---
class DeltaExchangeAPI:
    def __init__(self, keys, is_testnet=True, use_india_delta=True):
        self.keys = keys  # List of dicts with {"api_key": ..., "api_secret": ...}
        self.active_key_index = 0
        self.is_testnet = is_testnet
        self.use_india_delta = use_india_delta
        
        # Determine Base URL
        if self.is_testnet:
            self.base_url = "https://cdn-ind.testnet.deltaex.org" if use_india_delta else "https://testnet-api.delta.exchange"
        else:
            self.base_url = "https://api.india.delta.exchange" if use_india_delta else "https://api.delta.exchange"
        
        # Create a session with DNS caching adapter
        self.session = requests.Session()
        adapter = DNSCacheAdapter(max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        
        # Pre-resolve and cache the hostname
        from urllib.parse import urlparse
        hostname = urlparse(self.base_url).hostname
        try:
            cached_ip = socket.gethostbyname(hostname)
            DNSCacheAdapter._dns_cache[hostname] = cached_ip
            log_info(f"Initialized Delta Client on: {self.base_url} (Cached IP: {cached_ip})")
        except Exception as e:
            log_warning(f"Initial DNS resolution failed ({e}). Will retry on first request.")
            log_info(f"Initialized Delta Client on: {self.base_url}")
        
    def _get_active_credentials(self):
        k = self.keys[self.active_key_index]
        return k["api_key"], k["api_secret"]
        
    def _generate_signature(self, method, path, timestamp, body="", api_secret=None):
        message = method + str(timestamp) + path + body
        signature = hmac.new(
            api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    def request(self, method, endpoint, params=None, payload=None, auth=True):
        url = f"{self.base_url}{endpoint}"
        
        max_retries = 6
        retry_waits = [5, 10, 15, 20, 25, 30]
        
        # Run a retry loop with potential key rotation on IP whitelist issues
        
        for attempt in range(1, max_retries + 1):
            # Dynamic header construction based on active key
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "python-requests"
            }
            
            api_key, api_secret = self._get_active_credentials()
            
            if auth:
                timestamp = int(time.time())
                body_str = ""
                if payload:
                    body_str = json.dumps(payload)
                    
                sig_path = endpoint
                if params:
                    query_str = "&".join([f"{k}={v}" for k, v in params.items()])
                    sig_path = f"{endpoint}?{query_str}"
                    
                signature = self._generate_signature(method, sig_path, timestamp, body_str, api_secret)
                
                headers.update({
                    "api-key": api_key,
                    "signature": signature,
                    "timestamp": str(timestamp)
                })
                
            try:
                if method == "GET":
                    response = self.session.get(url, headers=headers, params=params, timeout=15)
                elif method == "POST":
                    response = self.session.post(url, headers=headers, json=payload, timeout=15)
                elif method == "PATCH":
                    response = self.session.patch(url, headers=headers, json=payload, timeout=15)
                elif method == "DELETE":
                    response = self.session.delete(url, headers=headers, json=payload, timeout=15)
                else:
                    raise ValueError(f"Unsupported method: {method}")
                    
                if response.status_code in [200, 201, 204]:
                    if not response.text.strip():
                        return {"success": True}
                    res_json = response.json()
                    
                    # Intercept API-level errors that return 200 OK but success=False
                    if isinstance(res_json, dict) and not res_json.get("success"):
                        err_str = str(res_json.get("error", ""))
                        if "ip_not_whitelisted_for_api_key" in err_str:
                            next_index = (self.active_key_index + 1) % len(self.keys)
                            if next_index != self.active_key_index:
                                log_warning(f"API 200 IP whitelist failure with API key index {self.active_key_index}. Rotating to key index {next_index}...")
                                self.active_key_index = next_index
                                # Force retry instantly
                                continue
                                
                    return res_json
                elif response.status_code >= 500:
                    raise requests.exceptions.HTTPError(f"Server Error {response.status_code}")
                else:
                    try:
                        err_json = response.json()
                        err_code = err_json.get("error", {}).get("code")
                        err_msg = err_json.get("error", {}).get("message", response.text)
                        
                        # Dynamic Key Rotation: Check for IP Whitelist rejection
                        if err_code == "ip_not_whitelisted_for_api_key":
                            next_index = (self.active_key_index + 1) % len(self.keys)
                            if next_index != self.active_key_index:
                                log_warning(f"IP whitelist failure with API key index {self.active_key_index} ({err_msg}). Rotating to key index {next_index}...")
                                self.active_key_index = next_index
                                # Force retry instantly
                                continue
                                
                        return {"success": False, "error": err_msg}
                    except Exception as parse_err:
                        return {"success": False, "error": f"HTTP {response.status_code}: {response.text}"}
                        
            except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout) as e:
                # Close stale connections so next retry uses a fresh socket
                self.session.close()
                wait = retry_waits[attempt - 1] if attempt <= len(retry_waits) else 30
                if attempt < max_retries:
                    log_warning(f"Connection attempt {attempt}/{max_retries} failed. Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    log_error(f"Failed after {max_retries} connection attempts. Will retry next scan cycle.")
                    return {"success": False, "error": str(e)}
                
            except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout) as e:
                self.session.close()
                if method == "GET":
                    wait = retry_waits[attempt - 1] if attempt <= len(retry_waits) else 30
                    if attempt < max_retries:
                        log_warning(f"Read timeout attempt {attempt}/{max_retries}. Retrying in {wait}s...")
                        time.sleep(wait)
                    else:
                        log_error(f"GET failed after {max_retries} timeout attempts. Will retry next scan cycle.")
                        return {"success": False, "error": str(e)}
                else:
                    log_error(f"Read timeout on non-idempotent {method} request to {endpoint}. Aborting to prevent duplicate side-effects. Error: {e}")
                    return {"success": False, "error": f"Read timeout: {e}"}
                    
            except requests.exceptions.HTTPError as e:
                wait = retry_waits[attempt - 1] if attempt <= len(retry_waits) else 30
                if attempt < max_retries:
                    log_warning(f"HTTP Server error attempt {attempt}/{max_retries}. Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    log_error(f"Failed after {max_retries} HTTP Server error attempts: {e}")
                    return {"success": False, "error": str(e)}
                
            except Exception as e:
                log_error(f"HTTP Request failed with unexpected error: {e}")
                return {"success": False, "error": str(e)}

    def get_products(self):
        res = self.request("GET", "/v2/products", auth=False)
        if res.get("success") or isinstance(res, list):
            return res.get("result", res) if isinstance(res, dict) else res
        return []

    def get_ticker(self, symbol):
        params = {"symbol": symbol}
        res = self.request("GET", "/v2/tickers", params=params, auth=False)
        if res.get("success"):
            result = res.get("result", {})
            if isinstance(result, list) and len(result) > 0:
                return result[0]
            return result
        return {}

    def get_candles(self, symbol, resolution, limit=200):
        end_time = int(time.time())
        res_seconds = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
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
            return candles[::-1]
        return []

    def set_margin_mode_isolated(self):
        payload = {"margin_mode": "isolated"}
        return self.request("PATCH", "/v2/margin_mode", payload=payload)

    def set_leverage(self, product_id, leverage):
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

    def place_reduce_only_limit_order(self, product_id, size, side, limit_price):
        payload = {
            "product_id": int(product_id),
            "size": int(size),
            "side": side.lower(),
            "order_type": "limit_order",
            "limit_price": str(limit_price),
            "reduce_only": True,
            "post_only": False
        }
        return self.request("POST", "/v2/orders", payload=payload)

    def edit_bracket_order(self, product_id, stop_loss_price, take_profit_price):
        payload = {
            "product_id": int(product_id),
            "stop_loss_order": {
                "order_type": "market_order",
                "stop_price": str(stop_loss_price)
            },
            "take_profit_order": {
                "order_type": "limit_order",
                "stop_price": str(take_profit_price),
                "limit_price": str(take_profit_price)
            },
            "bracket_stop_trigger_method": "last_traded_price"
        }
        return self.request("PUT", "/v2/orders/bracket", payload=payload)

    def cancel_order(self, order_id, product_id):
        payload = {
            "id": int(order_id),
            "product_id": int(product_id)
        }
        res = self.request("DELETE", "/v2/orders", payload=payload)
        if not res.get("success"):
            # Try fallback endpoint
            res = self.request("POST", f"/v2/orders/{order_id}/cancel", payload={})
        return res

    def get_open_orders(self, product_id):
        params = {
            "product_id": int(product_id),
            "state": "open"
        }
        res = self.request("GET", "/v2/orders", params=params)
        if isinstance(res, dict) and res.get("success"):
            return res.get("result", [])
        elif isinstance(res, list):
            return res
        return []




# --- STRATEGY HELPER FUNCTIONS ---
def get_usd_inr_rate():
    # Delta India uses a standard conversion rate of 1 USD = ₹85 as seen on the platform
    return 85.0

def is_within_killzone():
    return True, "Trading Active 24/7"

def round_step(value, step):
    if step == 0:
        return value
    step_str = f"{step:.10f}".rstrip('0')
    precision = len(step_str.split('.')[1]) if '.' in step_str else 0
    return round(round(value / step) * step, precision)

def floor_step(value, step):
    if step == 0:
        return value
    return math.floor((value + 1e-9) / step) * step

def convert_to_heikin_ashi(candles):
    ha_candles = []
    for idx, c in enumerate(candles):
        c_open = float(c['open'])
        c_high = float(c['high'])
        c_low = float(c['low'])
        c_close = float(c['close'])
        
        if idx == 0:
            ha_open = (c_open + c_close) / 2
        else:
            prev = ha_candles[-1]
            ha_open = (prev['open'] + prev['close']) / 2
            
        ha_close = (c_open + c_high + c_low + c_close) / 4
        ha_high = max(c_high, ha_open, ha_close)
        ha_low = min(c_low, ha_open, ha_close)
        
        ha_candles.append({
            'time': c['time'],
            'open': ha_open,
            'high': ha_high,
            'low': ha_low,
            'close': ha_close,
            'volume': float(c['volume'])
        })
    return ha_candles

def calculate_adx_wilders(ha_candles, period=14):
    n = len(ha_candles)
    if n < period * 2 + 5:
        return None, False
        
    highs = [c['high'] for c in ha_candles]
    lows = [c['low'] for c in ha_candles]
    closes = [c['close'] for c in ha_candles]
    
    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    
    for i in range(1, n):
        up = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        
    smoothed_tr = [0.0] * n
    smoothed_plus_dm = [0.0] * n
    smoothed_minus_dm = [0.0] * n
    
    smoothed_tr[period] = sum(tr[1:period+1])
    smoothed_plus_dm[period] = sum(plus_dm[1:period+1])
    smoothed_minus_dm[period] = sum(minus_dm[1:period+1])
    
    for i in range(period + 1, n):
        smoothed_tr[i] = smoothed_tr[i-1] - (smoothed_tr[i-1] / period) + tr[i]
        smoothed_plus_dm[i] = smoothed_plus_dm[i-1] - (smoothed_plus_dm[i-1] / period) + plus_dm[i]
        smoothed_minus_dm[i] = smoothed_minus_dm[i-1] - (smoothed_minus_dm[i-1] / period) + minus_dm[i]
        
    plus_di = [0.0] * n
    minus_di = [0.0] * n
    dx = [0.0] * n
    
    for i in range(period, n):
        if smoothed_tr[i] > 0:
            plus_di[i] = 100 * smoothed_plus_dm[i] / smoothed_tr[i]
            minus_di[i] = 100 * smoothed_minus_dm[i] / smoothed_tr[i]
        else:
            plus_di[i] = 0.0
            minus_di[i] = 0.0
            
        sum_di = plus_di[i] + minus_di[i]
        diff_di = abs(plus_di[i] - minus_di[i])
        dx[i] = 100 * diff_di / sum_di if sum_di > 0 else 0.0
        
    adx = [0.0] * n
    adx[period * 2 - 1] = sum(dx[period:period * 2]) / period
    for i in range(period * 2, n):
        adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period
        
    curr_adx = adx[-1]
    prev_adx = adx[-2]
    is_rising = curr_adx > prev_adx
    return curr_adx, is_rising

def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema


# --- CORE TREND BOT CLASS ---
class DeltaTrendBot:
    def __init__(self, config_path="trend_config.json"):
        with open(config_path, "r") as f:
            self.config = json.load(f)
            
        self.keys = self.config.get("keys", [{"api_key": self.config.get("api_key"), "api_secret": self.config.get("api_secret")}])
        # Use first key for backward-compatibility checks
        self.api_key = self.keys[0]["api_key"]
        self.api_secret = self.keys[0]["api_secret"]
        
        self.is_testnet = self.config["is_testnet"]
        self.use_india_delta = self.config["use_india_delta"]
        
        # Multi-symbol configuration
        self.symbols = self.config.get("symbols", [self.config.get("symbol", "BIOUSD")])
        self.symbol = self.symbols[0]  # Backwards compatibility
        self.symbol_settings = self.config.get("symbol_settings", {})
        
        self.risk_percent = self.config.get("risk_percent", 2.0)
        self.capital_mode = self.config.get("capital_mode", "auto")
        self.static_capital_inr = self.config.get("static_capital_inr", 350.0)
        self.use_breakeven = self.config.get("use_breakeven", True)
        self.breakeven_trigger = self.config.get("breakeven_trigger", 1.5)
        self.tp1_ratio = self.config.get("tp1_ratio", 2.0)
        self.tp2_ratio = self.config.get("tp2_ratio", 5.0)
        self.tp1_percent = self.config.get("tp1_percent", 25.0)
        self.rr_ratio = self.tp2_ratio # Keep for backward compatibility
        self.leverage = self.config["leverage"]
        self.usd_inr_fallback = self.config["usd_inr_rate_fallback"]
        self.poll_interval = self.config["poll_interval_seconds"]
        self.channel_period = self.config.get("channel_period", 20)
        self.resolution = self.config.get("resolution", "15m")
        
        self.is_mock_mode = (self.api_key == "YOUR_DELTA_API_KEY" or self.api_key is None or self.api_secret == "YOUR_DELTA_API_SECRET")
        
        self.api = DeltaExchangeAPI(
            self.keys, 
            is_testnet=self.is_testnet, 
            use_india_delta=self.use_india_delta
        )
        
        self.product_info = {}
        self.last_processed_candle_time = {}
        self.state_file = "active_trade_trend.json"
        self.load_trade_state()
        
    def get_symbol_setting(self, symbol, key, default):
        if symbol in self.symbol_settings and key in self.symbol_settings[symbol]:
            return self.symbol_settings[symbol][key]
        return self.config.get(key, default)
        
    def send_discord_message(self, message):
        enabled = self.config.get("discord_enabled", False)
        webhook_url = self.config.get("discord_webhook_url", "")
        
        if not (enabled and webhook_url):
            return
            
        payload = {
            "content": message
        }
        try:
            res = requests.post(webhook_url, json=payload, timeout=10)
            if res.status_code not in [200, 204]:
                log_error(f"Discord Webhook error: {res.status_code} - {res.text}")
        except Exception as e:
            log_error(f"Failed to send Discord alert: {e}")
        
    def setup_account(self):
        if self.is_mock_mode:
            log_warning("Bot is running in MOCK mode. Order placement simulated.")
            for symbol in self.symbols:
                self.product_info[symbol] = {
                    "id": 12345,
                    "symbol": symbol,
                    "tick_size": 0.05 if "ETH" in symbol else (0.0001 if any(s in symbol for s in ["SOL", "XRP", "SUI"]) else 0.00001),
                    "contract_value": 0.01 if "ETH" in symbol else (10.0 if "BIO" in symbol else 1.0),
                    "step_size": 1.0
                }
            return True
            
        log_info("Fetching product metadata from Delta Exchange...")
        products = self.api.get_products()
        
        for symbol in self.symbols:
            match = next((p for p in products if p.get('symbol') == symbol), None)
            if not match:
                log_error(f"Symbol {symbol} not found on Delta Exchange.")
                return False
                
            self.product_info[symbol] = {
                "id": match["id"],
                "symbol": match["symbol"],
                "tick_size": float(match["tick_size"]),
                "contract_value": float(match.get("contract_value", 1.0)),
                "step_size": float(match.get("step_size", 1.0))
            }
            log_success(f"Product Loaded for {symbol}: ID={self.product_info[symbol]['id']}, Tick Size={self.product_info[symbol]['tick_size']}, Contract Value={self.product_info[symbol]['contract_value']}")
            
            symbol_leverage = self.get_symbol_setting(symbol, "leverage", self.leverage)
            log_info(f"Setting leverage for {symbol} to {symbol_leverage}x...")
            res = self.api.set_leverage(self.product_info[symbol]["id"], symbol_leverage)
            if res.get("success"):
                log_success(f"Leverage for {symbol} set to {symbol_leverage}x.")
            else:
                log_warning(f"Could not set leverage for {symbol} automatically. Reason: {res.get('error', 'unknown')}")
                
        log_info("Setting Account Margin Mode to ISOLATED...")
        res = self.api.set_margin_mode_isolated()
        if res.get("success"):
            log_success("Margin mode set to ISOLATED.")
        else:
            log_warning(f"Could not set margin mode automatically. Reason: {res.get('error', 'unknown')}")
        return True

    def scan_market(self, symbol):
        # Skip scanning if a trade is already active for this symbol
        if self.active_trades.get(symbol, {}).get("position_active", False):
            return

        # 1. USDINR rate
        usd_inr = get_usd_inr_rate()
        if not usd_inr:
            usd_inr = self.usd_inr_fallback
        
        # 2. Fetch candles (limit=100 for enough warm-up data for ADX Wilder's calculation)
        candles = self.api.get_candles(symbol, self.resolution, limit=100)
        if len(candles) < 60:
            log_warning(f"[{symbol}] Insufficient candles to compute indicators.")
            return
            
        completed_candles = candles[:-1]
        latest_completed_time = completed_candles[-1]['time']
        
        if latest_completed_time <= self.last_processed_candle_time.get(symbol, 0):
            return
            
        self.last_processed_candle_time[symbol] = latest_completed_time
        
        # Latest completed standard close (used as the fill entry price)
        latest_close = float(completed_candles[-1]['close'])
        
        # 3. Convert candles to Heikin-Ashi in the background for signals
        ha_candles = convert_to_heikin_ashi(completed_candles)
        
        # Donchian channel is calculated over the preceding `channel_period` completed Heikin-Ashi candles
        channel_curr = ha_candles[-(self.channel_period + 1):-1]
        curr_upper = max([c['high'] for c in channel_curr])
        curr_lower = min([c['low'] for c in channel_curr])
        mid_band = (curr_upper + curr_lower) / 2.0
        
        # Preceding channel for crossover check
        channel_prev = ha_candles[-(self.channel_period + 2):-2]
        prev_upper = max([c['high'] for c in channel_prev])
        prev_lower = min([c['low'] for c in channel_prev])
        
        # 4. Volatility Filter (ADX > 20) - Disabled per user request
        adx_val, is_rising = calculate_adx_wilders(ha_candles, 14)
        is_volatile = True
        
        # 5. Volume Filter (Volume > 1.2x of 20-period average volume)
        volumes = [float(c['volume']) for c in completed_candles[-20:]]
        avg_volume = sum(volumes) / len(volumes)
        latest_volume = float(completed_candles[-1]['volume'])
        is_volume_confirmed = latest_volume > (avg_volume * 1.2)
        
        # 6. Macro Trend Filter (4-Hour EMA 50 on standard close)
        candles_4h = self.api.get_candles(symbol, "4h", limit=100)
        macro_bullish = True
        if len(candles_4h) >= 55:
            completed_4h = candles_4h[:-1]
            closes_4h = [float(c['close']) for c in completed_4h]
            ema_50_4h = calculate_ema(closes_4h, 50)
            if ema_50_4h:
                latest_close_4h = closes_4h[-1]
                macro_bullish = latest_close_4h > ema_50_4h
                log_info(f"[{symbol}] 4H EMA 50 Filter: Close={latest_close_4h:.5f} | EMA={ema_50_4h:.5f} | Bullish={macro_bullish}")
        else:
            log_warning(f"[{symbol}] Insufficient 4H candles for trend filter. Defaulting to True.")
            
        curr_ha_close = ha_candles[-1]['close']
        prev_ha_close = ha_candles[-2]['close']
        
        log_info(f"[{symbol}] HA Scan ({self.resolution}): Upper={curr_upper:.5f} | Lower={curr_lower:.5f} | Mid={mid_band:.5f} | HA Close={curr_ha_close:.5f} | ADX={adx_val:.2f} (Rising: {is_rising}) | Vol Confirmed: {is_volume_confirmed}")
        
        # Check triggers (crossover/crossunder on Heikin-Ashi close)
        buy_signal = prev_ha_close <= prev_upper and curr_ha_close > curr_upper and is_volatile and is_volume_confirmed and macro_bullish
        sell_signal = prev_ha_close >= prev_lower and curr_ha_close < curr_lower and is_volatile and is_volume_confirmed and not macro_bullish
        
        target_rr = self.get_symbol_setting(symbol, "target_rr_ratio", self.get_symbol_setting(symbol, "tp2_ratio", self.tp2_ratio))

        # BULLISH Breakout
        if buy_signal:
            entry_price = latest_close
            stop_loss = mid_band
            take_profit = entry_price + (target_rr * (entry_price - stop_loss))
            
            log_setup(f"[{symbol}] HA DONCHIAN BULLISH BREAKOUT TRIGGERED!")
            log_info(f"[{symbol}] Entry: {entry_price:.5f} | SL: {stop_loss:.5f} | TP: {take_profit:.5f}")
            self.execute_trade("BUY", symbol, entry_price, stop_loss, take_profit, usd_inr)
            
        # BEARISH Breakout
        elif sell_signal:
            entry_price = latest_close
            stop_loss = mid_band
            take_profit = entry_price - (target_rr * (stop_loss - entry_price))
            
            log_setup(f"[{symbol}] HA DONCHIAN BEARISH BREAKOUT TRIGGERED!")
            log_info(f"[{symbol}] Entry: {entry_price:.5f} | SL: {stop_loss:.5f} | TP: {take_profit:.5f}")
            self.execute_trade("SELL", symbol, entry_price, stop_loss, take_profit, usd_inr)

    def get_current_risk_inr(self, usd_inr):
        if self.is_mock_mode or self.capital_mode == "static":
            capital = self.static_capital_inr
        else:
            try:
                res = self.api.request("GET", "/v2/wallet/balances")
                capital = 0.0
                if res.get("success") or isinstance(res, list):
                    balances = res.get("result", res) if isinstance(res, dict) else res
                    for bal in balances:
                        symbol = bal.get("asset_symbol", bal.get("symbol", "")).upper()
                        val = float(bal.get("available_balance", bal.get("balance", 0.0)))
                        if symbol == "INR":
                            capital += val
                        elif symbol in ["USD", "USDT", "USDC"]:
                            capital += val * usd_inr
                if capital <= 0:
                    capital = self.static_capital_inr
            except Exception as e:
                capital = self.static_capital_inr
                log_warning(f"Failed to fetch balances: {e}. Using static capital.")
                
        risk_inr = capital * (self.risk_percent / 100.0)
        return risk_inr

    def execute_trade(self, side, symbol, entry, stop_loss, take_profit, usd_inr):
        price_distance = abs(entry - stop_loss)
        
        info = self.product_info.get(symbol, {})
        tick_size = info.get("tick_size", 0.01)
        entry_rounded = round_step(entry, tick_size)
        sl_rounded = round_step(stop_loss, tick_size)
        
        tp1_ratio = self.get_symbol_setting(symbol, "tp1_ratio", self.tp1_ratio)
        tp2_ratio = self.get_symbol_setting(symbol, "target_rr_ratio", self.get_symbol_setting(symbol, "tp2_ratio", self.tp2_ratio))
        tp1_percent = self.get_symbol_setting(symbol, "tp1_percent", self.tp1_percent)

        if side.upper() == "BUY":
            tp1_raw = entry + (tp1_ratio * price_distance)
            tp2_raw = entry + (tp2_ratio * price_distance)
        else:
            tp1_raw = entry - (tp1_ratio * price_distance)
            tp2_raw = entry - (tp2_ratio * price_distance)
            
        tp1_rounded = round_step(tp1_raw, tick_size)
        tp2_rounded = round_step(tp2_raw, tick_size)
        
        current_risk_inr = self.get_current_risk_inr(usd_inr)
        risk_usd = current_risk_inr / usd_inr
        contract_val = info.get("contract_value", 1.0)
        
        raw_size = risk_usd / (price_distance * contract_val)
        step_size = info.get("step_size", 1.0)
        final_size = floor_step(raw_size, step_size)
        
        actual_risk_inr = (final_size * contract_val * price_distance) * usd_inr
        
        log_info(f"[{symbol}] Sizing: Target Risk={current_risk_inr:.2f} INR | Calculated Size={final_size} contracts")
        log_info(f"[{symbol}] Actual Risk on execution: {actual_risk_inr:.2f} INR")
        
        if final_size <= 0:
            log_warning(f"[{symbol}] Calculated position size is 0. Trade skipped.")
            return
            
        if actual_risk_inr > (current_risk_inr + 0.05):
            log_error(f"PRO-GOVERNANCE MANDATE: Cash risk ({actual_risk_inr:.2f} INR) exceeds risk ceiling of {current_risk_inr:.2f} INR. Trade invalid.")
            return
            
        if self.is_mock_mode:
            log_success(f"[MOCK] Placed Trend Bracket Order:")
            log_success(f"[MOCK] Symbol: {symbol} | Side: {side} | Size: {final_size} contracts")
            log_success(f"[MOCK] Limit Entry: {entry_rounded:.5f} | Stop Loss: {sl_rounded:.5f} | TP1 ({tp1_percent}%): {tp1_rounded:.5f} | TP2 ({100 - tp1_percent}%): {tp2_rounded:.5f}")
            self.send_discord_message(
                f"🔔 **[MOCK] {symbol} Trend Breakout Trade Entered**\n"
                f"Side: `{side.upper()}`\n"
                f"Contracts: `{final_size}`\n"
                f"Entry Price: `${entry_rounded:.5f}`\n"
                f"Stop Loss: `${sl_rounded:.5f}`\n"
                f"TP1 ({tp1_percent}%): `${tp1_rounded:.5f}`\n"
                f"TP2 ({100 - tp1_percent}%): `${tp2_rounded:.5f}`"
            )
            state = self.active_trades[symbol]
            state["position_active"] = True
            state["entry_price"] = entry_rounded
            state["tp1_price"] = tp1_rounded
            state["tp2_price"] = tp2_rounded
            state["tp_price"] = tp2_rounded
            state["sl_price"] = sl_rounded
            state["entry_side"] = side.upper()
            state["entry_size"] = final_size
            state["tp1_order_placed"] = True if tp1_percent > 0 else False
            state["tp1_order_id"] = "MOCK_TP1" if tp1_percent > 0 else None
            state["is_breakeven_active"] = False
            self.save_trade_state()
        else:
            log_info(f"[{symbol}] Submitting bracket order to Delta Exchange API...")
            product_id = info["id"]
            res = self.api.place_bracket_order(
                product_id=product_id,
                size=final_size,
                side=side,
                limit_price=entry_rounded,
                stop_loss=sl_rounded,
                take_profit=tp2_rounded
            )
            if res.get("success"):
                log_success(f"Order successfully filled on Exchange: {res}")
                self.send_discord_message(
                    f"🔔 **{symbol} Trend Breakout Trade Entered**\n"
                    f"Side: `{side.upper()}`\n"
                    f"Contracts: `{final_size}`\n"
                    f"Entry Price: `${entry_rounded:.5f}`\n"
                    f"Stop Loss: `${sl_rounded:.5f}`\n"
                    f"TP1 ({tp1_percent}%): `${tp1_rounded:.5f}`\n"
                    f"TP2 ({100 - tp1_percent}%): `${tp2_rounded:.5f}`\n"
                    f"Risk: `₹{actual_risk_inr:.2f}`"
                )
                state = self.active_trades[symbol]
                state["position_active"] = True
                state["entry_price"] = entry_rounded
                state["tp1_price"] = tp1_rounded
                state["tp2_price"] = tp2_rounded
                state["tp_price"] = tp2_rounded
                state["sl_price"] = sl_rounded
                state["entry_side"] = side.upper()
                state["entry_size"] = final_size
                state["tp1_order_placed"] = False
                state["tp1_order_id"] = None
                state["is_breakeven_active"] = False
                self.save_trade_state()
                # Log success to dashboard
                try:
                    import json, os
                    status = {"trend_bot": "running", "latest_error": f"SUCCESS: Trend order filled for {final_size} {symbol}"}
                    if os.path.exists("bot_status.json"):
                        with open("bot_status.json", "r") as f:
                            status = {**json.load(f), **status}
                    with open("bot_status.json", "w") as f:
                        json.dump(status, f)
                except: pass
            else:
                err_msg = res.get("error", str(res))
                if "bracket_order_position_exists" in err_msg or "bracket_order_position_exists" in str(res):
                    log_info(f"Bracket order or position already exists for {symbol}. Skipping trade.")
                else:
                    log_error(f"Exchange rejected order: {res}")
                    self.send_discord_message(f"⚠️ **{symbol} Trend Bot order failed:** Exchange rejected the order: `{err_msg}`")
                    # Log error to dashboard
                    try:
                        import json, os
                        status = {"latest_error": f"REJECTED: Trend Bot order failed - {err_msg}"}
                        if os.path.exists("bot_status.json"):
                            with open("bot_status.json", "r") as f:
                                status = {**json.load(f), **status}
                        with open("bot_status.json", "w") as f:
                            json.dump(status, f)
                    except: pass

    def load_trade_state(self):
        self.active_trades = {}
        for symbol in self.symbols:
            self.active_trades[symbol] = {
                "position_active": False,
                "entry_price": None,
                "tp1_price": None,
                "tp2_price": None,
                "tp_price": None,
                "sl_price": None,
                "entry_side": None,
                "entry_size": None,
                "tp1_order_placed": False,
                "tp1_order_id": None,
                "is_breakeven_active": False
            }
        
        import os
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    if "position_active" in data:
                        # Legacy single-symbol trade state format
                        legacy_symbol = self.symbols[0]
                        self.active_trades[legacy_symbol] = {
                            "position_active": data.get("position_active", False),
                            "entry_price": data.get("entry_price"),
                            "tp1_price": data.get("tp1_price"),
                            "tp2_price": data.get("tp2_price"),
                            "tp_price": data.get("tp2_price"),
                            "sl_price": data.get("sl_price"),
                            "entry_side": data.get("entry_side"),
                            "entry_size": data.get("entry_size"),
                            "tp1_order_placed": data.get("tp1_order_placed", False),
                            "tp1_order_id": data.get("tp1_order_id"),
                            "is_breakeven_active": data.get("is_breakeven_active", False)
                        }
                        log_info(f"Loaded legacy trade state for {legacy_symbol}: {data}")
                    else:
                        for symbol in self.symbols:
                            if symbol in data:
                                self.active_trades[symbol] = {**self.active_trades[symbol], **data[symbol]}
                        log_info(f"Loaded active trade states: {self.active_trades}")
            except Exception as e:
                log_error(f"Error loading trade state: {e}")

    def save_trade_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.active_trades, f)
        except Exception as e:
            log_error(f"Error saving trade state: {e}")

    def check_position_exits(self, symbol):
        state = self.active_trades[symbol]
        use_breakeven = self.get_symbol_setting(symbol, "use_breakeven", self.use_breakeven)
        breakeven_trigger = self.get_symbol_setting(symbol, "breakeven_trigger", self.breakeven_trigger)
        tp1_ratio = self.get_symbol_setting(symbol, "tp1_ratio", self.tp1_ratio)
        tp2_ratio = self.get_symbol_setting(symbol, "target_rr_ratio", self.get_symbol_setting(symbol, "tp2_ratio", self.tp2_ratio))
        tp1_percent = self.get_symbol_setting(symbol, "tp1_percent", self.tp1_percent)
        
        # 1. MOCK MODE EXIT CHECKING
        if self.is_mock_mode:
            if not state["position_active"]:
                return
            ticker = self.api.get_ticker(symbol)
            if not ticker:
                return
            current_price = float(ticker.get("close", ticker.get("mark_price", 0)))
            if current_price <= 0:
                return
                
            price_distance = abs(state["tp2_price"] - state["entry_price"]) / tp2_ratio
            
            # A. Check breakeven trigger
            if use_breakeven and not state["is_breakeven_active"]:
                be_triggered = False
                if state["entry_side"] == "BUY" and current_price >= state["entry_price"] + (breakeven_trigger * price_distance):
                    be_triggered = True
                elif state["entry_side"] == "SELL" and current_price <= state["entry_price"] - (breakeven_trigger * price_distance):
                    be_triggered = True
                    
                if be_triggered:
                    state["is_breakeven_active"] = True
                    state["sl_price"] = state["entry_price"]
                    log_success(f"[MOCK] Stop Loss for {symbol} moved to Breakeven at ${state['entry_price']:.5f}")
                    self.send_discord_message(f"🔔 **[MOCK] {symbol} Stop Loss Moved to Breakeven** at `${state['entry_price']:.5f}`")
                    self.save_trade_state()
                    
            # B. Check TP1 fill
            if tp1_percent > 0 and state["tp1_order_placed"]:
                tp1_hit = False
                if state["entry_side"] == "BUY" and current_price >= state["tp1_price"]:
                    tp1_hit = True
                elif state["entry_side"] == "SELL" and current_price <= state["tp1_price"]:
                    tp1_hit = True
                    
                if tp1_hit:
                    tp1_size = state["entry_size"] * (tp1_percent / 100.0)
                    pnl_usd = (state["tp1_price"] - state["entry_price"]) * tp1_size if state["entry_side"] == "BUY" else (state["entry_price"] - state["tp1_price"]) * tp1_size
                    usd_inr = get_usd_inr_rate()
                    pnl_inr = pnl_usd * usd_inr
                    
                    state["entry_size"] = state["entry_size"] - tp1_size
                    state["tp1_order_placed"] = False
                    state["tp1_order_id"] = None
                    log_success(f"[MOCK] {symbol} TP1 Hit at ${state['tp1_price']:.5f}! Closed {tp1_percent}% of position.")
                    self.send_discord_message(
                        f"🔔 **[MOCK] {symbol} Trend TP1 Hit**\n"
                        f"Exit Price: `${state['tp1_price']:.5f}`\n"
                        f"Contracts Closed: `{tp1_size}`\n"
                        f"Realized TP1 PnL: `${pnl_usd:.2f} USD` (approx `₹{pnl_inr:.2f} INR`)\n"
                        f"Remaining Contracts: `{state['entry_size']}`"
                    )
                    self.save_trade_state()
                    
            # C. Check TP2 or SL exit
            pnl_usd = 0.0
            triggered = False
            exit_reason = ""
            exit_price = 0.0
            
            if state["entry_side"] == "BUY":
                if current_price >= state["tp2_price"]:
                    triggered = True
                    exit_price = state["tp2_price"]
                    exit_reason = "Take Profit 2 (TP2) Hit"
                    pnl_usd = (exit_price - state["entry_price"]) * state["entry_size"]
                elif current_price <= state["sl_price"]:
                    triggered = True
                    exit_price = state["sl_price"]
                    exit_reason = "Stop Loss (SL) Hit"
                    pnl_usd = (exit_price - state["entry_price"]) * state["entry_size"]
            elif state["entry_side"] == "SELL":
                if current_price <= state["tp2_price"]:
                    triggered = True
                    exit_price = state["tp2_price"]
                    exit_reason = "Take Profit 2 (TP2) Hit"
                    pnl_usd = (state["entry_price"] - exit_price) * state["entry_size"]
                elif current_price >= state["sl_price"]:
                    triggered = True
                    exit_price = state["sl_price"]
                    exit_reason = "Stop Loss (SL) Hit"
                    pnl_usd = (state["entry_price"] - exit_price) * state["entry_size"]
                    
            if triggered:
                usd_inr = get_usd_inr_rate()
                pnl_inr = pnl_usd * usd_inr
                sign_usd = "-" if pnl_usd < 0 else "+" if pnl_usd > 0 else ""
                sign_inr = "-" if pnl_inr < 0 else "+" if pnl_inr > 0 else ""
                self.send_discord_message(
                    f"🔔 **[MOCK] {symbol} Trend Trade Closed**\n"
                    f"Exit Reason: `{exit_reason}`\n"
                    f"Side: `{state['entry_side']}`\n"
                    f"Contracts: `{state['entry_size']}`\n"
                    f"Entry Price: `${state['entry_price']:.5f}`\n"
                    f"Exit Price: `${exit_price:.5f}`\n"
                    f"Realized PnL: `{sign_usd}${abs(pnl_usd):.2f} USD` (approx `{sign_inr}₹{abs(pnl_inr):.2f} INR`)"
                )
                state["position_active"] = False
                state["entry_price"] = None
                state["tp1_price"] = None
                state["tp2_price"] = None
                state["tp_price"] = None
                state["sl_price"] = None
                state["entry_side"] = None
                state["entry_size"] = None
                state["tp1_order_placed"] = False
                state["tp1_order_id"] = None
                state["is_breakeven_active"] = False
                self.save_trade_state()
            return

        # 2. REAL MODE EXIT CHECKING
        underlying_asset = symbol.replace("USD", "")
        res = self.api.request("GET", f"/v2/positions?underlying_asset_symbol={underlying_asset}")
        
        success = False
        positions = []
        if isinstance(res, dict):
            success = res.get("success", False)
            positions = res.get("result", [])
        elif isinstance(res, list):
            success = True
            positions = res
            
        if not success:
            log_warning(f"[{symbol}] Failed to fetch positions from exchange. Skipping exit check for this cycle.")
            return

        pos_match = None
        for p in positions:
            if p.get("product_symbol") == symbol:
                size = float(p.get("size", 0))
                if size != 0:
                    pos_match = p
                    break

        # Case A: Bot thought a trade was active, but position size is now 0 (or no position found)
        if state["position_active"] and pos_match is None:
            log_info(f"Position for {symbol} is closed. Fetching fills for exit summary...")
            
            # Cancel the remaining TP1 limit order if it is still open
            if tp1_percent > 0 and state["tp1_order_placed"] and state["tp1_order_id"]:
                try:
                    product_id = self.product_info.get(symbol, {}).get("id")
                    if product_id:
                        log_info(f"Cancelling remaining TP1 order {state['tp1_order_id']} for {symbol}...")
                        self.api.cancel_order(state["tp1_order_id"], product_id)
                except Exception as ex:
                    log_error(f"Failed to cancel TP1 order for {symbol}: {ex}")
            
            exit_price = None
            realized_pnl = 0.0
            
            # Fetch latest fills to extract realized PnL and exit price (request 10 to ensure we see our symbol)
            res_fills = self.api.request("GET", f"/v2/fills?product_symbol={symbol}&limit=10")
            if (isinstance(res_fills, dict) and res_fills.get("success")) or isinstance(res_fills, list):
                fills = res_fills.get("result", res_fills) if isinstance(res_fills, dict) else res_fills
                fills.sort(key=lambda x: x.get("id", 0), reverse=True)
                for f in fills:
                    # Filter fills for this symbol specifically
                    if f.get("product_symbol") != symbol:
                        continue
                    meta = f.get("meta_data", {})
                    new_pos = meta.get("new_position", {})
                    realized_pnl_val = float(new_pos.get("realized_pnl", 0))
                    if realized_pnl_val != 0 or f.get("side", "").upper() != state["entry_side"]:
                        exit_price = float(f.get("price", 0))
                        realized_pnl = realized_pnl_val
                        break
            
            if exit_price is None:
                ticker = self.api.get_ticker(symbol)
                exit_price = float(ticker.get("close", ticker.get("mark_price", 0))) if ticker else 0.0
                
            exit_reason = "Position Closed"
            if state["tp2_price"] is not None and abs(exit_price - state["tp2_price"]) / state["tp2_price"] < 0.005:
                exit_reason = "Take Profit 2 (TP2) Hit"
            elif state["sl_price"] is not None and abs(exit_price - state["sl_price"]) / state["sl_price"] < 0.005:
                exit_reason = "Stop Loss (SL) Hit"
                
            usd_inr = get_usd_inr_rate()
            pnl_inr = realized_pnl * usd_inr
            
            sign_usd = "-" if realized_pnl < 0 else "+" if realized_pnl > 0 else ""
            sign_inr = "-" if pnl_inr < 0 else "+" if pnl_inr > 0 else ""
            self.send_discord_message(
                f"🔔 **{symbol} Trend Trade Closed**\n"
                f"Exit Reason: `{exit_reason}`\n"
                f"Side: `{state['entry_side']}`\n"
                f"Contracts: `{state['entry_size']}`\n"
                f"Entry Price: `${state['entry_price']:.5f}`\n"
                f"Exit Price: `${exit_price:.5f}`\n"
                f"Realized PnL: `{sign_usd}${abs(realized_pnl):.2f} USD` (approx `{sign_inr}₹{abs(pnl_inr):.2f} INR`)"
            )
            
            state["position_active"] = False
            state["entry_price"] = None
            state["tp1_price"] = None
            state["tp2_price"] = None
            state["tp_price"] = None
            state["sl_price"] = None
            state["entry_side"] = None
            state["entry_size"] = None
            state["tp1_order_placed"] = False
            state["tp1_order_id"] = None
            state["is_breakeven_active"] = False
            self.save_trade_state()
            
        # Case B: Bot thought no trade was active, but an open position is found (e.g. startup recovery or manual trade)
        elif not state["position_active"] and pos_match is not None:
            state["position_active"] = True
            state["entry_price"] = float(pos_match.get("entry_price", 0))
            size = float(pos_match.get("size", 0))
            state["entry_side"] = "BUY" if size > 0 else "SELL"
            state["entry_size"] = abs(size)
            
            state["sl_price"] = None
            state["tp1_price"] = None
            state["tp2_price"] = None
            state["tp_price"] = None
            state["tp1_order_placed"] = False
            state["tp1_order_id"] = None
            state["is_breakeven_active"] = False
            
            try:
                product_id = self.product_info.get(symbol, {}).get("id")
                if product_id:
                    open_orders = self.api.get_open_orders(product_id)
                    for o in open_orders:
                        if o.get("stop_order_type") == "stop_loss_order" and o.get("stop_price"):
                            state["sl_price"] = float(o.get("stop_price"))
                        elif o.get("stop_order_type") == "take_profit_order" and o.get("stop_price"):
                            state["tp2_price"] = float(o.get("stop_price"))
                            state["tp_price"] = state["tp2_price"]
                            
                # Fallback to Donchian Channel calculation if SL/TP2 orders not found
                if state["sl_price"] is None or state["tp2_price"] is None:
                    candles = self.api.get_candles(symbol, self.resolution, limit=100)
                    if len(candles) >= self.channel_period + 2:
                        completed_candles = candles[:-1]
                        ha_candles = convert_to_heikin_ashi(completed_candles)
                        channel_curr = ha_candles[-(self.channel_period + 1):-1]
                        curr_upper = max([c['high'] for c in channel_curr])
                        curr_lower = min([c['low'] for c in channel_curr])
                        mid_band = (curr_upper + curr_lower) / 2.0
                        
                        if state["sl_price"] is None:
                            state["sl_price"] = mid_band
                        if state["tp2_price"] is None:
                            price_distance = abs(state["entry_price"] - state["sl_price"])
                            if state["entry_side"] == "BUY":
                                state["tp2_price"] = state["entry_price"] + (tp2_ratio * price_distance)
                            else:
                                state["tp2_price"] = state["entry_price"] - (tp2_ratio * price_distance)
                            state["tp_price"] = state["tp2_price"]

                # Calculate TP1 price from recovered or calculated SL/TP2
                if state["sl_price"] is not None and state["tp2_price"] is not None:
                    if abs(state["sl_price"] - state["entry_price"]) < 0.0001:
                        state["is_breakeven_active"] = True
                        price_distance = abs(state["tp2_price"] - state["entry_price"]) / tp2_ratio
                    else:
                        price_distance = abs(state["entry_price"] - state["sl_price"])
                        
                    if state["entry_side"] == "BUY":
                        state["tp1_price"] = state["entry_price"] + (tp1_ratio * price_distance)
                    else:
                        state["tp1_price"] = state["entry_price"] - (tp1_ratio * price_distance)
                        
                # Check if TP1 order is already open on exchange
                if tp1_percent > 0 and product_id:
                    open_orders = self.api.get_open_orders(product_id)
                    for o in open_orders:
                        if o.get("order_type") == "limit_order" and state["tp1_price"] is not None and abs(float(o.get("limit_price", 0)) - state["tp1_price"]) / state["tp1_price"] < 0.002:
                            state["tp1_order_placed"] = True
                            state["tp1_order_id"] = o.get("id")
                            log_info(f"[{symbol}] Recovered TP1 open order during startup: ID={state['tp1_order_id']}")
                            break
            except Exception as ex:
                log_error(f"Failed to reconstruct levels for {symbol} during startup recovery: {ex}")
                
            log_info(f"[{symbol}] Synchronized active position from exchange: Side={state['entry_side']}, Size={state['entry_size']}, Entry=${state['entry_price']:.5f}")
            self.save_trade_state()

        # Case C: Bot thought trade was active, and position is still active (normal monitoring)
        elif state["position_active"] and pos_match is not None:
            product_id = self.product_info.get(symbol, {}).get("id")
            if not product_id:
                return
                
            # 1. Place TP1 limit order if it hasn't been placed yet
            if tp1_percent > 0 and not state["tp1_order_placed"] and state["tp1_price"] is not None:
                step_size = self.product_info.get(symbol, {}).get("step_size", 1.0)
                tp1_size = floor_step(state["entry_size"] * (tp1_percent / 100.0), step_size)
                if tp1_size > 0:
                    log_info(f"[{symbol}] Placing TP1 reduce_only limit order: Size={tp1_size} contracts at ${state['tp1_price']:.5f}")
                    side = "sell" if state["entry_side"] == "BUY" else "buy"
                    res_tp1 = self.api.place_reduce_only_limit_order(
                        product_id=product_id,
                        size=tp1_size,
                        side=side,
                        limit_price=state["tp1_price"]
                    )
                    if res_tp1.get("success"):
                        state["tp1_order_placed"] = True
                        state["tp1_order_id"] = res_tp1.get("result", {}).get("id")
                        log_success(f"[{symbol}] TP1 order placed successfully: ID={state['tp1_order_id']}")
                        self.save_trade_state()
                    else:
                        log_error(f"[{symbol}] Failed to place TP1 limit order: {res_tp1}")
            
            # 2. Check if TP1 order has been filled
            if tp1_percent > 0 and state["tp1_order_placed"] and state["tp1_price"] is not None:
                current_size = abs(float(pos_match.get("size", 0)))
                step_size = self.product_info.get(symbol, {}).get("step_size", 1.0)
                tp1_size = floor_step(state["entry_size"] * (tp1_percent / 100.0), step_size)
                expected_remaining_size = state["entry_size"] - tp1_size
                
                if current_size <= expected_remaining_size + 0.0001:
                    log_success(f"[{symbol}] TP1 order was filled! Remaining position size: {current_size} contracts")
                    self.send_discord_message(
                        f"🔔 **{symbol} Trend TP1 Hit** at `${state['tp1_price']:.5f}`!\n"
                        f"Closed {tp1_percent}% of position. Remaining: `{current_size}` contracts."
                    )
                    state["tp1_order_placed"] = False
                    state["tp1_order_id"] = None
                    state["entry_size"] = current_size
                    self.save_trade_state()
                    
            # 3. Check breakeven trigger
            if use_breakeven and not state["is_breakeven_active"] and state["tp2_price"] is not None:
                current_price = float(pos_match.get("mark_price", 0))
                if current_price <= 0:
                    ticker = self.api.get_ticker(symbol)
                    current_price = float(ticker.get("close", ticker.get("mark_price", 0))) if ticker else 0.0
                    
                price_distance = abs(state["tp2_price"] - state["entry_price"]) / tp2_ratio
                be_triggered = False
                if state["entry_side"] == "BUY" and current_price >= state["entry_price"] + (breakeven_trigger * price_distance):
                    be_triggered = True
                elif state["entry_side"] == "SELL" and current_price <= state["entry_price"] - (breakeven_trigger * price_distance):
                    be_triggered = True
                    
                if be_triggered:
                    log_info(f"[{symbol}] Price reached breakeven trigger (+{breakeven_trigger}R). Modifying Stop Loss to entry price (${state['entry_price']:.5f})...")
                    res_be = self.api.edit_bracket_order(
                        product_id=product_id,
                        stop_loss_price=state["entry_price"],
                        take_profit_price=state["tp2_price"]
                    )
                    if res_be.get("success"):
                        state["is_breakeven_active"] = True
                        state["sl_price"] = state["entry_price"]
                        log_success(f"[{symbol}] Stop Loss moved to Breakeven successfully on exchange.")
                        self.send_discord_message(
                            f"🔔 **{symbol} Stop Loss Moved to Breakeven** at `${state['entry_price']:.5f}`\n"
                            f"Price crossed +{breakeven_trigger}R trigger."
                        )
                        self.save_trade_state()
                    else:
                        log_error(f"[{symbol}] Failed to modify Stop Loss to breakeven: {res_be}")

    def start(self):
        log_info("Starting Donchian Trend Following Bot...")
        if not self.setup_account():
            log_error("Setup failed. Terminating.")
            return
            
        log_success("Trend Bot is active and running.")
        symbols_str = ", ".join(self.symbols)
        self.send_discord_message(f"🚀 **Trend Bot ({symbols_str}) is active and scanning on Render!** (Resolution: `{self.resolution}`)")
        
        while True:
            for symbol in self.symbols:
                try:
                    self.check_position_exits(symbol)
                    self.scan_market(symbol)
                except Exception as e:
                    log_error(f"Error in scan loop for {symbol}: {e}")
                    self.send_discord_message(f"❌ **{symbol} Trend Bot Loop Error:** `{str(e)}`")
            time.sleep(self.poll_interval)


def main():
    bot = DeltaTrendBot()
    bot.start()

if __name__ == "__main__":
    main()

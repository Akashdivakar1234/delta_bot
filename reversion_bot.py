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

def calculate_bollinger_bands(prices, period=20, num_std=2.0):
    if len(prices) < period:
        return None, None, None
    sma = sum(prices[-period:]) / period
    variance = sum((x - sma) ** 2 for x in prices[-period:]) / period
    std_dev = math.sqrt(variance)
    upper = sma + (num_std * std_dev)
    lower = sma - (num_std * std_dev)
    return upper, lower, sma

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
            
    # Initial average
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    # Wilder's smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema


# --- CORE REVERSION BOT CLASS ---
class DeltaReversionBot:
    def __init__(self, config_path="reversion_config.json"):
        with open(config_path, "r") as f:
            self.config = json.load(f)
            
        self.keys = self.config.get("keys", [{"api_key": self.config.get("api_key"), "api_secret": self.config.get("api_secret")}])
        # Use first key for backward-compatibility checks
        self.api_key = self.keys[0]["api_key"]
        self.api_secret = self.keys[0]["api_secret"]
        
        self.is_testnet = self.config["is_testnet"]
        self.use_india_delta = self.config["use_india_delta"]
        self.symbol = self.config["symbol"]
        self.risk_percent = self.config.get("risk_percent", 2.0)
        self.capital_mode = self.config.get("capital_mode", "auto")
        self.static_capital_inr = self.config.get("static_capital_inr", 350.0)
        self.leverage = self.config["leverage"]
        self.usd_inr_fallback = self.config["usd_inr_rate_fallback"]
        self.poll_interval = self.config["poll_interval_seconds"]
        self.bb_period = self.config.get("bb_period", 20)
        self.bb_std = self.config.get("bb_std", 2.0)
        self.rsi_period = self.config.get("rsi_period", 14)
        self.rsi_lower = self.config.get("rsi_lower", 30)
        self.rsi_upper = self.config.get("rsi_upper", 70)
        self.enable_trend_filter = self.config.get("enable_trend_filter", False)
        self.trend_filter_period = self.config.get("trend_filter_period", 50)
        self.htf_resolution = self.config.get("htf_resolution", "1h")
        self.stop_loss_pct = self.config.get("stop_loss_pct", 1.5)
        self.resolution = self.config.get("resolution", "15m")
        
        self.is_mock_mode = (self.api_key == "YOUR_DELTA_API_KEY" or self.api_key is None or self.api_secret == "YOUR_DELTA_API_SECRET")
        
        self.api = DeltaExchangeAPI(
            self.keys, 
            is_testnet=self.is_testnet, 
            use_india_delta=self.use_india_delta
        )
        
        self.product_info = {}
        self.last_processed_candle_time = 0
        self.state_file = "active_trade_reversion.json"
        self.load_trade_state()
        
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
            return True
            
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
        
        log_info("Setting Account Margin Mode to ISOLATED...")
        res = self.api.set_margin_mode_isolated()
        if res.get("success"):
            log_success("Margin mode set to ISOLATED.")
        else:
            log_warning(f"Could not set margin mode automatically. Reason: {res.get('error', 'unknown')}")
        
        log_info(f"Setting leverage to {self.leverage}x...")
        res = self.api.set_leverage(self.product_info["id"], self.leverage)
        if res.get("success"):
            log_success(f"Leverage set to {self.leverage}x.")
        else:
            log_warning(f"Could not set leverage automatically. Reason: {res.get('error', 'unknown')}")
        return True

    def scan_market(self):
        # 1. USDINR rate
        usd_inr = get_usd_inr_rate()
        if not usd_inr:
            usd_inr = self.usd_inr_fallback
        
        # 2. Fetch candles (need enough to calculate Bollinger Bands + RSI)
        limit_needed = max(self.bb_period, self.rsi_period + 20) + 5
        candles = self.api.get_candles(self.symbol, self.resolution, limit=limit_needed)
        if len(candles) < limit_needed - 5:
            log_warning("Insufficient candles to compute indicators.")
            return
            
        completed_candles = candles[:-1]
        latest_completed_time = completed_candles[-1]['time']
        
        if latest_completed_time <= self.last_processed_candle_time:
            return
            
        self.last_processed_candle_time = latest_completed_time
        
        # Extract closes
        closes = [float(c['close']) for c in completed_candles]
        latest_close = closes[-1]
        
        # Calculate Indicators
        upper_bb, lower_bb, sma_20 = calculate_bollinger_bands(closes, self.bb_period, self.bb_std)
        rsi = calculate_rsi(closes, self.rsi_period)
        
        if upper_bb is None or rsi is None:
            log_warning("Failed to compute indicator values.")
            return
            
        log_info(f"BB/RSI Scan ({self.resolution}): Upper={upper_bb:.5f} | Lower={lower_bb:.5f} | Close={latest_close:.5f} | RSI={rsi:.2f}")
        
        # Check triggers
        # Calculate HTF trend filter if enabled
        htf_trend = None
        if self.enable_trend_filter:
            htf_limit = self.trend_filter_period + 5
            htf_candles = self.api.get_candles(self.symbol, self.htf_resolution, limit=htf_limit)
            if len(htf_candles) >= self.trend_filter_period:
                htf_completed = htf_candles[:-1]
                htf_closes = [float(c['close']) for c in htf_completed]
                htf_ema = calculate_ema(htf_closes, self.trend_filter_period)
                if htf_ema:
                    htf_close = htf_closes[-1]
                    htf_trend = "BULLISH" if htf_close > htf_ema else "BEARISH"
                    log_info(f"HTF Trend Filter ({self.htf_resolution}): Close={htf_close:.5f} | EMA {self.trend_filter_period}={htf_ema:.5f} | Trend={htf_trend}")

        # BULLISH Reversion (BUY)
        if latest_close < lower_bb and rsi < self.rsi_lower:
            if self.enable_trend_filter and htf_trend == "BEARISH":
                log_info(f"Bullish mean reversion setup ignored: HTF Trend on {self.htf_resolution} is BEARISH.")
                return
                
            entry_price = latest_close
            stop_loss = entry_price * (1 - (self.stop_loss_pct / 100.0))
            take_profit = sma_20
            
            log_setup("BULLISH MEAN REVERSION DETECTED!")
            log_info(f"Entry: {entry_price:.5f} | SL: {stop_loss:.5f} | TP: {take_profit:.5f}")
            self.execute_trade("BUY", entry_price, stop_loss, take_profit, usd_inr)
            
        # BEARISH Reversion (SELL)
        elif latest_close > upper_bb and rsi > self.rsi_upper:
            if self.enable_trend_filter and htf_trend == "BULLISH":
                log_info(f"Bearish mean reversion setup ignored: HTF Trend on {self.htf_resolution} is BULLISH.")
                return
                
            entry_price = latest_close
            stop_loss = entry_price * (1 + (self.stop_loss_pct / 100.0))
            take_profit = sma_20
            
            log_setup("BEARISH MEAN REVERSION DETECTED!")
            log_info(f"Entry: {entry_price:.5f} | SL: {stop_loss:.5f} | TP: {take_profit:.5f}")
            self.execute_trade("SELL", entry_price, stop_loss, take_profit, usd_inr)

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

    def execute_trade(self, side, entry, stop_loss, take_profit, usd_inr):
        price_distance = abs(entry - stop_loss)
        
        tick_size = self.product_info.get("tick_size", 0.01)
        entry_rounded = round_step(entry, tick_size)
        sl_rounded = round_step(stop_loss, tick_size)
        tp_rounded = round_step(take_profit, tick_size)
        
        current_risk_inr = self.get_current_risk_inr(usd_inr)
        risk_usd = current_risk_inr / usd_inr
        contract_val = self.product_info.get("contract_value", 1.0)
        
        raw_size = risk_usd / (price_distance * contract_val)
        step_size = self.product_info.get("step_size", 1.0)
        final_size = floor_step(raw_size, step_size)
        
        actual_risk_inr = (final_size * contract_val * price_distance) * usd_inr
        
        log_info(f"Sizing: Target Risk={current_risk_inr:.2f} INR | Calculated Size={final_size} contracts")
        log_info(f"Actual Risk on execution: {actual_risk_inr:.2f} INR")
        
        if final_size <= 0:
            log_warning("Calculated position size is 0. Trade skipped.")
            return
            
        if actual_risk_inr > (current_risk_inr + 0.05):
            log_error(f"PRO-GOVERNANCE MANDATE: Cash risk ({actual_risk_inr:.2f} INR) exceeds risk ceiling of {current_risk_inr:.2f} INR. Trade invalid.")
            return
            
        if self.is_mock_mode:
            log_success(f"[MOCK] Placed Reversion Bracket Order:")
            log_success(f"[MOCK] Symbol: {self.symbol} | Side: {side} | Size: {final_size} contracts")
            log_success(f"[MOCK] Limit Entry: {entry_rounded:.5f} | Stop Loss: {sl_rounded:.5f} | Take Profit: {tp_rounded:.5f}")
            self.send_discord_message(
                f"🔔 **[MOCK] ADA Reversion Trade Entered**\n"
                f"Side: `{side.upper()}`\n"
                f"Contracts: `{final_size}`\n"
                f"Entry Price: `${entry_rounded:.5f}`\n"
                f"Stop Loss: `${sl_rounded:.5f}`\n"
                f"Take Profit: `${tp_rounded:.5f}`"
            )
            self.position_active = True
            self.entry_price = entry_rounded
            self.tp_price = tp_rounded
            self.sl_price = sl_rounded
            self.entry_side = side.upper()
            self.entry_size = final_size
            self.save_trade_state()
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
                self.send_discord_message(
                    f"🔔 **ADA Reversion Trade Entered**\n"
                    f"Side: `{side.upper()}`\n"
                    f"Contracts: `{final_size}`\n"
                    f"Entry Price: `${entry_rounded:.5f}`\n"
                    f"Stop Loss: `${sl_rounded:.5f}`\n"
                    f"Take Profit: `${tp_rounded:.5f}`\n"
                    f"Risk: `₹{actual_risk_inr:.2f}`"
                )
                self.position_active = True
                self.entry_price = entry_rounded
                self.tp_price = tp_rounded
                self.sl_price = sl_rounded
                self.entry_side = side.upper()
                self.entry_size = final_size
                self.save_trade_state()
                # Log success to dashboard
                try:
                    import json, os
                    status = {"reversion_bot": "running", "latest_error": f"SUCCESS: Reversion order filled for {final_size} ADAUSD"}
                    if os.path.exists("bot_status.json"):
                        with open("bot_status.json", "r") as f:
                            status = {**json.load(f), **status}
                    with open("bot_status.json", "w") as f:
                        json.dump(status, f)
                except: pass
            else:
                err_msg = res.get("error", str(res))
                if "bracket_order_position_exists" in err_msg or "bracket_order_position_exists" in str(res):
                    log_info(f"Bracket order or position already exists for {self.symbol}. Skipping trade.")
                else:
                    log_error(f"Exchange rejected order: {res}")
                    self.send_discord_message(f"⚠️ **ADA Reversion Bot order failed:** Exchange rejected the order: `{err_msg}`")
                    # Log error to dashboard
                    try:
                        import json, os
                        status = {"latest_error": f"REJECTED: Reversion Bot order failed - {err_msg}"}
                        if os.path.exists("bot_status.json"):
                            with open("bot_status.json", "r") as f:
                                status = {**json.load(f), **status}
                        with open("bot_status.json", "w") as f:
                            json.dump(status, f)
                    except: pass

    def load_trade_state(self):
        self.position_active = False
        self.entry_price = None
        self.tp_price = None
        self.sl_price = None
        self.entry_side = None
        self.entry_size = None
        
        import os
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    self.position_active = data.get("position_active", False)
                    self.entry_price = data.get("entry_price")
                    self.tp_price = data.get("tp_price")
                    self.sl_price = data.get("sl_price")
                    self.entry_side = data.get("entry_side")
                    self.entry_size = data.get("entry_size")
                    log_info(f"Loaded active trade state: {data}")
            except Exception as e:
                log_error(f"Error loading trade state: {e}")

    def save_trade_state(self):
        try:
            data = {
                "position_active": self.position_active,
                "entry_price": self.entry_price,
                "tp_price": self.tp_price,
                "sl_price": self.sl_price,
                "entry_side": self.entry_side,
                "entry_size": self.entry_size
            }
            with open(self.state_file, "w") as f:
                json.dump(data, f)
        except Exception as e:
            log_error(f"Error saving trade state: {e}")

    def check_position_exits(self):
        # 1. MOCK MODE EXIT CHECKING
        if self.is_mock_mode:
            if not self.position_active:
                return
            # Fetch latest price to check wicks/close against TP or SL
            ticker = self.api.get_ticker(self.symbol)
            if not ticker:
                return
            current_price = float(ticker.get("close", ticker.get("mark_price", 0)))
            if current_price <= 0:
                return
                
            pnl_usd = 0.0
            triggered = False
            exit_reason = ""
            exit_price = 0.0
            
            if self.entry_side == "BUY":
                if current_price >= self.tp_price:
                    triggered = True
                    exit_price = self.tp_price
                    exit_reason = "Take Profit (TP) Hit"
                    pnl_usd = (exit_price - self.entry_price) * self.entry_size
                elif current_price <= self.sl_price:
                    triggered = True
                    exit_price = self.sl_price
                    exit_reason = "Stop Loss (SL) Hit"
                    pnl_usd = (exit_price - self.entry_price) * self.entry_size
            elif self.entry_side == "SELL":
                if current_price <= self.tp_price:
                    triggered = True
                    exit_price = self.tp_price
                    exit_reason = "Take Profit (TP) Hit"
                    pnl_usd = (self.entry_price - exit_price) * self.entry_size
                elif current_price >= self.sl_price:
                    triggered = True
                    exit_price = self.sl_price
                    exit_reason = "Stop Loss (SL) Hit"
                    pnl_usd = (self.entry_price - exit_price) * self.entry_size
                    
            if triggered:
                usd_inr = get_usd_inr_rate()
                pnl_inr = pnl_usd * usd_inr
                self.send_discord_message(
                    f"🔔 **[MOCK] ADA Reversion Trade Closed**\n"
                    f"Exit Reason: `{exit_reason}`\n"
                    f"Side: `{self.entry_side}`\n"
                    f"Contracts: `{self.entry_size}`\n"
                    f"Entry Price: `${self.entry_price:.5f}`\n"
                    f"Exit Price: `${exit_price:.5f}`\n"
                    f"Realized PnL: `${pnl_usd:.2f} USD` (approx `₹{pnl_inr:.2f} INR`)"
                )
                self.position_active = False
                self.entry_price = None
                self.tp_price = None
                self.sl_price = None
                self.entry_side = None
                self.entry_size = None
                self.save_trade_state()
            return

        # 2. REAL MODE EXIT CHECKING
        res = self.api.request("GET", "/v2/positions")
        pos_match = None
        if isinstance(res, dict) and res.get("success"):
            positions = res.get("result", [])
            for p in positions:
                if p.get("product_symbol") == self.symbol:
                    size = float(p.get("size", 0))
                    if size != 0:
                        pos_match = p
                        break
        elif isinstance(res, list):
            for p in res:
                if p.get("product_symbol") == self.symbol:
                    size = float(p.get("size", 0))
                    if size != 0:
                        pos_match = p
                        break

        # Case A: Bot thought a trade was active, but position size is now 0 (or no position found)
        if self.position_active and pos_match is None:
            log_info(f"Position for {self.symbol} is closed. Fetching fills for exit summary...")
            exit_price = None
            realized_pnl = 0.0
            
            # Fetch latest fills to extract realized PnL and exit price
            res_fills = self.api.request("GET", f"/v2/fills?product_symbol={self.symbol}&limit=5")
            if (isinstance(res_fills, dict) and res_fills.get("success")) or isinstance(res_fills, list):
                fills = res_fills.get("result", res_fills) if isinstance(res_fills, dict) else res_fills
                fills.sort(key=lambda x: x.get("id", 0), reverse=True)
                for f in fills:
                    meta = f.get("meta_data", {})
                    new_pos = meta.get("new_position", {})
                    realized_pnl_val = float(new_pos.get("realized_pnl", 0))
                    # Check if this is an exit fill (either has pnl, or opposite side)
                    if realized_pnl_val != 0 or f.get("side", "").upper() != self.entry_side:
                        exit_price = float(f.get("price", 0))
                        realized_pnl = realized_pnl_val
                        break
            
            if exit_price is None:
                # Fallback to current ticker if fills couldn't be loaded
                ticker = self.api.get_ticker(self.symbol)
                exit_price = float(ticker.get("close", ticker.get("mark_price", 0))) if ticker else 0.0
                
            exit_reason = "Position Closed"
            if self.tp_price is not None and abs(exit_price - self.tp_price) / self.tp_price < 0.005:
                exit_reason = "Take Profit (TP) Hit"
            elif self.sl_price is not None and abs(exit_price - self.sl_price) / self.sl_price < 0.005:
                exit_reason = "Stop Loss (SL) Hit"
                
            usd_inr = get_usd_inr_rate()
            pnl_inr = realized_pnl * usd_inr
            
            self.send_discord_message(
                f"🔔 **ADA Reversion Trade Closed**\n"
                f"Exit Reason: `{exit_reason}`\n"
                f"Side: `{self.entry_side}`\n"
                f"Contracts: `{self.entry_size}`\n"
                f"Entry Price: `${self.entry_price:.5f}`\n"
                f"Exit Price: `${exit_price:.5f}`\n"
                f"Realized PnL: `${realized_pnl:.2f} USD` (approx `₹{pnl_inr:.2f} INR`)"
            )
            
            self.position_active = False
            self.entry_price = None
            self.tp_price = None
            self.sl_price = None
            self.entry_side = None
            self.entry_size = None
            self.save_trade_state()
            
        # Case B: Bot thought no trade was active, but an open position is found (e.g. startup recovery or manual trade)
        elif not self.position_active and pos_match is not None:
            self.position_active = True
            self.entry_price = float(pos_match.get("entry_price", 0))
            size = float(pos_match.get("size", 0))
            self.entry_side = "BUY" if size > 0 else "SELL"
            self.entry_size = abs(size)
            # TP and SL are unknown since we recovered state from live position
            self.tp_price = None
            self.sl_price = None
            log_info(f"Synchronized active position from exchange: Side={self.entry_side}, Size={self.entry_size}, Entry=${self.entry_price:.5f}")
            self.save_trade_state()

    def start(self):
        log_info("Starting Bollinger/RSI Mean Reversion Bot...")
        if not self.setup_account():
            log_error("Setup failed. Terminating.")
            return
            
        log_success("Mean Reversion Bot is active and running.")
        self.send_discord_message(f"🚀 **ADA Reversion Bot is active and scanning on Render!** (Resolution: `{self.resolution}`)")
        
        while True:
            try:
                self.check_position_exits()
                self.scan_market()
            except Exception as e:
                log_error(f"Error in scan loop: {e}")
                self.send_discord_message(f"❌ **ADA Reversion Bot Loop Error:** `{str(e)}`")
            time.sleep(self.poll_interval)


def main():
    bot = DeltaReversionBot()
    bot.start()

if __name__ == "__main__":
    main()

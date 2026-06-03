import threading
import time
import json
import os
from flask import Flask, jsonify

app = Flask(__name__)

STATUS_FILE = "bot_status.json"

def write_status(trend_status=None, reversion_status=None, last_trend=None, last_rev=None, latest_error=None):
    status = {
        "trend_bot": "starting",
        "reversion_bot": "starting",
        "last_scan_trend": "never",
        "last_scan_reversion": "never",
        "latest_error": "none"
    }
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r") as f:
                status = json.load(f)
        except:
            pass
            
    if trend_status: status["trend_bot"] = trend_status
    if reversion_status: status["reversion_bot"] = reversion_status
    if last_trend: status["last_scan_trend"] = last_trend
    if last_rev: status["last_scan_reversion"] = last_rev
    if latest_error: status["latest_error"] = latest_error
    
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f)
    except:
        pass

def run_trend_bot():
    write_status(trend_status="running")
    import trend_bot
    
    bot = trend_bot.DeltaTrendBot()
    original_scan = bot.scan_market
    
    def patched_scan(*args, **kwargs):
        # Format in Indian Standard Time (IST)
        from datetime import datetime, timedelta, timezone
        ist = timezone(timedelta(hours=5, minutes=30))
        ist_time = datetime.now(timezone.utc).astimezone(ist)
        write_status(trend_status="running", last_trend=ist_time.strftime("%Y-%m-%d %H:%M:%S IST"))
        return original_scan(*args, **kwargs)
    
    bot.scan_market = patched_scan
    
    try:
        bot.start()
    except Exception as e:
        write_status(trend_status=f"failed: {str(e)}")

def run_reversion_bot():
    write_status(reversion_status="running")
    import reversion_bot
    
    bot = reversion_bot.DeltaReversionBot()
    original_scan = bot.scan_market
    
    def patched_scan(*args, **kwargs):
        # Format in Indian Standard Time (IST)
        from datetime import datetime, timedelta, timezone
        ist = timezone(timedelta(hours=5, minutes=30))
        ist_time = datetime.now(timezone.utc).astimezone(ist)
        write_status(reversion_status="running", last_rev=ist_time.strftime("%Y-%m-%d %H:%M:%S IST"))
        return original_scan(*args, **kwargs)
    
    bot.scan_market = patched_scan
    
    try:
        bot.start()
    except Exception as e:
        write_status(reversion_status=f"failed: {str(e)}")

# Only start the threads in the master Gunicorn worker or if not running inside gunicorn master
if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
    write_status(trend_status="starting", reversion_status="starting")
    t1 = threading.Thread(target=run_trend_bot, daemon=True)
    t2 = threading.Thread(target=run_reversion_bot, daemon=True)
    t1.start()
    t2.start()

@app.route("/")
def home():
    status = {
        "trend_bot": "starting",
        "reversion_bot": "starting",
        "last_scan_trend": "never",
        "last_scan_reversion": "never"
    }
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r") as f:
                status = json.load(f)
        except:
            pass
    return jsonify({
        "status": "healthy",
        "message": "Delta Exchange Bots are active on Render!",
        "bots": status
    })

@app.route("/stats")
def stats():
    try:
        with open("reversion_config.json", "r") as f:
            config = json.load(f)
        k = config["keys"][0]
        
        base_url = "https://api.india.delta.exchange"
        
        def req(method, endpoint):
            t_stamp = int(time.time())
            message = method + str(t_stamp) + endpoint
            sig = hmac.new(k["api_secret"].encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {
                "api-key": k["api_key"],
                "signature": sig,
                "timestamp": str(t_stamp),
                "Content-Type": "application/json"
            }
            res = requests.request(method, f"{base_url}{endpoint}", headers=headers, timeout=10)
            return res.json()
            
        orders = req("GET", "/v2/orders/history?limit=50")
        fills = req("GET", "/v2/fills?limit=50")
        
        return jsonify({
            "success": True,
            "orders": orders.get("result", orders) if isinstance(orders, dict) else orders,
            "fills": fills.get("result", fills) if isinstance(fills, dict) else fills
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

import threading
import time
import json
import os
from flask import Flask, jsonify

app = Flask(__name__)

STATUS_FILE = "bot_status.json"

def write_status(trend_status=None, reversion_status=None, last_trend=None, last_rev=None):
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
            
    if trend_status: status["trend_bot"] = trend_status
    if reversion_status: status["reversion_bot"] = reversion_status
    if last_trend: status["last_scan_trend"] = last_trend
    if last_rev: status["last_scan_reversion"] = last_rev
    
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
        write_status(trend_status="running", last_trend=time.strftime("%Y-%m-%d %H:%M:%S"))
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
        write_status(reversion_status="running", last_rev=time.strftime("%Y-%m-%d %H:%M:%S"))
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

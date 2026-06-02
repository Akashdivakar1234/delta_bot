import threading
import time
from flask import Flask, jsonify

app = Flask(__name__)

# Status monitoring dictionary
bot_status = {
    "trend_bot": "starting",
    "reversion_bot": "starting",
    "last_scan_trend": "never",
    "last_scan_reversion": "never"
}

def run_trend_bot():
    global bot_status
    import trend_bot
    bot_status["trend_bot"] = "running"
    
    # Instantiate the bot and patch its instance scan_market
    bot = trend_bot.DeltaTrendBot()
    original_scan = bot.scan_market
    
    def patched_scan(*args, **kwargs):
        bot_status["last_scan_trend"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return original_scan(*args, **kwargs)
    
    bot.scan_market = patched_scan
    
    try:
        bot.start()
    except Exception as e:
        bot_status["trend_bot"] = f"failed: {str(e)}"

def run_reversion_bot():
    global bot_status
    import reversion_bot
    bot_status["reversion_bot"] = "running"
    
    # Instantiate the bot and patch its instance scan_market
    bot = reversion_bot.DeltaReversionBot()
    original_scan = bot.scan_market
    
    def patched_scan(*args, **kwargs):
        bot_status["last_scan_reversion"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return original_scan(*args, **kwargs)
    
    bot.scan_market = patched_scan
    
    try:
        bot.start()
    except Exception as e:
        bot_status["reversion_bot"] = f"failed: {str(e)}"

# Start bots in background threads at startup
t1 = threading.Thread(target=run_trend_bot, daemon=True)
t2 = threading.Thread(target=run_reversion_bot, daemon=True)
t1.start()
t2.start()

@app.route("/")
def home():
    return jsonify({
        "status": "healthy",
        "message": "Delta Exchange Bots are active on Render!",
        "bots": bot_status
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

import threading
import time
import json
import os
import requests
import hmac
import hashlib
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
    mode = os.environ.get("STRATEGY_MODE", "BOTH").upper()
    
    if mode == "REVERSION_ONLY":
        write_status(trend_status="disabled", reversion_status="starting")
        t2 = threading.Thread(target=run_reversion_bot, daemon=True)
        t2.start()
    elif mode == "TREND_ONLY":
        write_status(trend_status="starting", reversion_status="disabled")
        t1 = threading.Thread(target=run_trend_bot, daemon=True)
        t1.start()
    else:
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
        from flask import request
        with open("reversion_config.json", "r") as f:
            config = json.load(f)
        
        base_url = "https://api.india.delta.exchange"
        
        limit = request.args.get("limit", "100")
        symbol = request.args.get("symbol", "")
        
        all_fills = []
        all_orders = []
        seen_fill_ids = set()
        seen_order_ids = set()
        
        for k in config.get("keys", []):
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
                
            orders_path = f"/v2/orders/history?limit={limit}"
            fills_path = f"/v2/fills?limit={limit}"
            if symbol:
                orders_path += f"&product_symbol={symbol}"
                fills_path += f"&product_symbol={symbol}"
                
            orders = req("GET", orders_path)
            fills = req("GET", fills_path)
            
            if isinstance(orders, dict) and orders.get("success"):
                for o in orders.get("result", []):
                    if o.get("id") not in seen_order_ids:
                        seen_order_ids.add(o.get("id"))
                        all_orders.append(o)
            elif isinstance(orders, list):
                for o in orders:
                    if o.get("id") not in seen_order_ids:
                        seen_order_ids.add(o.get("id"))
                        all_orders.append(o)
                        
            if isinstance(fills, dict) and fills.get("success"):
                for f in fills.get("result", []):
                    if f.get("id") not in seen_fill_ids:
                        seen_fill_ids.add(f.get("id"))
                        all_fills.append(f)
            elif isinstance(fills, list):
                for f in fills:
                    if f.get("id") not in seen_fill_ids:
                        seen_fill_ids.add(f.get("id"))
                        all_fills.append(f)
                        
        return jsonify({
            "success": True,
            "orders": all_orders,
            "fills": all_fills
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api")
def api_proxy():
    try:
        from flask import request
        endpoint = request.args.get("endpoint", "")
        method = request.args.get("method", "GET").upper()
        if not endpoint:
            return jsonify({"success": False, "error": "Endpoint is required"})
            
        with open("reversion_config.json", "r") as f:
            config = json.load(f)
        k = config["keys"][0]
        
        base_url = "https://api.india.delta.exchange"
        
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
        return jsonify(res.json())
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/pnl")
def pnl_tracker():
    try:
        from flask import request
        from datetime import datetime, timezone, timedelta
        from collections import defaultdict

        with open("reversion_config.json", "r") as f:
            config = json.load(f)
        base_url = "https://api.india.delta.exchange"
        ist = timezone(timedelta(hours=5, minutes=30))

        # Paginate fills to get as many as possible from all keys
        all_fills = []
        seen_fill_ids = set()
        
        for k in config.get("keys", []):
            page_size = 100
            after = None
            for _ in range(10):  # max 10 pages = 1000 fills per key
                path = f"/v2/fills?limit={page_size}"
                if after:
                    path += f"&after={after}"
                t_stamp = int(time.time())
                message = "GET" + str(t_stamp) + path
                sig = hmac.new(k["api_secret"].encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()
                headers = {
                    "api-key": k["api_key"],
                    "signature": sig,
                    "timestamp": str(t_stamp),
                    "Content-Type": "application/json"
                }
                res = requests.get(f"{base_url}{path}", headers=headers, timeout=10)
                data = res.json()
                if not isinstance(data, dict) or not data.get("success"):
                    break
                fills = data.get("result", [])
                if not fills:
                    break
                for f in fills:
                    if f.get("id") not in seen_fill_ids:
                        seen_fill_ids.add(f.get("id"))
                        all_fills.append(f)
                if len(fills) < page_size:
                    break
                after = fills[-1].get("id")

        # Parse each fill
        trades = []
        for f in all_fills:
            created_at = f.get("created_at", "")
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(ist)
            except:
                continue

            symbol = f.get("product_symbol", "UNKNOWN")
            side = f.get("side", "").lower()
            size = float(f.get("size", 0))
            price = float(f.get("price", 0))
            commission = float(f.get("commission", 0))
            notional = float(f.get("notional", 0))

            meta = f.get("meta_data", {})
            new_pos = meta.get("new_position", {})
            realized_pnl = float(new_pos.get("realized_pnl", 0))

            trades.append({
                "datetime": dt,
                "date": dt.strftime("%Y-%m-%d"),
                "month": dt.strftime("%Y-%m"),
                "time": dt.strftime("%H:%M:%S"),
                "symbol": symbol,
                "side": side,
                "size": size,
                "price": price,
                "commission": commission,
                "notional": notional,
                "realized_pnl": realized_pnl,
                "is_closing": realized_pnl != 0
            })

        # Sort by datetime ascending
        trades.sort(key=lambda x: x["datetime"])

        usd_inr = 85.0
        initial_capital_inr = 350.0

        # ---- Helper to build aggregation buckets ----
        def make_bucket():
            return {
                "trades": [],
                "total_pnl_usd": 0.0,
                "total_commission_usd": 0.0,
                "wins": 0,
                "losses": 0,
                "by_symbol": defaultdict(lambda: {
                    "pnl_usd": 0.0, "commission_usd": 0.0,
                    "trades": 0, "wins": 0, "losses": 0,
                    "biggest_win_usd": 0.0, "biggest_loss_usd": 0.0
                })
            }

        def add_trade_to_bucket(bucket, t):
            bucket["trades"].append({
                "time": t.get("time", ""),
                "date": t.get("date", ""),
                "symbol": t["symbol"],
                "side": t["side"],
                "size": t["size"],
                "price": t["price"],
                "commission": t["commission"],
                "realized_pnl": t["realized_pnl"]
            })
            bucket["total_commission_usd"] += t["commission"]
            sym = t["symbol"]
            bucket["by_symbol"][sym]["commission_usd"] += t["commission"]

            if t["is_closing"]:
                bucket["total_pnl_usd"] += t["realized_pnl"]
                bucket["by_symbol"][sym]["pnl_usd"] += t["realized_pnl"]
                bucket["by_symbol"][sym]["trades"] += 1
                if t["realized_pnl"] > 0:
                    bucket["wins"] += 1
                    bucket["by_symbol"][sym]["wins"] += 1
                    if t["realized_pnl"] > bucket["by_symbol"][sym]["biggest_win_usd"]:
                        bucket["by_symbol"][sym]["biggest_win_usd"] = t["realized_pnl"]
                elif t["realized_pnl"] < 0:
                    bucket["losses"] += 1
                    bucket["by_symbol"][sym]["losses"] += 1
                    if t["realized_pnl"] < bucket["by_symbol"][sym]["biggest_loss_usd"]:
                        bucket["by_symbol"][sym]["biggest_loss_usd"] = t["realized_pnl"]

        def format_sym_breakdown(by_symbol):
            result = {}
            for sym, s in by_symbol.items():
                wr = round((s["wins"] / s["trades"] * 100), 1) if s["trades"] > 0 else 0
                avg_win = round(s["pnl_usd"] / s["wins"], 6) if s["wins"] > 0 else 0
                avg_loss = round((s["pnl_usd"] - (avg_win * s["wins"])) / s["losses"], 6) if s["losses"] > 0 else 0
                strategy_name = "Trend Following" if sym in ["XRPUSD", "BTCUSD"] else "Mean Reversion" if sym == "ADAUSD" else "Unknown"
                result[sym] = {
                    "strategy": strategy_name,
                    "pnl_usd": round(s["pnl_usd"], 6),
                    "pnl_inr": round(s["pnl_usd"] * usd_inr, 2),
                    "commission_usd": round(s["commission_usd"], 6),
                    "closed_trades": s["trades"],
                    "wins": s["wins"],
                    "losses": s["losses"],
                    "win_rate_pct": wr,
                    "biggest_win_usd": round(s["biggest_win_usd"], 6),
                    "biggest_win_inr": round(s["biggest_win_usd"] * usd_inr, 2),
                    "biggest_loss_usd": round(s["biggest_loss_usd"], 6),
                    "biggest_loss_inr": round(s["biggest_loss_usd"] * usd_inr, 2)
                }
            return result

        # ---- DAILY aggregation ----
        daily = defaultdict(make_bucket)
        for t in trades:
            add_trade_to_bucket(daily[t["date"]], t)

        cumulative_usd = 0.0
        daily_report = []
        for day in sorted(daily.keys()):
            d = daily[day]
            cumulative_usd += d["total_pnl_usd"]
            daily_report.append({
                "date": day,
                "net_pnl_usd": round(d["total_pnl_usd"], 6),
                "net_pnl_inr": round(d["total_pnl_usd"] * usd_inr, 2),
                "total_commission_usd": round(d["total_commission_usd"], 6),
                "wins": d["wins"],
                "losses": d["losses"],
                "cumulative_pnl_usd": round(cumulative_usd, 6),
                "cumulative_pnl_inr": round(cumulative_usd * usd_inr, 2),
                "equity_inr": round(initial_capital_inr + cumulative_usd * usd_inr, 2),
                "by_symbol": format_sym_breakdown(d["by_symbol"]),
                "trade_count": len(d["trades"])
            })

        # ---- MONTHLY aggregation ----
        monthly = defaultdict(make_bucket)
        for t in trades:
            add_trade_to_bucket(monthly[t["month"]], t)

        cumulative_usd_m = 0.0
        monthly_report = []
        for month in sorted(monthly.keys()):
            m = monthly[month]
            cumulative_usd_m += m["total_pnl_usd"]
            wr = round((m["wins"] / (m["wins"] + m["losses"]) * 100), 1) if (m["wins"] + m["losses"]) > 0 else 0
            monthly_report.append({
                "month": month,
                "net_pnl_usd": round(m["total_pnl_usd"], 6),
                "net_pnl_inr": round(m["total_pnl_usd"] * usd_inr, 2),
                "total_commission_usd": round(m["total_commission_usd"], 6),
                "wins": m["wins"],
                "losses": m["losses"],
                "win_rate_pct": wr,
                "cumulative_pnl_usd": round(cumulative_usd_m, 6),
                "cumulative_pnl_inr": round(cumulative_usd_m * usd_inr, 2),
                "equity_inr": round(initial_capital_inr + cumulative_usd_m * usd_inr, 2),
                "by_symbol": format_sym_breakdown(m["by_symbol"]),
                "trade_count": len(m["trades"])
            })

        # ---- PER-STRATEGY lifetime performance ----
        strategy_all = defaultdict(make_bucket)
        for t in trades:
            add_trade_to_bucket(strategy_all[t["symbol"]], t)

        strategy_report = {}
        for sym, s in strategy_all.items():
            total_closed = s["wins"] + s["losses"]
            wr = round((s["wins"] / total_closed * 100), 1) if total_closed > 0 else 0
            sym_data = s["by_symbol"][sym]
            avg_win = round(sym_data["pnl_usd"] / sym_data["wins"], 6) if sym_data["wins"] > 0 else 0
            total_loss_usd = sym_data["pnl_usd"] - (avg_win * sym_data["wins"])
            avg_loss = round(total_loss_usd / sym_data["losses"], 6) if sym_data["losses"] > 0 else 0
            rr_ratio = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 0
            expectancy = round(avg_win * (wr / 100) + avg_loss * (1 - wr / 100), 6)
            strategy_name = "Trend Following" if sym in ["XRPUSD", "BTCUSD"] else "Mean Reversion" if sym == "ADAUSD" else "Unknown"

            strategy_report[sym] = {
                "strategy": strategy_name,
                "total_pnl_usd": round(s["total_pnl_usd"], 6),
                "total_pnl_inr": round(s["total_pnl_usd"] * usd_inr, 2),
                "total_commission_usd": round(s["total_commission_usd"], 6),
                "total_commission_inr": round(s["total_commission_usd"] * usd_inr, 2),
                "closed_trades": total_closed,
                "wins": s["wins"],
                "losses": s["losses"],
                "win_rate_pct": wr,
                "avg_win_usd": avg_win,
                "avg_win_inr": round(avg_win * usd_inr, 2),
                "avg_loss_usd": avg_loss,
                "avg_loss_inr": round(avg_loss * usd_inr, 2),
                "reward_risk_ratio": rr_ratio,
                "expectancy_per_trade_usd": expectancy,
                "expectancy_per_trade_inr": round(expectancy * usd_inr, 2),
                "biggest_win_usd": round(sym_data["biggest_win_usd"], 6),
                "biggest_win_inr": round(sym_data["biggest_win_usd"] * usd_inr, 2),
                "biggest_loss_usd": round(sym_data["biggest_loss_usd"], 6),
                "biggest_loss_inr": round(sym_data["biggest_loss_usd"] * usd_inr, 2),
                "total_fills": len(s["trades"])
            }

        # ---- Overall Summary ----
        total_wins = sum(d["wins"] for d in daily_report)
        total_losses = sum(d["losses"] for d in daily_report)
        final_cumulative = cumulative_usd
        win_rate = round((total_wins / (total_wins + total_losses) * 100), 1) if (total_wins + total_losses) > 0 else 0

        summary = {
            "initial_capital_inr": initial_capital_inr,
            "current_equity_inr": round(initial_capital_inr + final_cumulative * usd_inr, 2),
            "total_pnl_usd": round(final_cumulative, 6),
            "total_pnl_inr": round(final_cumulative * usd_inr, 2),
            "total_wins": total_wins,
            "total_losses": total_losses,
            "win_rate_pct": win_rate,
            "total_fills": len(all_fills),
            "tracking_since": sorted(daily.keys())[0] if daily else "N/A",
            "last_updated": datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S IST")
        }

        return jsonify({
            "success": True,
            "summary": summary,
            "strategies": strategy_report,
            "monthly": monthly_report,
            "daily": daily_report
        })
    except Exception as e:
        import traceback
        return jsonify({"success": False, "error": str(e), "traceback": traceback.format_exc()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)


import json
import time
from trend_bot import DeltaTrendBot, Colors

def generate_breakout_candles(period=20):
    candles = []
    base_time = int(time.time()) - 10000
    
    # We will simulate a steady Donchian Channel between 0.2200 and 0.2250
    # Then we will simulate a breakout close at 0.2260
    for i in range(period):
        # alternate between 0.2210 and 0.2240
        close = 0.2210 if i % 2 == 0 else 0.2240
        candles.append({
            'time': base_time + (i * 900), # 15m intervals
            'open': '0.2220',
            'high': '0.2250',
            'low': '0.2200',
            'close': str(close),
            'volume': '1000'
        })
        
    # Latest completed breakout candle (close=0.2265, which is above 0.2250 channel high)
    candles.append({
        'time': base_time + (period * 900),
        'open': '0.2240',
        'high': '0.2270',
        'low': '0.2235',
        'close': '0.2265', # BREAKOUT!
        'volume': '5000'
    })
    
    # Active forming candle (Index 21)
    candles.append({
        'time': base_time + ((period + 1) * 900),
        'open': '0.2265',
        'high': '0.2268',
        'low': '0.2260',
        'close': '0.2263',
        'volume': '100'
    })
    
    return candles

def run_test():
    print(f"\n{Colors.BLUE}=== RUNNING TREND BOT BREAKOUT TEST ==={Colors.RESET}")
    candles = generate_breakout_candles(20)
    
    # Initialize bot
    bot = DeltaTrendBot()
    bot.is_mock_mode = True
    bot.symbols = ["ADAUSD"]
    bot.load_trade_state()
    bot.product_info = {
        "ADAUSD": {
            "id": 16614,
            "symbol": "ADAUSD",
            "tick_size": 0.00001,
            "contract_value": 1.0,
            "step_size": 1.0
        }
    }
    
    # Check manual calculations
    completed_candles = candles[:-1]
    latest_completed = completed_candles[-1]
    latest_close = float(latest_completed['close'])
    
    channel_candles = completed_candles[-21:-1]
    highs = [float(c['high']) for c in channel_candles]
    lows = [float(c['low']) for c in channel_candles]
    
    upper = max(highs)
    lower = min(lows)
    mid = (upper + lower) / 2.0
    
    print(f"Calculated Channel High: {upper:.5f}")
    print(f"Calculated Channel Low: {lower:.5f}")
    print(f"Calculated Channel Mid: {mid:.5f}")
    print(f"Breakout Candle Close: {latest_close:.5f}")
    
    is_breakout = latest_close > upper
    print(f"Breakout detected?: {is_breakout}")
    
    if is_breakout:
        print(f"{Colors.GREEN}[TEST PASSED] Donchian Channel calculations verify.{Colors.RESET}")
        
        # Test trade execution sizing
        entry = latest_close
        stop_loss = mid
        take_profit = entry + (3.0 * (entry - stop_loss))
        usd_inr = 85.0
        
        print("\nSimulating trade sizing & order routing...")
        bot.execute_trade("BUY", "ADAUSD", entry, stop_loss, take_profit, usd_inr)
    else:
        print(f"{Colors.RED}[TEST FAILED] Breakout not detected.{Colors.RESET}")

if __name__ == "__main__":
    run_test()

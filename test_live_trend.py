from trend_bot import DeltaTrendBot

def test_live_scan():
    print("Initializing Trend Bot in mock mode on live data...")
    bot = DeltaTrendBot()
    bot.is_mock_mode = True
    
    print("Setting up account & loading product info (will bypass auth for public endpoints)...")
    if bot.setup_account():
        print("\nExecuting live scan_market() cycle for all symbols...")
        for symbol in bot.symbols:
            bot.scan_market(symbol)
    else:
        print("Setup failed.")

if __name__ == "__main__":
    test_live_scan()

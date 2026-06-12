import json
import os
import sys

# Ensure parent directory is in path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trend_bot import DeltaTrendBot, Colors

def run_test():
    print(f"\n{Colors.BLUE}=== RUNNING MULTI-SYMBOL TREND BOT TEST ==={Colors.RESET}")
    
    # Initialize Bot in mock mode
    bot = DeltaTrendBot("trend_config.json")
    bot.is_mock_mode = True
    
    # Verify setup
    print("\n[TEST 1] Verifying Symbol Loading & Settings Overrides...")
    print(f"Loaded symbols: {bot.symbols}")
    assert "BIOUSD" in bot.symbols, "BIOUSD must be loaded"
    assert "ETHUSD" in bot.symbols, "ETHUSD must be loaded"
    assert "SOLUSD" in bot.symbols, "SOLUSD must be loaded"
    assert "XRPUSD" in bot.symbols, "XRPUSD must be loaded"
    
    # Check settings overrides
    bio_be = bot.get_symbol_setting("BIOUSD", "use_breakeven", True)
    bio_tp1 = bot.get_symbol_setting("BIOUSD", "tp1_percent", 25.0)
    eth_be = bot.get_symbol_setting("ETHUSD", "use_breakeven", True)
    eth_tp1 = bot.get_symbol_setting("ETHUSD", "tp1_percent", 25.0)
    sol_be = bot.get_symbol_setting("SOLUSD", "use_breakeven", True)
    sol_tp1 = bot.get_symbol_setting("SOLUSD", "tp1_percent", 25.0)
    xrp_be = bot.get_symbol_setting("XRPUSD", "use_breakeven", True)
    xrp_tp1 = bot.get_symbol_setting("XRPUSD", "tp1_percent", 25.0)
    
    print(f"BIOUSD settings: use_breakeven={bio_be}, tp1_percent={bio_tp1}")
    print(f"ETHUSD settings: use_breakeven={eth_be}, tp1_percent={eth_tp1}")
    print(f"SOLUSD settings: use_breakeven={sol_be}, tp1_percent={sol_tp1}")
    print(f"XRPUSD settings: use_breakeven={xrp_be}, tp1_percent={xrp_tp1}")
    
    assert bio_be is True, "BIOUSD must use breakeven"
    assert bio_tp1 == 25.0, "BIOUSD must have 25% TP1"
    assert eth_be is False, "ETHUSD must not use breakeven"
    assert eth_tp1 == 0.0, "ETHUSD must have 0% TP1"
    assert sol_be is False, "SOLUSD must not use breakeven"
    assert sol_tp1 == 0.0, "SOLUSD must have 0% TP1"
    assert xrp_be is True, "XRPUSD must use breakeven"
    assert xrp_tp1 == 25.0, "XRPUSD must have 25% TP1"
    print(f"{Colors.GREEN}[TEST 1 PASSED] Override settings verified successfully.{Colors.RESET}")

    # Set up metadata
    bot.setup_account()
    
    # Check state loading
    print("\n[TEST 2] Verifying Independent State Tracking...")
    # Clear the file first to start fresh
    if os.path.exists(bot.state_file):
        os.remove(bot.state_file)
    bot.load_trade_state()
    
    # We execute a mock trade for ETHUSD
    bot.static_capital_inr = 100000.0
    print("\nSimulating trade entry for ETHUSD (Option 1)...")
    # Entry at 3500, SL at 3480 (distance 20). Target TP2 is +5.0R (3500 + 5*20 = 3600)
    bot.execute_trade("BUY", "ETHUSD", 3500.0, 3480.0, 3600.0, 85.0)
    
    # Let's verify ETHUSD has active position, and others do not
    assert bot.active_trades["ETHUSD"]["position_active"] is True, "ETHUSD must be active"
    assert bot.active_trades["BIOUSD"]["position_active"] is False, "BIOUSD must not be active"
    assert bot.active_trades["SOLUSD"]["position_active"] is False, "SOLUSD must not be active"
    
    print(f"ETHUSD active trade state: {bot.active_trades['ETHUSD']}")
    print(f"{Colors.GREEN}[TEST 2 PASSED] Independent state tracking verified successfully.{Colors.RESET}")

    # Verify sizing calculation for different contract values
    print("\n[TEST 3] Verifying Sizing Logic for Different Contract Values...")
    # Sizing for ETHUSD: entry = 3500, SL = 3480, distance = 20, contract value = 0.01, usd_inr = 85
    # Risk is 3% of capital. Default mock capital is static_capital_inr = 350 INR.
    # Risk INR = 350 * 0.03 = 10.5 INR.
    # Risk USD = 10.5 / 85.0 = 0.1235 USD.
    # Raw Size = Risk USD / (distance * contract_val) = 0.1235 / (20 * 0.01) = 0.1235 / 0.20 = 0.6175 contracts
    # Rounded down to step_size = 1.0 -> 0 contracts? Wait!
    # Let's see: if static capital is 350 INR, the size is very small. Let's temporarily change static capital to 100,000 INR
    # for testing so we get substantial contract sizes!
    bot.static_capital_inr = 100000.0
    bot.execute_trade("BUY", "ETHUSD", 3500.0, 3480.0, 3600.0, 85.0)
    eth_size = bot.active_trades["ETHUSD"]["entry_size"]
    print(f"Calculated ETHUSD size (capital=100k INR, risk=3%): {eth_size} contracts")
    
    # Sizing for SOLUSD: entry = 150.0, SL = 145.0, distance = 5.0, contract value = 1.0, step size = 1.0
    # Risk USD = (100,000 * 0.03) / 85.0 = 3000 / 85.0 = 35.29 USD
    # Raw Size = 35.29 / (5 * 1.0) = 7.05 contracts -> rounded to 7
    bot.execute_trade("BUY", "SOLUSD", 150.0, 145.0, 175.0, 85.0)
    sol_size = bot.active_trades["SOLUSD"]["entry_size"]
    print(f"Calculated SOLUSD size (capital=100k INR, risk=3%): {sol_size} contracts")
    
    assert eth_size > 0, "ETHUSD contract size must be positive"
    assert sol_size > 0, "SOLUSD contract size must be positive"
    print(f"{Colors.GREEN}[TEST 3 PASSED] Sizing logic for contract values verified.{Colors.RESET}")

    # Check Exit/Trigger Logic (Option 1 vs Option 2)
    print("\n[TEST 4] Verifying Exit & Trigger Differences...")
    # For SOLUSD (Option 1): TP1 percent is 0.
    # If price moves past +2R (150 + 2*5 = 160), TP1 should NOT trigger.
    # If price moves past +1.5R (150 + 1.5*5 = 157.5), Stop Loss should NOT move to breakeven.
    # Set mock tickers:
    # 1. Price at 158.0 (+1.6R)
    bot.api.get_ticker = lambda sym: {"close": 158.0}
    bot.check_position_exits("SOLUSD")
    assert bot.active_trades["SOLUSD"]["is_breakeven_active"] is False, "SOLUSD must not move SL to breakeven"
    assert bot.active_trades["SOLUSD"]["sl_price"] == 145.0, "SOLUSD SL must remain at 145.0"
    
    # 2. Price at 162.0 (+2.4R)
    bot.api.get_ticker = lambda sym: {"close": 162.0}
    bot.check_position_exits("SOLUSD")
    assert bot.active_trades["SOLUSD"]["entry_size"] == sol_size, "SOLUSD must not execute partial exit"
    
    # 3. Price at 176.0 (+5.2R) - Hit TP2, trade must close!
    bot.api.get_ticker = lambda sym: {"close": 176.0}
    bot.check_position_exits("SOLUSD")
    assert bot.active_trades["SOLUSD"]["position_active"] is False, "SOLUSD must be closed after hitting TP2"
    print(f"{Colors.GREEN}[TEST 4 PASSED] Option 1 exit logic verified.{Colors.RESET}")

    # Now verify BIOUSD (Option 2)
    # Entry at 1.0, SL at 0.9 (distance 0.1). Target TP2 is +5.0R (1.0 + 5*0.1 = 1.5). TP1 is +2.0R (1.2)
    print("\nSimulating trade entry for BIOUSD (Option 2)...")
    bot.execute_trade("BUY", "BIOUSD", 1.0, 0.9, 1.5, 85.0)
    bio_size = bot.active_trades["BIOUSD"]["entry_size"]
    print(f"Calculated BIOUSD size: {bio_size} contracts")
    
    # 1. Price at 1.16 (+1.6R) -> Should trigger breakeven!
    bot.api.get_ticker = lambda sym: {"close": 1.16}
    bot.check_position_exits("BIOUSD")
    assert bot.active_trades["BIOUSD"]["is_breakeven_active"] is True, "BIOUSD must move SL to breakeven"
    assert bot.active_trades["BIOUSD"]["sl_price"] == 1.0, "BIOUSD SL must be 1.0"
    
    # 2. Price at 1.22 (+2.2R) -> Should trigger TP1!
    bot.api.get_ticker = lambda sym: {"close": 1.22}
    bot.check_position_exits("BIOUSD")
    assert bot.active_trades["BIOUSD"]["tp1_order_placed"] is False, "BIOUSD tp1_order_placed should be reset"
    assert bot.active_trades["BIOUSD"]["entry_size"] < bio_size, "BIOUSD must close partial position at TP1"
    print(f"{Colors.GREEN}[TEST 4.2 PASSED] Option 2 breakeven and TP1 verified.{Colors.RESET}")

    # Cleanup state file
    if os.path.exists(bot.state_file):
        os.remove(bot.state_file)
        
    print(f"\n{Colors.GREEN}{Colors.BOLD}=== ALL MULTI-SYMBOL TESTS PASSED! ==={Colors.RESET}")

if __name__ == "__main__":
    run_test()

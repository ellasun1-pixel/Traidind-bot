"""Research package CLI dispatcher.

Usage:
    python -m research fetch_data [args]
    python -m research validate_data [args]
    python -m research run_backtest [args]
    python -m research run_walk_forward [args]
    python -m research generate_report [args]
"""
import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m research <command> [args]")
        print()
        print("Commands:")
        print("  fetch_data       Download OHLCV from Kraken/Coinbase")
        print("  validate_data    Validate data files before backtesting")
        print("  run_backtest     Run historical backtest")
        print("  run_walk_forward Run walk-forward evaluation")
        print("  generate_report  Generate comprehensive report")
        sys.exit(1)

    command = sys.argv[1]
    sys.argv = [sys.argv[0]] + sys.argv[2:]

    if command == "fetch_data":
        from research.fetch_data import main as cmd
    elif command == "validate_data":
        from research.validate_data import main as cmd
    elif command == "run_backtest":
        from research.run_backtest import main as cmd
    elif command == "run_walk_forward":
        from research.run_walk_forward import main as cmd
    elif command == "generate_report":
        from research.generate_report import main as cmd
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

    cmd()


if __name__ == "__main__":
    main()

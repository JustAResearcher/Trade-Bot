# MEWC Trader v1.01

A market-making trading bot for **MEWC/USDT** on [NonKYC.io](https://nonkyc.io), built with Python and Tkinter.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Automated limit-order placement** — places buy and sell orders on the MEWC/USDT pair
- **Persistent settings** — all configuration is saved to `nonkyc_settings.json` and restored on launch
- **Real-time market data** — live price, spread, and 24h volume display
- **Order tracking** — monitors open orders, detects fills, and logs trade history
- **Safety controls** — budget cap, price ceiling/floor, MEWC reserve, and emergency stop button
- **Dark-themed Tkinter GUI** — stats cards, scrolling log, and in-app settings editor
- **Session & cumulative stats** — tracks orders placed, fills detected, USDT spent, MEWC acquired/sold
- **HMAC-SHA256 API authentication** — secure communication with NonKYC.io REST API

## Screenshots

*(Coming soon)*

## Requirements

- Python 3.10 or newer
- `tkinter` (included with most Python installs)
- A [NonKYC.io](https://nonkyc.io) account with API keys

No third-party packages are required — the bot uses only the Python standard library.

## Quick Start

### From Source

```bash
# Clone the repo
git clone https://github.com/JustAResearcher/Trade-Bot.git
cd Trade-Bot

# Run
python mewc_trader.py
```

### From Executable (Windows)

Download `mewc_trader.exe` from the [Releases](https://github.com/JustAResearcher/Trade-Bot/releases) page and run it. No Python installation needed.

## Configuration

On first launch the bot creates `nonkyc_settings.json` in the same directory. Enter your NonKYC.io API keys in the GUI, adjust any settings, and click **💾 Save Settings**.

| Setting | Description | Default |
|---------|-------------|---------|
| API Access Key | NonKYC.io API access key | *(required)* |
| API Secret Key | NonKYC.io API secret key | *(required)* |
| Buy Interval | Seconds between buy orders | 45 |
| USDT per Buy | Amount of USDT per buy order | 1.50 |
| Max Buy Price | Safety ceiling — won't buy above this | 0.00015 |
| Budget Limit | Total USDT the bot will spend | 5000 |
| Price Bump % | How far above best ask to place limit buys | 1.0% |
| Sell Enabled | Toggle sell-side on/off | Off |
| Sell Interval | Seconds between sell orders | 300 |
| MEWC per Sell | Amount of MEWC per sell order | 5000 |
| Min Sell Price | Floor — won't sell below this | 0.00005 |
| Sell Spread % | How far above best bid for sell price | 10.0% |
| MEWC Reserve | Minimum MEWC to keep in wallet | 50000 |

## How It Works

1. The bot fetches the current MEWC/USDT orderbook from NonKYC.io
2. It places a limit buy order just above the best ask (configurable bump %)
3. Optionally, it places a limit sell order above the best bid (configurable spread %)
4. Open orders are monitored for fills; stats are updated in real time
5. The cycle repeats on a configurable timer

All orders, fills, and errors are displayed in the scrolling log and written to `mewc_trader.log`.

## Files

| File | Purpose |
|------|---------|
| `mewc_trader.py` | Main application |
| `nonkyc_settings.json` | API keys + bot settings (auto-created) |
| `mewc_trader_state.json` | Cumulative trade statistics (auto-created) |
| `mewc_trader.log` | Runtime log file (auto-created) |

## Building the Executable

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name mewc_trader mewc_trader.py
```

The executable will be in the `dist/` folder.

## Disclaimer

This software is provided as-is for educational and personal use. Cryptocurrency trading carries risk. Use at your own discretion — the author is not responsible for any financial losses.

## License

MIT

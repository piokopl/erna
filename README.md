# ERNA Polymarket Straddle Bot (ERNA11-5-M-UD)

A multi-asset runner for a **Straddle** strategy on Polymarket:
- At the start of each 5-minute window the bot buys **YES** and **NO**.
- Each side has independent configuration and martingale tracking.
- Positions are monitored and sold once `bid >= sell_target` (per side).
- Includes a lightweight FastAPI dashboard.

> **Disclaimer:** This repository is provided for educational and research purposes. Trading involves risk. Use at your own discretion.

## Project layout

- `runner.py` — main entrypoint (demo/live)
- `polymarket_client.py` — Polymarket API + CLOB client wrapper
- `ws_client.py` — WebSocket price feed client
- `dashboard.py` — FastAPI dashboard (served from `runner.py`)
- `config.yaml` — strategy configuration
- `state/` — runtime state (ignored by git; kept locally)
- `logs/` — runtime logs (ignored by git; kept locally)

## Setup

### 1) Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate  # Windows PowerShell
```

### 2) Install dependencies

```bash
pip install -r requirements.txt
```

### 3) Configure environment variables

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

### 4) Adjust strategy config

Edit `config.yaml` to match your markets and desired parameters.

## Run

### Demo (paper simulation)

```bash
python runner.py
```

### Live trading

```bash
python runner.py --live
```

## Dashboard

The runner can start a FastAPI dashboard (see console output for the URL).
If you prefer to run it standalone, you can use:

```bash
uvicorn dashboard:app --host 0.0.0.0 --port 8000
```

## Notes on security

- Do **not** commit your `.env` file.
- Wallet keys and API credentials should only be stored locally and securely.

## Risk

5M is a new market. Beware of martingale – run the bot in demo mode for a few days 
during the week and on weekends and observe the hours when there is a clear upward 
and downward trend. Avoid such times; enter such times in the configuration as 
blacklist hours! 

Martingale is very risky – adjust it based on your own analysis of the times 
when the rate is most volatile – these are the golden hours for profiting! 
Remember to have sufficient funds in your account corresponding to the martingale levels.

For safety reasons, it's best to disable trading between 8:00 PM and 8:00 AM UTC, 
as the market is relatively quiet during these hours and the price is too stable. 
You can use the TradingView script from the repository to visualize trend recurrence. 
Note: it's based on stock market data, not the data Polymarket uses for price action, 
so results may vary. However, it will help you understand which days and hours to avoid.

The bot is only used to simulate trades, use it at your own risk!

Here you can see demo instance: http://158.69.209.65:8092/

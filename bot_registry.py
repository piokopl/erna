#!/usr/bin/env python3
"""In-memory registry shared between bots and the web dashboard.

Thread safety: uses a single global lock.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


TRADES_FILE = "state/trades_history.json"


@dataclass
class TradeRecord:
    ts: str
    asset: str
    market_slug: str
    side: str  # YES or NO
    size: float
    buy_price: float
    sell_price: float
    pnl: float
    result: str  # WIN/LOSS
    winning_side: Optional[str] = None
    martingale_step: int = 0


@dataclass
class AssetStatus:
    asset: str
    enabled: bool
    decision: str = ""
    market_slug: str = ""
    market_question: str = ""
    market_end_ts: int = 0
    time_left: int = 0

    # Live prices
    yes_ask: float = 0.0
    yes_bid: float = 0.0
    no_ask: float = 0.0
    no_bid: float = 0.0

    # YES position
    yes_order_id: str = ""
    yes_filled: bool = False
    yes_filled_size: float = 0.0
    yes_sold: bool = False
    yes_buy_price: float = 0.0
    yes_avg_fill_price: float = 0.0
    yes_sell_target: float = 0.0
    yes_order_size: float = 0.0
    yes_target_hit_price: float = 0.0
    yes_martingale_step: int = 0

    # NO position
    no_order_id: str = ""
    no_filled: bool = False
    no_filled_size: float = 0.0
    no_sold: bool = False
    no_buy_price: float = 0.0
    no_avg_fill_price: float = 0.0
    no_sell_target: float = 0.0
    no_order_size: float = 0.0
    no_target_hit_price: float = 0.0
    no_martingale_step: int = 0

    last_update: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class BotRegistry:
    def __init__(self, trades_file: str = TRADES_FILE):
        self._lock = threading.Lock()
        self._status: Dict[str, AssetStatus] = {}
        self._trades: List[TradeRecord] = []
        self._trades_file = trades_file
        self.dashboard_title: str = "PolySniper Straddle"

        Path(self._trades_file).parent.mkdir(parents=True, exist_ok=True)
        self._load_trades()

    @property
    def trades(self) -> List[TradeRecord]:
        return self._trades

    def _load_trades(self) -> None:
        try:
            if Path(self._trades_file).exists():
                with open(self._trades_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                known = {f for f in TradeRecord.__dataclass_fields__}
                for t in data.get("trades", []):
                    self._trades.append(TradeRecord(**{k: v for k, v in t.items() if k in known}))
                print(f"📊 Loaded {len(self._trades)} trades from {self._trades_file}")
        except Exception as e:
            print(f"⚠️ Failed to load trades: {e}")

    def _save_trades(self) -> None:
        try:
            data = {"trades": [asdict(t) for t in self._trades], "last_updated": datetime.utcnow().isoformat()}
            tmp = self._trades_file + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            Path(tmp).rename(self._trades_file)
        except Exception as e:
            print(f"⚠️ Failed to save trades: {e}")

    def upsert_status(self, status: AssetStatus) -> None:
        with self._lock:
            status.last_update = datetime.utcnow().isoformat()
            self._status[status.asset] = status

    def add_trade(self, trade: TradeRecord, max_records: int = 5000) -> None:
        with self._lock:
            self._trades.append(trade)
            if len(self._trades) > max_records:
                self._trades = self._trades[-max_records:]
            self._save_trades()

    def get_status(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: asdict(v) for k, v in self._status.items()}

    def get_trades(self, asset: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            trades = self._trades
            if asset:
                trades = [t for t in trades if t.asset == asset]
            return [asdict(t) for t in trades[-limit:]]

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            per: Dict[str, Dict[str, Any]] = {}
            total = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
            for t in self._trades:
                s = per.setdefault(t.asset, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
                s["trades"] += 1; total["trades"] += 1
                if t.result == "WIN":
                    s["wins"] += 1; total["wins"] += 1
                elif t.result == "LOSS":
                    s["losses"] += 1; total["losses"] += 1
                s["pnl"] += float(t.pnl); total["pnl"] += float(t.pnl)
            for s in per.values():
                d = s["wins"] + s["losses"]
                s["win_rate"] = (s["wins"] / d * 100.0) if d else 0.0
            dt = total["wins"] + total["losses"]
            total["win_rate"] = (total["wins"] / dt * 100.0) if dt else 0.0
            return {"per_asset": per, "total": total}

    def clear_trades(self) -> int:
        with self._lock:
            c = len(self._trades); self._trades = []; self._save_trades(); return c


REGISTRY = BotRegistry()

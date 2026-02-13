#!/usr/bin/env python3
"""ERNA-11-5-M-UD

Straddle strategy:
- The bot buys YES and NO simultaneously at the start of each window.
- Each side has its own config (buy_price, sell_target, order_size).
- Each side has its own martingale ladder (independent WIN/LOSS tracking).
- The bot monitors prices and sells when bid >= target (per side).

Usage:
  python runner.py            # demo
  python runner.py --live     # live
"""


from __future__ import annotations

import argparse
import csv
import logging
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import uvicorn
import yaml
from dotenv import load_dotenv

from bot_registry import REGISTRY, AssetStatus, TradeRecord
from polymarket_client import (
    PolymarketClient, MarketInfo, CloudflareBlockError,
    InsufficientBalanceError, OrderStatus, ExecutionInfo,
)
from ws_client import PolymarketWebSocket


WINDOW_SECONDS = 300
FILL_DEADLINE_BUFFER = 10
DEFAULT_SELL_FEE_BUFFER = 0.02
DEFAULT_MARTINGALE_MULTIPLIERS = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0]
MIN_SELL_TARGET = 0.51
WIN_THRESHOLD = 0.50
SELL_FLOOR_PRICE = 0.99


def ensure_dirs():
    Path('logs').mkdir(exist_ok=True)
    Path('state').mkdir(exist_ok=True)


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def make_logger(asset: str) -> logging.Logger:
    ensure_dirs()
    lg = logging.getLogger(f"bot.{asset}")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        fmt = logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        sh = logging.StreamHandler(sys.stdout); sh.setFormatter(fmt)
        fh = logging.FileHandler(f'logs/polysniper_{asset}.log', encoding='utf-8'); fh.setFormatter(fmt)
        lg.addHandler(sh); lg.addHandler(fh)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    return lg


def get_next_window(now: Optional[float] = None, grace_period: int = 30) -> Tuple[int, int]:
    n = int(now if now is not None else time.time())
    cs = (n // WINDOW_SECONDS) * WINDOW_SECONDS
    ce = cs + WINDOW_SECONDS
    if n - cs <= grace_period:
        return cs, ce
    return cs + WINDOW_SECONDS, ce + WINDOW_SECONDS


# ═════════════════════════════════════════════════════════════════════════════
# Data classes
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class OrderState:
    side: str  # "YES" or "NO"
    order_id: Optional[str] = None
    token_id: Optional[str] = None
    filled: bool = False
    filled_size: float = 0.0
    sell_size: float = 0.0
    limit_price: float = 0.0
    avg_fill_price: float = 0.0
    sold: bool = False
    sell_order_id: Optional[str] = None
    target_hit_price: float = 0.0
    pnl: float = 0.0
    fill_state: str = "PENDING"


@dataclass
class MartingaleState:
    multipliers: List[float] = field(default_factory=lambda: DEFAULT_MARTINGALE_MULTIPLIERS.copy())
    step: int = 0
    consecutive_losses: int = 0
    last_result: Optional[str] = None

    def get_multiplier(self) -> float:
        return self.multipliers[min(self.step, len(self.multipliers) - 1)]

    def on_win(self):
        self.step = 0; self.consecutive_losses = 0; self.last_result = "WIN"

    def on_loss(self):
        self.consecutive_losses += 1
        if self.step >= len(self.multipliers) - 1:
            self.step = 0; self.last_result = "LOSS_RESET"
        else:
            self.step += 1; self.last_result = "LOSS"


@dataclass
class SideConfig:
    """Configuration for one side (YES or NO)."""
    buy_price: float = 0.52
    sell_target: float = 0.98
    base_order_size: float = 5.0


# ═════════════════════════════════════════════════════════════════════════════
# Bot
# ═════════════════════════════════════════════════════════════════════════════

class StraddleBot(threading.Thread):

    def __init__(self, asset: str, cfg: Dict[str, Any], is_demo: bool, shared_stop: threading.Event):
        super().__init__(daemon=True)
        self.asset = asset.upper()
        self.cfg = cfg
        self.is_demo = is_demo
        self.stop_event = shared_stop
        self.log = make_logger(self.asset)

        self.enabled = bool(cfg.get('enabled', True))
        self.symbol = cfg.get('symbol', f"{self.asset}/USDT")
        self.sell_fee_buffer = float(cfg.get('sell_fee_buffer', DEFAULT_SELL_FEE_BUFFER))

        # Per-side config
        self.yes_cfg = SideConfig(
            buy_price=float(cfg.get('yes_buy_price', 0.52)),
            sell_target=float(cfg.get('yes_sell_target', 0.98)),
            base_order_size=float(cfg.get('yes_order_size', 5.0)),
        )
        self.no_cfg = SideConfig(
            buy_price=float(cfg.get('no_buy_price', 0.52)),
            sell_target=float(cfg.get('no_sell_target', 0.98)),
            base_order_size=float(cfg.get('no_order_size', 5.0)),
        )

        # Martingale target reduction
        self.target_reduction_second_last = float(cfg.get('_target_reduction_second_last', 0.0))
        self.target_reduction_last = float(cfg.get('_target_reduction_last', 0.0))

        # Blacklist
        self.blacklist_hours = cfg.get('blacklist_hours', [])

        # Polymarket client
        self.pm = PolymarketClient(
            private_key=os.getenv('POLYMARKET_PRIVATE_KEY', ''),
            funder_address=os.getenv('POLYMARKET_PROXY_ADDRESS', ''),
            api_key=os.getenv('POLYMARKET_API_KEY', ''),
            api_secret=os.getenv('POLYMARKET_API_SECRET', ''),
            api_passphrase=os.getenv('POLYMARKET_API_PASSPHRASE', ''),
            is_demo=is_demo,
        )

        self.transactions_file = f"transactions_{self.asset.lower()}.csv"
        self._init_csv()

        # State
        self.yes_order: Optional[OrderState] = None
        self.no_order: Optional[OrderState] = None
        self.target_window_start: int = 0
        self.target_window_end: int = 0
        self.current_market: Optional[MarketInfo] = None
        self.ws: Optional[PolymarketWebSocket] = None

        # Independent martingale per side
        mults = cfg.get('_martingale_multipliers', DEFAULT_MARTINGALE_MULTIPLIERS)
        self.yes_martingale = MartingaleState(multipliers=list(mults))
        self.no_martingale = MartingaleState(multipliers=list(mults))

        # Live prices
        self.yes_ask: float = 0.0
        self.yes_bid: float = 0.0
        self.no_ask: float = 0.0
        self.no_bid: float = 0.0

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _side_cfg(self, side: str) -> SideConfig:
        return self.yes_cfg if side == "YES" else self.no_cfg

    def _side_mg(self, side: str) -> MartingaleState:
        return self.yes_martingale if side == "YES" else self.no_martingale

    def _get_order_size(self, side: str) -> float:
        return self._side_cfg(side).base_order_size * self._side_mg(side).get_multiplier()

    def _get_sell_target(self, side: str) -> float:
        mg = self._side_mg(side)
        base = self._side_cfg(side).sell_target
        mx = len(mg.multipliers) - 1
        s = mg.step
        if s == mx and self.target_reduction_last > 0:
            t = base - self.target_reduction_last
        elif s == mx - 1 and self.target_reduction_second_last > 0:
            t = base - self.target_reduction_second_last
        else:
            return base
        return max(t, MIN_SELL_TARGET)

    def _get_bid(self, side: str) -> float:
        return self.yes_bid if side == "YES" else self.no_bid

    # ── CSV ──────────────────────────────────────────────────────────────────

    def _init_csv(self):
        if not Path(self.transactions_file).exists():
            with open(self.transactions_file, 'w', newline='') as f:
                csv.writer(f).writerow([
                    'timestamp', 'asset', 'market', 'side', 'size',
                    'buy_price', 'sell_price', 'pnl', 'result',
                    'martingale_step', 'winning_side',
                ])

    def _log_trade_csv(self, slug, side, size, buy_price, sell_price, pnl, result, winning_side=None):
        mg = self._side_mg(side)
        with open(self.transactions_file, 'a', newline='') as f:
            csv.writer(f).writerow([
                utc_ts(), self.asset, slug, side, f"{size:.4f}",
                f"{buy_price:.4f}", f"{sell_price:.4f}", f"{pnl:.4f}", result,
                mg.step, winning_side or "",
            ])

    # ── Market / Blacklist ───────────────────────────────────────────────────

    def _find_market_for_window(self, window_start_ts):
        return self.pm.find_15min_market(self.asset, target_start_ts=window_start_ts, window_size=WINDOW_SECONDS)

    def _is_blacklisted_hour(self, window_start_ts):
        if not self.blacklist_hours:
            return False
        dt = datetime.fromtimestamp(window_start_ts, tz=timezone.utc)
        if dt.weekday() >= 5:
            return False
        if dt.hour in self.blacklist_hours:
            self.log.warning(f"⛔ [{self.asset}] {dt.hour:02d}:{dt.minute:02d} UTC blacklisted")
            return True
        return False

    # ── Orders ───────────────────────────────────────────────────────────────

    def _place_buy_order(self, token_id: str, side: str, size: float, price: float) -> Optional[OrderStatus]:
        neg_risk = self.current_market.neg_risk if self.current_market else True
        self.log.info(f"📤 [{self.asset}] {side} BUY @ ${price} x {size}")
        try:
            r = self.pm.place_order(token_id=token_id, side="BUY", size=size, price=price, neg_risk=neg_risk)
            if r:
                self.log.info(f"✅ [{self.asset}] {side} buy placed: {r.order_id[:20]}... status={r.status} filled={r.filled_size}")
                return r
            self.log.error(f"❌ [{self.asset}] {side} buy returned None")
            if self.pm.last_order_error:
                self.log.error(f"   Error: {self.pm.last_order_error}")
            return None
        except (CloudflareBlockError, InsufficientBalanceError):
            raise
        except Exception as e:
            self.log.error(f"❌ [{self.asset}] {side} buy failed: {e}")
            return None

    def _place_sell_order(self, token_id: str, side: str, size: float, price: float) -> Optional[OrderStatus]:
        neg_risk = self.current_market.neg_risk if self.current_market else True
        self.log.info(f"📤 [{self.asset}] {side} SELL @ ${price} x {size}")
        try:
            r = self.pm.place_order(token_id=token_id, side="SELL", size=size, price=price, neg_risk=neg_risk)
            if r:
                self.log.info(f"✅ [{self.asset}] {side} sell placed: {r.order_id[:20]}...")
                return r
            self.log.error(f"❌ [{self.asset}] {side} sell returned None")
            return None
        except Exception as e:
            self.log.error(f"❌ [{self.asset}] {side} sell exception: {e}")
            return None

    # ── Fill confirmation ────────────────────────────────────────────────────

    def _confirm_fill(self, buy_result: OrderStatus, order_id: str, deadline: float) -> Tuple[bool, float, float]:
        """Confirm fill for a single order. Returns (is_filled, filled_size, avg_fill_price)."""
        remaining = max(0, deadline - time.time())

        if buy_result.status.upper() == "MATCHED" and buy_result.filled_size > 0:
            ei = self.pm.get_execution_info(order_id, timeout_s=min(8.0, remaining), poll_s=1.0)
            if ei and ei.filled_size > 0:
                return True, ei.filled_size, ei.avg_price
            return True, buy_result.filled_size, buy_result.avg_fill_price

        fi = self.pm.confirm_fill_until_deadline(
            order_id=order_id, deadline_ts=deadline,
            min_fill_threshold=0.01, poll_interval=1.5,
        )
        if fi and fi.filled_size > 0:
            return True, fi.filled_size, fi.avg_price
        return False, 0.0, 0.0

    # ── WebSocket ────────────────────────────────────────────────────────────

    def _start_websocket(self, market: MarketInfo):
        try:
            self.ws = PolymarketWebSocket()
            self.ws.start(); time.sleep(1)
            self.ws.subscribe_market(market.yes_token_id, market.no_token_id)
            self.log.info(f"📡 [{self.asset}] WebSocket started")
        except Exception as e:
            self.log.warning(f"WS start failed: {e}"); self.ws = None

    def _stop_websocket(self):
        if self.ws:
            try: self.ws.stop()
            except: pass
            self.ws = None
        self.yes_ask = self.yes_bid = self.no_ask = self.no_bid = 0.0

    def _update_prices_from_ws(self):
        if self.ws:
            q = self.ws.get_current_quote()
            if q:
                self.yes_ask = q.yes_ask or 0.0; self.yes_bid = q.yes_bid or 0.0
                self.no_ask = q.no_ask or 0.0;   self.no_bid = q.no_bid or 0.0

    # ── Registry ─────────────────────────────────────────────────────────────

    def _update_registry(self, state: str, slug: str = ""):
        now = time.time()
        tl = max(0, int((self.target_window_start if state == "WAITING" else self.target_window_end) - now)) if (self.target_window_start or self.target_window_end) else 0

        yo = self.yes_order
        no = self.no_order

        REGISTRY.upsert_status(AssetStatus(
            asset=self.asset, enabled=self.enabled, decision=state,
            market_slug=slug,
            market_question=self.current_market.question if self.current_market else "",
            market_end_ts=self.target_window_end, time_left=tl,
            yes_ask=self.yes_ask, yes_bid=self.yes_bid,
            no_ask=self.no_ask, no_bid=self.no_bid,
            # YES
            yes_order_id=(yo.order_id or "") if yo else "",
            yes_filled=yo.filled if yo else False,
            yes_filled_size=yo.filled_size if yo else 0.0,
            yes_sold=yo.sold if yo else False,
            yes_buy_price=self.yes_cfg.buy_price,
            yes_avg_fill_price=yo.avg_fill_price if yo and yo.filled else 0.0,
            yes_sell_target=self._get_sell_target("YES"),
            yes_order_size=self._get_order_size("YES") if state == "WAITING" else (yo.filled_size if yo else self.yes_cfg.base_order_size),
            yes_target_hit_price=yo.target_hit_price if yo else 0.0,
            yes_martingale_step=self.yes_martingale.step,
            # NO
            no_order_id=(no.order_id or "") if no else "",
            no_filled=no.filled if no else False,
            no_filled_size=no.filled_size if no else 0.0,
            no_sold=no.sold if no else False,
            no_buy_price=self.no_cfg.buy_price,
            no_avg_fill_price=no.avg_fill_price if no and no.filled else 0.0,
            no_sell_target=self._get_sell_target("NO"),
            no_order_size=self._get_order_size("NO") if state == "WAITING" else (no.filled_size if no else self.no_cfg.base_order_size),
            no_target_hit_price=no.target_hit_price if no else 0.0,
            no_martingale_step=self.no_martingale.step,
        ))

    # ── Trade recording ──────────────────────────────────────────────────────

    def _record_trade(self, side: str, result: str, order: OrderState, sell_price: float, winning_side: str):
        slug = f"{self.asset.lower()}-updown-5m-{self.target_window_start}"
        fp = order.avg_fill_price if order.avg_fill_price > 0 else order.limit_price
        if result == "WIN":
            ss = order.sell_size if order.sell_size > 0 else order.filled_size
            pnl = (ss * sell_price) - (order.filled_size * fp)
        else:
            pnl = -(order.filled_size * fp)
        order.pnl = pnl

        self._log_trade_csv(slug, side, order.filled_size, fp, sell_price, pnl, result, winning_side)
        REGISTRY.add_trade(TradeRecord(
            ts=utc_ts(), asset=self.asset, market_slug=slug,
            side=side, size=order.filled_size, buy_price=fp,
            sell_price=sell_price, pnl=pnl, result=result,
            winning_side=winning_side, martingale_step=self._side_mg(side).step,
        ))

        mg = self._side_mg(side)
        if result == "WIN":
            mg.on_win()
            self.log.info(f"🎉 [{self.asset}] {side} WIN! Martingale reset → 0")
        else:
            mg.on_loss()
            if mg.last_result == "LOSS_RESET":
                self.log.warning(f"💀 [{self.asset}] {side} LOSS at MAX! Martingale RESET → 0")
            else:
                self.log.info(f"💀 [{self.asset}] {side} LOSS! Martingale → {mg.step}")

    # ── Sell verification thread ─────────────────────────────────────────────

    def _verify_sell_order_thread(self, sell_order_id: str, info: dict):
        side = info['side']
        try:
            self.log.info(f"🔍 [{self.asset}] {side} verify sell {sell_order_id[:16]}...")
            time.sleep(5)
            actual_price = None; is_matched = False
            for _ in range(12):
                try:
                    st = self.pm.get_order_status(sell_order_id)
                    if st and st.status.upper() == "MATCHED":
                        is_matched = True
                        ap = st.avg_fill_price; tp = info.get('target_hit_price', 0)
                        if ap > 0 and tp > 0 and ap < info.get('buy_price', 0):
                            actual_price = tp
                        elif ap > 0:
                            actual_price = ap
                        elif tp > 0:
                            actual_price = tp
                        else:
                            actual_price = info.get('buy_price', 0)
                        break
                    elif st and st.status.upper() in ("CANCELLED", "EXPIRED"):
                        break
                except Exception as e:
                    self.log.warning(f"Verify error: {e}")
                time.sleep(5)

            slug = info['market_slug']; bp = info['buy_price']
            fs = info['filled_size']; ss = info.get('sell_size', fs)
            if is_matched and actual_price:
                pnl = (ss * actual_price) - (fs * bp)
                self.log.info(f"📊 [{self.asset}] {side} verified PnL: ${pnl:.2f}")
                self._update_trade_verification(slug, side, "WIN", actual_price, pnl)
            else:
                pnl = -(fs * bp)
                self.log.warning(f"📊 [{self.asset}] {side} SELL NOT FILLED → LOSS ${pnl:.2f}")
                self._update_trade_verification(slug, side, "LOSS", 0.0, pnl)
        except Exception as e:
            self.log.error(f"❌ [{self.asset}] {side} verify error: {e}")

    def _update_trade_verification(self, slug: str, side: str, result: str, sell_price: float, pnl: float):
        try:
            for t in REGISTRY.trades:
                if t.market_slug == slug and t.asset == self.asset and t.side == side:
                    t.result = result; t.sell_price = sell_price; t.pnl = pnl
                    REGISTRY._save_trades()
                    self.log.info(f"📝 [{self.asset}] {side} trade updated: {result} ${pnl:.2f}")
                    break
        except Exception as e:
            self.log.error(f"Trade update failed: {e}")

    # ── Emergency sell ───────────────────────────────────────────────────────

    def _emergency_sell_side(self, order: Optional[OrderState]) -> bool:
        if not order:
            return True
        if order.fill_state == "PENDING" and order.order_id:
            self.log.info(f"🛑 [{self.asset}] {order.side}: cancelling PENDING")
            self.pm.safe_cancel(order.order_id); return True
        if order.sold:
            return True
        if order.fill_state != "CONFIRMED_FILLED" or not order.filled or order.filled_size <= 0:
            return True
        token_id = order.token_id
        if not token_id:
            self.log.error(f"🛑 [{self.asset}] {order.side}: no token_id! Manual sell needed.")
            return False

        ss = order.sell_size if order.sell_size > 0 else math.floor(order.filled_size * (1 - self.sell_fee_buffer) * 100) / 100
        try:
            rc = self.pm.recheck_fill_size(order.order_id)
            if rc and rc.filled_size > order.filled_size:
                order.filled_size = rc.filled_size
                ss = math.floor(rc.filled_size * (1 - self.sell_fee_buffer) * 100) / 100
        except: pass

        self.log.warning(f"🛑 [{self.asset}] EMERGENCY SELL {ss} {order.side} @ ${SELL_FLOOR_PRICE}")
        for attempt in range(3):
            try:
                r = self._place_sell_order(order.token_id, order.side, ss, SELL_FLOOR_PRICE)
                if r:
                    order.sold = True; order.sell_order_id = r.order_id; return True
            except Exception as e:
                self.log.error(f"❌ Emergency sell {order.side} attempt {attempt+1}: {e}")
            time.sleep(2)
        self.log.critical(f"🚨 [{self.asset}] FAILED TO SELL {order.side}! Manual intervention needed.")
        return False

    def emergency_sell(self) -> bool:
        r1 = self._emergency_sell_side(self.yes_order)
        r2 = self._emergency_sell_side(self.no_order)
        return r1 and r2

    # ── Sell attempt for one side ────────────────────────────────────────────

    def _try_sell_side(self, order: OrderState, filled_size: float, avg_fill_price: float,
                       sell_size: float, sell_target: float, slug: str,
                       sell_attempts: int, max_attempts: int) -> Tuple[bool, Optional[float], Optional[str], int, float, float, float]:
        """Try to sell one side if target hit. Returns updated state."""
        bid = self._get_bid(order.side)
        if bid <= 0 or bid >= 1.0:
            return False, None, None, sell_attempts, filled_size, avg_fill_price, sell_size

        if bid >= sell_target:
            sell_attempts += 1
            self.log.info(f"🎯 [{self.asset}] {order.side} TARGET! Bid ${bid:.4f} >= ${sell_target:.2f} (try {sell_attempts}/{max_attempts})")

            # Recheck fill
            try:
                rc = self.pm.recheck_fill_size(order.order_id)
                if rc and rc.filled_size > filled_size:
                    filled_size = rc.filled_size
                    avg_fill_price = rc.avg_price if rc.avg_price > 0 else avg_fill_price
                    order.filled_size = filled_size
                    order.avg_fill_price = avg_fill_price
                    sell_size = math.floor(filled_size * (1 - self.sell_fee_buffer) * 100) / 100
                    order.sell_size = sell_size
            except: pass

            # Balance check
            if not self.is_demo and sell_size > 0:
                try:
                    bal = self.pm.get_token_balance(order.token_id)
                    if bal < sell_size:
                        self.log.warning(f"⚠️ [{self.asset}] {order.side} balance {bal} < {sell_size}")
                except: pass

            sr = self._place_sell_order(order.token_id, order.side, sell_size, SELL_FLOOR_PRICE)
            if sr:
                order.sold = True; order.sell_order_id = sr.order_id; order.target_hit_price = bid
                self.log.info(f"✅ [{self.asset}] {order.side} TARGET HIT — Sell sent")
                return True, bid, sr.order_id, sell_attempts, filled_size, avg_fill_price, sell_size
            else:
                self.log.warning(f"⚠️ [{self.asset}] {order.side} sell failed, {max_attempts - sell_attempts} left")
                time.sleep(2)

        return False, None, None, sell_attempts, filled_size, avg_fill_price, sell_size

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self):
        if not self.enabled:
            self._update_registry("DISABLED")
            while not self.stop_event.is_set(): time.sleep(2)
            return
        if not self.pm.initialize():
            self.log.error("Polymarket init failed"); self._update_registry("ERROR"); return

        self.log.info(f"🚀 {self.asset} Straddle ({'DEMO' if self.is_demo else 'LIVE'})")
        self.log.info(f"   YES: buy ${self.yes_cfg.buy_price} | target ${self.yes_cfg.sell_target} | size {self.yes_cfg.base_order_size}")
        self.log.info(f"   NO:  buy ${self.no_cfg.buy_price} | target ${self.no_cfg.sell_target} | size {self.no_cfg.base_order_size}")
        self.log.info(f"   Fee buffer: {self.sell_fee_buffer*100:.0f}%")
        self.log.info(f"   Martingale: {len(self.yes_martingale.multipliers)} steps {self.yes_martingale.multipliers}")

        while not self.stop_event.is_set():
            try:
                now = time.time()
                next_start, next_end = get_next_window(now)
                self.target_window_start = next_start
                self.target_window_end = next_end
                time_to_start = next_start - now
                slug = f"{self.asset.lower()}-updown-5m-{next_start}"

                # Blacklist check
                if self._is_blacklisted_hour(next_start):
                    while time.time() < next_end and not self.stop_event.is_set():
                        self._update_registry("BLACKLISTED", slug); time.sleep(1)
                    continue

                self._update_registry("WAITING", slug)

                # ═══ PHASE 1: Wait and find market ═══
                if time_to_start > 0:
                    ymg = self.yes_martingale; nmg = self.no_martingale
                    self.log.info(
                        f"⏳ [{self.asset}] Next: {time.strftime('%H:%M:%S', time.localtime(next_start))} | "
                        f"Wait {int(time_to_start)}s | YES step {ymg.step} ({ymg.get_multiplier()}x) | "
                        f"NO step {nmg.step} ({nmg.get_multiplier()}x)"
                    )
                    prep = max(next_start - 10, now)
                    while time.time() < prep and not self.stop_event.is_set():
                        self._update_registry("WAITING", slug); time.sleep(1)
                    if self.stop_event.is_set(): break

                    market = self._find_market_for_window(next_start)
                    if not market:
                        self.log.error(f"❌ [{self.asset}] Market not found")
                        while time.time() < next_end and not self.stop_event.is_set(): time.sleep(1)
                        continue
                    self.current_market = market

                    while time.time() < next_start and not self.stop_event.is_set():
                        self._update_registry("READY", slug); time.sleep(0.1)
                    if self.stop_event.is_set(): break
                else:
                    market = self._find_market_for_window(next_start)
                    if not market:
                        self.log.error(f"❌ [{self.asset}] Market not found"); continue
                    self.current_market = market

                # ═══ PHASE 2: BUY BOTH YES AND NO ═══
                self.log.info(f"🟢 [{self.asset}] Window STARTED → BUY YES + NO")

                yes_size = self._get_order_size("YES")
                no_size = self._get_order_size("NO")
                cf_blocked = False

                # Place YES buy
                yes_buy_result = None
                try:
                    yes_buy_result = self._place_buy_order(
                        market.yes_token_id, "YES", yes_size, self.yes_cfg.buy_price)
                except CloudflareBlockError:
                    cf_blocked = True
                    self.log.error(f"🛑 [{self.asset}] Cloudflare block on YES!")
                except InsufficientBalanceError:
                    self.log.warning(f"💸 [{self.asset}] No balance for YES")

                # Place NO buy (skip if Cloudflare blocked)
                no_buy_result = None
                if not cf_blocked:
                    try:
                        no_buy_result = self._place_buy_order(
                            market.no_token_id, "NO", no_size, self.no_cfg.buy_price)
                    except CloudflareBlockError:
                        cf_blocked = True
                        self.log.error(f"🛑 [{self.asset}] Cloudflare block on NO!")
                    except InsufficientBalanceError:
                        self.log.warning(f"💸 [{self.asset}] No balance for NO")

                # Handle CF block
                if cf_blocked:
                    self._update_registry("CF_BLOCKED", slug)
                    # Cancel any placed order
                    if yes_buy_result:
                        self.pm.safe_cancel(yes_buy_result.order_id)
                    if no_buy_result:
                        self.pm.safe_cancel(no_buy_result.order_id)
                    while time.time() < next_end and not self.stop_event.is_set(): time.sleep(5)
                    for _ in range(60):
                        if self.stop_event.is_set(): break
                        time.sleep(1)
                    continue

                # Both failed?
                if not yes_buy_result and not no_buy_result:
                    self.log.error(f"❌ [{self.asset}] Both orders failed")
                    self._update_registry("ORDER_FAILED", slug)
                    while time.time() < next_end and not self.stop_event.is_set(): time.sleep(2)
                    continue

                # Create order states
                if yes_buy_result:
                    self.yes_order = OrderState(
                        side="YES", order_id=yes_buy_result.order_id,
                        token_id=market.yes_token_id,
                        limit_price=self.yes_cfg.buy_price, fill_state="PENDING",
                    )
                if no_buy_result:
                    self.no_order = OrderState(
                        side="NO", order_id=no_buy_result.order_id,
                        token_id=market.no_token_id,
                        limit_price=self.no_cfg.buy_price, fill_state="PENDING",
                    )

                self._update_registry("PENDING_FILL", slug)
                fill_deadline = next_end - FILL_DEADLINE_BUFFER

                # ── Confirm YES fill ──
                yes_filled = False; yes_fs = 0.0; yes_fp = 0.0
                if yes_buy_result:
                    self.log.info(f"⏳ [{self.asset}] Confirming YES fill...")
                    yes_filled, yes_fs, yes_fp = self._confirm_fill(yes_buy_result, yes_buy_result.order_id, fill_deadline)
                    if yes_filled:
                        self.log.info(f"✅ [{self.asset}] YES FILLED: {yes_fs} @ ${yes_fp:.4f}")
                        self.yes_order.fill_state = "CONFIRMED_FILLED"
                        self.yes_order.filled = True; self.yes_order.filled_size = yes_fs; self.yes_order.avg_fill_price = yes_fp
                        if yes_fs > yes_size * 1.1:
                            self.log.critical(f"🚨 [{self.asset}] YES FILL MISMATCH! {yes_fs} vs {yes_size}")
                            yes_fs = yes_size; self.yes_order.filled_size = yes_fs
                    else:
                        self.log.warning(f"⏱️ [{self.asset}] YES UNFILLED")
                        self.yes_order.fill_state = "UNFILLED_AT_DEADLINE"
                        self.pm.safe_cancel(yes_buy_result.order_id)

                # ── Confirm NO fill ──
                no_filled = False; no_fs = 0.0; no_fp = 0.0
                if no_buy_result:
                    self.log.info(f"⏳ [{self.asset}] Confirming NO fill...")
                    no_filled, no_fs, no_fp = self._confirm_fill(no_buy_result, no_buy_result.order_id, fill_deadline)
                    if no_filled:
                        self.log.info(f"✅ [{self.asset}] NO FILLED: {no_fs} @ ${no_fp:.4f}")
                        self.no_order.fill_state = "CONFIRMED_FILLED"
                        self.no_order.filled = True; self.no_order.filled_size = no_fs; self.no_order.avg_fill_price = no_fp
                        if no_fs > no_size * 1.1:
                            self.log.critical(f"🚨 [{self.asset}] NO FILL MISMATCH! {no_fs} vs {no_size}")
                            no_fs = no_size; self.no_order.filled_size = no_fs
                    else:
                        self.log.warning(f"⏱️ [{self.asset}] NO UNFILLED")
                        self.no_order.fill_state = "UNFILLED_AT_DEADLINE"
                        self.pm.safe_cancel(no_buy_result.order_id)

                # Neither filled?
                if not yes_filled and not no_filled:
                    self.log.warning(f"⏱️ [{self.asset}] BOTH UNFILLED → NO TRADE")
                    self.yes_order = None; self.no_order = None; self.current_market = None
                    while time.time() < next_end and not self.stop_event.is_set():
                        self._update_registry("UNFILLED", slug); time.sleep(1)
                    continue

                # Calculate sell sizes
                yes_sell_size = 0.0; no_sell_size = 0.0
                if yes_filled:
                    yes_sell_size = math.floor(yes_fs * (1 - self.sell_fee_buffer) * 100) / 100
                    self.yes_order.sell_size = yes_sell_size
                    self.log.info(f"💰 [{self.asset}] YES: {yes_fs} tokens, sell: {yes_sell_size}")
                if no_filled:
                    no_sell_size = math.floor(no_fs * (1 - self.sell_fee_buffer) * 100) / 100
                    self.no_order.sell_size = no_sell_size
                    self.log.info(f"💰 [{self.asset}] NO: {no_fs} tokens, sell: {no_sell_size}")

                # ═══ PHASE 3: Monitor both for sell ═══
                yes_tgt = self._get_sell_target("YES") if yes_filled else 0
                no_tgt = self._get_sell_target("NO") if no_filled else 0
                if yes_filled:
                    self.log.info(f"📈 [{self.asset}] YES: {yes_fs} @ ${yes_fp:.4f} | Target ${yes_tgt:.2f}")
                if no_filled:
                    self.log.info(f"📈 [{self.asset}] NO:  {no_fs} @ ${no_fp:.4f} | Target ${no_tgt:.2f}")

                self._start_websocket(self.current_market)
                time.sleep(2)

                yes_sell_placed = False; yes_sell_px = None; yes_sell_oid = None; yes_sell_att = 0
                no_sell_placed = False;  no_sell_px = None;  no_sell_oid = None;  no_sell_att = 0
                yes_last_bid = 0.0; no_last_bid = 0.0
                max_sell_attempts = 3

                while time.time() < next_end and not self.stop_event.is_set():
                    self._update_prices_from_ws()
                    self._update_registry("HOLDING", slug)

                    # Track last valid bids
                    if self.yes_bid > 0 and self.yes_bid < 1.0:
                        yes_last_bid = self.yes_bid
                    if self.no_bid > 0 and self.no_bid < 1.0:
                        no_last_bid = self.no_bid

                    # ── Try sell YES ──
                    if yes_filled and not yes_sell_placed and yes_sell_att < max_sell_attempts:
                        sold, px, oid, yes_sell_att, yes_fs, yes_fp, yes_sell_size = self._try_sell_side(
                            self.yes_order, yes_fs, yes_fp, yes_sell_size, yes_tgt, slug,
                            yes_sell_att, max_sell_attempts,
                        )
                        if sold:
                            yes_sell_placed = True; yes_sell_px = px; yes_sell_oid = oid

                    # ── Try sell NO ──
                    if no_filled and not no_sell_placed and no_sell_att < max_sell_attempts:
                        sold, px, oid, no_sell_att, no_fs, no_fp, no_sell_size = self._try_sell_side(
                            self.no_order, no_fs, no_fp, no_sell_size, no_tgt, slug,
                            no_sell_att, max_sell_attempts,
                        )
                        if sold:
                            no_sell_placed = True; no_sell_px = px; no_sell_oid = oid

                    time.sleep(1)

                self._stop_websocket()
                if self.stop_event.is_set(): break

                # ═══ PHASE 4: Record results independently ═══

                # ── YES result ──
                if yes_filled:
                    if yes_sell_placed:
                        self.log.info(f"🎉 [{self.asset}] YES WIN — SELL @ ${yes_sell_px:.4f}")
                        self._record_trade("YES", "WIN", self.yes_order, yes_sell_px, "YES")
                        vi = {'market_slug': slug, 'buy_price': yes_fp, 'filled_size': yes_fs,
                              'sell_size': yes_sell_size, 'side': "YES", 'target_hit_price': yes_sell_px}
                        threading.Thread(target=self._verify_sell_order_thread, args=(yes_sell_oid, vi), daemon=True).start()
                    elif yes_last_bid > WIN_THRESHOLD:
                        self.log.info(f"✅ [{self.asset}] YES WIN (bid ${yes_last_bid:.4f} > $0.50)")
                        self._record_trade("YES", "WIN", self.yes_order, yes_last_bid, "YES")
                    else:
                        self.log.info(f"💀 [{self.asset}] YES LOSS — bid ${yes_last_bid:.4f}")
                        self._record_trade("YES", "LOSS", self.yes_order, yes_last_bid, "NO")

                # ── NO result ──
                if no_filled:
                    if no_sell_placed:
                        self.log.info(f"🎉 [{self.asset}] NO WIN — SELL @ ${no_sell_px:.4f}")
                        self._record_trade("NO", "WIN", self.no_order, no_sell_px, "NO")
                        vi = {'market_slug': slug, 'buy_price': no_fp, 'filled_size': no_fs,
                              'sell_size': no_sell_size, 'side': "NO", 'target_hit_price': no_sell_px}
                        threading.Thread(target=self._verify_sell_order_thread, args=(no_sell_oid, vi), daemon=True).start()
                    elif no_last_bid > WIN_THRESHOLD:
                        self.log.info(f"✅ [{self.asset}] NO WIN (bid ${no_last_bid:.4f} > $0.50)")
                        self._record_trade("NO", "WIN", self.no_order, no_last_bid, "NO")
                    else:
                        self.log.info(f"💀 [{self.asset}] NO LOSS — bid ${no_last_bid:.4f}")
                        self._record_trade("NO", "LOSS", self.no_order, no_last_bid, "YES")

                # Reset
                self.yes_order = None; self.no_order = None
                self.current_market = None
                self.target_window_start = 0; self.target_window_end = 0

            except Exception as e:
                self.log.error(f"Main loop error: {e}")
                import traceback; traceback.print_exc()
                time.sleep(5)


# ═════════════════════════════════════════════════════════════════════════════
# Dashboard + Main
# ═════════════════════════════════════════════════════════════════════════════

def start_dashboard(port, stop_event):
    from dashboard import app
    uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")).run()


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description='PolySniper Straddle v6')
    p.add_argument('--live', action='store_true')
    p.add_argument('--config', default='config.yaml')
    p.add_argument('--dashboard-port', type=int, default=8050)
    p.add_argument('--no-dashboard', action='store_true')
    args = p.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}"); sys.exit(1)
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    is_demo = not args.live
    if cfg.get('mode', {}).get('demo') is False:
        is_demo = False

    REGISTRY.dashboard_title = cfg.get('dashboard', {}).get('title', 'PolySniper Straddle')

    sc = cfg.get('strategy', {})
    fee_buffer = float(sc.get('sell_fee_buffer', DEFAULT_SELL_FEE_BUFFER))
    yes_sc = sc.get('yes', {})
    no_sc = sc.get('no', {})
    yes_buy = float(yes_sc.get('buy_price', 0.52))
    yes_tgt = float(yes_sc.get('sell_target', 0.98))
    yes_size = float(yes_sc.get('order_size', 5.0))
    no_buy = float(no_sc.get('buy_price', 0.52))
    no_tgt = float(no_sc.get('sell_target', 0.98))
    no_size = float(no_sc.get('order_size', 5.0))

    blacklist = cfg.get('blacklist_hours', [])

    mc = cfg.get('martingale', {})
    mults = mc.get('multipliers', DEFAULT_MARTINGALE_MULTIPLIERS)
    if not isinstance(mults, list) or not mults: mults = DEFAULT_MARTINGALE_MULTIPLIERS
    trc = mc.get('target_reduction', {})
    tr_sl = float(trc.get('second_last', 0.0))
    tr_l = float(trc.get('last', 0.0))

    assets_cfg = cfg.get('assets', {}) or {
        'BTC': {'enabled': True, 'symbol': 'BTC/USDT'},
        'ETH': {'enabled': True, 'symbol': 'ETH/USDT'},
        'XRP': {'enabled': True, 'symbol': 'XRP/USDT'},
        'SOL': {'enabled': True, 'symbol': 'SOL/USDT'},
    }

    shared_stop = threading.Event()
    bots: List[StraddleBot] = []

    print(f"\n{'='*60}")
    print(f"  PolySniper Straddle v6 — {'DEMO' if is_demo else '🔴 LIVE'}")
    print(f"  YES: buy ${yes_buy} | target ${yes_tgt} | size {yes_size}")
    print(f"  NO:  buy ${no_buy} | target ${no_tgt} | size {no_size}")
    print(f"  Fee buffer: {fee_buffer*100:.0f}% | Martingale: {len(mults)} steps {mults}")
    print(f"{'='*60}\n")

    for asset, ac in assets_cfg.items():
        m = dict(ac)
        m.update(
            sell_fee_buffer=fee_buffer, blacklist_hours=blacklist,
            yes_buy_price=yes_buy, yes_sell_target=yes_tgt,
            yes_order_size=ac.get('yes_order_size', yes_size),
            no_buy_price=no_buy, no_sell_target=no_tgt,
            no_order_size=ac.get('no_order_size', no_size),
            _martingale_multipliers=mults,
            _target_reduction_second_last=tr_sl, _target_reduction_last=tr_l,
        )
        bot = StraddleBot(asset=asset, cfg=m, is_demo=is_demo, shared_stop=shared_stop)
        bots.append(bot); bot.start()

    if not args.no_dashboard:
        threading.Thread(target=start_dashboard, args=(args.dashboard_port, shared_stop), daemon=True).start()
        print(f"📊 Dashboard: http://localhost:{args.dashboard_port}")

    def shutdown(reason="unknown"):
        print(f"\n⏹️ Stopping ({reason})...")
        shared_stop.set()
        for b in bots:
            if b.is_alive():
                try: b.emergency_sell()
                except Exception as e: print(f"❌ {b.asset}: {e}")
        for b in bots: b.join(timeout=10)
        print("✅ Done")

    signal.signal(signal.SIGTERM, lambda *_: (shutdown("SIGTERM"), sys.exit(0)))
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        shutdown("Ctrl+C")


if __name__ == "__main__":
    main()

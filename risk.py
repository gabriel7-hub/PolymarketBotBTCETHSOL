"""
risk.py — Risk guard layer. All guards are hard limits — no override.
"""

import time
from loguru import logger
import config
import state


class RiskGuard:

    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        self._consecutive_losses = 0
        self._cooldown_remaining = 0     # windows to skip after a loss
        self._session_start_pnl = state.get_daily_pnl()

    # ─── Called once per tick before execution ─────────────────────────────────

    def check(self) -> tuple[bool, str]:
        """
        Returns (allowed: bool, reason: str).
        If not allowed, the executor must skip this signal.
        """
        # 1. Daily loss halt — LIVE only. Paper trades no real money, so never halt it.
        if not self.paper_mode:
            daily_pnl = state.get_daily_pnl()
            if daily_pnl <= -config.MAX_DAILY_LOSS:
                return False, f"DAILY_HALT: PnL {daily_pnl:.2f} ≤ -{config.MAX_DAILY_LOSS}"

        # 2. Consecutive-loss cooldown (disabled when POST_LOSS_COOLDOWN == 0)
        if config.POST_LOSS_COOLDOWN > 0 and self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return False, f"COOLDOWN: {self._cooldown_remaining + 1} windows remaining"

        # 3. Max consecutive losses (disabled when MAX_CONSECUTIVE_LOSSES == 0)
        if config.MAX_CONSECUTIVE_LOSSES > 0 and self._consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            return False, f"CONSEC_LOSS_HALT: {self._consecutive_losses} consecutive losses"

        # 4. Already have an open position
        open_pos = state.get_open_position()
        if open_pos:
            return False, "OPEN_POSITION: waiting for current position to resolve"

        return True, "OK"

    def on_win(self):
        self._consecutive_losses = 0

    def on_loss(self):
        self._consecutive_losses += 1
        self._cooldown_remaining = config.POST_LOSS_COOLDOWN
        msg = f"Loss recorded. Consecutive losses: {self._consecutive_losses}."
        if config.POST_LOSS_COOLDOWN > 0:
            msg += f" Cooling down for {config.POST_LOSS_COOLDOWN} windows."
        logger.warning(msg)

    def on_push(self):
        # Push (rare, price exactly at reference) doesn't reset or increment consecutive losses
        pass

    @property
    def is_halted(self) -> bool:
        if self.paper_mode:
            return False   # paper never halts
        halted = state.get_daily_pnl() <= -config.MAX_DAILY_LOSS
        if config.MAX_CONSECUTIVE_LOSSES > 0:
            halted = halted or self._consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES
        return halted

    def status_str(self) -> str:
        if self.is_halted:
            return "HALTED"
        if config.POST_LOSS_COOLDOWN > 0 and self._cooldown_remaining > 0:
            return f"COOLDOWN ({self._cooldown_remaining})"
        return "PAPER" if self.paper_mode else "LIVE"

"""
pricing.py — Single source of truth for Polymarket fee and expected-value math.

Polymarket crypto taker fee is charged PER SHARE on the traded price:

    fee_per_share(p) = TAKER_FEE_RATE · p · (1 − p)

This is maximal at p = 0.5 (~0.0175 at the 0.07 coefficient) and tends to zero at
the extremes — which is why a flat "cents of edge" threshold is wrong. We instead
gate every trade on fee-net expected value PER SHARE.

A binary share pays $1.00 if the side wins, $0.00 otherwise. Buying one share of a
side at price `a` whose true win-probability is `p`:

    payoff if win  =  (1 − a)        (paid a, receive 1)
    payoff if lose =  (− a)          (paid a, receive 0)
    E[payoff]      =  p·(1 − a) − (1 − p)·a  =  p − a

So the raw directional edge per share is simply (p − a); fees/rebates adjust it.
"""

import config


# ─── Fees ───────────────────────────────────────────────────────────────────────

def taker_fee_per_share(price: float, rate: float = None) -> float:
    """Polymarket taker fee per share at a given fill price."""
    rate = config.TAKER_FEE_RATE if rate is None else rate
    p = max(0.0, min(1.0, price))
    return rate * p * (1.0 - p)


def maker_rebate_per_share(price: float, rate: float = None,
                           share: float = None) -> float:
    """
    Approximate maker rebate per share. Rebates are a daily redistribution of
    MAKER_REBATE_SHARE of the taker-fee pool; we approximate our share of a fill as
    that fraction of the fee the taker on the other side paid at this price.
    """
    rate = config.TAKER_FEE_RATE if rate is None else rate
    share = config.MAKER_REBATE_SHARE if share is None else share
    return taker_fee_per_share(price, rate) * share


# ─── Expected value per share ─────────────────────────────────────────────────────

def taker_ev_per_share(p_true: float, ask: float) -> float:
    """
    Fee-net EV of buying one share at `ask` (an IOC taker fill) given our true
    probability estimate `p_true`. Positive ⇒ +EV before risk/variance.
    """
    return (p_true - ask) - taker_fee_per_share(ask)


def maker_ev_per_share(p_true: float, price: float,
                       haircut: float = None) -> float:
    """
    EV of a RESTING maker buy at `price` that gets filled. Makers pay no fee and earn
    a rebate, but resting fills are adversely selected (we only fill when informed
    flow trades against us), so we subtract an adverse-selection haircut.
    """
    haircut = config.ADVERSE_SELECTION_HAIRCUT if haircut is None else haircut
    return (p_true - price) + maker_rebate_per_share(price) - haircut


def pair_arb_edge(up_ask: float, down_ask: float) -> float:
    """
    Locked profit per pair from buying one UP + one DOWN as a taker, net of both fees.
    Exactly one side pays $1 at settlement, so cost = up_ask + down_ask, payout = $1.
    Positive ⇒ risk-free profit (the P_UP + P_DOWN = 1 invariant is violated cheap).
    """
    if up_ask is None or down_ask is None:
        return -1.0
    fees = taker_fee_per_share(up_ask) + taker_fee_per_share(down_ask)
    return 1.0 - (up_ask + down_ask) - fees


def reward_score(spread: float, max_spread: float) -> float:
    """
    Polymarket's per-order liquidity-reward score (official formula):

        S(v, s) = ((v − s) / v)²        for 0 ≤ s ≤ v,  else 0

    where v = rewardsMaxSpread and s = the order's distance from the (size-adjusted)
    midpoint, BOTH in the same units (price or cents). The score is QUADRATIC in
    proximity: an order at mid scores 1.0, at half the band 0.25, at the band edge 0.
    A maker's epoch reward = (their summed score) / (everyone's summed score) × pool.
    Implication: quote as close to mid as fill-risk allows — every cent closer is a
    super-linear earnings increase. (Source: docs.polymarket.com liquidity-rewards.)
    """
    if max_spread <= 0 or spread < 0 or spread >= max_spread:
        return 0.0
    return ((max_spread - spread) / max_spread) ** 2


def farm_reward_per_sec(quoted_notional: float, est_apr: float = None,
                        score: float = 1.0) -> float:
    """
    PAPER estimate of liquidity-reward accrual per second on quoted (two-sided) notional,
    scaled by the quadratic placement `score` (1.0 = quoting at mid, 0.25 = half-band).
    Real rewards are a pool-share proportional to qualifying size × time × score; the flat
    APR stands in for our pool share so tighter quotes correctly show higher yield.
    """
    import config
    est_apr = config.FARM_EST_APR if est_apr is None else est_apr
    return quoted_notional * est_apr * max(0.0, min(1.0, score)) / (365.0 * 24.0 * 3600.0)


def round_to_tick(price: float, tick: float = 0.01) -> float:
    """Round a price to the CLOB tick grid and clamp into the tradeable range."""
    if tick <= 0:
        tick = 0.01
    rounded = round(price / tick) * tick
    return max(tick, min(1.0 - tick, round(rounded, 4)))


# ─── Quick self-check ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Fee is maximal at 0.5 and tiny at the extremes.
    assert abs(taker_fee_per_share(0.5) - 0.0175) < 1e-9, taker_fee_per_share(0.5)
    assert taker_fee_per_share(0.9) < taker_fee_per_share(0.5)
    assert taker_fee_per_share(0.99) < taker_fee_per_share(0.9)

    # Tiny edge at 50¢ should NOT clear the fee; a real edge should.
    assert taker_ev_per_share(0.51, 0.50) < 0, "1¢ edge at 50¢ must be -EV after fee"
    assert taker_ev_per_share(0.60, 0.50) > 0, "10¢ edge at 50¢ must be +EV"

    # Maker EV includes rebate but is dragged by the adverse-selection haircut.
    assert maker_ev_per_share(0.52, 0.50) < (0.52 - 0.50), "haircut must reduce maker EV"

    print("pricing.py self-check passed")
    for px in (0.50, 0.70, 0.90, 0.97):
        print(f"  p={px:.2f}  fee/sh={taker_fee_per_share(px):.4f}  "
              f"rebate/sh={maker_rebate_per_share(px):.4f}")

"""
live_check.py — SAFE, READ-ONLY preflight for live trading. Places NO orders.

Fill in .env (PRIVATE_KEY, CLOB_API_KEY/SECRET/PASSPHRASE, WALLET_ADDRESS) then run:

    python3 live_check.py

It verifies, in order:
  1. CLOB reachability            (get_ok / get_server_time)   — no auth
  2. Key parses to your address   (get_address)                — local
  3. L2 API creds are valid       (get_api_keys)               — authenticated
  4. Wallet USDC balance/allowance(get_balance_allowance)      — authenticated

A green check on all four means the live order path can authenticate. It does NOT
place, sign, or cancel any order.
"""
import sys
import config

OK, BAD, WARN = "\033[92m✓\033[0m", "\033[91m✗\033[0m", "\033[93m!\033[0m"


def main():
    missing = [k for k in ("PRIVATE_KEY", "CLOB_API_KEY", "CLOB_API_SECRET",
                           "CLOB_API_PASSPHRASE", "WALLET_ADDRESS")
               if not getattr(config, k) or getattr(config, k) in ("0x", "0x" + "0" * 64)]
    if missing:
        print(f"{BAD} .env incomplete — fill these before a live run: {', '.join(missing)}")
        return 1

    try:
        from py_clob_client_v2 import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds
    except ImportError:
        print(f"{BAD} py-clob-client-v2 not installed — run: pip install py-clob-client-v2")
        return 1

    client = ClobClient(
        host=config.CLOB_HOST, key=config.PRIVATE_KEY, chain_id=137,
        creds=ApiCreds(api_key=config.CLOB_API_KEY,
                       api_secret=config.CLOB_API_SECRET,
                       api_passphrase=config.CLOB_API_PASSPHRASE),
        signature_type=config.SIGNATURE_TYPE, funder=config.WALLET_ADDRESS,
    )

    rc = 0

    # 1. reachability (no auth)
    try:
        client.get_ok()
        print(f"{OK} CLOB reachable          ({config.CLOB_HOST})")
    except Exception as e:
        print(f"{BAD} CLOB unreachable        — {type(e).__name__}: {str(e)[:100]}")
        rc = 1

    # 2. address derives from the key (local, no network)
    try:
        addr = client.get_address()
        match = addr and addr.lower() == config.WALLET_ADDRESS.lower()
        mark = OK if match else WARN
        note = "" if match else f"  (NOTE: signer {addr} != funder {config.WALLET_ADDRESS})"
        print(f"{mark} Key -> address          {addr}{note}")
    except Exception as e:
        print(f"{BAD} Key parse failed        — {type(e).__name__}: {str(e)[:100]}")
        rc = 1

    # 3. authenticated L2 call — creds are valid
    try:
        keys = client.get_api_keys()
        print(f"{OK} API creds valid         (L2 auth ok)")
    except Exception as e:
        print(f"{BAD} API creds REJECTED      — {type(e).__name__}: {str(e)[:120]}")
        print("    (regenerate with create_or_derive_api_key, or check the .env values)")
        rc = 1

    # 4. wallet USDC balance + exchange allowance
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        ba = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        bal = ba.get("balance") if isinstance(ba, dict) else ba
        print(f"{OK} USDC balance/allowance  {bal}")
        print("    (need USDC funded AND the CLOB exchange allowance set to place orders)")
    except Exception as e:
        print(f"{WARN} Balance check skipped   — {type(e).__name__}: {str(e)[:100]}")

    print()
    if rc == 0:
        print(f"{OK} Preflight PASSED — live auth works. Start small: python3 main.py --mode live")
        print("    Watch the dashboard and keep the STOP LIVE button in reach.")
    else:
        print(f"{BAD} Preflight FAILED — do NOT run live until the above are green.")
    return rc


if __name__ == "__main__":
    sys.exit(main())

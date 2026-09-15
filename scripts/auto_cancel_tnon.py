#!/usr/bin/env python3
"""Auto-cancel watcher for pending replacement orders.

Polls Alpaca API until the replacement chain unlocks, then immediately
cancels the orders.
"""

import datetime
from pathlib import Path
import sys
import time

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bot.client import AlpacaService
from bot.web_state import get_user_state

USER_ID = "68"
TARGET_ORDERS = [
    "35a54e36-ce5d-4439-b4c0-5b67cced5e65",
    "598af643-8480-4ea5-8b14-01c3cef36590",
]


def run_watcher(poll_interval: int = 15):
    print(f"[{datetime.datetime.now()}] Initializing auto-cancel watcher for user {USER_ID}...")
    st = get_user_state(USER_ID)
    srv = AlpacaService(st._base_config())
    print(f"[{datetime.datetime.now()}] Watching orders: {TARGET_ORDERS}")

    while True:
        try:
            open_orders = srv.list_orders(status="open")
            open_ids = {str(o.get("id")) for o in open_orders}
            remaining = [oid for oid in TARGET_ORDERS if oid in open_ids]

            if not remaining:
                print(f"[{datetime.datetime.now()}] All target orders are closed or cancelled. Exiting watcher.")
                break

            for oid in remaining:
                try:
                    srv.trading.cancel_order_by_id(oid)
                    print(f"[{datetime.datetime.now()}] Successfully cancelled order {oid}!")
                except Exception as exc:
                    err_msg = str(exc)
                    # Suppress repeating expected lock errors:
                    if "pending replacement" not in err_msg.lower():
                        print(f"[{datetime.datetime.now()}] Order {oid} cancel attempt: {err_msg}")
        except Exception as exc:
            print(f"[{datetime.datetime.now()}] Watcher check error: {exc}")

        time.sleep(poll_interval)


if __name__ == "__main__":
    run_watcher()

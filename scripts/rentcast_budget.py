#!/usr/bin/env python3
"""Lifetime spend guard for the RentCast API.

RentCast bills per API request against a prepaid credit balance, so the limit
that matters is a *lifetime* call count, not a monthly one -- once the credit is
gone it's gone, and there is no endpoint that reports the remaining balance.
This module is the single gate every RentCast request passes through, and
rentcast_budget.json is the committed counter. The count has to live in the repo
because GitHub Actions runners are ephemeral and would otherwise start from zero
on every run.

Why this exists: RentCast's listings endpoint takes ONE ZIP CODE PER REQUEST, so
a single Search Criteria row with a 16-zip ring costs 16 calls every time the
worker runs. Multiplied across active criteria rows and a 4x/day schedule that
is ~76 calls/day -- about $15/day, which exhausts a $100 balance in under a
week. Nothing in the search worker previously counted those calls.

Two layers:

1. LIFETIME_CALL_LIMIT -- an absolute ceiling. No request is issued past it.
2. PER_RUN_LIMIT -- a per-invocation cap. The ceiling alone would let one
   misconfigured run (a criteria row with a huge zip ring, or many rows
   activated at once) spend a large fraction of the balance before anyone
   noticed. This bounds the blast radius of a single run.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUDGET_PATH = ROOT / "rentcast_budget.json"

# $100 of prepaid credit at $0.20 per request.
LIFETIME_CALL_LIMIT = 500

# Most calls any single worker run may make. With the current criteria (19 zip
# queries per full pass) this lets one run cover everything with headroom, while
# still stopping a runaway configuration from draining the balance in one go.
PER_RUN_LIMIT = 25


class BudgetExhausted(Exception):
    """Raised when a request would exceed the lifetime ceiling."""


class Budget:
    """Tracks spend for one worker run and persists the lifetime total."""

    def __init__(self, state):
        self.state = state
        self.spent_this_run = 0

    @property
    def total(self):
        return self.state.get("totalCalls", 0)

    def remaining(self):
        return max(0, LIFETIME_CALL_LIMIT - self.total)

    def can_spend(self):
        return self.remaining() > 0 and self.spent_this_run < PER_RUN_LIMIT

    def spend(self, note=""):
        """Count one request. Call this BEFORE issuing it.

        A request that fails after reaching RentCast is still billed, so
        counting only successful responses would undercount and overspend.
        """
        if self.remaining() <= 0:
            raise BudgetExhausted(
                f"RentCast lifetime budget exhausted ({self.total}/{LIFETIME_CALL_LIMIT} calls). "
                f"No further requests will be made. Raise LIFETIME_CALL_LIMIT in "
                f"{Path(__file__).name} only after topping up the account balance."
            )
        if self.spent_this_run >= PER_RUN_LIMIT:
            raise BudgetExhausted(
                f"Per-run RentCast cap reached ({PER_RUN_LIMIT} calls this run). "
                f"Remaining work is deferred to the next scheduled run. "
                f"Narrow the zip rings in Search Criteria if this happens every time."
            )
        self.state["totalCalls"] = self.total + 1
        self.spent_this_run += 1
        if note:
            print(f"    [budget] call {self.spent_this_run} this run, "
                  f"{self.total}/{LIFETIME_CALL_LIMIT} lifetime -- {note}")
        return True

    def summary(self):
        return (f"RentCast: {self.spent_this_run} call(s) this run, "
                f"{self.total}/{LIFETIME_CALL_LIMIT} lifetime "
                f"(~${self.total * 0.20:.2f} of $100.00 spent, "
                f"{self.remaining()} calls left)")


def load():
    """Read the persisted counter.

    A missing file starts at zero, but an unreadable or malformed one is treated
    as FULLY SPENT. Failing open would silently unlock the entire balance, which
    is the one failure mode this module exists to prevent.
    """
    if not BUDGET_PATH.exists():
        return Budget({"totalCalls": 0, "limit": LIFETIME_CALL_LIMIT})
    try:
        state = json.loads(BUDGET_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  WARNING: {BUDGET_PATH.name} unreadable ({exc}); treating budget as exhausted")
        return Budget({"totalCalls": LIFETIME_CALL_LIMIT, "limit": LIFETIME_CALL_LIMIT})
    if not isinstance(state, dict) or not isinstance(state.get("totalCalls"), int):
        print(f"  WARNING: {BUDGET_PATH.name} malformed; treating budget as exhausted")
        return Budget({"totalCalls": LIFETIME_CALL_LIMIT, "limit": LIFETIME_CALL_LIMIT})
    state["limit"] = LIFETIME_CALL_LIMIT
    return Budget(state)


def save(budget):
    BUDGET_PATH.write_text(json.dumps(budget.state, indent=2) + "\n")

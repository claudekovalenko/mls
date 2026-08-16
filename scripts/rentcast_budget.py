#!/usr/bin/env python3
"""Spend guard for the RentCast API: free monthly allowance first, paid never.

RentCast gives a fixed number of free requests each month that refill on the
1st, on top of any prepaid credit sitting in the account. Those are completely
different kinds of budget and the earlier version only understood the second
one, so a daily run quietly ate paid credit while free calls went unused and
expired.

The rule now: the free monthly allowance is the budget. Prepaid credit is a
reserve that is never touched unless someone explicitly opts in for a single
run, because spending real money is the owner's decision, not a default.

  FREE_CALLS_PER_MONTH  refills on the 1st; the only budget normally used
  PAID_CALL_CEILING     prepaid reserve, requires ALLOW_PAID_CREDIT=1
  PER_RUN_LIMIT         blast radius of one misconfigured run

Why the counter lives in the repo: Actions runners are ephemeral, so anything
held in memory or on disk restarts at zero every run and the ceiling would
never bind. rentcast_budget.json is committed by the workflow after each run.

Cadence matters more than any cap. A full pass over the current criteria costs
about 9 requests, so a weekly schedule fits inside the free allowance with room
to spare while a daily one needs roughly 270 a month and cannot.
"""
import json
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUDGET_PATH = ROOT / "rentcast_budget.json"

# Refills on the 1st. This is the budget in normal operation.
FREE_CALLS_PER_MONTH = 50

# Prepaid credit, in requests ($100 at $0.20). Off by default -- reaching it
# means spending money, which requires ALLOW_PAID_CREDIT=1 on that run.
PAID_CALL_CEILING = 500

# Most calls any single run may make, free or paid. Bounds one bad config.
PER_RUN_LIMIT = 25

COST_PER_CALL = 0.20


class BudgetExhausted(Exception):
    """Raised when a request would exceed the allowance in force."""


def _this_month():
    return date.today().strftime("%Y-%m")


class Budget:
    """Tracks one run's spend and persists monthly + lifetime counters."""

    def __init__(self, state, allow_paid=None):
        self.state = state
        self.spent_this_run = 0
        # Read at construction so a test or a caller can pass it explicitly
        # rather than reaching through the environment.
        self.allow_paid = (os.environ.get("ALLOW_PAID_CREDIT") == "1"
                           if allow_paid is None else allow_paid)
        self._roll_month()

    def _roll_month(self):
        """Reset the monthly counter when the calendar month changes.

        Done on load rather than on a schedule: there is no process running on
        the 1st to notice, so the first run of a new month performs the reset.
        """
        if self.state.get("month") != _this_month():
            self.state["month"] = _this_month()
            self.state["monthlyCalls"] = 0

    @property
    def total(self):
        return self.state.get("totalCalls", 0)

    @property
    def monthly(self):
        return self.state.get("monthlyCalls", 0)

    def free_remaining(self):
        return max(0, FREE_CALLS_PER_MONTH - self.monthly)

    def paid_remaining(self):
        return max(0, PAID_CALL_CEILING - self.total)

    def remaining(self):
        """What this run may actually spend, under the allowance in force."""
        if self.free_remaining() > 0:
            return self.free_remaining()
        return self.paid_remaining() if self.allow_paid else 0

    def can_spend(self):
        return self.remaining() > 0 and self.spent_this_run < PER_RUN_LIMIT

    def spend(self, note=""):
        """Count one request. Call this BEFORE issuing it.

        A request that fails after reaching RentCast is still billed, so
        counting only successful responses would undercount and overspend.
        """
        if self.free_remaining() <= 0 and not self.allow_paid:
            raise BudgetExhausted(
                f"Free allowance used up ({self.monthly}/{FREE_CALLS_PER_MONTH} "
                f"this month). It refills on the 1st. No paid credit was spent. "
                f"To use prepaid credit for one run, set ALLOW_PAID_CREDIT=1 "
                f"— that spends real money."
            )
        if self.remaining() <= 0:
            raise BudgetExhausted(
                f"Prepaid credit exhausted ({self.total}/{PAID_CALL_CEILING} calls). "
                f"Raise PAID_CALL_CEILING only after topping the account up."
            )
        if self.spent_this_run >= PER_RUN_LIMIT:
            raise BudgetExhausted(
                f"Per-run cap reached ({PER_RUN_LIMIT} calls). Remaining work is "
                f"deferred to the next run. Narrow the zip rings in Search "
                f"Criteria if this happens every time."
            )
        self.state["totalCalls"] = self.total + 1
        self.state["monthlyCalls"] = self.monthly + 1
        self.spent_this_run += 1
        if note:
            kind = "free" if self.monthly <= FREE_CALLS_PER_MONTH else "PAID"
            print(f"    [budget] call {self.spent_this_run} this run, "
                  f"{self.monthly}/{FREE_CALLS_PER_MONTH} free this month ({kind}) "
                  f"-- {note}")
        return True

    def summary(self):
        paid_used = max(0, self.total - self.monthly)
        line = (f"RentCast: {self.spent_this_run} call(s) this run · "
                f"{self.monthly}/{FREE_CALLS_PER_MONTH} free used this month "
                f"({self.free_remaining()} left, refills on the 1st)")
        if paid_used or self.allow_paid:
            line += (f" · prepaid reserve {self.paid_remaining()} calls "
                     f"(~${self.paid_remaining() * COST_PER_CALL:.2f}) "
                     f"{'UNLOCKED' if self.allow_paid else 'locked'}")
        return line


def load(allow_paid=None):
    """Read the persisted counters.

    A missing file starts fresh, but an unreadable or malformed one is treated
    as FULLY SPENT. Failing open would silently unlock the whole allowance,
    which is the one failure mode this module exists to prevent.
    """
    if not BUDGET_PATH.exists():
        return Budget({"month": _this_month(), "monthlyCalls": 0, "totalCalls": 0},
                      allow_paid=allow_paid)
    try:
        state = json.loads(BUDGET_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  WARNING: {BUDGET_PATH.name} unreadable ({exc}); treating as exhausted")
        state = None
    if not isinstance(state, dict) or not isinstance(state.get("totalCalls"), int):
        if state is not None:
            print(f"  WARNING: {BUDGET_PATH.name} malformed; treating as exhausted")
        return Budget({"month": _this_month(), "monthlyCalls": FREE_CALLS_PER_MONTH,
                       "totalCalls": PAID_CALL_CEILING}, allow_paid=allow_paid)
    # An older file has no monthly counter. Assume this month's free calls are
    # untouched rather than spent -- the alternative blocks a legitimate run,
    # and the per-run cap still bounds any mistake.
    state.setdefault("monthlyCalls", 0)
    return Budget(state, allow_paid=allow_paid)


def save(budget):
    BUDGET_PATH.write_text(json.dumps(budget.state, indent=2) + "\n")

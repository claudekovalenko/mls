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

# Absolute ceiling on calls in a calendar month, free and paid combined.
# Set to the free allowance itself: the owner asked for searches to stay
# free, so the cap and the free tier are now the same number and a scheduled
# run can never reach paid credit. Prepaid credit still exists and is still
# spendable, but only by a human dispatching a run with allow_paid ticked.
#
# The cadence has to match: a full pass costs ~9 calls, so 50 free a month
# funds about five runs -- weekly, with margin. Three a week needs ~117 and
# cannot be free. Raising this without also raising the cadence would just
# produce silence for the back half of every month.
MONTHLY_CALL_CAP = FREE_CALLS_PER_MONTH

# Prepaid credit, in requests ($100 at $0.20). Off by default -- reaching it
# means spending money, which requires ALLOW_PAID_CREDIT=1 on that run.
PAID_CALL_CEILING = 500

# Most calls any single run may make, free or paid. Bounds one bad config.
PER_RUN_LIMIT = 25

# Pacing. The allowance is spread across the month instead of being
# spendable on day one: by day d of an n-day month, at most
# FREE * d / n + PACE_HEADROOM calls may have been made. This is what stops
# a run of eager manual dispatches from emptying September by the 17th,
# which happened, and left the scheduled runs with nothing.
PACE_HEADROOM = 6

# Manual dispatches (a person pressing Run workflow) are for trying a fix,
# not for a full pass: at most this many calls each, and never out of the
# last SCHEDULE_RESERVE calls of the month, which belong to the schedule.
MANUAL_RUN_LIMIT = 3
SCHEDULE_RESERVE = 12

COST_PER_CALL = 0.20


class BudgetExhausted(Exception):
    """Raised when a request would exceed the allowance in force."""


def _this_month():
    return date.today().strftime("%Y-%m")


def pace_allowance(today=None):
    """Calls the free allowance permits by this point in the month."""
    import calendar
    today = today or date.today()
    days = calendar.monthrange(today.year, today.month)[1]
    return min(FREE_CALLS_PER_MONTH,
               int(FREE_CALLS_PER_MONTH * today.day / days) + PACE_HEADROOM)


class Budget:
    """Tracks one run's spend and persists monthly + lifetime counters."""

    def __init__(self, state, allow_paid=None, manual=None, today=None):
        self.state = state
        self.spent_this_run = 0
        # Read at construction so a test or a caller can pass it explicitly
        # rather than reaching through the environment.
        self.allow_paid = (os.environ.get("ALLOW_PAID_CREDIT") == "1"
                           if allow_paid is None else allow_paid)
        self.manual = (os.environ.get("RUN_KIND", "scheduled") == "manual"
                       if manual is None else manual)
        self.today = today or date.today()
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

    def monthly_cap_remaining(self):
        return max(0, MONTHLY_CALL_CAP - self.monthly)

    def pace_remaining(self):
        """Free calls the month's pacing still permits today."""
        return max(0, pace_allowance(self.today) - self.monthly)

    def run_limit(self):
        return MANUAL_RUN_LIMIT if self.manual else PER_RUN_LIMIT

    def remaining(self):
        """What this run may actually spend, under the allowance in force."""
        if self.free_remaining() > 0:
            allowed = min(self.free_remaining(), self.pace_remaining())
            if self.manual:
                # The schedule's reserve is off limits to a person's run.
                allowed = min(allowed, max(0, self.free_remaining() - SCHEDULE_RESERVE))
        else:
            allowed = self.paid_remaining() if self.allow_paid else 0
        return min(allowed, self.monthly_cap_remaining())

    def can_spend(self):
        return self.remaining() > 0 and self.spent_this_run < self.run_limit()

    def why_not(self):
        """One line on what is holding spend back, for the run log."""
        if self.free_remaining() <= 0:
            return "free allowance used up; refills on the 1st"
        if self.pace_remaining() <= 0:
            return (f"pacing: {self.monthly} used, {pace_allowance(self.today)} "
                    f"allowed by day {self.today.day}")
        if self.manual and self.free_remaining() <= SCHEDULE_RESERVE:
            return (f"manual runs stop {SCHEDULE_RESERVE} calls short of the "
                    f"free cap; the rest belongs to the schedule")
        if self.spent_this_run >= self.run_limit():
            return f"{'manual' if self.manual else 'per-run'} cap of {self.run_limit()} reached"
        return ""

    def spend(self, note=""):
        """Count one request. Call this BEFORE issuing it.

        A request that fails after reaching RentCast is still billed, so
        counting only successful responses would undercount and overspend.
        """
        if self.monthly_cap_remaining() <= 0:
            raise BudgetExhausted(
                f"Monthly cap reached ({self.monthly}/{MONTHLY_CALL_CAP} calls "
                f"this month, free and paid combined). Resets on the 1st. "
                f"Raise MONTHLY_CALL_CAP in rentcast_budget.py only with the "
                f"owner's say-so -- it is their approved spend, written down."
            )
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
        if self.remaining() <= 0 or self.spent_this_run >= self.run_limit():
            raise BudgetExhausted(f"Held back -- {self.why_not()}. Remaining work "
                                  f"is deferred to a later run.")
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
                f"({self.free_remaining()} left, refills on the 1st) · "
                f"pacing allows {pace_allowance(self.today)} by today · "
                f"{'manual' if self.manual else 'scheduled'} run, "
                f"up to {self.run_limit()} calls")
        if paid_used or self.allow_paid:
            line += (f" · prepaid reserve {self.paid_remaining()} calls "
                     f"(~${self.paid_remaining() * COST_PER_CALL:.2f}) "
                     f"{'UNLOCKED' if self.allow_paid else 'locked'}")
        return line


def load(allow_paid=None, manual=None):
    """Read the persisted counters.

    A missing file starts fresh, but an unreadable or malformed one is treated
    as FULLY SPENT. Failing open would silently unlock the whole allowance,
    which is the one failure mode this module exists to prevent.
    """
    if not BUDGET_PATH.exists():
        return Budget({"month": _this_month(), "monthlyCalls": 0, "totalCalls": 0},
                      allow_paid=allow_paid, manual=manual)
    try:
        state = json.loads(BUDGET_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  WARNING: {BUDGET_PATH.name} unreadable ({exc}); treating as exhausted")
        state = None
    if not isinstance(state, dict) or not isinstance(state.get("totalCalls"), int):
        if state is not None:
            print(f"  WARNING: {BUDGET_PATH.name} malformed; treating as exhausted")
        return Budget({"month": _this_month(), "monthlyCalls": FREE_CALLS_PER_MONTH,
                       "totalCalls": PAID_CALL_CEILING}, allow_paid=allow_paid,
                      manual=manual)
    # An older file has no monthly counter. Assume this month's free calls are
    # untouched rather than spent -- the alternative blocks a legitimate run,
    # and the per-run cap still bounds any mistake.
    state.setdefault("monthlyCalls", 0)
    return Budget(state, allow_paid=allow_paid, manual=manual)


def save(budget):
    BUDGET_PATH.write_text(json.dumps(budget.state, indent=2) + "\n")

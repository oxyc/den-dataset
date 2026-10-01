"""Spend projections and the published-report monthly ledger for the daily job."""
import json
import math
import os

from .contract import StageError


def positive(value, name):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise StageError(f"daily spend: {name} must be a positive finite dollar amount")
    return float(value)


def month_to_date(directory, day):
    """Sum successful reports for ``day``'s UTC month. A malformed published report is fatal.

    The workflow downloads only assets from the public ``daily-reports`` release into this directory.
    Treating an unreadable asset as zero would turn corruption into permission to spend twice.
    """
    if not directory or not os.path.isdir(directory):
        return {"usd": 0.0, "reports": 0}
    prefix = day.strftime("%Y-%m-")
    total, reports = 0.0, 0
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as handle:
                report = json.load(handle)
        except (OSError, ValueError) as error:
            raise StageError(f"daily spend: published report {name} is unreadable: {error}") from error
        date = report.get("date") if isinstance(report, dict) else None
        amount = ((report.get("spend") or {}).get("totalUSD")
                  if isinstance(report, dict) else None)
        if not isinstance(date, str) or not isinstance(amount, (int, float)) or isinstance(amount, bool) \
                or not math.isfinite(amount) or amount < 0:
            raise StageError(f"daily spend: published report {name} has no valid date/spend.totalUSD")
        if date.startswith(prefix):
            total += amount
            reports += 1
    return {"usd": round(total, 6), "reports": reports}


class Ledger:
    """Reserve projected step costs before calls, then replace them with measured costs.

    `pending` is what submitted Batch jobs not yet collected are projected to cost (`pipeline/paid.py`). It
    is committed, so it counts against the monthly cap and in month to date, and leaves `pending` the day the
    job is collected (`collected`), when its measured cost becomes that step's actual."""

    def __init__(self, prior, monthly_cap, pending=0.0):
        self.prior = prior
        self.monthly_cap = positive(monthly_cap, "monthly cap")
        self.pending = max(float(pending), 0.0)
        self.steps = {}

    def current(self, name):
        step = self.steps.get(name) or {}
        return step["actualUSD"] if step.get("actualUSD") is not None else step.get("projectedUSD", 0.0)

    def others(self, name):
        return sum(self.current(key) for key in self.steps if key != name)

    def reserve(self, name, projected, daily_cap):
        projected = max(float(projected), 0.0)
        cap = positive(daily_cap, f"{name} daily cap")
        if projected > cap:
            raise StageError(f"daily spend: {name} projects ${projected:.4f}, crossing its ${cap:.2f} daily cap")
        other = self.others(name) + self.pending
        if self.prior["usd"] + other + projected > self.monthly_cap:
            raise StageError(f"daily spend: {name} projects ${projected:.4f}; ${self.prior['usd'] + other:.4f} "
                             f"is already spent/reserved this month, crossing the ${self.monthly_cap:.2f} monthly cap")
        self.steps[name] = {"projectedUSD": round(projected, 6), "dailyCapUSD": cap, "actualUSD": None}

    def allowance(self, name, daily_cap, collecting=0.0):
        """The most `name` may spend in all today: its daily cap, or what the month has left beside every other
        step's spend and reservation and the committed Batch jobs, whichever is less. `collecting` is the
        projection of a job being collected now, whose cost is about to become this step's rather than stay
        committed."""
        cap = positive(daily_cap, f"{name} daily cap")
        committed = max(0.0, self.pending - max(float(collecting), 0.0))
        return max(0.0, min(cap, self.monthly_cap - self.prior["usd"] - self.others(name) - committed))

    def committed(self, projected):
        """A Batch job submitted (or a submit not yet confirmed) joins `pending`."""
        self.pending += max(float(projected), 0.0)

    def collected(self, projected):
        """A collected Batch job's projection leaves `pending`."""
        self.pending = max(0.0, self.pending - max(float(projected), 0.0))

    def actual(self, name, amount):
        amount = max(float(amount), 0.0)
        if name not in self.steps:
            self.steps[name] = {"projectedUSD": 0.0, "dailyCapUSD": None, "actualUSD": amount}
        else:
            self.steps[name]["actualUSD"] = round(amount, 6)

    def report(self):
        today = sum(step["actualUSD"] or 0.0 for step in self.steps.values())
        # `totalUSD` is what later runs sum into the month, so it is measured spend only. A pending job's
        # projection shows in month to date here and is counted, measured, on the day it is collected.
        return {"steps": self.steps, "todayUSD": round(today, 6),
                "monthBeforeUSD": self.prior["usd"], "publishedReportsThisMonth": self.prior["reports"],
                "pendingBatchUSD": round(self.pending, 6),
                "monthToDateUSD": round(self.prior["usd"] + today + self.pending, 6),
                "monthlyCapUSD": self.monthly_cap, "totalUSD": round(today, 6)}

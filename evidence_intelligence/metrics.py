"""Deterministic transaction metrics. All amounts are Decimal; all windows are explicit."""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Iterable

from data_processing.models import Transaction
from data_processing.textutil import core_name, normalize_name

ZERO = Decimal("0")
CENT = Decimal("0.01")


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def ratio(num: Decimal, den: Decimal | None, places: str = "0.0001") -> Decimal | None:
    """``num/den`` or ``None`` when the denominator is missing or zero (never divides by zero)."""
    if den is None or den == 0:
        return None
    return (num / den).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def window_ending(end: date, days: int) -> tuple[date, date]:
    """Inclusive ``days``-day window ending on ``end``."""
    return end - timedelta(days=days - 1), end


def in_dates(t: Transaction, start: date, end: date) -> bool:
    return start <= t.booked_date <= end


def sort_tx(txs: Iterable[Transaction]) -> list[Transaction]:
    return sorted(txs, key=lambda t: (t.booked_at, t.transaction_id))


def total(txs: Iterable[Transaction]) -> Decimal:
    return sum((t.amount_gbp for t in txs), ZERO)


def counterparty_key(t: Transaction) -> str:
    """Conservative key: the normalised name; account details are kept separately."""
    return core_name(t.counterparty_name) if t.counterparty_name else (f"location:{normalize_name(t.location)}" if t.location else "unknown")


def _largest(txs: list[Transaction]) -> dict | None:
    if not txs:
        return None
    t = max(txs, key=lambda x: (x.amount_gbp, x.transaction_id))
    return {"transaction_id": t.transaction_id, "amount_gbp": t.amount_gbp, "booked_at": t.booked_at,
            "counterparty_name": t.counterparty_name, "channel": t.channel}


def summarize(txs: list[Transaction], *, label: str, start: date | None = None, end: date | None = None) -> dict:
    """Aggregates over ``txs`` with the IDs that make up each one."""
    txs = sort_tx(txs)
    inbound = [t for t in txs if t.direction == "in"]
    outbound = [t for t in txs if t.direction == "out"]
    cash_in = [t for t in inbound if t.is_cash]
    cash_out = [t for t in outbound if t.is_cash]
    by_country: dict[str, dict] = OrderedDict()
    by_channel: dict[str, dict] = OrderedDict()
    by_cp: dict[str, dict] = OrderedDict()
    for t in txs:
        for bucket, key in ((by_country, t.counterparty_country or "unknown"), (by_channel, t.channel)):
            b = bucket.setdefault(key, {"count": 0, "in_gbp": ZERO, "out_gbp": ZERO, "transaction_ids": []})
            b["count"] += 1
            b["in_gbp" if t.direction == "in" else "out_gbp"] += t.amount_gbp
            b["transaction_ids"].append(t.transaction_id)
        if not t.is_card:
            k = counterparty_key(t)
            b = by_cp.setdefault(k, {"name": t.counterparty_name or t.location, "count": 0, "in_gbp": ZERO,
                                     "out_gbp": ZERO, "accounts": [], "transaction_ids": []})
            b["count"] += 1
            b["in_gbp" if t.direction == "in" else "out_gbp"] += t.amount_gbp
            if t.counterparty_account and t.counterparty_account not in b["accounts"]:
                b["accounts"].append(t.counterparty_account)
            b["transaction_ids"].append(t.transaction_id)
    tin, tout = total(inbound), total(outbound)
    conc = []
    for k, b in by_cp.items():
        flow = b["in_gbp"] + b["out_gbp"]
        conc.append({"key": k, **b, "share_of_non_card_flow": ratio(flow, total(t for t in txs if not t.is_card))})
    conc.sort(key=lambda x: (-(x["in_gbp"] + x["out_gbp"]), x["key"]))
    return {
        "label": label,
        "from": start, "to": end,
        "transaction_count": len(txs),
        "inbound": {"count": len(inbound), "total_gbp": tin, "transaction_ids": [t.transaction_id for t in inbound],
                    "largest": _largest(inbound)},
        "outbound": {"count": len(outbound), "total_gbp": tout, "card_total_gbp": total(t for t in outbound if t.is_card),
                     "non_card_total_gbp": total(t for t in outbound if not t.is_card),
                     "non_card_transaction_ids": [t.transaction_id for t in outbound if not t.is_card],
                     "largest": _largest(outbound)},
        "cash": {"deposits_gbp": total(cash_in), "deposit_ids": [t.transaction_id for t in cash_in],
                 "withdrawals_gbp": total(cash_out), "withdrawal_ids": [t.transaction_id for t in cash_out]},
        "net_gbp": tin - tout,
        "unique_counterparties": {"inbound": len({counterparty_key(t) for t in inbound if not t.is_cash}),
                                  "outbound_non_card": len({counterparty_key(t) for t in outbound if not t.is_card and not t.is_cash})},
        "by_country": by_country,
        "by_channel": by_channel,
        "counterparty_concentration": conc[:10],
        "first_booked_at": txs[0].booked_at if txs else None,
        "last_booked_at": txs[-1].booked_at if txs else None,
    }


# --------------------------------------------------------------------------- TM02 helpers
def rapid_movement_hours(txs: list[Transaction], start: date, end: date, hours: int, *,
                         inflow_filter: Callable[[Transaction], bool] = lambda t: True,
                         exclude_card: bool = True) -> dict:
    """Outflows booked within ``hours`` after an inflow that arrived in the window.

    Two views are returned because neither alone proves *which* money left:

    * ``outflows_within_hours``: every outflow that falls inside some inflow's
      ``hours`` horizon, each counted once (this can exceed what came in);
    * ``fifo_allocated_gbp``: outflows allocated first-in-first-out against
      the money received, never more than was received.
    """
    txs = sort_tx(txs)
    inflows = [t for t in txs if t.direction == "in" and in_dates(t, start, end) and inflow_filter(t)]
    horizon = timedelta(hours=hours)
    # outflows must themselves fall inside the window (this reproduces the monitoring system's figures)
    outs = [t for t in txs if t.direction == "out" and not (exclude_card and t.is_card) and in_dates(t, start, end)]
    matched: list[Transaction] = []
    for o in outs:
        if any(i.booked_at <= o.booked_at <= i.booked_at + horizon for i in inflows):
            matched.append(o)
    # FIFO allocation
    pool: list[list] = []  # [inflow, remaining]
    allocated = ZERO
    links = []
    events = sorted([(i.booked_at, 0, i) for i in inflows] + [(o.booked_at, 1, o) for o in matched],
                    key=lambda e: (e[0], e[1], e[2].transaction_id))
    for when, kind, t in events:
        if kind == 0:
            pool.append([t, t.amount_gbp])
            continue
        need = t.amount_gbp
        for entry in pool:
            inflow, remaining = entry
            if remaining <= 0 or not (inflow.booked_at <= when <= inflow.booked_at + horizon):
                continue
            take = min(need, remaining)
            entry[1] -= take
            need -= take
            allocated += take
            links.append({"inflow_id": inflow.transaction_id, "outflow_id": t.transaction_id, "allocated_gbp": take})
            if need == 0:
                break
    received = total(inflows)
    paid = total(matched)
    return {
        "method": f"outflows (card excluded={exclude_card}) dated {start}..{end} and booked within {hours}h after an inflow "
                  f"dated in the same window",
        "received_gbp": received, "inflow_ids": [t.transaction_id for t in inflows],
        "outflows_within_hours_gbp": paid, "outflow_ids": [t.transaction_id for t in matched],
        "share_outflows_within_hours": ratio(paid, received),
        "share_capped": min(Decimal(1), ratio(paid, received)) if received else None,
        "fifo_allocated_gbp": allocated, "fifo_share": ratio(allocated, received),
        "fifo_links": links,
        "caveat": "Allocation is arithmetic only; it does not show that a particular inflow funded a particular outflow.",
    }


def rapid_movement_days_investment(txs: list[Transaction], start: date, end: date, days: int) -> dict:
    """Investment TM02: deposits in the window, withdrawals within ``days`` of a deposit, and money invested."""
    txs = sort_tx(txs)
    deposits = [t for t in txs if t.direction == "in" and t.channel not in ("fund_redemption",) and in_dates(t, start, end)]
    horizon = timedelta(days=days)
    withdrawals = [t for t in txs if t.direction == "out" and t.channel not in ("fund_subscription",)
                   and any(d.booked_at <= t.booked_at <= d.booked_at + horizon for d in deposits)]
    invested = [t for t in txs if t.channel == "fund_subscription" and in_dates(t, start, end)]
    received = total(deposits)
    return {
        "method": f"deposits dated {start}..{end}; withdrawals (not fund purchases) within {days} days of a deposit; "
                  f"fund purchases dated in the window",
        "received_gbp": received, "deposit_ids": [t.transaction_id for t in deposits],
        "withdrawn_gbp": total(withdrawals), "withdrawal_ids": [t.transaction_id for t in withdrawals],
        "withdrawn_share": ratio(total(withdrawals), received),
        "invested_gbp": total(invested), "investment_ids": [t.transaction_id for t in invested],
        "invested_share": ratio(total(invested), received),
    }


# --------------------------------------------------------------------------- TM03 helpers
def clusters(txs: list[Transaction], days: int, min_count: int) -> list[dict]:
    """Maximal groups of ``txs`` that fit within a ``days``-day span and number at least ``min_count``."""
    txs = sort_tx(txs)
    out: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    for i, first in enumerate(txs):
        group = [t for t in txs[i:] if (t.booked_date - first.booked_date).days < days]
        ids = tuple(t.transaction_id for t in group)
        if len(group) >= min_count and not any(set(ids) <= set(s) for s in seen):
            seen.add(ids)
            out.append({
                "count": len(group), "total_gbp": total(group),
                "first_date": group[0].booked_date, "last_date": group[-1].booked_date,
                "span_days": (group[-1].booked_date - group[0].booked_date).days + 1,
                "transaction_ids": list(ids),
                "amounts": [t.amount_gbp for t in group],
                "locations": sorted({t.location for t in group if t.location}),
                "counterparties": sorted({t.counterparty_name for t in group if t.counterparty_name}),
            })
    return out


# --------------------------------------------------------------------------- TM07 helpers
def dormancy(all_txs: list[Transaction], before: date, min_gap_days: int, onboarded_on: date | None) -> dict:
    """Largest inactivity gap ending on or before ``before`` (the alert date)."""
    txs = [t for t in sort_tx(all_txs) if t.booked_date <= before]
    best = None
    for prev, nxt in zip(txs, txs[1:]):
        gap = (nxt.booked_date - prev.booked_date).days
        if best is None or gap > best["gap_days"]:
            best = {"last_activity_id": prev.transaction_id, "last_activity_date": prev.booked_date,
                    "reactivation_id": nxt.transaction_id, "reactivation_date": nxt.booked_date, "gap_days": gap}
    earliest = txs[0].booked_date if txs else None
    if best and best["gap_days"] >= min_gap_days:
        status = "confirmed_by_history"
    elif txs and len(txs) == len([t for t in txs if t.booked_date >= (before - timedelta(days=min_gap_days))]) \
            and (onboarded_on is None or onboarded_on < before - timedelta(days=min_gap_days)):
        status = "insufficient_history"
    else:
        status = "not_observed"
    return {"status": status, "earliest_transaction_date": earliest, "onboarded_on": onboarded_on,
            "longest_gap": best, "threshold_days": min_gap_days}


def month_equivalent(total_gbp: Decimal, days: int) -> Decimal | None:
    """Scale a total to a 30-day equivalent (labelled heuristic)."""
    if days <= 0:
        return None
    return money(total_gbp * Decimal(30) / Decimal(days))


def group_by(txs: Iterable[Transaction], key: Callable[[Transaction], str]) -> dict[str, list[Transaction]]:
    out: dict[str, list[Transaction]] = defaultdict(list)
    for t in txs:
        out[key(t)].append(t)
    return out

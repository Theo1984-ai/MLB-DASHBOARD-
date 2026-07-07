"""
Shared merge-with-persistence helper for tracker snapshots.

Every tracker that writes today's picks to `<name>_history/YYYY-MM-DD.json`
used to overwrite the file on each cron run. That meant any pick that
appeared in a morning scan but no longer qualified at the afternoon scan
was silently lost — the user only ever saw the LATEST scan's snapshot.

This module provides a merge function that:
  - reads the existing file (if any)
  - unions its picks with the new scan's picks
  - dedupes by a caller-provided key function
  - for each re-appearing pick, increments `n_appearances`, updates
    `last_seen_at`, appends to `skew_history` (or an analogous list),
    and refreshes rolling fields (edge / price / probability) with the
    latest scan's values so downstream display shows fresh state
  - preserves `first_seen_at` from the first sighting

The `race_safe_git_pull()` helper pulls the latest remote version of
the repo before reading — protects against two concurrent GitHub
Actions runners silently overwriting each other's saved picks.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def race_safe_git_pull(cwd):
    """When running inside GitHub Actions, pull latest remote before
    reading local files. Prevents runner A + runner B concurrent
    overwrite race. Silent no-op outside CI or on failure."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    try:
        subprocess.run(
            ["git", "pull", "--rebase", "-X", "theirs", "origin", "main"],
            cwd=cwd, capture_output=True, text=True, timeout=60,
        )
    except Exception:
        pass


def load_existing(path):
    """Read existing snapshot picks. Returns [] if file missing/broken.
    Tolerates git merge-conflict markers by stripping them."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        try:
            return json.loads(raw).get("picks", []) or []
        except Exception:
            pass
        if "<<<<<<<" in raw or "=======" in raw or ">>>>>>>" in raw:
            out, skip = [], False
            for line in raw.split("\n"):
                if line.startswith("<<<<<<<"): skip = True; continue
                if line.startswith("======="): skip = False; continue
                if line.startswith(">>>>>>>"): continue
                if skip: continue
                out.append(line)
            return json.loads("\n".join(out)).get("picks", []) or []
        return []
    except Exception:
        return []


def merge_picks(existing, new_picks, key_fn,
                rolling_fields=None,
                history_fields=None):
    """
    Merge `new_picks` into `existing`. Returns the merged list.

    Parameters
    ----------
    existing : list of dict
        The saved picks from the file (may be empty).
    new_picks : list of dict
        Freshly scanned picks.
    key_fn : callable(pick) -> hashable
        Function that returns a unique key per pick. Picks with the same
        key are considered "same signal seen again."
    rolling_fields : iterable of str, optional
        Field names to overwrite from the new scan on re-hit (so display
        shows the most recent value). Non-None values only.
    history_fields : dict{str: str}, optional
        Fields to snapshot into `skew_history` per scan. Format:
        {history_key: source_field_name}. Example:
        {"edge": "edge_pp", "price": "best_price"} would produce
        history entries like {"t": iso, "edge": 5.2, "price": -125}.

    Returns
    -------
    list of dict — merged pick list with n_appearances etc. annotated.
    """
    rolling_fields = list(rolling_fields or [])
    history_fields = dict(history_fields or {})
    now_iso = datetime.now(tz=EASTERN).isoformat()

    by_key = {}
    for p in existing:
        try:
            k = key_fn(p)
        except Exception:
            continue
        if k is None: continue
        by_key[k] = p

    added = re_hit = 0
    for p in new_picks:
        try:
            k = key_fn(p)
        except Exception:
            continue
        if k is None: continue

        # Build the per-scan history entry
        hist_entry = {"t": now_iso}
        for h_key, src_field in history_fields.items():
            hist_entry[h_key] = p.get(src_field)

        if k not in by_key:
            # First sighting
            p["n_appearances"] = 1
            p["first_seen_at"] = now_iso
            p["last_seen_at"]  = now_iso
            p["scan_history"]  = [hist_entry]
            by_key[k] = p
            added += 1
        else:
            prev = by_key[k]
            prev["n_appearances"] = (prev.get("n_appearances") or 1) + 1
            prev["last_seen_at"]  = now_iso
            prev.setdefault("first_seen_at",
                            prev.get("captured_at") or now_iso)
            hist = prev.get("scan_history") or []
            hist.append(hist_entry)
            prev["scan_history"] = hist
            # Update rolling fields with fresh values so display is current
            for field in rolling_fields:
                if p.get(field) is not None:
                    prev[field] = p[field]
            # Preserve any settlement result already recorded — don't clobber
            # WIN/LOSS from an intra-day settler run.
            re_hit += 1

    return list(by_key.values()), added, re_hit


def summary(picks):
    """Compute the W/L/ROI summary dict used by Data Status page."""
    wins = sum(1 for p in picks if p.get("result") == "WIN")
    losses = sum(1 for p in picks if p.get("result") == "LOSS")
    pushes = sum(1 for p in picks if p.get("result") == "PUSH")
    voids = sum(1 for p in picks if p.get("result") == "VOID")
    settled = wins + losses + pushes
    risk = profit = 0.0
    for p in picks:
        r = p.get("result")
        if r not in ("WIN", "LOSS", "PUSH"):
            continue
        am = (p.get("best_price") or p.get("best_odds")
              or p.get("price") or 0)
        if not am: continue
        if am > 0: risk_i, payout = 100, am
        else:      risk_i, payout = abs(am), 100
        risk += risk_i
        if r == "WIN":   profit += payout
        elif r == "LOSS": profit -= risk_i
    return {
        "n_total":      len(picks),
        "n_settled":    settled,
        "n_void":       voids,
        "wins":         wins,
        "losses":       losses,
        "pushes":       pushes,
        "hit_rate":     round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0,
        "risk_total":   round(risk, 2),
        "profit_total": round(profit, 2),
        "roi_pct":      round(profit / risk * 100, 2) if risk else 0,
    }

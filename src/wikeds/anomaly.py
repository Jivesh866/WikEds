"""Z-score anomaly spotting on raw events. Pure pandas, no database in here.

Input is a frame with: event_time (unix s), wiki, type, title, user, bot,
old_len, new_len, namespace. Three detectors:

  rate_spikes       a wiki editing much faster than its own recent normal
  page_bursts       one page getting many edits from several people (breaking news)
  suspicious_edits  unusually big removals or additions (vandalism, spam)

Needs some history to compare against, so nothing fires until there are
`min_baseline_min` minutes of data (matters right after a cloud restart).
"""
import numpy as np
import pandas as pd

# logged-out editors show up as an IP address
_IP = r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f]*:[0-9A-Fa-f:]+)$"

RATE_COLS = ["wiki", "edits_recent", "recent_per_min", "baseline_per_min", "z"]
BURST_COLS = ["wiki", "title", "edits", "editors", "expected", "z", "net_bytes", "last_edit"]
SUS_COLS = ["edit_time", "wiki", "title", "user", "bytes_changed", "z", "anon", "reason"]


def _empty(cols):
    return pd.DataFrame(columns=cols)


def _num(s):
    """Nullable ints -> plain floats (NA becomes NaN)."""
    return pd.to_numeric(s, errors="coerce").astype("float64")


def is_anon(users):
    return users.fillna("").astype(str).str.match(_IP)


def rate_spikes(df, now, recent_min=5, baseline_min=120, min_baseline_min=30,
                min_z=4.0, min_edits=25):
    """Wikis whose last few minutes are far above their own per-minute baseline.

    z is for the mean of the recent minutes, so it's (recent - base) / (std / sqrt(n)).
    The std never drops below a Poisson floor, otherwise a dead-quiet wiki gets
    huge z scores from a handful of edits.
    """
    if df.empty:
        return _empty(RATE_COLS)
    end = (int(now) // 60) * 60                  # skip the partial minute
    r_lo = end - recent_min * 60
    data_lo = (int(df["event_time"].min()) // 60) * 60
    b_lo = max(r_lo - baseline_min * 60, data_lo)
    n_base = (r_lo - b_lo) // 60
    if n_base < min_baseline_min:
        return _empty(RATE_COLS)

    d = df[(df["bot"] == 0) & (df["event_time"] >= b_lo) & (df["event_time"] < end)]
    grid = np.arange(b_lo, end, 60)
    rows = []
    for wiki, g in d.groupby("wiki"):
        counts = ((g["event_time"] // 60) * 60).value_counts().reindex(grid, fill_value=0)
        counts = counts.to_numpy(dtype=float)
        base, recent = counts[:n_base], counts[n_base:]
        if recent.sum() < min_edits:
            continue
        mean = base.mean()
        std = max(base.std(ddof=1), np.sqrt(max(mean, 0.1)))
        z = (recent.mean() - mean) / (std / np.sqrt(recent_min))
        if z >= min_z:
            rows.append((wiki, int(recent.sum()), round(recent.mean(), 1), round(mean, 1), round(z, 1)))
    if not rows:
        return _empty(RATE_COLS)
    return pd.DataFrame(rows, columns=RATE_COLS).sort_values("z", ascending=False, ignore_index=True)


def page_bursts(df, now, recent_min=15, baseline_min=120, min_baseline_min=30,
                min_edits=6, min_editors=3, min_z=5.0, articles_only=True):
    """Pages with many human edits from several people in a short time.

    Count z-score against what the page normally gets (Poisson: (k - lam) / sqrt(lam)),
    with a small floor on lam so unseen pages don't divide by zero. Needing several
    different editors is what separates news from one person fiddling with a page.
    """
    if df.empty:
        return _empty(BURST_COLS)
    now = int(now)
    r_lo = now - recent_min * 60
    b_lo = max(r_lo - baseline_min * 60, int(df["event_time"].min()))
    base_min = (r_lo - b_lo) / 60
    if base_min < min_baseline_min:
        return _empty(BURST_COLS)

    d = df[(df["bot"] == 0) & df["type"].isin(["edit", "new"])]
    if articles_only and "namespace" in d:
        d = d[pd.to_numeric(d["namespace"], errors="coerce").fillna(-1) == 0]
    recent = d[d["event_time"] >= r_lo]
    if recent.empty:
        return _empty(BURST_COLS)
    base = d[(d["event_time"] >= b_lo) & (d["event_time"] < r_lo)]

    new_len, old_len = _num(recent["new_len"]), _num(recent["old_len"]).fillna(0)
    recent = recent.assign(_delta=(new_len - old_len).fillna(0))
    g = recent.groupby(["wiki", "title"]).agg(
        edits=("event_time", "size"), editors=("user", "nunique"),
        net_bytes=("_delta", "sum"), last_edit=("event_time", "max"),
    )
    base_n = base.groupby(["wiki", "title"]).size().reindex(g.index, fill_value=0)
    g["expected"] = np.maximum(base_n * recent_min / base_min, 0.05)
    g["z"] = (g["edits"] - g["expected"]) / np.sqrt(g["expected"])

    g = g[(g["edits"] >= min_edits) & (g["editors"] >= min_editors) & (g["z"] >= min_z)]
    if g.empty:
        return _empty(BURST_COLS)
    g = g.reset_index()
    g["expected"], g["z"] = g["expected"].round(2), g["z"].round(1)
    g["net_bytes"] = g["net_bytes"].astype("int64")
    return g[BURST_COLS].sort_values("z", ascending=False, ignore_index=True)


def suspicious_edits(df, now, minutes=60, min_z=6.0, min_bytes=2000, min_samples=30):
    """Human edits that are way outside the normal edit size for their wiki.

    Robust z-score (median / MAD, so the outliers don't skew their own baseline).
    Flags: page blanked, large removal, large addition by an anonymous editor.
    Blanking doesn't need the z-score, so it also works on quiet wikis.
    """
    if df.empty:
        return _empty(SUS_COLS)
    d = df[(df["bot"] == 0) & (df["type"] == "edit")
           & (df["event_time"] >= int(now) - minutes * 60)].copy()
    d["old"], d["new"] = _num(d["old_len"]).fillna(0), _num(d["new_len"])
    d = d[d["new"].notna()]
    if d.empty:
        return _empty(SUS_COLS)

    d["delta"] = d["new"] - d["old"]
    by_wiki = d.groupby("wiki")["delta"]
    med = by_wiki.transform("median")
    mad = (d["delta"] - med).abs().groupby(d["wiki"]).transform("median")
    scale = (1.4826 * mad).clip(lower=20)               # 20 bytes floor
    d["z"] = ((d["delta"] - med) / scale).where(by_wiki.transform("size") >= min_samples)

    d["anon"] = is_anon(d["user"])
    blanked = (d["old"] >= 1000) & (d["new"] <= 0.1 * d["old"])
    removal = (d["z"] <= -min_z) & (d["delta"] <= -min_bytes)
    anon_add = (d["z"] >= min_z) & (d["delta"] >= min_bytes) & d["anon"]
    d["reason"] = np.select([blanked, removal, anon_add],
                            ["page blanked", "large removal", "large addition by anonymous"],
                            default="")
    d = d[d["reason"] != ""]
    if d.empty:
        return _empty(SUS_COLS)
    d = d.assign(bytes_changed=d["delta"].astype("int64"), z=d["z"].round(1), edit_time=d["event_time"])
    order = d["bytes_changed"].abs().sort_values(ascending=False).index
    return d.loc[order, SUS_COLS].reset_index(drop=True)
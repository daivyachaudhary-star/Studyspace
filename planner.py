"""StudySpace study-gap planner.

Finds the free time left in a student's week after sleep, school, sport (and anything else
they add), then fits study blocks for upcoming tests and homework into those gaps.

The rules are plain Python on purpose, so the plan can be explained and tested:
  * Times are minutes after midnight (0 to 1440).
  * A study block is never placed inside sleep, school, sport or "other" time.
  * A 60 minute wind-down before bed, 30 minutes after waking and a 30 minute rest after sport are also kept free.
  * Blocks are 30 to 90 minutes long, with a 10 minute break after each one.
"""
import datetime
import math

DAY_MIN = 1440
WIND_DOWN_BEFORE_BED = 60
REST_AFTER_SPORT = 30
WAKE_UP_BUFFER = 30
MIN_BLOCK = 30
MAX_BLOCK = 90
BREAK_AFTER_BLOCK = 10
HOMEWORK_MIN = 45
TEST_TOTAL_MIN = 180
TEST_SESSIONS = 3

KINDS = ("sleep", "school", "sport", "other")

DEFAULT_COMMITMENTS = {
    "sleep": (23 * 60, 7 * 60),
    "school": [(d, 8 * 60 + 30, 15 * 60 + 30) for d in range(5)],
    "sport": [],
    "other": [],
}


def fmt(m):
    m = int(m)
    return f"{m // 60:02d}:{m % 60:02d}"


def to_min(t):
    return t.hour * 60 + t.minute


def _merge(intervals):
    out = []
    for s, e in sorted(i for i in intervals if i[1] > i[0]):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _overlaps(s1, e1, s2, e2):
    return s1 < e2 and s2 < e1


def blocked_by_kind(day, c, buffers=False):
    """{kind: [(start, end), ...]} for one date. With buffers=True the wind-down and sport rest are added."""
    out = {k: [] for k in KINDS}
    bed, wake = c["sleep"]
    if bed > wake:                      # e.g. 23:00 -> 07:00, sleeping across midnight
        out["sleep"] += [(0, min(DAY_MIN, wake + WAKE_UP_BUFFER) if buffers else wake), (max(0, bed - WIND_DOWN_BEFORE_BED) if buffers else bed, DAY_MIN)]
    elif bed < wake:                    # e.g. 01:00 -> 09:00, sleeping after midnight
        out["sleep"] += [(max(0, bed - WIND_DOWN_BEFORE_BED) if buffers else bed, min(DAY_MIN, wake + WAKE_UP_BUFFER) if buffers else wake)]
    wd = day.weekday()
    for kind in ("school", "sport", "other"):
        for d, s, e in c.get(kind, []):
            if d == wd and e > s:
                extra = REST_AFTER_SPORT if (buffers and kind == "sport") else 0
                out[kind].append((s, min(DAY_MIN, e + extra)))
    return out


def free_gaps(day, c, earliest=0):
    """Free intervals (at least MIN_BLOCK long) on one date, keeping the buffers."""
    blocked = []
    for ivs in blocked_by_kind(day, c, buffers=True).values():
        blocked += ivs
    blocked.append((0, earliest))
    gaps, cur = [], 0
    for s, e in _merge(blocked):
        if s - cur >= MIN_BLOCK:
            gaps.append([cur, s])
        cur = max(cur, e)
    if DAY_MIN - cur >= MIN_BLOCK:
        gaps.append([cur, DAY_MIN])
    return gaps


def block_conflicts(day, start, end, c):
    """Which kinds of commitment a block overlaps (no buffers: only real clashes)."""
    hit = []
    for kind, ivs in blocked_by_kind(day, c, buffers=False).items():
        if any(_overlaps(start, end, s, e) for s, e in ivs):
            hit.append(kind)
    return hit


def count_conflicts(blocks, c):
    """Independent check of a finished plan.
    blocks: list of dicts with 'date' (datetime.date), 'start', 'end'.
    Returns totals, overall and per kind, so the success criterion can be measured."""
    res = {"total": len(blocks), "any": 0, "sleep": 0, "school": 0, "sport": 0, "other": 0}
    for b in blocks:
        hit = block_conflicts(b["date"], b["start"], b["end"], c)
        if hit:
            res["any"] += 1
        for k in hit:
            res[k] += 1
    return res


def _take(gaps, s, e):
    """Remove [s, e) from a list of gaps (in place)."""
    new = []
    for gs, ge in gaps:
        if not _overlaps(gs, ge, s, e):
            new.append([gs, ge])
            continue
        if gs < s and s - gs >= MIN_BLOCK:
            new.append([gs, s])
        if ge > e and ge - e >= MIN_BLOCK:
            new.append([e, ge])
    gaps[:] = new


def _place(gaps, minutes):
    """Put one block of up to `minutes` into the first gap that fits (or the largest usable one)."""
    want = min(minutes, MAX_BLOCK)
    for gs, ge in gaps:
        if ge - gs >= want:
            s, e = gs, gs + want
            _take(gaps, s, min(DAY_MIN, e + BREAK_AFTER_BLOCK))
            return s, e
    usable = [g for g in gaps if g[1] - g[0] >= MIN_BLOCK]
    if not usable:
        return None
    gs, ge = max(usable, key=lambda g: g[1] - g[0])
    s, e = gs, min(ge, gs + want)
    _take(gaps, s, min(DAY_MIN, e + BREAK_AFTER_BLOCK))
    return s, e


def build_plan(c, tasks, today, now_min=0, horizon_days=14, manual_blocks=()):
    """tasks: [{'title', 'due' (date), 'kind': 'test' | 'homework'}].
    manual_blocks: the student's own blocks [{'date', 'start', 'end'}] that must be kept free.
    Returns (plan, unscheduled). plan = [{'date', 'start', 'end', 'title', 'kind'}]."""
    gaps = {}
    for i in range(horizon_days + 1):
        d = today + datetime.timedelta(days=i)
        earliest = 0
        if i == 0:
            earliest = min(DAY_MIN, int(math.ceil((now_min + 15) / 5.0) * 5))
        gaps[d] = free_gaps(d, c, earliest)
    for mb in manual_blocks:
        if mb["date"] in gaps:
            _take(gaps[mb["date"]], mb["start"], min(DAY_MIN, mb["end"] + BREAK_AFTER_BLOCK))

    plan, unscheduled = [], []
    for t in sorted(tasks, key=lambda x: (x["due"], x["title"])):
        last = max(today, t["due"] - datetime.timedelta(days=1))
        eligible = [d for d in sorted(gaps) if today <= d <= last]
        need = TEST_TOTAL_MIN if t["kind"] == "test" else HOMEWORK_MIN
        placed = []

        def put(d, minutes):
            r = _place(gaps[d], minutes)
            if r:
                placed.append({"date": d, "start": r[0], "end": r[1], "title": t["title"], "kind": t["kind"]})
                return r[1] - r[0]
            return 0

        remaining = need
        if t["kind"] == "test" and eligible:
            k = min(TEST_SESSIONS, len(eligible))
            picks = sorted({eligible[round(i * (len(eligible) - 1) / max(1, k - 1))] for i in range(k)})
            per = min(MAX_BLOCK, int(math.ceil(need / len(picks) / 5.0) * 5))
            for d in picks:
                remaining -= put(d, min(per, remaining))
        for d in (eligible if t["kind"] != "test" else list(reversed(eligible))):
            while remaining > 0:
                got = put(d, remaining)
                if not got:
                    break
                remaining -= got
            if remaining <= 0:
                break
        plan += placed
        if remaining > 0:
            unscheduled.append({"title": t["title"], "due": t["due"], "minutes_missing": remaining})
    plan.sort(key=lambda b: (b["date"], b["start"]))
    return plan, unscheduled

"""
Trading both indices: Nifty and Sensex move together, so the same call on both on one candle is really one bet.
BOTH_SAME_SIGNAL says what to do with it:
  "sensex"  take it on Sensex only and skip Nifty
  "split"   take it on both at half the lots each
  "off"     no special handling: whichever is processed first takes it, within MAX_OPEN_POSITIONS

One index's candle can close a moment before the other's, so a signal waits here until the other index has closed
the same candle (WAIT_SECONDS at most: a stuck feed must not hold a trade back for long).
"""
WAIT_SECONDS = 15


def make(mode, names):
    return PairGate(mode, names) if mode in ("sensex", "split") and len(names) == 2 else None


def half_lots(index, lots):
    """Half in whole lots. A single lot can't be halved: Sensex keeps it and Nifty gets 0, so it is skipped."""
    return max(1, lots // 2) if index == "SENSEX" else lots // 2


class PairGate:
    def __init__(self, mode, names):
        self.mode, self.names = mode, list(names)
        self.pending, self.settled = {}, {}

    def _other(self, n):
        return next(x for x in self.names if x != n)

    def offer(self, n, bucket, sigs, now):
        self.pending[n] = dict(bucket=bucket, sigs=sigs, since=now)

    def decide(self, n, closed, now):
        """closed: index -> time of its latest closed candle. Returns (signals to try now, half size?, note)."""
        w = self.pending.get(n)
        if not w:
            return [], False, None
        o = self._other(n)
        ready = (closed.get(o) is not None and closed[o] >= w["bucket"]) or (now - w["since"]).total_seconds() >= WAIT_SECONDS
        if not ready:
            return [], False, None
        del self.pending[n]
        side = w["sigs"][0]["side"]
        theirs, settled = self.pending.get(o), self.settled.get(o)
        if theirs and theirs["bucket"] == w["bucket"]:
            same = theirs["sigs"][0]["side"] == side
        else:
            same = bool(settled and settled["bucket"] == w["bucket"] and settled["side"] == side)
        late = same and settled and settled["bucket"] == w["bucket"] and settled["taken"] and not settled["half"]
        half, note = False, None
        if late:                                 # the other index already took it at full size
            sigs, note = [], f"{n}: same {side} signal already taken on {o} — skipped"
        elif same and self.mode == "sensex" and n != "SENSEX":
            sigs, note = [], f"{n}: same {side} signal on {o} — taking {o} only, Nifty skipped"
        else:
            sigs, half = w["sigs"], same and self.mode == "split"
            if half:
                note = f"{n}: same {side} signal on {o} — half quantity on each"
        self.settled[n] = dict(bucket=w["bucket"], side=side, taken=bool(sigs), half=half)
        return sigs, half, note

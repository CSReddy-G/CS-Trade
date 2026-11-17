"""
The panel's Edit stop/target button: the panel queues new levels in a file, the running bot checks them against the
live price and applies them on its next pass, then writes back whether it did.

Same safety rules as the Exit button (manual_exit): the request names the option, a request that waited too long or
names an option the bot no longer holds is refused, so an old edit can never land on a newer trade. A level that is
already on the wrong side of the price is refused too, because it would close the trade the moment it was applied.
"""
import json
import os
import time

BOT_FILE = "levels_command.json"
SCALP_FILE = "scalp_levels_command.json"
MAX_AGE = 60  # seconds a request may wait for the bot
FIELDS = ("sl", "target", "prem_sl", "prem_target")


def _read(path):
    try:
        with open(path) as f:
            cmd = json.load(f)
        return cmd if isinstance(cmd, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, cmd):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cmd, f)
    os.replace(tmp, path)


def _fresh(cmd):
    return time.time() - cmd.get("created", 0) <= MAX_AGE


def queued(path):
    """True while an earlier request is still waiting for the bot."""
    cmd = _read(path)
    return cmd.get("status") in ("pending", "applying") and _fresh(cmd)


def request(path, symbol, levels):
    """levels: any of FIELDS -> float. Missing fields stay as they are."""
    _write(path, dict(symbol=symbol, levels={k: float(v) for k, v in levels.items() if k in FIELDS and v is not None},
                      status="pending", created=time.time()))


def read(path):
    return _read(path)


def take(path, held):
    """Bot side: the waiting request (dict with symbol and levels), if it is fresh and names a held option."""
    cmd = _read(path)
    if cmd.get("status") != "pending":
        return None
    if not _fresh(cmd):
        _write(path, dict(cmd, status="expired", msg="The bot didn't pick up the change in time."))
        return None
    if cmd.get("symbol") not in held:
        _write(path, dict(cmd, status="not held", msg=f"The bot doesn't hold {cmd.get('symbol')} any more."))
        return None
    _write(path, dict(cmd, status="applying"))
    return cmd


def finish(path, ok, msg):
    """Bot side: record what happened to the request taken with take()."""
    cmd = _read(path)
    _write(path, dict(cmd, status="done" if ok else "rejected", msg=msg, finished=time.time()))


def check(up, now, sl=None, target=None, what="price"):
    """Why these levels can't be used right now, or None if they can.

    up: True when a higher `now` is good for the trade (a call on the index, or any option premium).
    A stop must sit on the losing side of the current price and a target on the winning side."""
    d = 1 if up else -1
    for name, v in (("Stop-loss", sl), ("Target", target)):
        if v is not None and (v != v or v <= 0):
            return f"{name} must be a positive number."
    if now is None:
        return f"No live {what} yet, so the change can't be checked. Try again in a moment."
    if sl is not None and (now - sl) * d <= 0:
        return f"Stop-loss {sl:g} is already {'at or above' if up else 'at or below'} the {what} ({now:g}); it would exit at once."
    if target is not None and (target - now) * d <= 0:
        return f"Target {target:g} is already {'at or below' if up else 'at or above'} the {what} ({now:g}); it would exit at once."
    return None

"""
The panel's Exit button: the panel queues a request in a file, the running bot acts on it on its next pass.

The request names the option. A request that waited too long (bot busy or restarted, market closed) or that names an
option the bot no longer holds is refused, so an old click can never close a newer trade.
"""
import json
import os
import time

BOT_FILE = "exit_command.json"
SCALP_FILE = "scalp_exit_command.json"
MAX_AGE = 60  # seconds a request may wait for the bot


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
    return cmd.get("status") == "pending" and _fresh(cmd)


def request(path, symbol):
    _write(path, dict(symbol=symbol, status="pending", created=time.time()))


def take(path, held):
    """Bot side: the option to exit now, if a valid request is waiting. held = the option symbols it holds."""
    cmd = _read(path)
    if cmd.get("status") != "pending":
        return None
    if not _fresh(cmd):
        _write(path, dict(cmd, status="expired"))
        return None
    if cmd.get("symbol") not in held:
        _write(path, dict(cmd, status="not held"))
        return None
    _write(path, dict(cmd, status="exiting"))
    return cmd["symbol"]


def settle(path, held):
    """Bot side: mark the request done once the option is fully sold."""
    cmd = _read(path)
    if cmd.get("status") == "exiting" and cmd.get("symbol") not in held:
        _write(path, dict(cmd, status="done"))

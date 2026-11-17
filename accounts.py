"""
Control-panel users. Each user has a folder under users/ holding their own settings, Zerodha login,
trades, logs and bot state, so the same bot runs separately for each person on their own Kite account.
Passwords are stored hashed in users/users.json. Sign-in sessions live in memory: restarting the app
signs everyone out (their bots keep running).
"""
import json, os, secrets, shutil, threading, time

from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
USERS_DIR = os.environ.get("KITEBOT_USERS_DIR") or os.path.join(BASE, "users")
USERS_FILE = os.path.join(USERS_DIR, "users.json")
DEFAULT_USERS = ["Sai Chand", "Chandara", "Kishor"]
DEFAULT_PASSWORD = "admin@123"
MIN_PASSWORD = 8
HASH = "pbkdf2:sha256"            # this Mac's Python has no scrypt (werkzeug's default)
SESSION_IDLE = 12 * 3600          # signed out after this long without a request
MAX_FAILURES = 5                  # wrong passwords in a row before the user is locked ...
LOCKOUT = 15 * 60                 # ... for this long (stops password guessing on a public server)

# Files from before there were users belong to the first user; they're copied into their folder once.
MIGRATE_TO = "saichand"
MIGRATE_FILES = ("settings.json", "access_token.txt", "token_meta.json", "state.json", "positions.json",
                 "trades.csv", "scalp_state.json", "scalp_trades.csv", "scalp_rejections.csv",
                 "backtest_trades.csv", "scalp_backtest_trades.csv", "bot.log", "scalp.log")
# New users start on paper with no Zerodha keys, so nobody trades through someone else's Kite app.
NEW_USER_SETTINGS = {"MODE": "paper", "API_KEY": "", "API_SECRET": ""}

_lock = threading.Lock()
_sessions = {}                    # token -> {"user": id, "seen": time}
_failures = {}                    # user id -> (wrong passwords in a row, time of the last one)


class LockedOut(Exception):
    pass


def user_id(name):
    """'Sai Chand', 'sai chand' and 'saichand' are the same user."""
    return "".join(c for c in str(name).lower() if c.isalnum())


def user_dir(uid):
    return os.path.join(USERS_DIR, uid)


def _load():
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(users):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(users, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, USERS_FILE)


def _write_settings(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def setup():
    """Create the default users and their folders on first run. Never touches an existing user."""
    os.makedirs(USERS_DIR, exist_ok=True)
    with _lock:
        users = _load()
        changed = False
        for name in DEFAULT_USERS:
            uid = user_id(name)
            if uid not in users:
                users[uid] = dict(name=name, pw=generate_password_hash(DEFAULT_PASSWORD, method=HASH), default_pw=True)
                changed = True
            d = user_dir(uid)
            if os.path.isdir(d):
                continue
            os.makedirs(d)
            if uid == MIGRATE_TO:
                moved = [f for f in MIGRATE_FILES if os.path.exists(os.path.join(BASE, f))]
                for f in moved:
                    shutil.copy2(os.path.join(BASE, f), os.path.join(d, f))
                if moved:
                    print(f"  Copied your existing settings and history to users/{uid}/ ({name}).")
            else:
                _write_settings(os.path.join(d, "settings.json"), NEW_USER_SETTINGS)
        if changed:
            _save(users)


def ids():
    return list(_load())


def name_of(uid):
    return _load().get(uid, {}).get("name", uid)


def is_default(uid):
    return bool(_load().get(uid, {}).get("default_pw"))


def check(username, password):
    """The user's id if the password is right, else None. Raises LockedOut (with the minutes left)
    after MAX_FAILURES wrong passwords in a row, until LOCKOUT has passed."""
    uid = user_id(username)
    u = _load().get(uid)
    with _lock:
        n, last = _failures.get(uid, (0, 0))
        left = LOCKOUT - (time.time() - last)
        if n >= MAX_FAILURES and left > 0:
            raise LockedOut(max(1, round(left / 60)))
    if u and check_password_hash(u["pw"], password):
        with _lock:
            _failures.pop(uid, None)
        return uid
    if u:
        with _lock:
            _failures[uid] = ((n if n < MAX_FAILURES else 0) + 1, time.time())
    return None


def set_password(uid, current, new):
    """None on success, else the reason it was refused."""
    with _lock:
        users = _load()
        u = users.get(uid)
        if not u or not check_password_hash(u["pw"], current):
            return "Your current password is wrong."
        if len(new) < MIN_PASSWORD:
            return f"The new password needs at least {MIN_PASSWORD} characters."
        if new == current:
            return "The new password must be different from the current one."
        u.update(pw=generate_password_hash(new, method=HASH), default_pw=False)
        _save(users)
    return None


def open_session(uid):
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = {"user": uid, "seen": time.time()}
    return token


def session_user(token):
    """The signed-in user for this token, or None if it's unknown or idle too long."""
    with _lock:
        s = _sessions.get(token or "")
        if not s:
            return None
        if time.time() - s["seen"] > SESSION_IDLE:
            del _sessions[token]
            return None
        s["seen"] = time.time()
        return s["user"]


def close_session(token):
    with _lock:
        _sessions.pop(token, None)


def close_other_sessions(uid, keep):
    """After a password change, sign the user out everywhere else."""
    with _lock:
        for t in [t for t, s in _sessions.items() if s["user"] == uid and t != keep]:
            del _sessions[t]

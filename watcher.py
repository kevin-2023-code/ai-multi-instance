"""Background watcher: reopen the instances an app update closed.

Claude and Codex ship as MSIX packages. When an updater installs a new version
it registers the package with ForceApplicationShutdown, and Windows then kills
EVERY process of that package — the main instance and every profile alike,
since they all run the same packaged exe. Whichever instance installs the
update takes all the others down with it.

This watcher notices that pattern — instances vanish, then the package version
changes — and reopens exactly the instances that were running before.
Instances you close yourself are left closed: without a version change (or,
for Claude, without the forced-shutdown line in the profile's log) nothing is
relaunched.

Run as `launcher.pyw --watch` (started by main.py and launcher.pyw). It is
single-instance via a named mutex and exits once nothing has been running for
a few minutes. Activity goes to watcher.log next to state.json.
"""
from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import engine

POLL = 5.0             # seconds between process snapshots
UPDATE_WINDOW = 180.0  # a drop counts as an update only if the version changes within this
SETTLE = 5.0           # pause after the new version shows up, before relaunching
STAGGER = 4.0          # gap between relaunches, so N instances don't all start at once
IDLE_EXIT = 180.0      # quit after nothing has been running for this long
LOG_MAX = 256 * 1024

MAIN = "<main>"  # the default-slot instance; '<' can never be in a profile name
MUTEX_NAME = "Local\\ai-multi-instance-watcher"
ERROR_ALREADY_EXISTS = 183
# What Claude logs when Windows force-closes it for a package update.
CLAUDE_FORCED_CLOSE = "Windows session ending (close-app)"


def _log(msg: str) -> None:
    try:
        if engine.WATCHER_LOG.exists() and engine.WATCHER_LOG.stat().st_size > LOG_MAX:
            engine.WATCHER_LOG.write_text("", encoding="utf-8")
        with engine.WATCHER_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass


def _single_instance() -> int | None:
    """Hold a named mutex for the watcher's lifetime. None = another watcher
    already runs."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    handle = k32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return None
    return handle  # never closed: the OS releases it when the process exits


def _label(names: set[str]) -> str:
    return ", ".join("main instance" if n == MAIN else n
                     for n in sorted(names, key=lambda n: (n != MAIN, n.lower())))


def _snapshot(app: engine.App) -> set[str]:
    profiles, main_running = engine.running_instances(app)
    names = set(profiles)
    if main_running:
        names.add(MAIN)
    return names


def _forced_close_logged(app: engine.App, names: set[str], since: datetime) -> bool:
    """For Claude, confirm from the profiles' own logs that Windows force-closed
    them (rather than the user quitting) at or after `since`. Other apps, or a
    drop of the main instance alone, have no log we can rely on: assume yes and
    let the version change decide."""
    profiles = names - {MAIN}
    if app is not engine.CLAUDE or not profiles:
        return True
    for name in profiles:
        log = engine.profiles_dir(app) / name / "logs" / "main.log"
        try:
            with log.open("rb") as f:
                f.seek(max(0, log.stat().st_size - 64 * 1024))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in tail.splitlines():
            if CLAUDE_FORCED_CLOSE not in line:
                continue
            try:
                at = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if at >= since:
                return True
    return False


def _relaunch(app: engine.App, names: set[str]) -> None:
    exe = engine.find_app_exe(refresh=True, app=app)
    order = sorted(names, key=lambda n: (n != MAIN, n.lower()))
    for i, name in enumerate(order):
        if i:
            time.sleep(STAGGER)
        if name != MAIN and not (engine.profiles_dir(app) / name).is_dir():
            _log(f"{app.display}: '{name}' no longer exists — skipped")
            continue
        try:
            if name == MAIN:
                engine.launch_default_slot(exe, app=app)
            else:
                engine.launch_profile(name, exe, app=app)
            _log(f"{app.display}: reopened {_label({name})}")
        except (OSError, RuntimeError) as e:
            _log(f"{app.display}: could not reopen {_label({name})}: {e}")


@dataclass
class _Track:
    version: str
    running: set[str] = field(default_factory=set)
    lost: set[str] = field(default_factory=set)
    lost_at: float = 0.0              # monotonic time of the latest drop
    lost_since: datetime = field(default_factory=datetime.now)  # wall time of the first


def _step(app: engine.App, t: _Track, now: float) -> None:
    running = _snapshot(app)
    dropped = t.running - running
    if dropped:
        if not t.lost:
            # Log lines carry whole seconds and the drop is seen up to one poll
            # late, so look a little before it.
            t.lost_since = datetime.now() - timedelta(seconds=POLL + 30)
        t.lost |= dropped
        t.lost_at = now
        _log(f"{app.display}: {_label(dropped)} closed — watching for an update")
    t.running = running
    if not t.lost:
        return

    version = engine.app_version(engine.find_app_exe(refresh=True, app=app))
    if version and version != t.version:
        _log(f"{app.display}: updated {t.version or '?'} -> {version}")
        t.version = version
        lost, t.lost = t.lost, set()
        if not _forced_close_logged(app, lost, t.lost_since):
            _log(f"{app.display}: {_label(lost)} were closed normally — not reopening")
            return
        time.sleep(SETTLE)
        todo = lost - _snapshot(app)
        if todo:
            _relaunch(app, todo)
        t.running = _snapshot(app)
    elif now - t.lost_at > UPDATE_WINDOW:
        _log(f"{app.display}: no update followed — {_label(t.lost)} stay closed")
        t.lost.clear()


def run() -> int:
    if not engine.auto_relaunch_enabled():
        return 0
    if _single_instance() is None:
        return 0
    _log("watcher started")
    tracks: dict[str, _Track] = {}
    idle_since = time.monotonic()
    while True:
        now = time.monotonic()
        busy = False
        for app in engine.APPS.values():
            t = tracks.get(app.key)
            if t is None:
                exe = engine.find_app_exe(app=app)
                if exe is None:
                    continue  # app not installed
                t = tracks[app.key] = _Track(engine.app_version(exe))
            try:
                _step(app, t, now)
            except Exception as e:  # never let one bad poll kill the watcher
                _log(f"{app.display}: poll failed: {e!r}")
            busy = busy or bool(t.running or t.lost)
        if busy:
            idle_since = now
        elif now - idle_since > IDLE_EXIT:
            _log("nothing running — watcher exiting")
            return 0
        time.sleep(POLL)


if __name__ == "__main__":
    raise SystemExit(run())

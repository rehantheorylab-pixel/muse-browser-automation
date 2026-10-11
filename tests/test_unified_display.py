"""Tests for the unified virtual-monitor architecture.

Virtual monitors are PRIMARY display isolation; virtual desktops are the
fallback. All Win32/dxcam/WSL calls are faked — runs on Linux.
"""
import os
import sys
import time

import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import rehan.virtual_monitor as vm
import rehan.agent_hierarchy as ah
import rehan.workers as workers
import rehan.linux_worker as lw
import rehan.lanerouter as lr


# ---------------------------------------------------------------- fakes

class FakeScreen:
    def __init__(self, name="VDD0"):
        self.monitor = {"name": name, "x": 1920, "y": 0,
                        "w": 1920, "h": 1080}
        self.started = False
        self.grabs = 0

    def start(self):
        self.started = True

    def grab(self):
        self.grabs += 1
        return Image.new("RGB", (64, 48), (10, 20, 30))

    def stop(self):
        self.started = False


class FakePool:
    """Mimics rehan.virtual_monitor.MonitorPool."""

    def __init__(self, n=2, fail_claim=False):
        self._free = [FakeScreen(f"VDD{i}") for i in range(n)]
        self._claimed = {}
        self.released = []
        self._fail_claim = fail_claim

    def refresh(self):
        return []

    def claim(self, agent_id):
        if self._fail_claim:
            raise vm.VirtualMonitorError("no free virtual monitor")
        if agent_id in self._claimed:
            return self._claimed[agent_id]
        if not self._free:
            raise vm.VirtualMonitorError("no free virtual monitor")
        s = self._free.pop(0)
        s.start()
        self._claimed[agent_id] = s
        return s

    def release(self, agent_id):
        s = self._claimed.pop(agent_id, None)
        if s is not None:
            s.stop()
            self.released.append(agent_id)

    def status(self):
        return {"monitors": 2, "claimed": sorted(self._claimed)}


class FakeDesktop:
    def __init__(self, name="fake", real=False):
        self.name = name
        self._real = real
        self.removed = False

    def create(self):
        return True

    def remove(self):
        self.removed = True

    def move_window_here(self, hwnd):
        return True

    def move_current_process_windows_here(self):
        return 0

    @property
    def is_real(self):
        return self._real


# ---------------------------------------------------------------- virtual_monitor helpers

def test_vmonitor_enabled_env(monkeypatch):
    monkeypatch.delenv("S3REHAN_NO_VMONITOR", raising=False)
    assert vm.vmonitor_enabled() is True
    monkeypatch.setenv("S3REHAN_NO_VMONITOR", "1")
    assert vm.vmonitor_enabled() is False


def test_monitors_available_never_raises(monkeypatch):
    monkeypatch.setenv("S3REHAN_NO_VMONITOR", "1")
    assert vm.monitors_available() is False
    monkeypatch.delenv("S3REHAN_NO_VMONITOR", raising=False)
    # off-Windows here: vdd_installed() -> False, no raise
    assert vm.monitors_available() is False


def test_pil_to_png():
    png = vm.pil_to_png(Image.new("RGB", (8, 8)))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------- Agent: monitor-first

def _agent(**kw):
    kw.setdefault("desktop", FakeDesktop())
    return ah.Agent(**kw)


def test_agent_claims_monitor_from_pool():
    pool = FakePool(n=2)
    a = _agent(name="a1", monitor_pool=pool)
    a.start()
    assert a.display_backend == "monitor"
    assert a.screen is not None
    assert a.status()["display_backend"] == "monitor"
    assert a.status()["has_monitor"] is True
    a.cleanup()
    assert pool.released == ["a1"]
    assert a.screen is None


def test_agent_screenshot_uses_monitor_grab():
    pool = FakePool(n=1)
    a = _agent(name="a2", monitor_pool=pool)
    a.start()
    png = a.screenshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert a.screen.grabs == 1
    # cached path: no second grab
    assert a.screenshot(fresh=False) == png
    assert a.screen.grabs == 1
    a.cleanup()


def test_agent_falls_back_to_desktop_when_claim_fails():
    pool = FakePool(n=1, fail_claim=True)
    a = _agent(name="a3", monitor_pool=pool)
    a.start()
    assert a.display_backend == "shim"  # FakeDesktop is not real
    assert a.screen is None
    assert a.status()["has_monitor"] is False
    a.cleanup()


def test_agent_auto_creates_pool_when_driver_present(monkeypatch):
    monkeypatch.setattr(vm, "monitors_available", lambda: True)
    monkeypatch.setattr(vm, "MonitorPool", FakePool)
    a = _agent(name="a4")
    a.start()
    assert a.display_backend == "monitor"
    a.cleanup()


def test_agent_no_monitor_when_disabled(monkeypatch):
    monkeypatch.setattr(vm, "monitors_available", lambda: False)
    a = _agent(name="a5")
    a.start()
    assert a.display_backend == "shim"
    a.cleanup()


def test_agent_prefer_monitor_false_skips_pool(monkeypatch):
    monkeypatch.setattr(vm, "monitors_available", lambda: True)
    a = _agent(name="a6", prefer_monitor=False)
    a.start()
    assert a.display_backend == "shim"
    assert a.screen is None
    a.cleanup()


def test_children_share_parent_pool():
    pool = FakePool(n=3)
    root = _agent(name="root", monitor_pool=pool)
    root.start()
    c1 = root.spawn_subagent(name="c1")
    c2 = root.spawn_subagent(name="c2")
    assert c1.display_backend == "monitor"
    assert c2.display_backend == "monitor"
    assert len({c1.screen.monitor["name"],
                c2.screen.monitor["name"],
                root.screen.monitor["name"]}) == 3  # distinct monitors
    assert c1.depth == 1
    root.cleanup()  # reaps subtree, releases all three
    assert sorted(pool.released) == ["c1", "c2", "root"]


def test_agent_screenshot_falls_back_to_desktop(monkeypatch):
    pool = FakePool(n=1)
    a = _agent(name="a7", monitor_pool=pool)
    a.start()

    def boom():
        raise RuntimeError("dxcam died")

    a.screen.grab = boom
    seen = {}

    def fake_png(vd, **kw):
        seen["vd"] = vd
        return b"\x89PNG-DESKTOP"

    monkeypatch.setattr("rehan.desktop_screenshot.screenshot_png", fake_png)
    assert a.screenshot() == b"\x89PNG-DESKTOP"
    assert seen["vd"] is a.desktop
    a.cleanup()


# ---------------------------------------------------------------- Worker: monitor binding

def _noop_task(task):
    return {"ok": True}


def test_worker_screenshot_prefers_monitor():
    released = []
    w = workers.Worker(0, _noop_task, desktop=FakeDesktop(),
                       screen=FakeScreen("VDD0"),
                       screen_release=lambda: released.append(True))
    w.start()
    png = w.screenshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert w.display_backend == "monitor"
    w.stop()
    assert released == [True]
    assert w.screen is None


def test_worker_screenshot_falls_back_to_desktop(monkeypatch):
    seen = {}

    def fake_png(vd, **kw):
        seen["called"] = True
        return b"\x89PNG-DESKTOP"

    monkeypatch.setattr("rehan.desktop_screenshot.screenshot_png", fake_png)
    w = workers.Worker(1, _noop_task, desktop=FakeDesktop())
    w.start()
    assert w.screenshot() == b"\x89PNG-DESKTOP"
    assert seen["called"] is True
    assert w.display_backend == "shim"
    w.stop()


# ---------------------------------------------------------------- Dispatcher: pool ownership

def test_dispatcher_claims_monitors_for_workers(monkeypatch):
    monkeypatch.setattr(vm, "monitors_available", lambda: True)
    monkeypatch.setattr(vm, "MonitorPool", FakePool)
    d = workers.TaskDispatcher(_noop_task, n_workers=2)
    d.start()
    try:
        assert d.monitor_pool is not None
        assert all(w.screen is not None for w in d.workers)
        assert all(w.display_backend == "monitor" for w in d.workers)
        st = d.status()
        assert st["monitors_claimed"] == 2
        assert st["monitor_pool"] == {"monitors": 2, "claimed": ["worker-0",
                                                                 "worker-1"]}
    finally:
        d.shutdown()
    # screens released back to the pool on shutdown
    assert sorted(d.monitor_pool.released) == ["worker-0", "worker-1"]


def test_dispatcher_no_vmonitor_env_disables(monkeypatch):
    monkeypatch.setenv("S3REHAN_NO_VMONITOR", "1")
    monkeypatch.setattr(vm, "monitors_available", lambda: True)
    d = workers.TaskDispatcher(_noop_task, n_workers=1, use_monitors=None)
    d.start()
    try:
        assert d.use_monitors is False
        assert d.monitor_pool is None
        assert d.workers[0].screen is None
    finally:
        d.shutdown()


def test_dispatcher_explicit_use_monitors_false(monkeypatch):
    monkeypatch.setattr(vm, "monitors_available", lambda: True)
    d = workers.TaskDispatcher(_noop_task, n_workers=1, use_monitors=False)
    d.start()
    try:
        assert d.monitor_pool is None
    finally:
        d.shutdown()


def test_dispatcher_pool_exhaustion_degrades(monkeypatch):
    # Pool with 1 monitor, 2 workers: second worker falls back gracefully.
    monkeypatch.setattr(vm, "monitors_available", lambda: True)
    monkeypatch.setattr(vm, "MonitorPool", lambda: FakePool(n=1))
    d = workers.TaskDispatcher(_noop_task, n_workers=2)
    d.start()
    try:
        screens = [w.screen is not None for w in d.workers]
        assert screens == [True, False]
        assert d.workers[1].display_backend == "shim"
    finally:
        d.shutdown()


def test_pump_prefers_monitor_worker_for_gui_tasks(monkeypatch):
    monkeypatch.setattr(vm, "monitors_available", lambda: False)
    d = workers.TaskDispatcher(_noop_task, n_workers=0)
    d._started = True
    # wid 0: no screen; wid 1: screen. Both idle.
    w0 = workers.Worker(0, d._run_one, desktop=FakeDesktop())
    w1 = workers.Worker(1, d._run_one, desktop=FakeDesktop(),
                        screen=FakeScreen("VDD0"))
    w0.start()
    w1.start()
    d.workers = [w0, w1]
    try:
        res = d.submit({"id": "gui-1", "needs_gui": True}, timeout_s=10)
        assert res["ok"] is True
        assert res["worker_id"] == 1  # the monitor-backed worker
        res2 = d.submit({"id": "headless-1"}, timeout_s=10)
        assert res2["ok"] is True
        assert res2["worker_id"] == 0  # no preference without needs_gui
    finally:
        d.shutdown()


# ---------------------------------------------------------------- lanerouter.route_display

def test_route_display_headless_is_inline():
    r = lr.route_display({"task_type": "api_call"})
    assert r["backend"] == "inline"


def test_route_display_gui_prefers_monitor():
    r = lr.route_display({"needs_gui": True}, monitors_available=True,
                         vdesktop_available=True)
    assert r["backend"] == "monitor"


def test_route_display_gui_vdesktop_fallback():
    r = lr.route_display({"needs_gui": True}, monitors_available=False,
                         vdesktop_available=True)
    assert r["backend"] == "vdesktop"


def test_route_display_gui_no_isolation():
    r = lr.route_display({"needs_windows_ui": True},
                         monitors_available=False,
                         vdesktop_available=False)
    assert r["backend"] == "inline"
    assert "background-only" in r["reason"]


def test_route_display_respects_no_vmonitor_env(monkeypatch):
    monkeypatch.setenv("S3REHAN_NO_VMONITOR", "1")
    r = lr.route_display({"needs_gui": True}, monitors_available=True,
                         vdesktop_available=True)
    assert r["backend"] == "vdesktop"


def test_route_placement_combines_platform_and_display():
    r = lr.route_placement({"platform": "linux", "needs_gui": False})
    assert r["platform"] == "linux"
    assert r["display"]["backend"] == "inline"
    r2 = lr.route_placement({"platform": "windows", "needs_gui": True},
                            monitors_available=True)
    assert r2["platform"] == "windows"
    assert r2["display"]["backend"] == "monitor"


# ---------------------------------------------------------------- linux_worker + monitors

def test_wsl_worker_screen_and_screenshot():
    released = []
    w = lw.WSLWorker(7, screen=FakeScreen("VDD9"),
                     screen_release=lambda: released.append(True))
    w.start()
    assert w.display_backend == "monitor"
    png = w.screenshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w.stop()
    assert released == [True]
    assert w.screen is None


def test_wsl_worker_headless_no_screenshot():
    w = lw.WSLWorker(8)
    w.start()
    assert w.display_backend == "headless"
    assert w.screenshot() is None
    assert w.place_gui_on_monitor() == 0
    w.stop()


def test_move_wslg_windows_off_windows_returns_zero():
    # os.name != "nt" here
    assert lw.move_wslg_windows_to_monitor({}) == 0

"""Round 4 integration tests: cross-component flows.

Covers wiring between hierarchy, workers, monitors, router, and Linux
workers that unit tests exercise only in isolation. All external
dependencies (proxy, Windows APIs, WSL, VDD driver) are mocked or
shimmed — these tests verify the *wiring*, not the hardware.
"""

import os
import threading
import time

import pytest

# ---------------------------------------------------------------- helpers
def _mock_run_task(task):
    """Trivial worker callable: echoes the payload."""
    time.sleep(0.01)
    return {"ok": True, "echo": task.get("payload", {})}


def _make_org_stack():
    """Build a CEO -> Manager -> OrgAgent stack with mock executors."""
    from rehan.hierarchy import CEOAgent, Manager, OrgAgent, TaskSpec
    from rehan.hierarchy import AGENT, MANAGER, SUBAGENT

    def ok_executor(task):
        from rehan.hierarchy import TaskResult
        return TaskResult(task_id=task.id, ok=True,
                          summary=f"did: {task.title}")

    ceo = CEOAgent(name="test-ceo")
    mgr = Manager(name="test-mgr")
    ceo.hire(mgr)
    agent = OrgAgent(name="test-agent", specialist="browser",
                     executor=ok_executor)
    agent.start()
    mgr.hire(agent)
    return ceo, mgr, agent


# ============================================================ 1. hierarchy
class TestHierarchyChain:
    def test_full_chain_completes(self):
        """CEO goal -> manager -> agent -> result, all levels touched."""
        from rehan.hierarchy import TaskSpec, TaskStatus
        ceo, mgr, agent = _make_org_stack()
        try:
            result = ceo.submit_goal("build website",
                                     description="test site")
            assert result.ok
            assert "1/1" in result.summary  # 1 manager slice ok
            # Task recorded at every level.
            assert len(ceo.goals) == 1
            assert len(mgr.tasks) == 1
            assert len(agent.tasks) == 1
            goal = next(iter(ceo.goals.values()))
            assert goal.status == TaskStatus.DONE
        finally:
            agent.cleanup()

    def test_agent_spawns_subagent_with_display(self):
        """Agent -> SubAgent: child gets own display, depth enforced."""
        from rehan.hierarchy import TaskSpec, SUBAGENT, AgentDepthError
        _, _, agent = _make_org_stack()
        try:
            sub = agent.spawn_subagent(name="test-sub",
                                       specialist="vision_grounding")
            try:
                assert sub.level == SUBAGENT
                assert sub.parent is agent
                assert sub.name in [c.name for c in agent.org_children()]
                # Sub-agent executes too.
                r = sub.execute(TaskSpec(title="click signup", level=SUBAGENT))
                assert r.ok
                # Cannot go deeper.
                with pytest.raises(AgentDepthError):
                    sub.spawn_subagent(name="too-deep")
            finally:
                sub.cleanup()
        finally:
            agent.cleanup()

    def test_peer_handoff_moves_task(self):
        """Manager -> peer Manager handoff transfers ownership."""
        from rehan.hierarchy import Manager, TaskSpec, AGENT
        m1 = Manager(name="m1")
        m2 = Manager(name="m2")
        m1.add_peer(m2)
        m2.add_peer(m1)
        t = TaskSpec(title="overflow task", level=AGENT)
        m1.accept(t)
        assert t.id in m1.tasks
        peer, env = m1.handoff(t, m2, reason="load balance")
        assert peer is m2
        assert t.id not in m1.tasks  # ownership moved
        assert env.to_node == "m2"

    def test_executor_failure_becomes_failed_result_not_crash(self):
        """A raising executor -> TaskResult(ok=False), chain survives."""
        from rehan.hierarchy import OrgAgent, TaskSpec

        def boom(task):
            raise RuntimeError("simulated executor failure")

        a = OrgAgent(name="fail-agent", executor=boom)
        a.start()
        try:
            r = a.execute(TaskSpec(title="doomed", level=2))
            assert not r.ok
            assert "simulated executor failure" in r.error
        finally:
            a.cleanup()


# ============================================================ 2. workers +
# ============================================================ monitors
class TestWorkersMonitors:
    def test_dispatcher_runs_tasks(self):
        from rehan.workers import TaskDispatcher
        d = TaskDispatcher(run_task=_mock_run_task, n_workers=2,
                           n_linux_workers=0, autoscale=False,
                           use_monitors=False)
        d.start()
        try:
            r1 = d.submit({"id": "t1", "payload": {"x": 1}}, timeout_s=10)
            r2 = d.submit({"id": "t2", "payload": {"x": 2}}, timeout_s=10)
            assert r1["ok"] and r2["ok"]
            assert r1["echo"] == {"x": 1}
            assert r2["echo"] == {"x": 2}
        finally:
            d.shutdown()

    def test_dispatcher_priority_order(self):
        from rehan.workers import TaskDispatcher
        order = []
        lock = threading.Lock()
        release = threading.Event()

        def run(task):
            if task["id"] == "blocker":
                release.wait(timeout=10)
            with lock:
                order.append(task["id"])
            return {"ok": True}

        d = TaskDispatcher(run_task=run, n_workers=1, n_linux_workers=0,
                           autoscale=False, use_monitors=False)
        d.start()
        results = {}

        def sub(tid, pri):
            results[tid] = d.submit({"id": tid}, priority=pri, timeout_s=15)

        try:
            tb = threading.Thread(target=sub, args=("blocker", 0))
            tb.start()
            time.sleep(0.2)  # blocker holds the worker
            t1 = threading.Thread(target=sub, args=("low", 0))
            t2 = threading.Thread(target=sub, args=("high", 10))
            t1.start()
            t2.start()
            time.sleep(0.3)  # both enqueued
            release.set()    # free the blocker
            tb.join(timeout=15)
            t1.join(timeout=15)
            t2.join(timeout=15)
            assert results["high"]["ok"] and results["low"]["ok"]
            assert order.index("high") < order.index("low")
        finally:
            release.set()
            d.shutdown()

    def test_dispatcher_dependency_chain(self):
        from rehan.workers import TaskDispatcher

        def rec(task):
            return {"ok": True, "id": task["id"]}

        d = TaskDispatcher(rec, n_workers=2, n_linux_workers=0,
                           autoscale=False, use_monitors=False)
        d.start()
        results = {}
        errors = {}

        def sub_b():
            try:
                results["b"] = d.submit(
                    {"id": "b", "depends_on": ["a"]}, timeout_s=15)
            except Exception as e:  # noqa: BLE001
                errors["b"] = e

        try:
            t = threading.Thread(target=sub_b)
            t.start()
            time.sleep(0.2)  # b enqueued, waiting on a
            ra = d.submit({"id": "a", "payload": {}}, timeout_s=15)
            t.join(timeout=20)
            assert not errors, f"errors: {errors}"
            assert ra["ok"] is True
            assert results["b"]["ok"] is True
            assert results["b"]["dep_results"]["a"]["ok"] is True
        finally:
            d.shutdown()

    def test_monitor_pool_claim_release_cycle(self, monkeypatch):
        from rehan import virtual_monitor as vm
        # Fake two monitors; stub VirtualScreen.start/grab to avoid dxcam.
        fake_monitors = [
            {"name": "VDD0", "x": 1920, "y": 0, "w": 1920, "h": 1080},
            {"name": "VDD1", "x": 3840, "y": 0, "w": 1920, "h": 1080},
        ]
        monkeypatch.setattr(vm, "virtual_monitors",
                            lambda: list(fake_monitors))
        monkeypatch.setattr(vm.VirtualScreen, "start", lambda self: True)
        monkeypatch.setattr(vm.VirtualScreen, "stop", lambda self: None)
        pool = vm.MonitorPool()
        pool.refresh()
        assert len(pool._monitors) == 2
        s1 = pool.claim("agent-1")
        s2 = pool.claim("agent-2")
        assert s1 is not None and s2 is not None
        assert s1.monitor["name"] != s2.monitor["name"]  # distinct monitors
        # Third claim on a 2-pool raises.
        with pytest.raises(vm.VirtualMonitorError):
            pool.claim("agent-3")
        pool.release("agent-1")
        pool.release("agent-2")
        # Re-claim after release works.
        s4 = pool.claim("agent-4")
        assert s4 is not None
        pool.release("agent-4")

    def test_worker_screenshot_fallback_no_monitor(self):
        """Worker.screenshot() degrades gracefully without VDD driver."""
        from rehan.workers import TaskDispatcher
        d = TaskDispatcher(run_task=_mock_run_task, n_workers=1,
                           n_linux_workers=0, autoscale=False,
                           use_monitors=False)
        d.start()
        try:
            assert len(d.workers) >= 1
            w = d.workers[0]
            # Should not raise even with no monitor hardware.
            shot = w.screenshot(mode="smart")
            # Returns None, PNG bytes, image-like, or dict;
            # key point: no exception.
            assert (shot is None or isinstance(shot, (bytes, dict))
                    or hasattr(shot, "size"))
            # display_backend reports the fallback.
            assert w.display_backend in ("monitor", "vdesktop", "shim",
                                          "headless", "inline", "none")
        finally:
            d.shutdown()


# ============================================================ 3. routing
class TestRouting:
    def test_route_platform_explicit_wins(self):
        from rehan.linux_worker import route_platform
        assert route_platform({"platform": "linux"}) == "linux"
        assert route_platform({"platform": "windows"}) == "windows"

    def test_route_platform_windows_ui_stays_windows(self):
        from rehan.linux_worker import route_platform
        assert route_platform({"needs_windows_ui": True}) == "windows"

    def test_route_display_modes(self):
        from rehan.lanerouter import route_display
        r = route_display({"needs_gui": True}, monitors_available=True)
        assert r["backend"] == "monitor"
        r = route_display({"needs_gui": True}, monitors_available=False,
                          vdesktop_available=True)
        assert r["backend"] == "vdesktop"
        r = route_display({"needs_gui": False})
        assert r["backend"] == "inline"

    def test_route_placement_combines_platform_and_display(self):
        from rehan.lanerouter import route_placement
        p = route_placement({"platform": "linux", "needs_gui": False})
        assert p["platform"] == "linux"
        assert "display" in p

    def test_total_time_router_prefers_cache(self):
        """lanerouter picks fastest lane for a cached action."""
        from rehan.lanerouter import route_action
        route = route_action(cache_hit=True)
        assert route["lane"] == "cache"
        assert route["est_ms"] < 1000

    def test_speculative_route_suggests_parallel(self):
        from rehan.lanerouter import speculative_route
        r = speculative_route()
        assert "primary" in r
        assert "speculative" in r


# ============================================================ 4. linux
class TestLinuxWorkers:
    def test_wsl_worker_lifecycle_mocked(self):
        import os
        os.environ["S3REHAN_NO_WSL"] = "1"
        try:
            from rehan.linux_worker import WSLWorker

            def fake_run(task):
                return {"ok": True}

            w = WSLWorker(worker_id=99, run_task=fake_run)
            assert w.platform == "linux"
            assert not w.busy
            w.start()
            try:
                assert w.alive
                assert isinstance(w.heartbeat, float)
            finally:
                w.stop()
        finally:
            del os.environ["S3REHAN_NO_WSL"]

    def test_route_to_linux_for_headless_tasks(self):
        from rehan.linux_worker import route_to_linux
        assert route_to_linux({"task_type": "web_scrape"}) in (True, False)
        # Windows UI tasks never route to Linux.
        assert route_to_linux({"needs_windows_ui": True}) is False

    def test_mixed_pool_routing(self):
        """Dispatcher with Linux+Windows workers routes by platform.

        With S3REHAN_NO_WSL=1 no Linux workers exist: a Windows task
        runs fine; an explicit-Linux task fails gracefully (timeout)
        rather than running on the wrong platform or hanging forever.
        """
        import os
        os.environ["S3REHAN_NO_WSL"] = "1"
        try:
            from rehan.workers import TaskDispatcher

            win_done = {}

            def win_task(task):
                win_done[task["id"]] = True
                return {"ok": True, "platform": "windows"}

            d = TaskDispatcher(run_task=win_task, n_workers=1,
                               n_linux_workers=1, autoscale=False,
                               use_monitors=False)
            d.start()
            try:
                # No Linux workers spawned without WSL.
                kinds = {getattr(w, "platform", "windows")
                         for w in d.workers}
                assert "linux" not in kinds
                rw = d.submit({"id": "w1", "platform": "windows"},
                              timeout_s=15)
                assert rw["ok"]
                assert "w1" in win_done
                # Explicit-Linux task: graceful timeout, no crash.
                rl = d.submit({"id": "l1", "platform": "linux"},
                              timeout_s=3)
                assert rl["ok"] is False
                assert "timeout" in rl.get("error", "").lower()
            finally:
                d.shutdown()
        finally:
            if "S3REHAN_NO_WSL" in os.environ:
                del os.environ["S3REHAN_NO_WSL"]


# ============================================================ 5. browser
# (browser-automation-v2 side: router + backends wiring.
# Depth is covered by browser-automation-v2/tests/test_router.py;
# here we verify the wiring: importable, 10 types, routing tiers.)
class TestBrowserWiring:
    def _setup_path(self):
        import sys
        import types
        # Stub third-party `websockets` (module-level import in
        # obscura_backend) like browser-automation-v2/tests/conftest.py.
        if "websockets" not in sys.modules:
            sys.modules["websockets"] = types.ModuleType("websockets")
        for p in ("/home/hatch/workspace/browser-automation-v2",
                  "/home/hatch/workspace/browser-automation-v2/tests"):
            if p not in sys.path:
                sys.path.insert(0, p)

    def test_router_resolves_ten_backend_types(self):
        self._setup_path()
        from core.types import BrowserBackendType
        names = [m.name for m in BrowserBackendType]
        for expected in ("CHROME", "PLAYWRIGHT", "MOLI", "OBSCURA",
                         "CAMOUFOX", "AGENT_BROWSER", "CSI", "LIGHTPANDA",
                         "UNDETECTED", "PATCHRIGHT"):
            assert expected in names, f"missing backend type: {expected}"

    def test_router_picks_stealth_for_cloudflare(self):
        self._setup_path()
        import asyncio
        import test_router as tr
        r = tr._router()
        b = asyncio.run(r.resolve_backend(cloudflare_required=True))
        from core.types import BrowserBackendType
        assert b.backend_type in (
            BrowserBackendType.PATCHRIGHT, BrowserBackendType.UNDETECTED,
            BrowserBackendType.OBSCURA, BrowserBackendType.CAMOUFOX,
        ), f"CF routed to non-stealth: {b.backend_type}"

    def test_router_picks_chrome_for_session(self):
        self._setup_path()
        import asyncio
        import test_router as tr
        from core.types import BrowserBackendType
        # Chrome must be *connected* for session routing (not just available).
        r = tr.BrowserRouter(
            chrome=tr.FakeBackend(BrowserBackendType.CHROME, connected=True),
            obscura=tr.FakeBackend(BrowserBackendType.OBSCURA),
            playwright=tr.FakeBackend(BrowserBackendType.PLAYWRIGHT),
        )
        b = asyncio.run(r.resolve_backend(session_required=True))
        assert b.backend_type == BrowserBackendType.CHROME

    def test_router_explicit_preference(self):
        self._setup_path()
        import asyncio
        import test_router as tr
        from core.types import BrowserBackendType
        r = tr._router()
        b = asyncio.run(r.resolve_backend(preference="obscura"))
        assert b.backend_type == BrowserBackendType.OBSCURA

    def test_all_ten_backend_modules_importable(self):
        """Every backend module imports (websockets stubbed)."""
        self._setup_path()
        import importlib
        modules = [
            "core.backends.chrome_backend",
            "core.backends.playwright_backend",
            "core.backends.moli_backend",
            "core.backends.obscura_backend",
            "core.backends.camoufox_backend",
            "core.backends.agent_browser_backend",
            "core.backends.csi_backend",
            "core.backends.lightpanda_backend",
            "core.backends.undetected_backend",
            "core.backends.patchright_backend",
        ]
        for m in modules:
            importlib.import_module(m)  # raises on failure


# ============================================================ 6. end to
# ============================================================ end
class TestEndToEnd:
    def test_ceo_website_through_workers(self):
        """CEO decomposes website -> workers execute slices.

        Hierarchy decides WHAT; TaskDispatcher decides WHERE/WHEN.
        """
        from rehan.hierarchy import CEOAgent, Manager, OrgAgent
        from rehan.hierarchy import TaskSpec, TaskResult, AGENT
        from rehan.workers import TaskDispatcher

        executed = []
        lock = threading.Lock()

        def worker_run(task):
            with lock:
                executed.append(task["payload"]["title"])
            return {"ok": True}

        d = TaskDispatcher(run_task=worker_run, n_workers=2,
                           n_linux_workers=0, autoscale=False,
                           use_monitors=False)
        d.start()
        try:
            def website_executor(task: TaskSpec) -> TaskResult:
                # Agent delegates its slice to the worker pool
                # (blocking submit returns the result directly).
                res = d.submit({"id": task.id,
                                "payload": {"title": task.title}},
                               timeout_s=15)
                return TaskResult(task_id=task.id,
                                  ok=res.get("ok", False),
                                  summary=f"worker: {task.title}")

            ceo = CEOAgent(name="e2e-ceo")
            mgr = Manager(name="e2e-mgr")
            ceo.hire(mgr)
            for spec in ("browser", "file_ops"):
                a = OrgAgent(name=f"e2e-{spec}", specialist=spec,
                             executor=website_executor)
                a.start()
                mgr.hire(a)
            try:
                result = ceo.submit_goal("build website")
                assert result.ok
                assert len(executed) >= 1  # workers actually ran slices
            finally:
                for a in mgr.agents:
                    a.cleanup()
        finally:
            d.shutdown()

    def test_failure_propagates_up_chain(self):
        """Failing leaf -> manager synthesizes failure -> CEO reports it."""
        from rehan.hierarchy import CEOAgent, Manager, OrgAgent
        from rehan.hierarchy import TaskSpec, TaskResult

        def fail_executor(task):
            return TaskResult(task_id=task.id, ok=False,
                              error="leaf failed")

        ceo = CEOAgent(name="fail-ceo")
        mgr = Manager(name="fail-mgr")
        ceo.hire(mgr)
        a = OrgAgent(name="fail-agent", executor=fail_executor)
        a.start()
        mgr.hire(a)
        try:
            result = ceo.submit_goal("doomed goal")
            assert not result.ok
            assert "0/1" in result.summary
        finally:
            a.cleanup()

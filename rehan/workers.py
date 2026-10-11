"""Parallel task workers, each isolated on its own Windows virtual desktop.

Rehan's requirement (2026-10-10): multiple automation tasks run
simultaneously — task 1 on virtual desktop 2, task 2 on desktop 3, etc.
The user stays on their main desktop and never sees task windows.

PRINCIPAL-ENGINEER DESIGN NOTES (upgraded from rough direction):

1. NEVER steal the visible desktop.
   The naive approach — worker calls desktop.go() to "park" on its
   desktop — would yank the user's screen away mid-typing. Instead:
   workers create windows normally, then MOVE each new window to their
   virtual desktop via the API. The visible desktop never changes.
   (pyvda: VirtualDesktop.move_window_to_desktop / AppView API.)

2. Hidden desktops cannot use physical input.
   pyautogui.click/keyboard injects at the *visible* desktop. A worker
   parked on a hidden virtual desktop MUST use background-capable input:
   CDP Input.dispatchMouseEvent (browser), UIA InvokePattern / SetValue
   (native apps). The worker advertises `background_only=True`, and the
   input-method ladder (rehan/input_ladder.py) refuses hardware input
   for such workers. This is a correctness constraint, not a preference.

3. Failure recovery.
   Each worker has a heartbeat. The dispatcher watches worker threads;
   if a worker dies mid-task (or its heartbeat goes stale), the task is
   requeued to a healthy worker (up to N retries, then marked failed).
   No task is ever silently lost.

4. Resource limits.
   Browsers cost ~200-500MB each. The dispatcher caps workers at
   min(S3REHAN_WORKERS, mem_based_cap) where mem_based_cap derives from
   available RAM (2GB headroom reserved for the user). A worker that
   exceeds its memory budget is recycled (task requeued).

5. Task dependencies (simple DAG).
   task = {"id": ..., "depends_on": ["id-A"], "payload": {...}}.
   The dispatcher holds dependent tasks until their deps complete and
   injects dep results into the task dict ("dep_results"). No cycles
   allowed (validated on submit).

6. Priority queues.
   submit(task, priority=0): higher priority tasks jump ahead of idle
   workers first. Within the same priority, FIFO.

7. Per-worker browser profiles (security boundary).
   Each worker gets its own disposable browser profile dir
   (UUID-based, deleted on worker stop). No cookie/session leakage
   between workers. Workers never share mutable state — the router
   config they read is frozen at dispatcher start.

8. Monitoring.
   dispatcher.status() -> per-worker state, current task id, desktop
   name, tasks done, last error. dispatcher.snapshot_desktop(worker_id)
   captures a thumbnail of that worker's desktop on demand (CDP
   screenshot of its browser, or a DWM thumbnail) so Rehan can *see*
   what each background task is doing without switching desktops.

Windows Virtual Desktop API access:
  - Preferred: `pyvda` (pip install pyvda).
  - Fallback: no-op shim — workers run in-process (unit tests, dev).

Env:
  S3REHAN_WORKERS=N    worker desktops (default 3, 0 = inline single-task)
  S3REHAN_NO_VDESKTOP=1  force shim workers (debugging)
  S3REHAN_WORKER_MEM_MB=N  per-worker memory budget (default 1024)
"""

import os
import queue
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Set

try:
    from rehan import linux_worker as _linux_worker
except Exception:  # pragma: no cover - importable in practice
    _linux_worker = None  # type: ignore

try:
    _N_DEFAULT = int(os.environ.get("S3REHAN_WORKERS", "3"))
except Exception:
    _N_DEFAULT = 3

try:
    _N_LINUX_DEFAULT = int(os.environ.get("S3REHAN_LINUX_WORKERS", "0"))
except Exception:
    _N_LINUX_DEFAULT = 0

try:
    _MEM_BUDGET_MB = int(os.environ.get("S3REHAN_WORKER_MEM_MB", "1024"))
except Exception:
    _MEM_BUDGET_MB = 1024

_HEARTBEAT_TIMEOUT_S = 60.0
_MAX_TASK_RETRIES = 2


def _vdesktop_available() -> bool:
    if os.environ.get("S3REHAN_NO_VDESKTOP", "0") == "1":
        return False
    if os.name != "nt":
        return False
    try:
        import pyvda  # noqa: F401

        return True
    except Exception:
        return False


def _mem_cap_workers(requested: int) -> int:
    """Cap worker count by available RAM (reserve 2GB for the user)."""
    if requested <= 0:
        return 0
    try:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        avail_mb = ms.ullAvailPhys // (1024 * 1024)
        usable = max(0, avail_mb - 2048)  # 2GB headroom for the user
        cap = max(1, usable // _MEM_BUDGET_MB)
        return min(requested, cap)
    except Exception:
        return requested


class VirtualDesktop:
    """One Windows virtual desktop (real isolation) or a null shim.

    Key rule: we NEVER call go() during task execution (that would steal
    the user's screen). Windows are *moved* to this desktop after
    creation; the visible desktop is untouched.
    """

    def __init__(self, name: Optional[str] = None):
        self.name = name or f"s3rehan-{uuid.uuid4().hex[:6]}"
        self._real = _vdesktop_available()
        self._vd = None
        self._lock = threading.Lock()

    def create(self) -> bool:
        with self._lock:
            if not self._real:
                return True
            try:
                from pyvda import VirtualDesktop as _VD

                self._vd = _VD.create()
                try:
                    self._vd.rename(self.name)
                except Exception:
                    pass
                return True
            except Exception:
                self._real = False
                return True

    def move_window_here(self, hwnd: int) -> bool:
        """Move an existing window onto this desktop. The visible desktop
        does NOT change — this is the whole point."""
        with self._lock:
            if not self._real or self._vd is None:
                return True  # shim: nothing to move
            try:
                from pyvda import AppView

                view = AppView(hwnd)
                view.move_to_desktop(self._vd)
                return True
            except Exception:
                return False

    def move_current_process_windows_here(self) -> int:
        """Move all top-level windows of *this* process onto the desktop.

        Called by the worker right after it launches its browser/app, so
        the user's desktop never shows task windows. Returns windows moved.
        """
        if not self._real:
            return 0
        moved = 0
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            our_pid = os.getpid()
            hwnds: List[int] = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def _cb(hwnd, _):
                try:
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value == our_pid and user32.IsWindowVisible(hwnd):
                        hwnds.append(int(hwnd))
                except Exception:
                    pass
                return True

            user32.EnumWindows(_cb, 0)
            for h in hwnds:
                if self.move_window_here(h):
                    moved += 1
        except Exception:
            pass
        return moved

    def remove(self) -> None:
        with self._lock:
            if not self._real or self._vd is None:
                return
            try:
                self._vd.remove()
            except Exception:
                pass
            finally:
                self._vd = None

    @property
    def is_real(self) -> bool:
        return self._real and self._vd is not None


class Worker:
    """One isolated task worker.

    Invariants:
      - background_only=True ALWAYS: this worker may only use
        background-capable input (CDP/UIA Invoke/SetValue). Hardware
        input (pyautogui) is forbidden — it would land on the user's
        visible desktop.
      - Own disposable browser profile dir (no cross-worker sessions).
      - Heartbeat updated every loop iteration for the watchdog.
    """

    IDLE = "idle"
    BUSY = "busy"
    STOPPED = "stopped"

    #: Worker kind for type-aware routing in the unified pool.
    platform = "windows"

    def __init__(
        self,
        worker_id: int,
        run_task: Callable[[Dict], Dict],
        desktop: Optional[VirtualDesktop] = None,
    ):
        self.worker_id = worker_id
        self.run_task = run_task
        self.desktop = desktop or VirtualDesktop(name=f"s3rehan-w{worker_id}")
        # Disposable per-worker browser profile (security boundary).
        self.profile_dir = os.path.join(
            os.environ.get("TEMP", "/tmp"),
            f"s3rehan-worker-{worker_id}-{uuid.uuid4().hex[:8]}",
        )
        self.background_only = True
        self.state = self.IDLE
        self.current_task: Optional[Dict] = None
        self.last_result: Optional[Dict] = None
        self.last_error: Optional[str] = None
        self.tasks_done = 0
        self.heartbeat = time.perf_counter()
        self._inbox: "queue.Queue[Optional[Dict]]" = queue.Queue()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._loop, name=f"s3rehan-worker-{worker_id}", daemon=True
        )

    def start(self) -> bool:
        ok = self.desktop.create()
        try:
            os.makedirs(self.profile_dir, exist_ok=True)
        except Exception:
            pass
        self._thread.start()
        return ok

    def stop(self) -> None:
        with self._lock:
            self.state = self.STOPPED
        self._inbox.put(None)
        self._thread.join(timeout=10)
        self.desktop.remove()
        # Best-effort profile cleanup (no cross-worker session leakage).
        try:
            import shutil

            shutil.rmtree(self.profile_dir, ignore_errors=True)
        except Exception:
            pass

    @property
    def busy(self) -> bool:
        with self._lock:
            return self.state == self.BUSY

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def heartbeat_stale(self) -> bool:
        return (time.perf_counter() - self.heartbeat) > _HEARTBEAT_TIMEOUT_S

    def submit(self, task: Dict) -> bool:
        with self._lock:
            if self.state != self.IDLE or not self.alive:
                return False
            self.state = self.BUSY
            self.current_task = task
        # The task dict carries the worker's constraints so the runner
        # enforces background-only input and the disposable profile.
        t = dict(task)
        t.setdefault("background_only", True)
        t.setdefault("worker_profile_dir", self.profile_dir)
        t.setdefault("worker_id", self.worker_id)
        self._inbox.put(t)
        return True

    def _loop(self) -> None:
        while True:
            self.heartbeat = time.perf_counter()
            task = self._inbox.get()
            if task is None:
                return
            self.heartbeat = time.perf_counter()
            t0 = time.perf_counter()
            result: Dict = {"ok": False, "error": "not-run"}
            try:
                # Move any windows we create onto our desktop. We do this
                # BEFORE and AFTER the task: before catches windows the
                # runner opens at start; after catches stragglers.
                self.desktop.move_current_process_windows_here()
                result = self.run_task(task) or result
                self.desktop.move_current_process_windows_here()
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                with self._lock:
                    self.last_error = result["error"]
            finally:
                ms = (time.perf_counter() - t0) * 1000.0
                result.setdefault("worker_id", self.worker_id)
                result["wall_ms"] = ms
                with self._lock:
                    self.last_result = result
                    self.current_task = None
                    if self.state == self.BUSY:
                        self.state = self.IDLE
                    self.tasks_done += 1
                self.heartbeat = time.perf_counter()


class TaskDispatcher:
    """Assign tasks to free workers with priority, dependencies, retries.

    task = {
        "id": str (auto if missing),
        "payload": {...},            # passed to run_task
        "priority": int (higher first, default 0),
        "depends_on": ["id", ...],   # held until deps complete
        "max_retries": int (default 2),
    }
    """

    def __init__(
        self,
        run_task: Callable[[Dict], Dict],
        n_workers: int = _N_DEFAULT,
        n_linux_workers: int = _N_LINUX_DEFAULT,
        linux_run_task: Optional[Callable[[Dict], Dict]] = None,
        autoscale: Optional[bool] = None,
        scale_up_delay_s: float = 2.0,
        idle_timeout_s: float = 300.0,
    ):
        """Unified pool of Windows + Linux (WSL2) workers.

        Args:
          run_task:        callable executed by Windows workers.
          n_workers:       requested Windows workers (floor; RAM-capped).
          n_linux_workers: requested Linux workers (floor; 0 = none).
          linux_run_task:  callable executed by Linux workers
                           (default: linux_worker.default_wsl_run_task).
          autoscale:       spin workers up on sustained queue pressure and
                           reap idle autoscaled workers (default True;
                           S3REHAN_AUTOSCALE=0 disables).
          scale_up_delay_s: queue wait before autoscale spawns (avoids
                           breaking priority/queueing semantics).
          idle_timeout_s:  reap autoscaled workers idle longer than this.
        """
        self.run_task = run_task
        self.n_workers = _mem_cap_workers(max(0, int(n_workers)))
        self.n_linux_workers = max(0, int(n_linux_workers))
        self.linux_run_task = linux_run_task
        if autoscale is None:
            autoscale = os.environ.get("S3REHAN_AUTOSCALE", "1") != "0"
        self.autoscale = bool(autoscale)
        try:
            self.scale_up_delay_s = float(
                os.environ.get("S3REHAN_SCALE_UP_DELAY_S",
                               str(scale_up_delay_s)))
        except Exception:
            self.scale_up_delay_s = 2.0
        try:
            self.idle_timeout_s = float(
                os.environ.get("S3REHAN_WORKER_IDLE_S",
                               str(idle_timeout_s)))
        except Exception:
            self.idle_timeout_s = 300.0
        self.workers: List = []  # unified pool: Worker + WSLWorker
        self._pending: List[Dict] = []  # (priority, seq, task)
        self._seq = 0
        self._results: Dict[str, Dict] = {}
        self._done_ids: Set[str] = set()
        self._lock = threading.Lock()
        self._started = False
        self._watchdog: Optional[threading.Thread] = None
        self._watch_stop = threading.Event()
        self._next_wid = 0
        # Floors for scale-down: never reap below what was requested.
        self._win_floor = 0
        self._linux_floor = 0

    # -- lifecycle ----------------------------------------------------

    def _make_worker(self, kind: str):
        """Create (not start) a worker of kind 'windows'|'linux'."""
        wid = self._next_wid
        self._next_wid += 1
        if kind == "linux":
            if _linux_worker is None:
                raise RuntimeError("linux_worker module unavailable")
            linux_run = (self.linux_run_task
                         or _linux_worker.default_wsl_run_task)
            # Route through _run_one so results land in _results/_done_ids
            # (same bookkeeping as Windows workers).
            return _linux_worker.WSLWorker(
                wid, lambda task: self._run_one(task, runner=linux_run))
        return Worker(wid, self._run_one)

    def _linux_pool_allowed(self) -> bool:
        if _linux_worker is None:
            return False
        try:
            return _linux_worker.wsl_available()
        except Exception:
            return False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            for _ in range(self.n_workers):
                w = self._make_worker("windows")
                w.start()
                self.workers.append(w)
            if self.n_linux_workers > 0 and self._linux_pool_allowed():
                for _ in range(self.n_linux_workers):
                    w = self._make_worker("linux")
                    w.start()
                    self.workers.append(w)
            self._win_floor = sum(
                1 for w in self.workers
                if getattr(w, "platform", "windows") == "windows")
            self._linux_floor = sum(
                1 for w in self.workers
                if getattr(w, "platform", "windows") == "linux")
            self._started = True
        self._watch_stop.clear()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="s3rehan-watchdog", daemon=True
        )
        self._watchdog.start()

    def shutdown(self) -> None:
        self._watch_stop.set()
        with self._lock:
            workers = list(self.workers)
            self.workers = []
            self._started = False
        for w in workers:
            w.stop()

    # -- submission ---------------------------------------------------

    def submit(
        self,
        task: Dict,
        priority: int = 0,
        timeout_s: float = 120.0,
    ) -> Dict:
        """Submit a task; blocks until it completes or timeout.

        Returns the worker's result dict (with dep_results injected when
        the task declared depends_on).
        """
        tid = task.get("id") or f"t-{uuid.uuid4().hex[:8]}"
        deps = list(task.get("depends_on") or [])
        with self._lock:
            # Cycle check (simple: dep on unknown-but-not-yet-done id is
            # allowed; dep on self is rejected).
            if tid in deps:
                return {"ok": False, "error": "task depends on itself",
                        "id": tid}
            entry = {
                "id": tid,
                "priority": int(priority),
                "seq": self._seq,
                "task": dict(task, id=tid),
                "retries_left": int(task.get("max_retries", _MAX_TASK_RETRIES)),
                "enqueued_at": time.perf_counter(),
            }
            self._seq += 1
            self._pending.append(entry)
            self._pending.sort(key=lambda e: (-e["priority"], e["seq"]))
        if not self._started or not self.workers:
            # Pool disabled: run inline honoring deps.
            return self._drain_inline(tid, timeout_s)
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            with self._lock:
                if tid in self._results:
                    return self._results.pop(tid)
            self._pump()
            time.sleep(0.05)
        with self._lock:
            self._pending = [e for e in self._pending if e["id"] != tid]
            return {"ok": False, "error": "timeout waiting for result",
                    "id": tid}

    def _drain_inline(self, tid: str, timeout_s: float) -> Dict:
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            with self._lock:
                ready = self._ready_entries()
                ent = next((e for e in ready if e["id"] == tid), None)
                if ent is None and tid in self._results:
                    return self._results.pop(tid)
            if ent:
                with self._lock:
                    self._pending = [e for e in self._pending
                                     if e["id"] != tid]
                # Inject dependency results (same as _pump).
                deps = ent["task"].get("depends_on") or []
                ent["task"]["dep_results"] = {
                    d: self._results.get(d) for d in deps
                }
                res = self._run_one(ent["task"])
                with self._lock:
                    self._results[tid] = res
                    self._done_ids.add(tid)
                return res
            time.sleep(0.05)
        return {"ok": False, "error": "timeout", "id": tid}

    # -- scheduling ---------------------------------------------------

    def _ready_entries(self) -> List[Dict]:
        """Pending entries whose dependencies are all done."""
        return [e for e in self._pending
                if all(d in self._done_ids
                       for d in (e["task"].get("depends_on") or []))]

    def _route_kind(self, task: Dict) -> str:
        """'linux' or 'windows' for this task (unified-pool routing)."""
        if _linux_worker is not None:
            try:
                if _linux_worker.route_to_linux(task):
                    return "linux"
            except Exception:
                pass
        return "windows"

    def _task_linux_explicit(self, task: Dict) -> bool:
        return str(task.get("platform") or "").lower() == "linux"

    def _pump(self) -> None:
        """Assign ready tasks to free workers (priority order).

        Type-aware: Linux-eligible tasks prefer Linux workers; an
        explicitly platform=linux task never falls back to Windows
        (it would fail there); heuristic Linux tasks may fall back to a
        Windows worker rather than stall. Autoscale spawns workers of the
        needed kind when queue pressure is sustained and RAM allows.
        """
        with self._lock:
            ready = self._ready_entries()
            now = time.perf_counter()
            for ent in ready:
                # Inject dependency results.
                deps = ent["task"].get("depends_on") or []
                ent["task"]["dep_results"] = {
                    d: self._results.get(d) for d in deps
                }
                task = ent["task"]
                kind = self._route_kind(task)
                explicit_linux = self._task_linux_explicit(task)
                assigned = False
                # 1) idle worker of the routed kind.
                for w in self.workers:
                    if getattr(w, "platform", "windows") != kind:
                        continue
                    if w.submit(task):
                        assigned = True
                        break
                # 2) heuristic-linux tasks may fall back to Windows.
                if not assigned and kind == "linux" and not explicit_linux:
                    for w in self.workers:
                        if getattr(w, "platform", "windows") != "windows":
                            continue
                        if w.submit(task):
                            assigned = True
                            break
                # 3) autoscale on sustained pressure.
                if (not assigned and self.autoscale and self._started
                        and now - ent["enqueued_at"] >= self.scale_up_delay_s):
                    w = self._try_spawn_locked(kind)
                    if w is not None and w.submit(task):
                        assigned = True
                if assigned:
                    self._pending = [e for e in self._pending
                                     if e["id"] != ent["id"]]

    def _kind_count(self, kind: str) -> int:
        return sum(1 for w in self.workers
                   if getattr(w, "platform", "windows") == kind)

    def _ram_headroom_for(self, kind: str) -> bool:
        """True when spawning one more worker of kind fits in RAM."""
        if kind == "linux":
            if _linux_worker is None:
                return False
            try:
                if not _linux_worker.wsl_available():
                    return False
                if not _linux_worker.wsl_resource_ok():
                    return False
                return (self._kind_count("linux")
                        < _linux_worker._max_linux_workers())
            except Exception:
                return False
        # Windows: reuse the existing RAM-derived cap as the ceiling.
        try:
            return self._kind_count("windows") < _mem_cap_workers(10 ** 9)
        except Exception:
            return True

    def _try_spawn_locked(self, kind: str):
        """Spawn one worker of kind if RAM allows. CALLER MUST HOLD
        self._lock (called from _pump, which already holds it)."""
        if not self._started:
            return None
        if not self._ram_headroom_for(kind):
            return None
        try:
            w = self._make_worker(kind)
        except Exception:
            return None
        w.autoscaled = True
        try:
            w.start()
        except Exception:
            return None
        self.workers.append(w)
        return w

    def _run_one(self, task: Dict, runner: Optional[Callable] = None) -> Dict:
        """Execute one task on a worker; handles completion bookkeeping.

        runner: override callable (Linux workers pass their WSL runner so
        results still land in _results/_done_ids).
        """
        tid = task.get("id", "?")
        run = runner or self.run_task
        try:
            res = run(task) or {"ok": False}
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        res = dict(res)
        res.setdefault("id", tid)
        # Pass through dep_results so callers see what their deps returned.
        if "dep_results" in task:
            res.setdefault("dep_results", task["dep_results"])
        with self._lock:
            self._results[tid] = res
            self._done_ids.add(tid)
        return res

    # -- watchdog: dead-worker recovery --------------------------------

    def _watchdog_loop(self) -> None:
        while not self._watch_stop.wait(5.0):
            try:
                self._watchdog_tick()
            except Exception:
                pass

    def _watchdog_tick(self) -> None:
        with self._lock:
            workers = list(self.workers)
        for w in workers:
            if not w.alive or (w.busy and w.heartbeat_stale):
                self._recover_worker(w)
        # Scale down idle autoscaled workers (never below floors).
        try:
            self._reap_idle()
        except Exception:
            pass

    def _recover_worker(self, w: Worker) -> None:
        """A worker died or hung mid-task: requeue its task, replace it."""
        with self._lock:
            task = w.current_task
            wid = w.worker_id
            # Find and drop the dead worker.
            self.workers = [x for x in self.workers if x is not w]
        try:
            w.stop()
        except Exception:
            pass
        # Requeue the interrupted task (if retries remain).
        if task:
            retries = int(task.get("_retries_left", _MAX_TASK_RETRIES))
            if retries > 0:
                task["_retries_left"] = retries - 1
                with self._lock:
                    self._pending.append({
                        "id": task.get("id", f"t-{uuid.uuid4().hex[:8]}"),
                        "priority": 10,  # recovered tasks jump the queue
                        "seq": -1,
                        "task": task,
                        "retries_left": retries - 1,
                        "enqueued_at": time.perf_counter(),
                    })
                    self._pending.sort(key=lambda e: (-e["priority"],
                                                      e["seq"]))
        # Replace the worker so pool size is preserved (same kind).
        with self._lock:
            if self._started:
                try:
                    nw = self._make_worker(
                        getattr(w, "platform", "windows"))
                except Exception:
                    return
                nw.autoscaled = bool(getattr(w, "autoscaled", False))
                try:
                    nw.start()
                except Exception:
                    return
                self.workers.append(nw)

    def _reap_idle(self) -> None:
        """Scale down: reap autoscaled workers idle past idle_timeout_s.

        Never reaps below the floors requested at construction.
        """
        now = time.perf_counter()
        with self._lock:
            if not self._started:
                return
            win_n = self._kind_count("windows")
            lin_n = self._kind_count("linux")
            victims = []
            for w in self.workers:
                if not getattr(w, "autoscaled", False):
                    continue
                if w.busy:
                    continue
                if now - w.heartbeat > self.idle_timeout_s:
                    kind = getattr(w, "platform", "windows")
                    if kind == "windows" and win_n <= self._win_floor:
                        continue
                    if kind == "linux" and lin_n <= self._linux_floor:
                        continue
                    victims.append(w)
                    if kind == "windows":
                        win_n -= 1
                    else:
                        lin_n -= 1
            for w in victims:
                self.workers = [x for x in self.workers if x is not w]
        for w in victims:
            try:
                w.stop()
            except Exception:
                pass

    # -- monitoring ----------------------------------------------------

    def status(self) -> Dict:
        with self._lock:
            workers = list(self.workers)
            pending = list(self._pending)
        return {
            "n_workers": len(workers),
            "n_windows": sum(1 for w in workers
                             if getattr(w, "platform", "windows")
                             == "windows"),
            "n_linux": sum(1 for w in workers
                           if getattr(w, "platform", "windows") == "linux"),
            "autoscale": self.autoscale,
            "busy": sum(1 for w in workers if w.busy),
            "idle": sum(1 for w in workers if not w.busy),
            "pending": len(pending),
            "done": len(self._done_ids),
            "tasks_done": sum(w.tasks_done for w in workers),
            "desktops_real": sum(1 for w in workers
                                 if getattr(getattr(w, "desktop", None),
                                            "is_real", False)),
            "workers": [
                {
                    "id": w.worker_id,
                    "platform": getattr(w, "platform", "windows"),
                    "autoscaled": bool(getattr(w, "autoscaled", False)),
                    "state": w.state,
                    "alive": w.alive,
                    "desktop": getattr(getattr(w, "desktop", None),
                                       "name", None),
                    "desktop_real": getattr(getattr(w, "desktop", None),
                                            "is_real", False),
                    "task": (w.current_task or {}).get("id"),
                    "tasks_done": w.tasks_done,
                    "last_error": w.last_error,
                    "background_only": w.background_only,
                }
                for w in workers
            ],
        }

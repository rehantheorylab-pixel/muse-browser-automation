"""Linux parallel workers via WSL2.

Rehan's requirement (2026-10-10): tasks that don't need Windows UI should
run on Linux in parallel — storage and RAM efficient. WSL2 is the fit:
built into Windows 10/11, ~1GB base, dynamic memory (returns RAM when
idle), no separate VM disk image.

Design mirrors rehan/workers.py so the dispatcher treats both pools as
one unified pool with type-aware routing:

  - WSLWorker mirrors the Worker public interface
    (start/stop/submit/busy/alive/heartbeat_stale/background_only).
  - route_to_linux(task) is a pure function: explicit platform wins,
    then needs_windows_ui, then task_type sets, then keyword heuristics.
    Ambiguous tasks route to Windows (safe default — Windows workers can
    run anything; a Linux worker cannot touch Win32/UIA).
  - Task transport: JSON task -> base64 -> stdin of a small runner script
    (~/.s3rehan/wsl_runner.py) inside WSL2 -> single-line JSON result on
    stdout. The runner supports op "python" (exec code, capture `result`)
    and op "shell" (subprocess, capture rc/stdout/stderr).

Resource model:
  - WSL2 guest memory is capped by %USERPROFILE%\\.wslconfig (the setup
    script writes memory=min(4GB, 25% host RAM)). All Linux workers share
    that one guest allocation, so the dispatcher gates *task assignment*
    on wsl_resource_ok() instead of counting worker threads.
  - _max_linux_workers() derives a thread cap from the guest cap so the
    pool can't grow without bound: (guest_cap - 1GB headroom) // budget.

Linux GUI apps (WSL2 + WSLg):
  WSL2 ships WSLg — Linux GUI apps render as native Windows windows
  (Weston/RDP backend, window class "wslg"). A Linux worker that claims
  a virtual monitor can host GUI Linux apps on its own screen:
  move_wslg_windows_to_monitor() finds this session's WSLg windows and
  moves them onto the monitor via SetWindowPos; the worker then
  screenshots them with screen.grab() — zero flicker, same as Windows
  workers. Headless tasks skip this entirely (no monitor needed).

Env:
  S3REHAN_NO_WSL=1            force shim (tests, non-Windows dev)
  S3REHAN_WSL_DISTRO=name     WSL distro (default "Ubuntu")
  S3REHAN_WSL_MEM_MB=N        guest memory cap reference (default 4096)
  S3REHAN_WSL_WORKER_MEM_MB=N per-task memory budget (default 1024)
  S3REHAN_LINUX_WORKERS=N     requested linux workers (default 0)
  S3REHAN_LINUX_VMONITOR=1    claim a virtual monitor per Linux worker
                              (for WSLg GUI apps)
"""

import base64
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

_DISTRO = os.environ.get("S3REHAN_WSL_DISTRO", "Ubuntu")

try:
    _WSL_MEM_MB = int(os.environ.get("S3REHAN_WSL_MEM_MB", "4096"))
except Exception:
    _WSL_MEM_MB = 4096

try:
    _WSL_TASK_MEM_MB = int(os.environ.get("S3REHAN_WSL_WORKER_MEM_MB", "1024"))
except Exception:
    _WSL_TASK_MEM_MB = 1024

try:
    _N_LINUX_DEFAULT = int(os.environ.get("S3REHAN_LINUX_WORKERS", "0"))
except Exception:
    _N_LINUX_DEFAULT = 0

_HEARTBEAT_TIMEOUT_S = 60.0
_MAX_TASK_RETRIES = 2
_RUNNER_PATH = "~/.s3rehan/wsl_runner.py"
_MIN_GUEST_FREE_MB = 512

# ---------------------------------------------------------------------------
# WSL availability / resources
# ---------------------------------------------------------------------------

_wsl_available_cache: Optional[bool] = None


def wsl_available() -> bool:
    """True when the `wsl` CLI exists and a distro responds."""
    global _wsl_available_cache
    if os.environ.get("S3REHAN_NO_WSL", "0") == "1":
        return False
    if os.name != "nt":
        return False
    if _wsl_available_cache is not None:
        return _wsl_available_cache
    try:
        r = subprocess.run(
            ["wsl", "--status"],
            capture_output=True,
            timeout=15,
        )
        _wsl_available_cache = r.returncode == 0
    except Exception:
        _wsl_available_cache = False
    return _wsl_available_cache


def _run_wsl(args: List[str], input_text: Optional[str] = None,
             timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a command inside WSL2. Monkeypatch point for tests."""
    cmd = ["wsl", "--distribution", _DISTRO, "--"] + args
    return subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def wsl_memory_info() -> Dict[str, int]:
    """Guest memory in MB: {total, used, free}. {} when unavailable."""
    try:
        r = _run_wsl(["free", "-m"], timeout=15.0)
        if r.returncode != 0:
            return {}
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0].rstrip(":") == "Mem":
                return {
                    "total": int(parts[1]),
                    "used": int(parts[2]),
                    "free": int(parts[3]),
                }
        return {}
    except Exception:
        return {}


def wsl_resource_ok(min_free_mb: int = _MIN_GUEST_FREE_MB) -> bool:
    """Gate for dispatch: True when the guest has room for another task.

    Unknown memory info -> True (don't stall the pool on a failed probe;
    the per-task timeout still bounds damage).
    """
    info = wsl_memory_info()
    if not info:
        return True
    return info.get("free", 0) >= min_free_mb


def _max_linux_workers() -> int:
    """Thread cap derived from the guest memory cap (dynamic, not hardcoded).

    The WSL2 guest is one VM; this caps worker *threads* so the pool can't
    grow without bound. Real admission control is wsl_resource_ok().
    """
    if not wsl_available():
        return 0
    usable = max(0, _WSL_MEM_MB - 1024)  # 1GB headroom inside the guest
    budget = max(256, _WSL_TASK_MEM_MB)
    return max(1, usable // budget)


# ---------------------------------------------------------------------------
# Routing: which tasks belong on Linux
# ---------------------------------------------------------------------------

LINUX_TASK_TYPES = frozenset({
    "web_scrape", "scrape", "crawl", "crawler",
    "api_call", "http_fetch", "fetch",
    "headless_browser", "browser_task",
    "file_process", "data_process", "etl",
    "shell", "python_exec", "download",
})

WINDOWS_TASK_TYPES = frozenset({
    "windows_app", "uia", "native_click", "native_type",
    "screenshot_windows", "paint", "win32", "powershell",
    "registry", "gui_task",
})

_LINUX_KEYWORDS = ("scrape", "crawl", "http", "api call", "download",
                   "headless", "fetch url")
_WINDOWS_KEYWORDS = ("paint", "notepad", "uia", "powershell", "registry",
                     "window", "desktop app", "win32")


def route_to_linux(task: Dict) -> bool:
    """True when this task should run on a Linux worker.

    Precedence: explicit platform > needs_windows_ui > task_type >
    keyword heuristics > safe default (Windows).
    """
    if not isinstance(task, dict):
        return False
    plat = str(task.get("platform") or "auto").lower()
    if plat == "linux":
        return True
    if plat == "windows":
        return False
    if task.get("needs_windows_ui") is True:
        return False
    tt = str(task.get("task_type") or "").lower()
    if tt in LINUX_TASK_TYPES:
        return True
    if tt in WINDOWS_TASK_TYPES:
        return False
    text = " ".join([
        str(task.get("id") or ""),
        str(task.get("action") or ""),
        str(task.get("description") or ""),
        str((task.get("payload") or {}).get("url") or ""),
    ]).lower()
    if any(k in text for k in _LINUX_KEYWORDS):
        return True
    if any(k in text for k in _WINDOWS_KEYWORDS):
        return False
    # Ambiguous -> Windows. A Windows worker can run anything (slower);
    # a Linux worker cannot touch Win32/UIA at all.
    return False


def route_platform(task: Dict) -> str:
    """'linux' if route_to_linux(task) else 'windows'."""
    return "linux" if route_to_linux(task) else "windows"


# ---------------------------------------------------------------------------
# The in-WSL runner (deployed lazily by ensure_wsl_runner)
# ---------------------------------------------------------------------------

WSL_RUNNER_SOURCE = '''"""Generic JSON-in/JSON-out task runner inside WSL2.

Protocol: base64-encoded JSON task on stdin -> single-line JSON result on
stdout. Ops:
  {"op": "python", "code": "..."}  exec code with `task` in scope;
                                   capture variable `result` (default {"ok": True})
  {"op": "shell", "cmd": "..."}    run via bash; capture rc/stdout/stderr
Deployed by rehan/linux_worker.py ensure_wsl_runner() (idempotent).
"""
import base64
import json
import subprocess
import sys
import traceback


def _run_python(task, code):
    ns = {"task": task}
    try:
        exec(compile(code, "<wsl_task>", "exec"), ns)
        result = ns.get("result", {"ok": True})
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        return result
    except Exception:
        return {"ok": False, "error": traceback.format_exc(limit=5)}


def _run_shell(task, cmd):
    try:
        p = subprocess.run(
            cmd, shell=True, executable="/bin/bash",
            capture_output=True, text=True,
            timeout=float(task.get("timeout_s", 120)),
        )
        return {"ok": p.returncode == 0, "returncode": p.returncode,
                "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def main():
    try:
        raw = sys.stdin.read()
        task = json.loads(base64.b64decode(raw.strip()).decode("utf-8"))
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"bad input: {e}"}))
        return
    op = str(task.get("op") or "python").lower()
    if op == "shell":
        out = _run_shell(task, task.get("cmd", "true"))
    else:
        out = _run_python(task, task.get("code", "result = {'ok': True}"))
    out = dict(out) if isinstance(out, dict) else {"ok": True, "result": out}
    out.setdefault("id", task.get("id"))
    print(json.dumps(out))


if __name__ == "__main__":
    main()
'''


def ensure_wsl_runner() -> bool:
    """Deploy ~/.s3rehan/wsl_runner.py inside WSL2 if missing. Idempotent."""
    try:
        check = _run_wsl(
            ["bash", "-lc", "test -f ~/.s3rehan/wsl_runner.py && echo YES || echo NO"],
            timeout=20.0,
        )
        if "YES" in (check.stdout or ""):
            return True
        b64 = base64.b64encode(WSL_RUNNER_SOURCE.encode("utf-8")).decode("ascii")
        inst = _run_wsl(
            ["bash", "-lc",
             "mkdir -p ~/.s3rehan && base64 -d > ~/.s3rehan/wsl_runner.py"],
            input_text=b64,
            timeout=30.0,
        )
        return inst.returncode == 0
    except Exception:
        return False


def default_wsl_run_task(task: Dict) -> Dict:
    """Execute one task inside WSL2 via the JSON runner. No WSL -> error."""
    tid = task.get("id", "?")
    if not wsl_available():
        return {"ok": False, "id": tid, "error": "wsl-unavailable"}
    if not ensure_wsl_runner():
        return {"ok": False, "id": tid, "error": "wsl-runner-deploy-failed"}
    payload = {k: v for k, v in task.items()
               if k not in ("dep_results", "_retries_left")}
    blob = base64.b64encode(
        json.dumps(payload).encode("utf-8")).decode("ascii")
    timeout = float(task.get("timeout_s", 120))
    try:
        r = _run_wsl(
            ["bash", "-lc", "python3 ~/.s3rehan/wsl_runner.py"],
            input_text=blob,
            timeout=timeout + 15.0,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "id": tid, "error": "wsl-timeout"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "id": tid,
                "error": f"wsl-exec-failed: {type(e).__name__}: {e}"}
    if r.returncode != 0:
        err = (r.stderr or "").strip()[-300:]
        return {"ok": False, "id": tid,
                "error": f"wsl-rc-{r.returncode}: {err}"}
    try:
        # Runner prints exactly one JSON line; take the last non-empty line
        # in case the shell echoed anything.
        lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        out = json.loads(lines[-1])
        out = dict(out)
        out.setdefault("id", tid)
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "id": tid,
                "error": f"wsl-bad-result: {e}"}


# ---------------------------------------------------------------------------
# WSLg GUI placement on a virtual monitor
# ---------------------------------------------------------------------------

def move_wslg_windows_to_monitor(monitor: Dict) -> int:
    """Move this session's WSLg (Linux GUI) windows onto a virtual monitor.

    WSLg renders Linux GUI apps as native Windows windows owned by
    ``wslg.exe``. This finds those top-level visible windows and moves
    them onto ``monitor`` (a virtual-monitor dict from
    rehan.virtual_monitor) via SetWindowPos — the user's physical screen
    is untouched. Returns windows moved. Windows-only; 0 elsewhere.
    """
    if os.name != "nt":
        return 0
    moved = 0
    try:
        import ctypes
        from ctypes import wintypes

        from rehan import virtual_monitor as _vm

        user32 = ctypes.windll.user32
        psapi = ctypes.windll.psapi
        hwnds: List[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                hproc = ctypes.windll.kernel32.OpenProcess(
                    0x0400 | 0x0010, False, pid.value)  # QUERY_INFO|VM_READ
                if not hproc:
                    return True
                try:
                    buf = ctypes.create_unicode_buffer(260)
                    if psapi.GetModuleFileNameExW(hproc, None, buf, 260):
                        exe = (buf.value or "").lower()
                        if "wslg.exe" in exe:
                            hwnds.append(int(hwnd))
                finally:
                    ctypes.windll.kernel32.CloseHandle(hproc)
            except Exception:
                pass
            return True

        user32.EnumWindows(_cb, 0)
        for h in hwnds:
            try:
                if _vm.move_window_to_monitor(h, monitor):
                    moved += 1
            except Exception:
                pass
    except Exception:
        pass
    return moved


# ---------------------------------------------------------------------------
# WSLWorker — mirrors rehan.workers.Worker
# ---------------------------------------------------------------------------

class WSLWorker:
    """One Linux task worker (WSL2 backend).

    Mirrors rehan.workers.Worker so TaskDispatcher treats both pools as
    one unified pool with type-aware routing. background_only is always
    True (there is no Windows UI inside WSL2 by design).
    """

    IDLE = "idle"
    BUSY = "busy"
    STOPPED = "stopped"

    platform = "linux"

    def __init__(
        self,
        worker_id: int,
        run_task: Optional[Callable[[Dict], Dict]] = None,
        screen=None,
        screen_release: Optional[Callable[[], None]] = None,
    ):
        self.worker_id = worker_id
        self.run_task = run_task or default_wsl_run_task
        self.background_only = True
        self.autoscaled = False
        # Optional virtual monitor (for WSLg GUI apps). Headless tasks
        # leave this None — no monitor wasted.
        self.screen = screen
        self._screen_release = screen_release
        self.state = self.IDLE
        self.current_task: Optional[Dict] = None
        self.last_result: Optional[Dict] = None
        self.last_error: Optional[str] = None
        self.tasks_done = 0
        self.heartbeat = time.perf_counter()
        self._inbox: "queue.Queue[Optional[Dict]]" = queue.Queue()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._loop, name=f"s3rehan-wsl-{worker_id}", daemon=True
        )

    def start(self) -> bool:
        self._thread.start()
        return True

    def stop(self) -> None:
        with self._lock:
            self.state = self.STOPPED
        self._inbox.put(None)
        self._thread.join(timeout=10)
        if self._screen_release is not None:
            try:
                self._screen_release()
            except Exception:
                pass
            finally:
                self._screen_release = None
                self.screen = None

    @property
    def display_backend(self) -> str:
        """'monitor' when a virtual monitor is claimed, else 'headless'."""
        return "monitor" if self.screen is not None else "headless"

    def screenshot(self) -> Optional[bytes]:
        """PNG bytes of this worker's virtual monitor (WSLg GUI apps).

        Returns None for headless workers (nothing to see) or when the
        grab fails.
        """
        if self.screen is None:
            return None
        try:
            from rehan import virtual_monitor as _vm

            img = self.screen.grab()
            return _vm.pil_to_png(img) if img is not None else None
        except Exception:
            return None

    def place_gui_on_monitor(self) -> int:
        """Move this session's WSLg windows onto the worker's monitor.

        Call after launching a Linux GUI app inside WSL2. Returns the
        number of windows moved. No-op without a claimed monitor.
        """
        if self.screen is None:
            return 0
        return move_wslg_windows_to_monitor(self.screen.monitor)

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
        t = dict(task)
        t.setdefault("background_only", True)
        t.setdefault("worker_id", self.worker_id)
        t.setdefault("platform", "linux")
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
                result = self.run_task(task) or result
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                with self._lock:
                    self.last_error = result["error"]
            finally:
                ms = (time.perf_counter() - t0) * 1000.0
                result = dict(result)
                result.setdefault("worker_id", self.worker_id)
                result.setdefault("platform", "linux")
                result["wall_ms"] = ms
                with self._lock:
                    self.last_result = result
                    self.current_task = None
                    if self.state == self.BUSY:
                        self.state = self.IDLE
                    self.tasks_done += 1
                self.heartbeat = time.perf_counter()

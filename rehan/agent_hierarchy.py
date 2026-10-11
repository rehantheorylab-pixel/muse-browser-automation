"""Agent hierarchy for the multi-desktop architecture.

Model
-----
- The main agent lives on the user's own desktop.
- :meth:`Agent.spawn_subagent` creates a child agent with its OWN Windows
  virtual desktop (pyvda, via :class:`rehan.workers.VirtualDesktop`) and,
  optionally, its OWN WSL2 Linux worker at the same time.
- Sub-agents can spawn sub-sub-agents — arbitrary depth. ``cleanup()``
  reaps the whole subtree (children first), then the agent's Linux
  worker, then its virtual desktop.

Filesystem
----------
Windows virtual desktops share one filesystem, so a file an agent writes
on its desktop is visible from the user's desktop automatically — no
sync needed. (Rehan: "both should be connected like normal in the
Windows default new desktop feature" — they are.)

Screenshots
-----------
:meth:`Agent.screenshot` captures THIS agent's display via its virtual
monitor (:mod:`rehan.virtual_monitor`) — pixel-perfect, zero flicker on
the user's screen. When no virtual-monitor driver is installed it falls
back to the virtual-desktop capture in :mod:`rehan.desktop_screenshot`.
Never the user's visible desktop, never another agent's.
"""

import os
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional


class AgentError(Exception):
    """Base for agent-hierarchy failures."""


class Agent:
    """One agent in the hierarchy.

    :param name: human-readable name (default ``agent-<6 hex>``).
    :param parent: parent Agent or None for the root (main) agent.
    :param with_linux: also provision a WSL2 worker for this agent.
    :param desktop: inject a VirtualDesktop-like object (tests / reuse).
    :param wsl_factory: callable ``(worker_id: int) -> WSLWorker-like``;
        default lazily imports :class:`rehan.linux_worker.WSLWorker`.
    :param monitor_pool: a :class:`rehan.virtual_monitor.MonitorPool`
        (or compatible). When None, the agent inherits its parent's pool,
        else auto-creates one when a virtual-display driver is present.
        Virtual monitors are the PRIMARY display isolation; the virtual
        desktop is the fallback when no driver is installed.
    :param prefer_monitor: set False to force the virtual-desktop path
        even when virtual monitors exist (debugging).
    """

    def __init__(
        self,
        name: Optional[str] = None,
        parent: Optional["Agent"] = None,
        with_linux: bool = False,
        desktop=None,
        wsl_factory: Optional[Callable[[int], object]] = None,
        monitor_pool=None,
        prefer_monitor: bool = True,
    ):
        self.name = name or f"agent-{uuid.uuid4().hex[:6]}"
        self.parent = parent
        self.children: List["Agent"] = []
        self.with_linux = bool(with_linux)
        self._wsl_factory = wsl_factory
        self._wsl = None
        self._wsl_id = uuid.uuid4().hex[:8]
        self._lock = threading.Lock()
        self._cleaned = False
        self.created_at = time.time()
        self._shot_cache: Optional[bytes] = None
        # Display isolation: virtual monitor (primary) -> virtual desktop
        # (fallback). Resolved in start().
        self._monitor_pool = monitor_pool
        self._prefer_monitor = bool(prefer_monitor)
        self._screen = None
        self._display_backend: str = "unknown"

        if desktop is None:
            from rehan.workers import VirtualDesktop

            desktop = VirtualDesktop(name=f"s3rehan-{self.name}")
        self.desktop = desktop

        if parent is not None:
            parent._adopt(self)

    # -- tree ------------------------------------------------------------
    def _adopt(self, child: "Agent") -> None:
        with self._lock:
            if child not in self.children:
                self.children.append(child)

    def _disown(self, child: "Agent") -> None:
        with self._lock:
            if child in self.children:
                self.children.remove(child)

    @property
    def depth(self) -> int:
        d, p = 0, self.parent
        while p is not None:
            d += 1
            p = p.parent
        return d

    @property
    def root(self) -> "Agent":
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    @property
    def is_root(self) -> bool:
        return self.parent is None

    def descendants(self) -> List["Agent"]:
        out: List["Agent"] = []
        with self._lock:
            stack = list(self.children)
        while stack:
            node = stack.pop()
            out.append(node)
            with node._lock:
                stack.extend(node.children)
        return out

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "Agent":
        """Create the display isolation (and WSL worker when requested).

        Display priority: virtual monitor (zero flicker) -> virtual
        desktop (fallback) -> shim (no isolation, tests/dev).
        """
        self.desktop.create()
        self._resolve_display()
        if self.with_linux and self._wsl is None:
            factory = self._wsl_factory or self._default_wsl_factory()
            try:
                self._wsl = factory(self._wsl_id)
                start = getattr(self._wsl, "start", None)
                if callable(start):
                    start()
            except Exception as exc:
                raise AgentError(f"linux worker failed for {self.name}: {exc}")
        return self

    def _resolve_display(self) -> None:
        """Claim a virtual monitor when possible, else use the desktop.

        Children share the parent's pool so one pool serves the whole
        subtree. Never raises: any failure degrades to the desktop path.
        """
        pool = self._monitor_pool
        if pool is None and self.parent is not None:
            pool = self.parent._monitor_pool
        if pool is None and self._prefer_monitor:
            try:
                from rehan import virtual_monitor as _vm

                if _vm.monitors_available():
                    pool = _vm.MonitorPool()
                    pool.refresh()
            except Exception:
                pool = None
        self._monitor_pool = pool
        if pool is not None:
            try:
                self._screen = pool.claim(self.name)
                self._display_backend = "monitor"
                return
            except Exception:
                self._screen = None
        # Fallback: virtual desktop (real or shim).
        self._display_backend = (
            "vdesktop" if bool(getattr(self.desktop, "is_real", False))
            else "shim"
        )

    @property
    def display_backend(self) -> str:
        """'monitor' | 'vdesktop' | 'shim' | 'unknown' (before start)."""
        return self._display_backend

    @property
    def screen(self):
        """The claimed VirtualScreen, or None on the fallback path."""
        return self._screen

    @staticmethod
    def _default_wsl_factory() -> Callable[[int], object]:
        def _make(worker_id) -> object:
            from rehan.linux_worker import WSLWorker

            return WSLWorker(worker_id=worker_id)

        return _make

    def spawn_subagent(self, name: Optional[str] = None,
                       with_linux: bool = False) -> "Agent":
        """Create + start a child agent on its own display.

        The child claims its own virtual monitor from the shared pool
        (or its own virtual desktop when no driver is installed). It may
        itself request Linux (``with_linux=True``): an agent can deploy
        one Linux AND one Windows display at the same time.
        """
        child = Agent(name=name, parent=self, with_linux=with_linux,
                      wsl_factory=self._wsl_factory,
                      monitor_pool=self._monitor_pool,
                      prefer_monitor=self._prefer_monitor)
        child.start()
        return child

    # -- observation -----------------------------------------------------
    def screenshot(self, strategy: str = "smart", fresh: bool = True,
                   **kwargs) -> bytes:
        """PNG bytes of THIS agent's display — pixel-perfect.

        Virtual-monitor path: zero-flicker DXGI grab of the agent's own
        monitor (``strategy`` is ignored; there is nothing to gate).
        Fallback path: ``strategy`` selects "smart" (default: idle-gated
        switch capture with measured flicker), "switch" (immediate), or
        "composite" (zero-flicker approximate).

        Never the user's visible desktop, never another agent's.

        ``fresh=False`` reuses the last capture (the display only changes
        when this agent acts, so re-screenshotting every loop is pure
        flicker). Call :meth:`invalidate_screenshot` after acting.
        """
        with self._lock:
            if not fresh and self._shot_cache is not None:
                return self._shot_cache
            screen = self._screen
        if screen is not None:
            try:
                from rehan import virtual_monitor as _vm

                img = screen.grab()
                if img is not None:
                    png = _vm.pil_to_png(img)
                    with self._lock:
                        self._shot_cache = png
                    return png
            except Exception:
                pass
            # Monitor grab failed: fall through to the desktop path.
        from rehan import desktop_screenshot as _ds

        png = _ds.screenshot_png(self.desktop, mode=strategy, **kwargs)
        with self._lock:
            self._shot_cache = png
        return png

    def invalidate_screenshot(self) -> None:
        """Drop the cached screenshot (call after this agent acts)."""
        with self._lock:
            self._shot_cache = None

    # -- teardown --------------------------------------------------------
    def cleanup(self) -> None:
        """Reap the whole subtree: children first, then Linux, then display.

        Idempotent and exception-tolerant — a failing child must not block
        the rest of the teardown.
        """
        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
            children = list(self.children)
            self.children = []
        for child in children:
            try:
                child.cleanup()
            except Exception:
                pass
        if self._wsl is not None:
            try:
                stop = getattr(self._wsl, "stop", None)
                if callable(stop):
                    stop()
            except Exception:
                pass
            finally:
                self._wsl = None
        # Release the virtual monitor back to the pool (stops its DXGI
        # capture session). The pool itself is owned by the root.
        if self._screen is not None:
            try:
                if self._monitor_pool is not None:
                    self._monitor_pool.release(self.name)
            except Exception:
                pass
            finally:
                self._screen = None
        try:
            from rehan import desktop_screenshot as _ds

            _ds.release_desktop(self.desktop)
        except Exception:
            pass
        try:
            self.desktop.remove()
        except Exception:
            pass
        if self.parent is not None:
            try:
                self.parent._disown(self)
            except Exception:
                pass

    def __enter__(self) -> "Agent":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.cleanup()

    # -- introspection ---------------------------------------------------
    def status(self) -> Dict:
        with self._lock:
            kids = [c.name for c in self.children]
        return {
            "name": self.name,
            "depth": self.depth,
            "is_root": self.is_root,
            "children": kids,
            "n_descendants": len(self.descendants()),
            "has_linux": self._wsl is not None,
            "display_backend": self._display_backend,
            "has_monitor": self._screen is not None,
            "desktop_real": bool(getattr(self.desktop, "is_real", False)),
            "age_s": round(time.time() - self.created_at, 1),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"Agent(name={self.name!r}, depth={self.depth}, "
                f"children={len(self.children)}, linux={self._wsl is not None})")

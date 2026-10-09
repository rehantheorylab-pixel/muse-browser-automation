"""Mock-based tests for the CSI and Lightpanda backends.

Everything external is mocked: no network, no browsers, no installs.
- CSI's daemon (POST /command) is faked by patching urllib.request.urlopen.
- Availability probing is faked by patching socket.create_connection.
- Lightpanda detection is faked by patching shutil.which.
"""

import asyncio
import base64
import io
import json
import os
import urllib.error
from unittest import mock

from core.backends.csi_backend import CSIBackend
from core.backends.lightpanda_backend import LightpandaBackend
from core.interfaces import BaseBrowserBackend
from core.types import BrowserBackendType


def run(coro):
    return asyncio.run(coro)


class _FakeHTTPResponse:
    """Minimal urlopen() stand-in: context manager with .read()."""

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def make_urlopen(calls, routes):
    """Return a fake urlopen that records every request and answers from routes.

    calls: list the fake appends (url, body, headers) dicts to.
    routes: {action: callable(args) -> response dict}.
    """

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        calls.append(
            {
                "url": req.full_url,
                "body": body,
                "headers": dict(req.header_items()),
            }
        )
        action = body["action"]
        handler = routes.get(action)
        payload = handler(body["args"]) if handler else {"success": True}
        return _FakeHTTPResponse(payload)

    return fake_urlopen


def make_tcp_probe_ok():
    cm = mock.MagicMock()
    cm.__enter__.return_value = cm
    return mock.patch("socket.create_connection", return_value=cm)


def seed_tab(backend, tab_id="t1", url="https://example.com/", title="Example"):
    backend._tabs = {tab_id: {"url": url, "title": title, "active": True}}
    backend._current_tab_id = tab_id


# ----------------------------------------------------------------------
# Enum
# ----------------------------------------------------------------------

def test_enum_has_csi_and_lightpanda():
    assert BrowserBackendType.CSI.value == "csi"
    assert BrowserBackendType.LIGHTPANDA.value == "lightpanda"
    # Existing values untouched (append-only).
    assert BrowserBackendType.CHROME.value == "chrome"
    assert BrowserBackendType.MOLI.value == "moli"
    assert BrowserBackendType.AGENT_BROWSER.value == "agent-browser"


def test_backends_report_their_type():
    assert CSIBackend().backend_type is BrowserBackendType.CSI
    assert LightpandaBackend().backend_type is BrowserBackendType.LIGHTPANDA


def test_backends_satisfy_interface():
    # Instantiation itself proves every abstract method is implemented.
    assert isinstance(CSIBackend(), BaseBrowserBackend)
    assert isinstance(LightpandaBackend(), BaseBrowserBackend)


# ----------------------------------------------------------------------
# CSI: availability
# ----------------------------------------------------------------------

def test_csi_is_available_true_when_port_open():
    backend = CSIBackend()
    with make_tcp_probe_ok():
        assert run(backend.is_available()) is True


def test_csi_is_available_false_on_refused():
    backend = CSIBackend()
    with mock.patch("socket.create_connection", side_effect=ConnectionRefusedError):
        assert run(backend.is_available()) is False


def test_csi_is_available_false_on_timeout():
    import socket as socket_module

    backend = CSIBackend()
    with mock.patch("socket.create_connection", side_effect=socket_module.timeout):
        assert run(backend.is_available()) is False


def test_csi_start_raises_helpful_error_when_daemon_down():
    backend = CSIBackend()
    with mock.patch("socket.create_connection", side_effect=ConnectionRefusedError):
        try:
            run(backend.start())
        except RuntimeError as exc:
            msg = str(exc)
            assert "not running" in msg
            assert "csi start" in msg
        else:
            raise AssertionError("start() should raise when the daemon is down")


def test_csi_start_succeeds_with_daemon_up():
    backend = CSIBackend()
    calls = []
    routes = {
        "list_tabs": lambda args: {
            "success": True,
            "tabs": [{"tabId": "t1", "url": "https://a.test/", "title": "A", "active": True}],
        }
    }
    with make_tcp_probe_ok(), mock.patch(
        "urllib.request.urlopen", side_effect=make_urlopen(calls, routes)
    ):
        run(backend.start())
        assert run(backend.is_connected()) is True
        assert calls[0]["body"]["action"] == "list_tabs"


def test_csi_start_reports_auth_failure():
    backend = CSIBackend()

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}')
        )

    with make_tcp_probe_ok(), mock.patch("urllib.request.urlopen", side_effect=boom):
        try:
            run(backend.start())
        except RuntimeError as exc:
            assert "CSI_API_KEY" in str(exc)
        else:
            raise AssertionError("start() should raise on HTTP 401")


def test_csi_stop_is_noop():
    backend = CSIBackend()
    backend._connected = True
    with mock.patch("urllib.request.urlopen") as urlopen:
        run(backend.stop())
        urlopen.assert_not_called()
    assert run(backend.is_connected()) is False


# ----------------------------------------------------------------------
# CSI: command translation
# ----------------------------------------------------------------------

def test_csi_navigate_posts_expected_call():
    backend = CSIBackend()
    seed_tab(backend)
    calls = []
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, {})):
        ok = run(backend.navigate("t1", "https://example.com/page"))
    assert ok is True
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "http://127.0.0.1:10088/command"
    assert call["body"]["action"] == "navigate"
    assert call["body"]["args"] == {"url": "https://example.com/page"}
    assert call["body"]["session"] == "muse-v2"  # session is top-level


def test_csi_create_tab_uses_new_tab():
    backend = CSIBackend()
    calls = []
    routes = {
        "navigate": lambda args: {"success": True, "tabId": "tab-42", "url": args["url"]},
    }
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, routes)):
        tab_id = run(backend.create_tab("https://example.com/"))
    assert tab_id == "tab-42"
    assert calls[0]["body"]["args"]["newTab"] is True


def test_csi_list_tabs_maps_fields():
    backend = CSIBackend()
    calls = []
    routes = {
        "list_tabs": lambda args: {
            "success": True,
            "tabs": [
                {"tabId": "a", "url": "https://a.test/", "title": "A", "active": True},
                {"tabId": "b", "url": "https://b.test/", "title": "B", "active": False},
            ],
        }
    }
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, routes)):
        tabs = run(backend.list_tabs())
    assert tabs[0] == {"id": "a", "tabId": "a", "title": "A", "url": "https://a.test/", "active": True}
    assert tabs[1]["id"] == "b" and tabs[1]["active"] is False


def test_csi_switch_tab_re_targets_by_url():
    backend = CSIBackend()
    backend._tabs = {
        "t1": {"url": "https://one.test/", "title": "One", "active": True},
        "t2": {"url": "https://two.test/", "title": "Two", "active": False},
    }
    backend._current_tab_id = "t1"
    calls = []
    routes = {
        "list_tabs": lambda args: {
            "success": True,
            "tabs": [
                {"tabId": "t1", "url": "https://one.test/", "title": "One", "active": True},
                {"tabId": "t2", "url": "https://two.test/", "title": "Two", "active": False},
            ],
        },
        "find_tab": lambda args: {"success": True, "tabId": "t2", "url": args["url"]},
    }
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, routes)):
        run(backend.switch_tab("t2"))
    find_calls = [c for c in calls if c["body"]["action"] == "find_tab"]
    assert len(find_calls) == 1
    assert find_calls[0]["body"]["args"] == {"url": "https://two.test/"}
    assert backend._current_tab_id == "t2"


def test_csi_evaluate_returns_value_field():
    backend = CSIBackend()
    seed_tab(backend)
    calls = []
    routes = {
        "evaluate": lambda args: {"success": True, "type": "string", "value": "hello"},
    }
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, routes)):
        assert run(backend.evaluate("t1", "document.title")) == "hello"
    assert calls[0]["body"]["action"] == "evaluate"
    assert calls[0]["body"]["args"] == {"code": "document.title"}


def test_csi_click_uses_coordinate_mouse_click():
    backend = CSIBackend()
    seed_tab(backend)
    calls = []
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, {})):
        assert run(backend.click("t1", 120, 340)) is True
    assert calls[0]["body"]["action"] == "mouse_click"
    assert calls[0]["body"]["args"] == {"x": 120, "y": 340}


def test_csi_type_text_uses_key_type():
    backend = CSIBackend()
    seed_tab(backend)
    calls = []
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, {})):
        assert run(backend.type_text("t1", "hello world")) is True
    assert calls[0]["body"]["action"] == "key_type"
    assert calls[0]["body"]["args"] == {"text": "hello world"}


def test_csi_press_key_uses_send_keys():
    backend = CSIBackend()
    seed_tab(backend)
    calls = []
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, {})):
        assert run(backend.press_key("t1", "Enter")) is True
    assert calls[0]["body"]["action"] == "send_keys"
    assert calls[0]["body"]["args"] == {"keys": "Enter"}


def test_csi_scroll_maps_dominant_axis():
    backend = CSIBackend()
    seed_tab(backend)
    calls = []
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, {})):
        run(backend.scroll("t1", delta_x=0, delta_y=400))
        run(backend.scroll("t1", delta_x=-60, delta_y=5))
    assert calls[0]["body"]["action"] == "scroll"
    assert calls[0]["body"]["args"] == {"direction": "down", "amount": 400}
    assert calls[1]["body"]["args"] == {"direction": "left", "amount": 60}


def test_csi_screenshot_decodes_base64():
    backend = CSIBackend()
    seed_tab(backend)
    calls = []
    routes = {
        "screenshot": lambda args: {"success": True, "data": base64.b64encode(b"PNGDATA").decode()},
    }
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, routes)):
        assert run(backend.screenshot("t1")) == b"PNGDATA"
    assert calls[0]["body"]["action"] == "screenshot"


def test_csi_close_tab():
    backend = CSIBackend()
    seed_tab(backend)
    calls = []
    routes = {
        "close_tab": lambda args: {"success": True, "closed": True},
        "list_tabs": lambda args: {"success": True, "tabs": []},
    }
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, routes)):
        run(backend.close_tab("t1"))
    assert calls[0]["body"]["action"] == "close_tab"
    assert backend._tabs == {}


def test_csi_get_cookies_via_cdp_passthrough():
    backend = CSIBackend()
    seed_tab(backend)
    calls = []
    routes = {
        "cdp": lambda args: {
            "success": True,
            "data": {"cookies": [{"name": "sid", "value": "abc", "domain": "example.com"}]},
        },
    }
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, routes)):
        cookies = run(backend.get_cookies("t1"))
    assert cookies == [{"name": "sid", "value": "abc", "domain": "example.com"}]
    assert calls[0]["body"]["action"] == "cdp"
    assert calls[0]["body"]["args"]["method"] == "Network.getAllCookies"


def test_csi_set_cookies_via_cdp_passthrough():
    backend = CSIBackend()
    seed_tab(backend)
    calls = []
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, {})):
        ok = run(
            backend.set_cookies(
                "t1",
                [
                    {"name": "sid", "value": "abc", "domain": "example.com", "path": "/"},
                    {"name": "pref", "value": "dark"},
                ],
            )
        )
    assert ok is True
    cdp_calls = [c for c in calls if c["body"]["action"] == "cdp"]
    assert len(cdp_calls) == 2
    assert cdp_calls[0]["body"]["args"]["method"] == "Network.setCookie"
    assert cdp_calls[0]["body"]["args"]["params"]["name"] == "sid"
    assert cdp_calls[0]["body"]["args"]["params"]["domain"] == "example.com"
    assert cdp_calls[1]["body"]["args"]["params"]["name"] == "pref"


def test_csi_attaches_auth_header_when_key_set():
    backend = CSIBackend(api_key="secret-key")
    seed_tab(backend)
    calls = []
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, {})):
        run(backend.navigate("t1", "https://example.com/"))
    headers = {k.lower(): v for k, v in calls[0]["headers"].items()}
    assert headers.get("authorization") == "Bearer secret-key"


def test_csi_build_page_model_uses_evaluate():
    backend = CSIBackend()
    seed_tab(backend)
    calls = []
    model_raw = {
        "url": "https://example.com/",
        "title": "Example",
        "elements": [
            {
                "ref": "e1", "tag": "button", "role": "button", "name": "Submit",
                "x": 10, "y": 20, "w": 80, "h": 30, "selector": "button.submit",
                "attributes": {"id": "submit"},
            }
        ],
    }
    routes = {
        "evaluate": lambda args: {"success": True, "type": "object", "value": model_raw},
    }
    with mock.patch("urllib.request.urlopen", side_effect=make_urlopen(calls, routes)):
        model = run(backend.build_page_model("t1"))
    assert model.url == "https://example.com/"
    assert model.title == "Example"
    assert len(model.interactive_elements) == 1
    assert model.interactive_elements[0].name == "Submit"
    assert calls[0]["body"]["action"] == "evaluate"


# ----------------------------------------------------------------------
# Lightpanda: detection only (no downloads, no installs)
# ----------------------------------------------------------------------

def _without_lightpanda_bin():
    env = dict(os.environ)
    env.pop("LIGHTPANDA_BIN", None)
    return mock.patch.dict(os.environ, env, clear=True)


def test_lightpanda_is_available_false_when_binary_missing():
    backend = LightpandaBackend()
    with mock.patch("shutil.which", return_value=None), _without_lightpanda_bin():
        assert run(backend.is_available()) is False


def test_lightpanda_is_available_true_when_binary_present():
    backend = LightpandaBackend()
    with mock.patch("shutil.which", return_value="/usr/local/bin/lightpanda"):
        assert run(backend.is_available()) is True


def test_lightpanda_start_raises_without_binary():
    backend = LightpandaBackend()
    with mock.patch("shutil.which", return_value=None), _without_lightpanda_bin():
        try:
            run(backend.start())
        except RuntimeError as exc:
            assert "Lightpanda binary not found" in str(exc)
            assert "LIGHTPANDA_BIN" in str(exc)
        else:
            raise AssertionError("start() should raise when the binary is missing")

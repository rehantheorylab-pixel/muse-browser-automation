"""jev lane tests — pure, no network, no proxy, no UI.

Adapted from s3rehan/tests/test_jev.py for browser-automation-v2's
rehan/jev.py. The proxy transport (JevLane._chat) is always mocked;
no test performs a real HTTP call.
"""
import io
import json
import sys
import time
import types
import urllib.error
from unittest import mock

import pytest

from rehan.jev import (
    JevInvalid,
    JevLane,
    JevTransport,
    _try_decide_json,
    _try_decide_legacy,
    element_table,
    element_table_rows,
    jev_enabled,
    parse_decision,
    parse_reply,
    run_jev_steps,
    validate_decision,
)

ELEMENTS = [
    {"id": 1, "role": "ButtonControl", "name": "Submit", "x": 100, "y": 200, "w": 80, "h": 30},
    {"id": 2, "role": "EditControl", "name": "Search box", "x": 300, "y": 120, "w": 200, "h": 24},
    {"id": 3, "role": "ListItemControl", "name": "Option A", "x": 50, "y": 400, "w": 120, "h": 20},
]


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # Retry paths back off with time.sleep; keep the suite fast.
    monkeypatch.setattr(time, "sleep", lambda s: None)


def _lane():
    return JevLane(
        base_url="http://127.0.0.1:9/v1",  # unroutable: must never be hit
        decide_model="mock-model",
        text_model="mock-model",
    )


# ── validate_decision: JSON contract ────────────────────────────────────

def test_validate_decision_ok():
    assert validate_decision({"op": "CLICK", "index": 2, "text": None}, 5) == ("CLICK", 2)
    assert validate_decision({"op": "DONE", "index": None, "text": None}, 5) == ("DONE", None)
    assert validate_decision({"op": "BLOCKED", "index": None, "text": None}, 5) == ("BLOCKED", None)
    assert validate_decision({"op": "TYPE_TEXT", "index": 1, "text": "hi"}, 5) == ("TYPE_TEXT", (1, "hi"))
    assert validate_decision({"op": "TYPE_TEXT", "index": 1, "text": None}, 5) == ("TYPE_TEXT", 1)
    assert validate_decision({"op": "WAIT", "index": 2.5, "text": None}, 5) == ("WAIT", 2.5)
    assert validate_decision({"op": "OPEN_APP", "index": None, "text": "notepad"}, 5) == ("OPEN_APP", "notepad")
    assert validate_decision({"op": "PRESS_KEY", "index": None, "text": "ctrl+s"}, 5) == ("PRESS_KEY", ["ctrl", "s"])
    # Op names are case-insensitive.
    assert validate_decision({"op": "click", "index": 1, "text": None}, 5) == ("CLICK", 1)


def test_validate_decision_index_zero_is_screen_convention():
    # [0] is the whole screen (scroll target); validate_decision accepts 0
    # for index ops by design — build_code rejects it for non-scroll ops.
    assert validate_decision({"op": "SCROLL_DOWN", "index": 0, "text": None}, 5) == ("SCROLL_DOWN", 0)
    assert validate_decision({"op": "CLICK", "index": 0, "text": None}, 5) == ("CLICK", 0)


def test_validate_decision_rejects():
    bad = [
        {"op": "CLICK", "index": 99, "text": None},      # index out of range
        {"op": "CLICK", "index": 1},                     # missing key
        {"op": "CLICK", "index": 1, "text": None, "x": 1},  # extra key
        {"op": "NOPE", "index": 1, "text": None},        # unknown op
        {"op": "DONE", "index": 1, "text": None},        # terminal op with index
        {"op": "DONE", "index": None, "text": "x"},      # terminal op with text
        {"op": "WAIT", "index": 99, "text": None},       # WAIT out of range
        {"op": "WAIT", "index": "2", "text": None},      # WAIT non-numeric
        {"op": "WAIT", "index": True, "text": None},     # bool is not a number
        {"op": "CLICK", "index": True, "text": None},    # bool is not an index
        {"op": "CLICK", "index": 1, "text": "x"},        # index op with text
        {"op": "TYPE_TEXT", "index": 1, "text": 5},      # text must be str/null
        {"op": "OPEN_APP", "index": 1, "text": "x"},    # text op with index
        {"op": "PRESS_KEY", "index": None, "text": "a+b+c+d+e"},  # too many keys
        "not a dict",
        None,
    ]
    for b in bad:
        with pytest.raises(JevInvalid):
            validate_decision(b, 5)


# ── decide_json: JSON parsing with a mocked transport (no proxy) ────────

def _mock_chat(return_value=None, side_effect=None):
    lane = _lane()
    lane._chat = mock.Mock(return_value=return_value, side_effect=side_effect)
    return lane


def test_decide_json_parses_valid_reply():
    lane = _mock_chat('{"op": "CLICK", "index": 2, "text": null}')
    op, arg = lane.decide_json("task", "[1] x", n_rows=5)
    assert (op, arg) == ("CLICK", 2)
    # json_mode was requested on the (mocked) transport.
    _, kwargs = lane._chat.call_args
    assert kwargs.get("json_mode") is True


def test_decide_json_strips_code_fences():
    lane = _mock_chat('```json\n{"op": "DONE", "index": null, "text": null}\n```')
    assert lane.decide_json("task", "t", n_rows=3) == ("DONE", None)


def test_decide_json_recovers_braced_span():
    lane = _mock_chat('thinking out loud {"op": "WAIT", "index": 2, "text": null} done')
    assert lane.decide_json("task", "t", n_rows=3) == ("WAIT", 2.0)


def test_decide_json_rejects_non_json():
    lane = _mock_chat("CLICK [2]")  # free text, not JSON
    with pytest.raises(JevInvalid):
        lane.decide_json("task", "t", n_rows=3)


def test_decide_json_rejects_schema_violation():
    lane = _mock_chat('{"op": "CLICK", "index": 99, "text": null}')
    with pytest.raises(JevInvalid):
        lane.decide_json("task", "t", n_rows=3)


def test_decide_json_transport_error_propagates():
    lane = _mock_chat(side_effect=JevTransport("proxy down"))
    with pytest.raises(JevTransport):
        lane.decide_json("task", "t", n_rows=3)


# ── _chat transport mapping (urllib mocked, still no real network) ──────

def test_chat_maps_http_error_to_transport():
    lane = _lane()
    err = urllib.error.HTTPError(
        "http://x/chat/completions", 500, "boom", {}, io.BytesIO(b"err-body"))
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(JevTransport, match="proxy HTTP 500"):
            lane._chat("m", [{"role": "user", "content": "hi"}], max_tokens=8)


def test_chat_maps_url_error_to_transport():
    lane = _lane()
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("refused"),
    ):
        with pytest.raises(JevTransport, match="proxy unreachable"):
            lane._chat("m", [{"role": "user", "content": "hi"}], max_tokens=8)


def test_chat_maps_empty_content_to_transport():
    lane = _lane()
    payload = json.dumps({"choices": [{"message": {"content": "  "}}]}).encode()
    resp = mock.MagicMock()
    resp.read.return_value = payload
    resp.__enter__.return_value = resp
    with mock.patch("urllib.request.urlopen", return_value=resp):
        with pytest.raises(JevTransport, match="empty content"):
            lane._chat("m", [{"role": "user", "content": "hi"}], max_tokens=8)


def test_chat_returns_content_on_success():
    lane = _lane()
    payload = json.dumps(
        {"choices": [{"message": {"content": '{"op":"DONE","index":null,"text":null}'}}]}
    ).encode()
    resp = mock.MagicMock()
    resp.read.return_value = payload
    resp.__enter__.return_value = resp
    with mock.patch("urllib.request.urlopen", return_value=resp):
        out = lane._chat("m", [{"role": "user", "content": "hi"}], max_tokens=8)
    assert json.loads(out)["op"] == "DONE"


# ── retry logic ─────────────────────────────────────────────────────────

def test_try_decide_json_retries_with_escalating_temperature():
    calls = []

    class _Flaky(_ScriptedLane):
        def decide_json(self, task, table, n_rows, history=None,
                        correction="", temperature=0):
            calls.append((correction, temperature))
            if not correction:
                raise JevInvalid("unparseable decision: 'TYPE_TEXT ['")
            return ("DONE", None)

    lane = _Flaky([])
    op, arg = _try_decide_json(lane, "task", "table", 5, [], lambda *a: None)
    assert (op, arg) == ("DONE", None)
    # First attempt at temp 0, retry carries the correction at temp 0.7.
    assert calls[0] == ("", 0.0)
    assert calls[1][0].startswith("unparseable decision")
    assert calls[1][1] == 0.7


def test_try_decide_json_exhausts_three_attempts():
    temps = []

    class _AlwaysBad(_ScriptedLane):
        def decide_json(self, task, table, n_rows, history=None,
                        correction="", temperature=0):
            temps.append(temperature)
            raise JevInvalid("still bad")

    with pytest.raises(JevInvalid, match="still bad"):
        _try_decide_json(_AlwaysBad([]), "task", "t", 5, [], lambda *a: None)
    assert temps == [0.0, 0.7, 1.0]


def test_try_decide_legacy_retries_and_succeeds():
    calls = []

    class _FlakyLegacy(_ScriptedLane):
        def decide(self, task, table, correction="", temperature=0):
            calls.append(temperature)
            if not correction:
                raise JevInvalid("garbage")
            return ("DONE", None)

    op, arg = _try_decide_legacy(_FlakyLegacy([]), "task", "t", lambda *a: None)
    assert (op, arg) == ("DONE", None)
    assert calls == [0.0, 0.7]


def test_try_decide_legacy_exhausts_three_attempts():
    class _AlwaysBad(_ScriptedLane):
        def decide(self, task, table, correction="", temperature=0):
            raise JevInvalid("nope")

    with pytest.raises(JevInvalid):
        _try_decide_legacy(_AlwaysBad([]), "task", "t", lambda *a: None)


# ── gen_text strict contract ────────────────────────────────────────────

def test_gen_text_returns_text():
    lane = _mock_chat('{"text": "hello world"}')
    assert lane.gen_text("task", {"name": "Q", "role": "Edit"}) == "hello world"


def test_gen_text_null_raises():
    lane = _mock_chat('{"text": null}')
    with pytest.raises(JevInvalid, match="null"):
        lane.gen_text("task", {"name": "Q", "role": "Edit"})


def test_gen_text_bad_shape_raises():
    for raw in ['{"text": "x", "extra": 1}', '["x"]', '{"text": ""}', '{"text": 5}']:
        lane = _mock_chat(raw)
        with pytest.raises(JevInvalid):
            lane.gen_text("task", {"name": "Q", "role": "Edit"})


# ── element table ───────────────────────────────────────────────────────

def test_element_table_format():
    t = element_table(ELEMENTS)
    lines = t.splitlines()
    assert lines[0] == "[0] screen  (whole screen)"
    assert lines[1] == "[1] \u25b8 button  Submit"
    assert lines[2] == "[2] \u270e edit  Search box"
    assert lines[3] == "[3] \u25b8 edit  Open Search box"
    assert lines[4] == "[4] \u25b8 listitem  Option A"


def test_element_table_caps_rows():
    els = [{"role": "b", "name": f"e{i}", "x": i, "y": i} for i in range(500)]
    t = element_table(els)
    assert "[250]" in t
    assert "[251]" not in t
    assert "more elements not shown" in t


def test_element_table_rows_twins_and_options():
    els = [
        {"role": "EditControl", "name": "Q", "x": 1, "y": 1},
        {"role": "ComboBoxControl", "name": "C", "x": 1, "y": 1,
         "options": ["A", "B"]},
        {"role": "TextControl", "name": "static", "x": 1, "y": 1},
    ]
    rows, _ = element_table_rows(els)
    kinds = [r["kind"] for r in rows]
    assert kinds == ["fill", "twin", "fill", "select", "select"], kinds
    assert rows[3]["label"] == "C -> A"
    assert rows[3]["option"] == "A"


def test_element_table_drops_non_actionable():
    els = [
        {"role": "TextControl", "name": "label", "x": 1, "y": 1},
        {"role": "ImageControl", "name": "pic", "x": 1, "y": 1},
        {"role": "ButtonControl", "name": "Go", "x": 1, "y": 1},
    ]
    rows, _ = element_table_rows(els)
    assert [r["role"] for r in rows] == ["button"]


# ── legacy free-text parsing ────────────────────────────────────────────

def test_parse_click():
    assert parse_decision("CLICK [7]") == ("CLICK", 7)


def test_parse_case_and_spaces():
    assert parse_decision("  click [3]  ") == ("CLICK", 3)
    assert parse_decision("DONE") == ("DONE", None)
    assert parse_decision("blocked") == ("BLOCKED", None)


def test_parse_wait_open_press():
    assert parse_decision("WAIT [2]") == ("WAIT", 2.0)
    assert parse_decision("OPEN_APP [notepad]") == ("OPEN_APP", "notepad")
    assert parse_decision("PRESS_KEY [ctrl+s]") == ("PRESS_KEY", ["ctrl", "s"])


def test_parse_inline_text():
    assert parse_decision("TYPE_TEXT [116] winver") == ("TYPE_TEXT", (116, "winver"))
    assert parse_decision("TYPE_TEXT [2]") == ("TYPE_TEXT", 2)


def test_parse_rejects_garbage():
    for bad in ["", "CLICK", "JUMP [3]", "CLICK [abc]", "WAIT [99]",
                "DONE [1]", "PRESS_KEY [ctrl+;rm -rf]", "OPEN_APP [a/b\\c]",
                "CLICK [(100, 200)]", "pyautogui.click(1,2)"]:
        with pytest.raises(JevInvalid):
            parse_decision(bad)


def test_parse_reply_split_across_lines():
    assert parse_reply("PRESS_KEY\n[win+r]") == ("PRESS_KEY", ["win", "r"])


def test_parse_reply_with_preamble():
    assert parse_reply("Sure, I will click it now.\nCLICK [2]") == ("CLICK", 2)


def test_parse_reply_rejects_garbage():
    with pytest.raises(JevInvalid):
        parse_reply("hmm, let me think about this for a while")


def test_press_key_aliases():
    assert parse_decision("PRESS_KEY [windows+r]") == ("PRESS_KEY", ["win", "r"])
    assert parse_decision("PRESS_KEY [escape]") == ("PRESS_KEY", ["esc"])


# ── build_code: coordinates always come from the element bbox ───────────

def test_build_code_click_uses_element_coords():
    code = _lane().build_code("CLICK", 1, ELEMENTS)
    assert "pyautogui.click(100, 200)" in code
    assert "9999" not in code


def test_build_code_type_text_needs_text_role():
    lane = _lane()
    code = lane.build_code("TYPE_TEXT", 2, ELEMENTS, text="hello")
    assert "pyautogui.click(300, 120)" in code
    assert "'hello'" in code
    with pytest.raises(JevInvalid):
        lane.build_code("TYPE_TEXT", 1, ELEMENTS, text="hello")  # button


def test_build_code_type_text_needs_text():
    with pytest.raises(JevInvalid):
        _lane().build_code("TYPE_TEXT", 2, ELEMENTS, text="")


def test_build_code_bad_index():
    lane = _lane()
    for bad in (0, 99, -1, "x"):
        with pytest.raises(JevInvalid):
            lane.build_code("CLICK", bad, ELEMENTS)


def test_build_code_wait_press_scroll():
    lane = _lane()
    assert "time.sleep(2.5)" in lane.build_code("WAIT", 2.5, ELEMENTS)
    assert "pyautogui.hotkey('ctrl', 's')" in lane.build_code(
        "PRESS_KEY", ["ctrl", "s"], ELEMENTS)
    assert "pyautogui.scroll(3)" in lane.build_code("SCROLL_UP", 0, ELEMENTS)
    assert "pyautogui.scroll(-3)" in lane.build_code("SCROLL_DOWN", 2, ELEMENTS)


def test_build_code_open_app_delegates_to_fastlaunch(monkeypatch):
    # rehan.fastlaunch is not shipped in this repo; inject a mock module
    # to verify build_code delegates to it.
    fake = types.ModuleType("rehan.fastlaunch")
    fake.build_open_code = lambda app: f"S3REHAN_LAUNCH:{app}"
    monkeypatch.setitem(sys.modules, "rehan.fastlaunch", fake)
    code = _lane().build_code("OPEN_APP", "notepad", ELEMENTS)
    assert code == "S3REHAN_LAUNCH:notepad"


def test_build_code_drag_needs_box():
    lane = _lane()
    assert "dragTo" in lane.build_code("DRAG", 1, ELEMENTS)
    nobox = [{"role": "b", "name": "x", "x": 1, "y": 2, "w": 0, "h": 0}]
    with pytest.raises(JevInvalid):
        lane.build_code("DRAG", 1, nobox)


# ── check_fresh ─────────────────────────────────────────────────────────

def test_check_fresh_without_coordcache_returns_false(monkeypatch):
    # rehan.coordcache is not shipped in this repo; the lane must degrade
    # to "not fresh" instead of crashing.
    monkeypatch.delitem(sys.modules, "rehan.coordcache", raising=False)
    assert _lane().check_fresh(b"a", b"a") is False


def test_check_fresh_delegates_to_frames_match(monkeypatch):
    fake = types.ModuleType("rehan.coordcache")
    fake.frames_match = lambda a, b: a == b
    monkeypatch.setitem(sys.modules, "rehan.coordcache", fake)
    lane = _lane()
    assert lane.check_fresh(b"same", b"same") is True
    assert lane.check_fresh(b"a", b"b") is False


# ── env toggle ──────────────────────────────────────────────────────────

def test_jev_enabled_env(monkeypatch):
    monkeypatch.delenv("S3REHAN_JEV", raising=False)
    assert jev_enabled() is True
    monkeypatch.setenv("S3REHAN_JEV", "0")
    assert jev_enabled() is False
    monkeypatch.setenv("S3REHAN_JEV", "1")
    assert jev_enabled() is True


# ── run_jev_steps orchestration (scripted lane, no proxy) ───────────────

class _ScriptedLane(JevLane):
    """Fake decide/decide_json/gen_text; real parse/build/check logic."""

    def __init__(self, script, texts=None, fresh=True, json_fail=False):
        super().__init__("http://127.0.0.1:9", "m", "m")
        self.script = list(script)
        self.texts = texts or {}
        self.fresh = fresh
        self.decide_calls = 0
        self.json_fail = json_fail

    def decide(self, task, table, correction="", temperature=0):
        self.decide_calls += 1
        return parse_decision(self.script.pop(0))

    def decide_json(self, task, table, n_rows, history=None, correction="",
                    temperature=0):
        self.decide_calls += 1
        if self.json_fail:
            raise JevInvalid("scripted JSON failure")
        return parse_decision(self.script.pop(0))

    def gen_text(self, task, element):
        return self.texts.get(element["name"], "typed text")

    def check_fresh(self, a, b):
        return self.fresh


def _run(lane, script_unused=None, **kw):
    kw.setdefault("log", lambda *a, **k: None)
    return run_jev_steps(kw.pop("task", "do x"), lane,
                         kw.pop("observe", lambda: (ELEMENTS, b"p")),
                         kw.pop("exec_fn", lambda c: None),
                         kw.pop("screenshot_fn", lambda: b"p"), **kw)


def test_run_jev_steps_click_then_done():
    lane = _ScriptedLane(["CLICK [1]", "DONE"])
    executed = []
    status = _run(lane, task="click Submit",
                  exec_fn=lambda code: executed.append(code) or None)
    assert status == "done"
    assert len(executed) == 1
    assert "pyautogui.click(100, 200)" in executed[0]
    assert lane.decide_calls == 2


def test_run_jev_steps_blocked_escalates():
    lane = _ScriptedLane(["BLOCKED"])
    assert _run(lane) == "blocked"


def test_run_jev_steps_stale_frame_escalates_after_strikes():
    lane = _ScriptedLane(
        ["CLICK [1]", "CLICK [2]", "CLICK [3]", "CLICK [1]"] + ["CLICK [2]"] * 6,
        fresh=False)
    observes = []

    def observe():
        observes.append(1)
        return ELEMENTS, b"png"

    status = _run(lane, observe=observe, screenshot_fn=lambda: b"other")
    assert status == "blocked"
    # 1 initial observe + 3 re-observes = 4; the 4th stale frame escalates.
    assert len(observes) == 4
    # No input ever executed on a stale frame.
    assert lane.decide_calls == 4


def test_run_jev_steps_type_text_uses_text_model():
    lane = _ScriptedLane(["TYPE_TEXT [2]", "DONE"],
                         texts={"Search box": "zurich"})
    executed = []
    status = _run(lane, task="search for zurich",
                  exec_fn=lambda code: executed.append(code) or None)
    assert status == "done"
    assert "zurich" in executed[0]


def test_run_jev_steps_budget():
    lane = _ScriptedLane(["WAIT [1]", "WAIT [2]", "WAIT [3]"] + ["WAIT [4]"] * 50)
    assert _run(lane, task="wait around", max_steps=3) == "budget"


def test_run_jev_steps_loop_guard_escalates():
    lane = _ScriptedLane(["WAIT [1]"] * 10)
    status = _run(lane, task="wait around", max_steps=30)
    assert status == "blocked"
    assert lane.decide_calls == 3  # stuck after 3 identical decisions


def test_run_jev_steps_inline_text_skips_text_model():
    class _NoGen(_ScriptedLane):
        def gen_text(self, task, element):
            raise AssertionError("gen_text should not be called for inline text")

    lane = _NoGen(["TYPE_TEXT [2] winver", "DONE"])
    executed = []
    status = _run(lane, task="type winver",
                  exec_fn=lambda code: executed.append(code) or None)
    assert status == "done"
    assert "winver" in executed[0]
    assert "pyautogui.click(300, 120)" in executed[0]


def test_run_jev_steps_json_failure_falls_back_to_legacy():
    # JSON path exhausted -> legacy free-text path still drives the task.
    lane = _ScriptedLane(["CLICK [1]", "DONE"], json_fail=True)
    executed = []
    status = _run(lane, task="click Submit",
                  exec_fn=lambda code: executed.append(code) or None)
    assert status == "done"
    assert "pyautogui.click(100, 200)" in executed[0]


def test_run_jev_steps_both_paths_invalid_escalates():
    class _BadBoth(_ScriptedLane):
        def decide_json(self, *a, **k):
            raise JevInvalid("bad json")

        def decide(self, *a, **k):
            raise JevInvalid("bad legacy")

    assert _run(_BadBoth([])) == "blocked"


def test_run_jev_steps_transport_error_escalates():
    class _Down(_ScriptedLane):
        def decide_json(self, *a, **k):
            raise JevTransport("proxy down")

    assert _run(_Down([])) == "blocked"


def test_run_jev_steps_exec_exception_escalates():
    lane = _ScriptedLane(["CLICK [1]", "DONE"])

    def boom(code):
        raise RuntimeError("pc exploded")

    assert _run(lane, exec_fn=boom) == "blocked"

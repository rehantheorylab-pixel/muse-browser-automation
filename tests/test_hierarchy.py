"""Tests for rehan/hierarchy: CEO -> Manager -> Agent -> SubAgent.

All executors/decomposers are injected mocks (proxy offline). Display
isolation degrades to the shim on this VM (no pyvda).
"""

import pytest

from rehan.hierarchy import (
    AGENT, CEO, MANAGER, SUBAGENT, MAX_DEPTH,
    HandoffEnvelope, TaskResult, TaskSpec, TaskStatus,
    ResourceGuard, ScopeGuard, HandoffError, perform_handoff,
    OrgAgent, SubAgent, Manager, ManagerError, CEOAgent, CEOError,
    AgentDepthError,
)


def ok_executor(task: TaskSpec) -> TaskResult:
    return TaskResult(task_id=task.id, ok=True,
                      summary=f"did: {task.title}")


# ---------------------------------------------------------------- contracts
def test_task_spec_roundtrip():
    t = TaskSpec(title="x", level=AGENT, specialist="browser")
    d = t.to_dict()
    t2 = TaskSpec.from_dict(d)
    assert t2.id == t.id and t2.specialist == "browser"
    assert t2.status == TaskStatus.PENDING


def test_task_spec_children():
    t = TaskSpec(title="p")
    t.add_child("c1")
    t.add_child("c1")
    assert t.children_ids == ["c1"]


def test_result_roundtrip():
    r = TaskResult(task_id="t1", ok=False, error="boom")
    r2 = TaskResult.from_dict(r.to_dict())
    assert r2.error == "boom" and not r2.ok


def test_handoff_envelope_roundtrip():
    e = HandoffEnvelope(task_id="t", from_node="a", to_node="b",
                        reason="busy")
    e2 = HandoffEnvelope.from_dict(e.to_dict())
    assert e2.to_node == "b" and e2.reason == "busy"


def test_levels():
    assert (CEO, MANAGER, AGENT, SUBAGENT) == (0, 1, 2, 3)
    assert MAX_DEPTH == SUBAGENT


# ---------------------------------------------------------------- guardrails
def test_scope_guard_relaxed_allows_everything():
    g = ScopeGuard("n1")
    assert g.check("delete", "/tmp/x") is True
    assert g.check("write", "/tmp/y") is True
    s = g.summary()
    assert s["actions"] == 2 and s["risky"] == 1  # delete flagged, not blocked
    assert len(g.risky_entries()) == 1


def test_resource_guard_soft_caps():
    r = ResourceGuard("n1", max_tasks=2, max_children=1)
    assert r.note_task() is True
    assert r.note_task() is True
    assert r.note_task() is False  # over budget: recorded, still allowed
    assert r.note_child() is True
    assert r.note_child() is False
    assert len(r.summary()["violations"]) == 2


# ---------------------------------------------------------------- handoff
def test_handoff_peer_ok():
    t = TaskSpec(title="t")
    env = perform_handoff(t, "a1", "a2", AGENT, AGENT, reason="busy")
    assert isinstance(env, HandoffEnvelope)
    assert t.assignee == "a2" and t.status == TaskStatus.HANDED_OFF


def test_handoff_cross_level_rejected():
    t = TaskSpec(title="t")
    with pytest.raises(HandoffError):
        perform_handoff(t, "a1", "m1", AGENT, MANAGER)


def test_handoff_self_rejected():
    t = TaskSpec(title="t")
    with pytest.raises(HandoffError):
        perform_handoff(t, "a1", "a1", AGENT, AGENT)


# ---------------------------------------------------------------- agent
def test_org_agent_execute_mock():
    a = OrgAgent(name="a1", specialist="browser", executor=ok_executor)
    t = TaskSpec(title="click login", level=AGENT)
    r = a.execute(t)
    assert r.ok and r.by == "a1"
    assert t.status == TaskStatus.DONE
    assert a.results[t.id].summary.startswith("did:")


def test_org_agent_executor_exception_becomes_failed_result():
    def boom(task):
        raise RuntimeError("kaput")

    a = OrgAgent(name="a1", executor=boom)
    r = a.execute(TaskSpec(title="t"))
    assert not r.ok and "kaput" in r.error


def test_org_agent_no_executor_accepts():
    a = OrgAgent(name="a1")
    r = a.execute(TaskSpec(title="t"))
    assert r.ok and "no executor" in r.summary


def test_subagent_cannot_spawn():
    a = OrgAgent(name="a1", executor=ok_executor)
    sub = a.spawn_subagent(name="s1")
    assert isinstance(sub, SubAgent)
    assert sub.level == SUBAGENT
    assert sub.parent is a
    with pytest.raises(AgentDepthError):
        sub.spawn_subagent(name="s2")
    a.cleanup()


def test_agent_handoff_to_peer():
    a1 = OrgAgent(name="a1", executor=ok_executor)
    a2 = OrgAgent(name="a2", executor=ok_executor)
    a1.add_peer(a2)
    t = TaskSpec(title="t", level=AGENT)
    a1.accept(t)
    env = a1.handoff(t, a2, reason="overloaded")
    assert env.to_node == "a2"
    assert t.id in a2.tasks  # peer now owns it
    assert t.id not in a1.tasks
    a1.cleanup()
    a2.cleanup()


def test_agent_handoff_wrong_level_rejected():
    a = OrgAgent(name="a1", executor=ok_executor)
    m = Manager(name="m1")
    t = TaskSpec(title="t", level=AGENT)
    a.accept(t)
    with pytest.raises(HandoffError):
        a.handoff(t, m)  # type: ignore[arg-type]
    a.cleanup()


def test_agent_display_fallback_shim():
    a = OrgAgent(name="a1")
    a.start()
    assert a.display_backend in ("monitor", "vdesktop", "shim")
    a.cleanup()


# ---------------------------------------------------------------- manager
def test_manager_run_default_decompose():
    m = Manager(name="m1")
    a = OrgAgent(name="a1", specialist="browser", executor=ok_executor)
    a.start()
    m.hire(a)
    r = m.run(TaskSpec(title="build login", level=MANAGER,
                       specialist="browser"))
    assert r.ok and "1/1" in r.summary
    a.cleanup()


def test_manager_specialist_routing():
    seen = []

    def exec1(task):
        seen.append(("a1", task.title))
        return TaskResult(task_id=task.id, ok=True, summary="ok")

    def exec2(task):
        seen.append(("a2", task.title))
        return TaskResult(task_id=task.id, ok=True, summary="ok")

    m = Manager(name="m1",
                decompose=lambda t: [TaskSpec(title="fe", level=AGENT,
                                             specialist="browser"),
                                     TaskSpec(title="be", level=AGENT,
                                             specialist="linux_shell")])
    a1 = OrgAgent(name="a1", specialist="browser", executor=exec1)
    a2 = OrgAgent(name="a2", specialist="linux_shell", executor=exec2)
    for a in (a1, a2):
        a.start()
        m.hire(a)
    r = m.run(TaskSpec(title="site", level=MANAGER))
    assert r.ok
    assert ("a1", "fe") in seen and ("a2", "be") in seen
    for a in (a1, a2):
        a.cleanup()


def test_manager_no_agents_raises():
    m = Manager(name="m1")
    with pytest.raises(ManagerError):
        m.run(TaskSpec(title="t", level=MANAGER))


def test_manager_handoff_peer():
    m1 = Manager(name="m1")
    m2 = Manager(name="m2")
    m1.add_peer(m2)
    t = TaskSpec(title="t", level=MANAGER)
    m1.accept(t)
    peer, env = m1.handoff(t, m2, reason="rebalance")
    assert peer is m2 and env.to_node == "m2"
    assert t.id not in m1.tasks


# ---------------------------------------------------------------- CEO
def test_ceo_submit_goal_default():
    ceo = CEOAgent(name="ceo1")
    m = Manager(name="m1")
    a = OrgAgent(name="a1", executor=ok_executor)
    a.start()
    m.hire(a)
    ceo.hire(m)
    r = ceo.submit_goal("build website", "a small site")
    assert r.ok and "1/1" in r.summary
    a.cleanup()


def test_ceo_no_managers_raises():
    ceo = CEOAgent(name="ceo1")
    with pytest.raises(CEOError):
        ceo.submit_goal("x")


def test_ceo_status_tree():
    ceo = CEOAgent(name="ceo1")
    m = ceo.spawn_manager(name="m-backend")
    a = m.spawn_agent(name="a-login", specialist="pc_app")
    s = a.spawn_subagent(name="s-signup")
    st = ceo.status()
    assert st["managers"] == ["m-backend"]
    assert st["tree"]["m-backend"]["agents"] == ["a-login"]
    assert st["tree"]["m-backend"]["subagents"] == {"a-login": ["s-signup"]}
    assert s.level == SUBAGENT
    ceo_tree_cleanup(ceo)


def ceo_tree_cleanup(ceo):
    for m in ceo.managers:
        for a in m.agents:
            a.cleanup()


# ---------------------------------------------------------------- website scenario (Rehan's example)
def website_decompose(goal: TaskSpec):
    """CEO: 'build website' -> backend / frontend / seo manager slices."""
    return [TaskSpec(title="backend", description="server + login system",
                     level=MANAGER, specialist="linux_shell"),
            TaskSpec(title="frontend", description="pages",
                     level=MANAGER, specialist="browser"),
            TaskSpec(title="seo", description="meta + sitemap",
                     level=MANAGER, specialist="file_ops")]


def backend_decompose(task: TaskSpec):
    return [TaskSpec(title="login system", level=AGENT,
                     specialist="linux_shell")]


def frontend_decompose(task: TaskSpec):
    return [TaskSpec(title="login page", level=AGENT, specialist="browser"),
            TaskSpec(title="front page", level=AGENT, specialist="browser"),
            TaskSpec(title="landing page", level=AGENT, specialist="browser")]


def login_agent_decompose_via_agent(task: TaskSpec):
    """The login agent itself fans out to sub-agents (done in test)."""
    return []


def test_website_scenario_end_to_end():
    """Rehan's example: CEO('build website') -> managers -> agents ->
    sub-agents (signup button, animations). All mocked, all asserted."""
    ceo = CEOAgent(name="ceo1", decompose=website_decompose)

    m_backend = Manager(name="m-backend", decompose=backend_decompose)
    m_frontend = Manager(name="m-frontend", decompose=frontend_decompose)
    m_seo = Manager(name="m-seo")  # default: single agent task
    for m in (m_backend, m_frontend, m_seo):
        ceo.hire(m)

    a_login_sys = OrgAgent(name="a-login-sys", specialist="linux_shell",
                           executor=ok_executor)
    a_login_page = OrgAgent(name="a-login-page", specialist="browser",
                            executor=ok_executor)
    a_front = OrgAgent(name="a-front", specialist="browser",
                       executor=ok_executor)
    a_landing = OrgAgent(name="a-landing", specialist="browser",
                         executor=ok_executor)
    a_seo = OrgAgent(name="a-seo", specialist="file_ops",
                     executor=ok_executor)
    for a in (a_login_sys, a_login_page, a_front, a_landing, a_seo):
        a.start()
    m_backend.hire(a_login_sys)
    for a in (a_login_page, a_front, a_landing):
        m_frontend.hire(a)
    m_seo.hire(a_seo)

    # The login-system agent spawns sub-agents for signup button + animations.
    s_signup = a_login_sys.spawn_subagent(name="s-signup",
                                          specialist="vision_grounding")
    s_anim = a_login_sys.spawn_subagent(name="s-anim",
                                        specialist="browser")
    for s in (s_signup, s_anim):
        r = s.execute(TaskSpec(title=f"subtask-{s.name}", level=SUBAGENT))
        assert r.ok

    # Sub-agents cannot go deeper.
    with pytest.raises(AgentDepthError):
        s_signup.spawn_subagent(name="too-deep")

    result = ceo.submit_goal("build website")
    assert result.ok
    assert "3/3" in result.summary  # 3 manager slices ok

    # Hierarchy integrity: every task has a parent chain to the goal.
    assert a_login_sys.level == AGENT
    assert s_signup.level == SUBAGENT
    assert s_signup.parent is a_login_sys

    # Guard audit captured the whole run (relaxed: nothing blocked).
    total_actions = (ceo.guard.summary()["actions"]
                     + m_backend.guard.summary()["actions"]
                     + a_login_sys.guard.summary()["actions"])
    assert total_actions > 0

    for a in (a_login_sys, a_login_page, a_front, a_landing, a_seo):
        a.cleanup()

import json

import pytest

from foreman.core.models import Project
from foreman.planner import (
    ACCEPTANCE,
    DOCS,
    SETUP,
    PlanError,
    chat_stage,
    chat_system_prompt,
    decompose_system_prompt,
    find_cycle,
    parse_chat,
    parse_tasks,
    render_tasks_md,
    standard_tasks,
    topo_order,
    writable,
)

P = Project(name="todo", repo_url="x", feature="001-initial")
D = "specs/001-initial"


def test_parse_chat_extracts_documents():
    text = f"""I drafted the requirements.

<<<FILE {D}/requirements.md
# Requirements
- FR-1.1 Add todos.
>>>

What storage do you want?"""
    out = parse_chat(P, text)
    assert out.files == {f"{D}/requirements.md": "# Requirements\n- FR-1.1 Add todos."}
    assert f"[wrote {D}/requirements.md]" in out.message
    assert "What storage do you want?" in out.message
    assert "FR-1.1" not in out.message  # file bodies are not repeated in the chat


def test_planner_may_not_write_code_tests_or_commands():
    text = (
        "<<<FILE src/app.py\nx\n>>>\n"
        "<<<FILE tests/acceptance/test_x.py\nx\n>>>\n"
        "<<<FILE .foreman/Dockerfile\nFROM x\n>>>\n"
        "<<<FILE ../../etc/passwd\nx\n>>>\n"
        "<<<ACCEPTANCE_CMD\npytest\n>>>\n"
    )
    out = parse_chat(P, text)
    assert out.files == {}
    assert out.rejected == [
        "src/app.py",
        "tests/acceptance/test_x.py",
        ".foreman/Dockerfile",
        "../../etc/passwd",
        "an acceptance command",
    ]


def test_writable_is_the_three_planning_documents():
    for name in ("requirements.md", "design.md", "acceptance-criteria.md"):
        assert writable(P, f"{D}/{name}")
    assert not writable(P, f"{D}/tasks.md")  # Foreman keeps the task list
    assert not writable(P, f"{D}/notes.md")
    assert not writable(P, "specs/other/requirements.md")
    assert not writable(P, "tests/acceptance/test_x.py")
    assert not writable(P, "src/main.py")


TASKS = """Here you go:
```json
[
  {"key": "T2", "title": "Search", "goal": "Find todos by text.", "deps": ["T1"],
   "criteria": ["Searching shows matching todos"], "req_refs": "FR-1.2"},
  {"key": "T1", "title": "Store", "goal": "Todos are kept between runs.",
   "criteria": ["A saved todo is there after a restart"], "size": "medium"}
]
```"""


def test_parse_tasks_and_order():
    tasks = parse_tasks(TASKS)
    assert [t.key for t in topo_order(tasks)] == ["T1", "T2"]
    assert tasks[0].req_refs == ["FR-1.2"]  # a bare string is accepted as a list
    assert tasks[0].goal == "Find todos by text." and tasks[1].size == "medium"


def task(**kw):
    return {"key": "A", "title": "a", "goal": "g", "criteria": ["c"], **kw}


@pytest.mark.parametrize(
    "items, problem",
    [
        ([task(deps=["B"])], "unknown deps B"),
        ([task(), task(title="b")], "duplicate or reserved keys"),
        ([task(key="SETUP")], "duplicate or reserved keys"),
        ([task(deps=["B"]), task(key="B", deps=["A"])], "dependency cycle"),
        ([{"key": "A", "goal": "g", "criteria": ["c"]}], "missing title"),
        ([task(criteria=[])], "at least one acceptance criterion"),
        ([task(criteria=[str(i) for i in range(7)])], "7 criteria"),
        ([task(size="large")], "too big; split it"),
        ([task(allowed_files=["src/a.py"])], "don't give allowed_files"),
        ([task(validation_cmd="pytest")], "don't give validation_cmd"),
    ],
)
def test_parse_tasks_problems(items, problem):
    with pytest.raises(PlanError, match=problem):
        parse_tasks(json.dumps(items))


@pytest.mark.parametrize(
    "text, problem",
    [("no json here", "no JSON array"), ("[1, 2", "no JSON array"), ("[]", "non-empty")],
)
def test_parse_tasks_bad_json(text, problem):
    with pytest.raises(PlanError, match=problem):
        parse_tasks(text)


def test_standard_tasks_frame_the_plan():
    planned = standard_tasks(parse_tasks(TASKS))
    keys = [t.key for t in planned]
    assert keys[0] == SETUP and keys[1] == ACCEPTANCE and keys[-1] == DOCS
    by_key = {t.key: t for t in planned}
    assert by_key["T1"].deps == [SETUP]  # everything waits for setup
    assert by_key["T2"].deps == ["T1"]
    assert set(by_key[DOCS].deps) == {ACCEPTANCE, "T1", "T2"}  # docs come last
    assert [t.kind for t in planned] == ["setup", "acceptance", "feature", "feature", "docs"]
    assert any("README.md" in c for c in by_key[SETUP].criteria)


def test_find_cycle():
    assert find_cycle({"a": ["b"], "b": ["c"], "c": ["a"]}) == ["a", "b", "c", "a"]
    assert find_cycle({"a": ["b"], "b": []}) == []


def test_render_tasks_md():
    md = render_tasks_md(
        P,
        [
            {
                "key": "T1",
                "title": "Store",
                "goal": "Keep todos.",
                "criteria": ["A todo survives a restart"],
                "req_refs": ["FR-1.1"],
                "deps": [],
                "kind": "feature",
                "violations": ["too big"],
            }
        ],
    )
    assert "## T1: Store" in md and "Keep todos." in md
    assert "Done when:\n- A todo survives a restart" in md
    assert "> Size check: too big" in md


PLANNING = {
    "requirements": "- FR-1 x",
    "design": "Two parts.",
    "acceptance": "- AC-1 (FR-1) works",
}


def test_chat_stage_moves_from_design_to_review():
    assert chat_stage({}) == "design"
    assert chat_stage({**PLANNING, "acceptance": " "}) == "design"
    assert chat_stage(PLANNING) == "review"


def test_planner_prompts_think_in_plain_language():
    design = chat_system_prompt(P, {}, [])
    assert "Current step: DESIGN." in design
    review = chat_system_prompt(P, PLANNING, [])
    assert "Current step: REVIEW." in review and "Two parts." in review
    breakdown = decompose_system_prompt(P, PLANNING)
    for prompt in (design, review, breakdown):
        assert "Never write code, tests, file names or paths, shell commands" in prompt
    assert f"{D}/acceptance-criteria.md" in design
    assert "Foreman adds three standard tasks" in breakdown


def test_parse_chat_accepts_separator_style_blocks():
    # How Qwen wrote them: path on the next line, each <<<FILE ends the previous file,
    # and a bare <<<FILE before prose only terminates.
    text = f"""Here you go.
<<<FILE {D}/requirements.md
# Requirements
<<<FILE
{D}/design.md
# Design
<<<FILE

Have a look.
Done."""
    out = parse_chat(P, text)
    assert out.files == {f"{D}/requirements.md": "# Requirements", f"{D}/design.md": "# Design"}
    assert out.message.splitlines() == [
        "Here you go.",
        f"[wrote {D}/requirements.md]",
        f"[wrote {D}/design.md]",
        "",
        "Have a look.",
        "Done.",
    ]


def test_parse_chat_does_not_apply_a_cut_off_file():
    out = parse_chat(P, f"Writing.\n<<<FILE {D}/design.md\n# Des")
    assert out.files == {}
    assert out.message.endswith(f"[incomplete file {D}/design.md: not written]")


def test_parse_chat_bare_terminator_before_prose_is_not_a_path():
    out = parse_chat(P, f"<<<FILE {D}/design.md\n# S\n<<<FILE\nAll done here.")
    assert out.files == {f"{D}/design.md": "# S"}
    assert out.message.endswith("All done here.")


def test_old_projects_keep_their_document_names(tmp_path, monkeypatch):
    from foreman import gitops
    from foreman.core import roles

    def exists(rel: str) -> bool:
        return rel in {f"{D}/spec.md", f"{D}/plan.md"}

    assert roles.doc_file(P, "requirements", exists) == f"{D}/spec.md"
    assert roles.doc_file(P, "design", exists) == f"{D}/plan.md"
    assert roles.doc_file(P, "acceptance", exists) == f"{D}/acceptance-criteria.md"
    assert gitops  # imported for the repo-backed variant used by doc_path

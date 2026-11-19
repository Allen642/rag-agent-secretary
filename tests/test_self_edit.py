"""``scripts/self_edit.py``（mini coding agent）的流程与安全边界测试。

覆盖：

1. 正常链路：规划 -> 读文件 -> 应用修改 -> pytest 通过 -> commit + push；
2. 安全边界：规划或修改里出现 ``secretary/``、``scripts/``、``tests/`` 之外的
   路径时直接拒绝，不落盘、不提交；
3. 失败重试：old 未精确命中和 pytest 失败都会回喂模型重试，最多 2 轮；
   仍失败时清理 worktree、删除空分支并给出失败原因。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from secretary.config import get_settings

# scripts 不是包，按文件路径加载被测算模块
_PATH = Path(__file__).resolve().parents[1] / "scripts" / "self_edit.py"
_SPEC = importlib.util.spec_from_file_location("self_edit_for_test", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
self_edit = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = self_edit
_SPEC.loader.exec_module(self_edit)

ORIGINAL = "def hello():\n    return 'hi'\n"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """带一个提交和 bare origin 的临时仓库，方便验证 commit + push。"""

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "tester")
    _git(root, "remote", "add", "origin", str(remote))

    (root / "scripts").mkdir()
    (root / "scripts" / "demo.py").write_text(ORIGINAL, encoding="utf-8")
    (root / "secretary").mkdir()
    (root / "secretary" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_demo.py").write_text(
        "from scripts.demo import hello\n\n\ndef test_hello():\n    assert hello()\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text("data/runtime/\n", encoding="utf-8")

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


class _FakeLLM:
    """按顺序返回预设文本，并记住每次收到的 prompt。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> SimpleNamespace:
        self.prompts.append(prompt)
        if not self.replies:
            raise AssertionError("模型被调用的次数超出预期")
        return SimpleNamespace(content=self.replies.pop(0))


def _plan(*files: str) -> str:
    return json.dumps(list(files), ensure_ascii=False)


def _edits(*pairs: dict[str, str]) -> str:
    return json.dumps(list(pairs), ensure_ascii=False)


def _replace_hello(new: str = "return 'hello'") -> str:
    return _edits({"path": "scripts/demo.py", "old": "return 'hi'", "new": new})


def _worktree_exists(root: Path, name: str) -> bool:
    return any(
        name in line
        for line in _git(root, "worktree", "list").splitlines()
    )


def _branch_exists(root: Path, branch: str) -> bool:
    output = _git(root, "branch", "--list", branch)
    return bool(output.strip())


# ------------------------------------------------------------ 路径安全


@pytest.mark.parametrize(
    "path",
    [
        "secretary/graph.py",
        "scripts/wecom_bot.py",
        "tests/test_self_edit.py",
    ],
)
def test_is_allowed_path_accepts_project_dirs(path: str) -> None:
    assert self_edit.is_allowed_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "",
        "app.py",
        ".env",
        "README.md",
        "docs/notes.md",
        "data/runtime/x.py",
        "secretary/../app.py",
        "tests/../tests/test_self_edit.py",
        "../outside.py",
        "/etc/passwd",
        "C:/Windows/system.ini",
        "secretary",
        "secretary/",
    ],
)
def test_is_allowed_path_rejects_outside_paths(path: str) -> None:
    assert self_edit.is_allowed_path(path) is False


# ------------------------------------------------------------ 正常流程


def test_self_edit_happy_path_commits_and_pushes(repo: Path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_pytest(worktree, python_exe=None):
        seen["worktree"] = Path(worktree)
        seen["content"] = (Path(worktree) / "scripts" / "demo.py").read_text(
            encoding="utf-8"
        )
        return True, "2 passed in 0.12s"

    monkeypatch.setattr(self_edit, "run_pytest", fake_pytest)
    llm = _FakeLLM([_plan("scripts/demo.py"), _replace_hello()])

    result = self_edit.self_edit(
        "把 hello 的返回值改成 hello",
        llm=llm,
        repo_root=repo,
        branch="auto/test-happy",
    )

    assert "✅" in result
    assert "auto/test-happy" in result
    assert "通过" in result
    assert "scripts/demo.py" in result

    # 修改发生在隔离 worktree 里，主仓库的工作区不受影响
    assert seen["content"] == "def hello():\n    return 'hello'\n"
    assert (repo / "scripts" / "demo.py").read_text(encoding="utf-8") == ORIGINAL

    # worktree 已清理，但分支和提交已推送
    assert not _worktree_exists(repo, "test-happy")
    assert not (repo / "data" / "runtime" / "auto-edit" / "test-happy").exists()
    assert _branch_exists(repo, "auto/test-happy")
    committed = _git(repo, "show", "auto/test-happy:scripts/demo.py")
    assert "return 'hello'" in committed

    remote = repo.parent / "remote.git"
    assert _git(remote, "rev-parse", "auto/test-happy").strip()


def test_self_edit_reads_planned_files_into_prompt(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        self_edit, "run_pytest", lambda worktree, python_exe=None: (True, "1 passed")
    )
    llm = _FakeLLM([_plan("scripts/demo.py"), _replace_hello()])

    self_edit.self_edit(
        "改 return 值", llm=llm, repo_root=repo, branch="auto/test-context"
    )

    assert len(llm.prompts) == 2
    # 第一轮是规划；第二轮要带上需求和被读文件的内容
    assert "改 return 值" in llm.prompts[0]
    assert "=== scripts/demo.py ===" in llm.prompts[1]
    assert "return 'hi'" in llm.prompts[1]


# ------------------------------------------------------------ 越界拒绝


def test_self_edit_rejects_out_of_bounds_plan(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        self_edit, "run_pytest", lambda worktree, python_exe=None: (True, "1 passed")
    )
    llm = _FakeLLM([_plan("../outside.py")])

    result = self_edit.self_edit(
        "改点东西", llm=llm, repo_root=repo, branch="auto/test-plan-oob"
    )

    assert "❌" in result
    assert "越界" in result
    assert not _worktree_exists(repo, "test-plan-oob")
    assert not _branch_exists(repo, "auto/test-plan-oob")
    assert not (repo / "data" / "runtime" / "auto-edit" / "test-plan-oob").exists()


def test_self_edit_rejects_out_of_bounds_edit_path(repo: Path, monkeypatch) -> None:
    """模型改用修改对里的 path 越界，同样要在落盘前拒绝。"""

    monkeypatch.setattr(
        self_edit, "run_pytest", lambda worktree, python_exe=None: (True, "1 passed")
    )
    bad = _edits({"path": ".env", "old": "A=1", "new": "A=2"})
    llm = _FakeLLM([_plan("scripts/demo.py"), bad, bad, bad])

    result = self_edit.self_edit(
        "改环境变量", llm=llm, repo_root=repo, branch="auto/test-edit-oob"
    )

    assert "❌" in result
    assert "越界" in result
    assert "已尝试 3 轮" in result
    assert not _worktree_exists(repo, "test-edit-oob")
    assert not _branch_exists(repo, "auto/test-edit-oob")


def test_self_edit_rejects_non_matching_old_and_retries(repo: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_pytest(worktree, python_exe=None):
        calls.append("pytest")
        return True, "1 passed"

    monkeypatch.setattr(self_edit, "run_pytest", fake_pytest)
    wrong = _edits({"path": "scripts/demo.py", "old": "return 'nope'", "new": "x"})
    llm = _FakeLLM([_plan("scripts/demo.py"), wrong, _replace_hello()])

    result = self_edit.self_edit(
        "改 return 值", llm=llm, repo_root=repo, branch="auto/test-old-miss"
    )

    assert "✅" in result
    assert calls == ["pytest"]  # 只有命中后才进 pytest
    assert "命中 0 次" in llm.prompts[2]  # 错误被回喂给模型


# ------------------------------------------------------------ 失败重试


def test_self_edit_retries_after_pytest_failure(repo: Path, monkeypatch) -> None:
    results = iter(
        [
            (False, "1 failed in 0.10s"),
            (False, "1 failed in 0.10s"),
            (True, "2 passed in 0.10s"),
        ]
    )
    calls: list[int] = []

    def fake_pytest(worktree, python_exe=None):
        calls.append(len(calls) + 1)
        return next(results)

    monkeypatch.setattr(self_edit, "run_pytest", fake_pytest)
    llm = _FakeLLM(
        [
            _plan("scripts/demo.py"),
            _replace_hello(),
            _replace_hello(),
            _replace_hello(),
        ]
    )

    result = self_edit.self_edit(
        "改 return 值", llm=llm, repo_root=repo, branch="auto/test-retry"
    )

    assert "✅" in result
    assert calls == [1, 2, 3]
    # 第三轮的 prompt 要带上 pytest 失败输出
    assert "1 failed" in llm.prompts[-1]
    assert _branch_exists(repo, "auto/test-retry")


def test_self_edit_gives_up_cleans_worktree_and_branch(repo: Path, monkeypatch) -> None:
    calls: list[int] = []

    def always_fail(worktree, python_exe=None):
        calls.append(len(calls) + 1)
        return False, "1 failed, 1 passed in 0.20s"

    monkeypatch.setattr(self_edit, "run_pytest", always_fail)
    llm = _FakeLLM(
        [
            _plan("scripts/demo.py"),
            _replace_hello(),
            _replace_hello(),
            _replace_hello(),
        ]
    )

    result = self_edit.self_edit(
        "改 return 值", llm=llm, repo_root=repo, branch="auto/test-giveup"
    )

    assert "❌" in result
    assert "已尝试 3 轮" in result
    assert "1 failed" in result
    assert calls == [1, 2, 3]
    assert not _worktree_exists(repo, "test-giveup")
    assert not (repo / "data" / "runtime" / "auto-edit" / "test-giveup").exists()
    assert not _branch_exists(repo, "auto/test-giveup")


def test_self_edit_returns_usage_for_empty_requirement() -> None:
    assert self_edit.self_edit("   ") == "用法：/code <需求描述>"


# ------------------------------------------------------------ 配置读取


def test_config_reads_self_edit_user_ids(monkeypatch) -> None:
    monkeypatch.setenv("SELF_EDIT_USER_IDS", " zhangsan, lisi ,, ")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.self_edit_user_ids == ("zhangsan", "lisi")
    finally:
        get_settings.cache_clear()

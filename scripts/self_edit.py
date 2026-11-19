"""mini coding agent：接收一条中文需求，在隔离环境里改代码并跑测试。

入口只有 :func:`self_edit`，流程固定为：

1. ``git worktree add`` 建隔离副本，基于当前 HEAD 新建 ``auto/<timestamp>`` 分支；
2. 让模型规划要读的文件（只允许 ``secretary/``、``scripts/``、``tests/``）；
3. 读取这些文件，连同需求一起喂给模型；
4. 模型输出 JSON 修改对 ``[{"path", "old", "new"}]``，程序逐条做精确字符串替换；
5. 在 worktree 里用主仓库的 ``.venv`` 跑 ``python -m pytest -q``；
6. 通过：``git add`` → ``commit`` → ``push origin auto/<timestamp>``；
   失败：把 pytest 输出喂回模型重试，最多 2 轮，仍失败则报告并清理。

安全边界是硬编码的，不依赖模型自觉：

* 路径必须在 ``secretary/``、``scripts/``、``tests/`` 下，且不能跳出 worktree；
* 只执行 ``git`` 和 ``pytest``，不接受任意 shell 命令；
* worktree 固定在 ``data/runtime/auto-edit/`` 下，结束（无论成败）都清理；
  成功时保留分支供人工 review，失败时删除未提交的空分支。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger("self_edit")

# 允许读写的顶层目录（硬编码，模型无法覆盖）
ALLOWED_DIRS = ("secretary", "scripts", "tests")
# worktree 根目录，相对仓库根；已在 .gitignore 里
WORKTREE_ROOT = Path("data") / "runtime" / "auto-edit"
# 一次规划最多读多少个文件，以及单个文件喂给模型的上限
MAX_PLAN_FILES = 12
MAX_FILE_CHARS = 24_000
# 首次 + 最多 2 轮重试
MAX_ATTEMPTS = 3
PYTEST_TIMEOUT_SECONDS = 1800
GIT_TIMEOUT_SECONDS = 120
# 企业微信单条文本上限 2048 字节，留出余量
MAX_RESULT_CHARS = 1500
# pytest / 模型输出回喂时的截断长度
FEEDBACK_TAIL_CHARS = 4000

PLAN_PROMPT = """你是本项目的自动改代码助手（mini coding agent）。

项目是 Python + LangGraph 的个人秘书助手，核心代码在 secretary/、scripts/、tests/。

用户需求：
{requirement}

请判断动手前需要先读哪些文件。最多 {max_files} 个，优先选直接相关的实现和对应测试。
只能从 secretary/、scripts/、tests/ 下选择。

只输出 JSON 数组，不要任何解释，例如：
["secretary/graph.py", "tests/test_graph.py"]
"""


EDIT_PROMPT = """你是本项目的自动改代码助手（mini coding agent），请按需求修改代码。

需求：
{requirement}

当前文件内容：
{context}

{feedback}

输出要求（必须严格遵守）：
1. 只输出 JSON 数组，不要 Markdown 代码块，不要解释；
2. 每个元素形如 {{"path": "secretary/x.py", "old": "原文", "new": "新文"}}；
3. path 只能位于 secretary/、scripts/、tests/ 下，且文件必须已存在；
4. old 必须是文件里一段唯一的原文，缩进和换行都要逐字精确；
5. 可以给多条修改，按顺序应用；不要输出可能匹配多处或匹配不到的片段。

只输出 JSON 数组。
"""


class SelfEditError(RuntimeError):
    """流程中可预期的失败（路径越界、模型输出不合法、old 未命中）。"""


# --------------------------------------------------------------- 路径安全


def _normalize_path(raw: str) -> str:
    """统一分隔符并去掉开头的 ``./``，便于做白名单判断。"""

    value = (raw or "").strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def is_allowed_path(raw: str) -> bool:
    """判断相对路径是否落在允许修改的三个目录内。

    拒绝绝对路径、盘符路径、含 ``..`` 的路径，以及目录本身。
    """

    path = _normalize_path(raw)
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return False
    parts = PurePosixPath(path).parts
    if len(parts) < 2:
        return False
    if any(part in ("", ".", "..") for part in parts):
        return False
    return parts[0] in ALLOWED_DIRS


def _safe_file(worktree: Path, raw: str) -> Path:
    """把相对路径解析成 worktree 内的真实文件；越界或不存在时报错。"""

    relative = _normalize_path(raw)
    if not is_allowed_path(relative):
        raise SelfEditError(f"路径不在允许范围（只能改 {'、'.join(ALLOWED_DIRS)}）：{raw}")
    root = worktree.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise SelfEditError(f"路径跳出隔离区：{raw}")
    if not path.is_file():
        raise SelfEditError(f"文件不存在：{relative}")
    return path


# --------------------------------------------------------------- 子进程


def _git(
    cwd: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """执行一条 git 命令（不经过 shell），可选在失败时抛错。"""

    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:500]
        raise SelfEditError(f"git {' '.join(args)} 失败：{detail}")
    return result


def _default_python(repo_root: Path) -> Path:
    """优先用主仓库的虚拟环境解释器；找不到时退回当前解释器。"""

    candidates = (
        repo_root / ".venv" / "Scripts" / "python.exe",
        repo_root / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def run_pytest(
    worktree: Path,
    python_exe: Path | str | None = None,
) -> tuple[bool, str]:
    """在 worktree 里跑 ``-m pytest -q``，返回 ``(是否通过, 合并输出)``。"""

    python = Path(python_exe) if python_exe else Path(sys.executable)
    try:
        result = subprocess.run(
            [str(python), "-m", "pytest", "-q"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PYTEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"pytest 超时（>{PYTEST_TIMEOUT_SECONDS}s）"
    except OSError as exc:
        return False, f"无法启动 pytest：{exc}"

    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
    )
    return result.returncode == 0, output or f"pytest 退出码 {result.returncode}"


def create_worktree(repo_root: Path, worktree: Path, branch: str) -> None:
    """基于当前 HEAD 新建分支和隔离 worktree。"""

    if worktree.exists():
        raise SelfEditError(f"隔离目录已存在：{worktree}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "worktree", "add", "-b", branch, str(worktree))


def remove_worktree(repo_root: Path, worktree: Path) -> None:
    """强制清理 worktree；保留分支（成功时供人工 review）。"""

    if worktree.exists():
        _git(repo_root, "worktree", "remove", "--force", str(worktree), check=False)
        if worktree.exists():  # git 清理失败时兜底删目录，避免残留
            shutil.rmtree(worktree, ignore_errors=True)
    _git(repo_root, "worktree", "prune", check=False)


def reset_worktree(worktree: Path) -> None:
    """丢弃上一轮未提交的修改，保证每次重试都从 HEAD 开始。"""

    _git(worktree, "reset", "--hard", "HEAD", check=False)


def commit_and_push(
    worktree: Path,
    branch: str,
    files: list[str],
    message: str,
) -> tuple[bool, str]:
    """提交改动并推送分支，返回 ``(push 是否成功, 说明)``。"""

    if not files:
        raise SelfEditError("没有可提交的改动")
    # 自动提交固定用 bot 身份，避免服务器上没配 user.name / user.email
    identity = [
        "-c",
        "user.name=auto-edit",
        "-c",
        "user.email=auto-edit@localhost",
    ]
    _git(worktree, "add", "--", *files)
    _git(worktree, *identity, "commit", "-m", message)
    pushed = _git(worktree, "push", "-u", "origin", branch, check=False)
    if pushed.returncode == 0:
        return True, "已推送 origin"
    detail = (pushed.stderr or pushed.stdout).strip()[:300]
    return False, f"push 失败：{detail or '未知原因'}"


# --------------------------------------------------------------- 模型交互


def _invoke(llm: Any, prompt: str) -> str:
    """调用聊天模型并取出文本，兼容 str / content blocks 两种返回。"""

    response = llm.invoke(prompt)
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict) and "text" in item:
                chunks.append(str(item["text"]))
        return "".join(chunks)
    return str(content)


def _parse_json(text: str) -> Any:
    """从模型输出里提取 JSON，容忍 Markdown 代码块和前后说明。"""

    cleaned = (text or "").strip()
    fence = re.match(r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n```\s*$", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("[", "]"), ("{", "}")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise SelfEditError("模型没有返回合法 JSON")


def plan_files(requirement: str, llm: Any) -> list[str]:
    """第一阶段：让模型给出要读的文件列表，并做白名单校验。"""

    prompt = PLAN_PROMPT.format(
        requirement=requirement, max_files=MAX_PLAN_FILES
    )
    data = _parse_json(_invoke(llm, prompt))
    if isinstance(data, dict):
        data = data.get("files") or data.get("paths") or []
    if not isinstance(data, list):
        raise SelfEditError("规划结果必须是 JSON 数组")

    files: list[str] = []
    for item in data:
        relative = _normalize_path(item) if isinstance(item, str) else ""
        if not is_allowed_path(relative):
            raise SelfEditError(f"规划了越界路径，拒绝执行：{item}")
        if relative not in files:
            files.append(relative)
    if not files:
        raise SelfEditError("模型没有给出要读的文件")
    return files[:MAX_PLAN_FILES]


def read_files(worktree: Path, files: list[str]) -> str:
    """读取文件内容并拼成带路径标题的上下文。"""

    chunks: list[str] = []
    for relative in files:
        path = _safe_file(worktree, relative)
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + "\n……（文件过长，已截断）"
        chunks.append(f"=== {relative} ===\n{text}")
    return "\n\n".join(chunks)


def propose_edits(
    requirement: str,
    context: str,
    llm: Any,
    *,
    feedback: str = "",
) -> list[dict[str, str]]:
    """第二阶段：让模型输出修改对，并校验结构（不落盘）。"""

    if feedback:
        feedback_text = f"上一轮的结果需要修正：\n{feedback}"
    else:
        feedback_text = "这是第一轮修改。"
    prompt = EDIT_PROMPT.format(
        requirement=requirement,
        context=context,
        feedback=feedback_text,
    )
    data = _parse_json(_invoke(llm, prompt))
    if isinstance(data, dict):
        data = data.get("edits") or data.get("changes") or data.get("files") or []
    if not isinstance(data, list) or not data:
        raise SelfEditError("模型没有给出修改对")

    edits: list[dict[str, str]] = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise SelfEditError(f"第 {index} 条修改不是对象")
        relative = _normalize_path(str(item.get("path", "")))
        old = item.get("old")
        new = item.get("new")
        if not is_allowed_path(relative):
            raise SelfEditError(f"第 {index} 条修改路径越界，拒绝执行：{item.get('path')}")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise SelfEditError(f"第 {index} 条修改缺少有效的 old/new")
        edits.append({"path": relative, "old": old, "new": new})
    return edits


def apply_edits(worktree: Path, edits: list[dict[str, str]]) -> list[str]:
    """逐条做精确字符串替换；old 必须恰好命中一次，否则整轮视为失败。"""

    changed: list[str] = []
    for index, edit in enumerate(edits, 1):
        relative = edit["path"]
        path = _safe_file(worktree, relative)
        raw = path.read_bytes().decode("utf-8")
        # 统一成 \n 做匹配，写回时恢复原文件的换行风格
        newline = "\r\n" if "\r\n" in raw else "\n"
        text = raw.replace("\r\n", "\n")
        old = edit["old"].replace("\r\n", "\n")
        new = edit["new"].replace("\r\n", "\n")
        hits = text.count(old)
        if hits != 1:
            raise SelfEditError(
                f"第 {index} 条修改在 {relative} 中命中 {hits} 次（必须恰好 1 次）"
            )
        path.write_bytes(text.replace(old, new, 1).replace("\n", newline).encode("utf-8"))
        if relative not in changed:
            changed.append(relative)
    return changed


# --------------------------------------------------------------- 结果与入口


def _tail(text: str, limit: int = FEEDBACK_TAIL_CHARS) -> str:
    """截取文本末尾，pytest 的关键信息通常在最后。"""

    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "……（前面已省略）\n" + text[-limit:]


def _pytest_summary(output: str) -> str:
    """从 pytest 输出里取最后一行有意义的结果，例如 ``12 passed in 3s``。"""

    for line in reversed((output or "").strip().splitlines()):
        stripped = line.strip().strip("=").strip()
        if stripped:
            return stripped[:200]
    return "无输出"


def _result(text: str) -> str:
    """按企业微信文本上限截断最终回报。"""

    text = text.strip()
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + "\n……（结果过长，已截断）"


def _delete_branch(repo_root: Path, branch: str) -> None:
    """删除没有提交的失败分支，避免仓库里堆积空分支。"""

    _git(repo_root, "branch", "-D", branch, check=False)


def _timestamp() -> str:
    """``20261120-153045-ab12``，秒级时间戳加随机后缀避免撞名。"""

    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def self_edit(
    requirement: str,
    *,
    llm: Any = None,
    repo_root: Path | str | None = None,
    python_exe: Path | str | None = None,
    branch: str | None = None,
) -> str:
    """自动改代码主入口：返回一段可以直接发给用户的结果说明。

    ``llm`` / ``repo_root`` / ``python_exe`` / ``branch`` 只用于测试注入，
    正常调用只需要传需求文本。
    """

    requirement = (requirement or "").strip()
    if not requirement:
        return "用法：/code <需求描述>"
    requirement = requirement[:2000]

    root = Path(repo_root).resolve() if repo_root else Path(__file__).resolve().parents[1]
    branch = branch or f"auto/{_timestamp()}"
    worktree = root / WORKTREE_ROOT / branch.split("/", 1)[-1]
    model = llm if llm is not None else _default_llm()
    python = Path(python_exe) if python_exe else _default_python(root)

    try:
        create_worktree(root, worktree, branch)
    except SelfEditError as exc:
        return _result(f"❌ 无法创建隔离环境：{exc}")
    except Exception as exc:  # noqa: BLE001 - 入口统一转成可读结果
        logger.exception("创建 worktree 失败")
        return _result(f"❌ 无法创建隔离环境：{type(exc).__name__}: {exc}")

    success = False
    attempts = 0
    changed: list[str] = []
    last_output = ""
    error_note = ""
    push_note = ""

    try:
        files = plan_files(requirement, model)
        logger.info("规划读取文件：%s", "、".join(files))
        context = read_files(worktree, files)

        feedback = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempts = attempt
            reset_worktree(worktree)
            try:
                edits = propose_edits(
                    requirement, context, model, feedback=feedback
                )
                changed = apply_edits(worktree, edits)
            except SelfEditError as exc:
                last_output = str(exc)
                feedback = (
                    f"上一轮修改没有应用成功：{exc}\n"
                    "请重新输出完整的 JSON 修改对。"
                )
                logger.warning("第 %d 轮修改未应用：%s", attempt, exc)
                continue

            passed, output = run_pytest(worktree, python)
            last_output = output
            if passed:
                success = True
                logger.info("第 %d 轮 pytest 通过", attempt)
                break

            feedback = (
                "上一轮修改后 pytest 失败，输出如下：\n"
                f"{_tail(output)}\n"
                "请修复后重新输出完整的 JSON 修改对。"
            )
            logger.warning("第 %d 轮 pytest 失败", attempt)

        if success:
            push_ok, push_note = commit_and_push(
                worktree,
                branch,
                changed,
                f"auto-edit: {requirement.splitlines()[0][:60]}",
            )
            if not push_ok:
                logger.warning("自动提交已生成，但%s", push_note)
    except SelfEditError as exc:
        success = False
        error_note = str(exc)
        logger.warning("自动改代码失败：%s", exc)
    except Exception as exc:  # noqa: BLE001 - 兜住模型/磁盘等未知错误
        success = False
        logger.exception("自动改代码异常")
        error_note = f"{type(exc).__name__}: {exc}"
    finally:
        remove_worktree(root, worktree)
        if not success:
            _delete_branch(root, branch)

    if success:
        lines = [
            "✅ 自动改代码完成",
            f"分支：{branch}",
            f"pytest：通过（{_pytest_summary(last_output)}）",
            f"改动文件：{'、'.join(changed)}",
            push_note or "未推送",
        ]
        if push_note.startswith("push 失败"):
            lines.append("提交已在本地分支，请在开发机手动推送后再 review。")
        else:
            lines.append(
                f"开发机执行：git fetch && git checkout {branch}，review 后 merge。"
            )
        return _result("\n".join(lines))

    lines = ["❌ 自动改代码失败"]
    if attempts:
        lines[0] += f"（已尝试 {attempts} 轮，隔离环境已清理）"
    else:
        lines[0] += "（未进入修改阶段，隔离环境已清理）"
    lines.append(f"分支：{branch}（未提交，已删除）")
    if error_note:
        lines.append(f"原因：{error_note}")
    if last_output:
        lines.append("最后一轮输出：")
        lines.append(_tail(last_output, 800))
    return _result("\n".join(lines))


def _default_llm() -> Any:
    """默认使用项目当前的聊天模型配置（deepseek / zhipu）。"""

    from secretary.llm import build_chat_model

    return build_chat_model(temperature=0.0)


__all__ = [
    "ALLOWED_DIRS",
    "SelfEditError",
    "apply_edits",
    "is_allowed_path",
    "plan_files",
    "propose_edits",
    "read_files",
    "run_pytest",
    "self_edit",
]

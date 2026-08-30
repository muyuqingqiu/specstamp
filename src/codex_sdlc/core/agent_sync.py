from __future__ import annotations

import errno
import json
import os
import signal
import shutil
import hashlib
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata
from pathlib import Path

# 允许干净 Python 预览：当作为独立文件加载且未安装包时，fallback 到标准库 SdlcError
try:
    from codex_sdlc.core.errors import SdlcError
except ImportError:  # pragma: no cover - 仅供干净预览路径
    class SdlcError(Exception):  # type: ignore
        def __init__(self, message: str, exit_code: int = 2) -> None:
            super().__init__(message)
            self.message = message
            self.exit_code = exit_code


class AtomicTempCleanupError(SdlcError):
    """原子写入失败后，事务自己创建的临时文件第一次清理也失败。"""

    def __init__(
        self,
        operation_message: str,
        cleanup_errors: dict[Path, str],
        original_error: BaseException | None = None,
        resource_cleanup_errors: list[str] | None = None,
    ) -> None:
        self.operation_message = operation_message
        self.cleanup_errors = cleanup_errors
        self.original_error = original_error
        self.resource_cleanup_errors = list(resource_cleanup_errors or [])
        detail = "、".join(f"{path.absolute()}（{error}）" for path, error in cleanup_errors.items())
        resource_detail = ""
        if self.resource_cleanup_errors:
            resource_detail = "；原子写入资源清理失败：" + "、".join(self.resource_cleanup_errors)
        super().__init__(
            f"{operation_message}；事务拥有临时文件清理失败：{detail}{resource_detail}",
            exit_code=1,
        )


class AtomicResourceCleanupError(SdlcError):
    """原子写入失败后，函数仍持有的文件描述符无法安全关闭。"""

    def __init__(
        self,
        operation_message: str,
        cleanup_errors: list[str],
        original_error: BaseException | None = None,
    ) -> None:
        self.operation_message = operation_message
        self.cleanup_errors = cleanup_errors
        self.original_error = original_error
        super().__init__(
            f"{operation_message}；原子写入资源清理失败：{'、'.join(cleanup_errors)}",
            exit_code=1,
        )


SYNC_SCHEMA = "codex-sdlc.agent-sync.v1"
SDLC_SYNC_START = "<!-- SDLC-SYNC:START -->"
SDLC_SYNC_END = "<!-- SDLC-SYNC:END -->"


@dataclass(frozen=True)
class AgentSyncPaths:
    agent_home: Path
    source_skills_home: Path
    shared_source_skills_home: Path
    codex_skills_home: Path
    agents_skills_home: Path
    claude_home: Path

    @property
    def standard_skills_home(self) -> Path:
        return self.agent_home / "skills"

    @property
    def backup_home(self) -> Path:
        return self.agent_home / "backups"

    @property
    def claude_commands_home(self) -> Path:
        return self.claude_home / "commands"


@dataclass(frozen=True)
class SkillEntry:
    name: str
    source: Path
    description: str
    kind: str
    body: str


def default_paths() -> AgentSyncPaths:
    home = Path.home()
    bundled_skills_home = versioned_skills_home()
    bundled_shared_skills_home = versioned_shared_skills_home()
    return AgentSyncPaths(
        agent_home=Path(os.environ.get("CODEX_SDLC_AGENT_HOME", str(home / ".agents" / "sdlc"))).expanduser(),
        source_skills_home=Path(
            os.environ.get("CODEX_SDLC_SOURCE_SKILLS_HOME", str(bundled_skills_home))
        ).expanduser(),
        shared_source_skills_home=Path(
            os.environ.get("CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME", str(bundled_shared_skills_home))
        ).expanduser(),
        codex_skills_home=Path(os.environ.get("CODEX_SDLC_CODEX_SKILLS_HOME", str(home / ".codex" / "skills"))).expanduser(),
        agents_skills_home=Path(os.environ.get("CODEX_SDLC_AGENTS_SKILLS_HOME", str(home / ".agents" / "skills"))).expanduser(),
        claude_home=Path(os.environ.get("CODEX_SDLC_CLAUDE_HOME", str(home / ".claude"))).expanduser(),
    )


def _installed_skill_home(folder: str, marker: str) -> Path | None:
    """通过 wheel 的 RECORD 定位数据文件，兼容虚拟环境和用户级安装目录。"""

    try:
        distribution = metadata.distribution("specstamp")
    except metadata.PackageNotFoundError:
        return None
    module_suffix = "codex_sdlc/core/agent_sync.py"
    owns_current_module = any(
        str(item).replace("\\", "/").endswith(module_suffix)
        and Path(distribution.locate_file(item)).resolve() == Path(__file__).resolve()
        for item in distribution.files or ()
    )
    if not owns_current_module:
        return None
    suffix = f"share/specstamp/{folder}/{marker}/SKILL.md"
    for item in distribution.files or ():
        if str(item).replace("\\", "/").endswith(suffix):
            return Path(distribution.locate_file(item)).resolve().parents[1]
    return None


def versioned_skills_home() -> Path:
    repository_home = Path(__file__).resolve().parents[3] / "skills"
    if repository_home.is_dir():
        return repository_home
    return _installed_skill_home("skills", "sdlc-agent-sync") or repository_home


def versioned_shared_skills_home() -> Path:
    repository_home = Path(__file__).resolve().parents[3] / "shared-skills"
    if repository_home.is_dir():
        return repository_home
    return _installed_skill_home("shared-skills", "agent-capability-sync") or repository_home


def versioned_cli_entry() -> Path:
    """版本化仓库内正式 CLI 入口，随来源目录动态推导，不写死本机路径。"""

    repository_entry = versioned_skills_home().parent / "bin" / "codex-sdlc"
    if repository_entry.is_file():
        return repository_entry
    installed_entry = Path(sys.executable).parent / "codex-sdlc"
    if installed_entry.is_file():
        return installed_entry
    discovered_entry = shutil.which("codex-sdlc") or shutil.which("specstamp")
    return Path(discovered_entry) if discovered_entry else repository_entry


def is_sdlc_agent_skill(name: str) -> bool:
    return name.startswith("sdlc-")


def skill_kind(name: str) -> str:
    if name.startswith("sdlc-"):
        return "sdlc"
    return "other"


def same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except FileNotFoundError:
        return left.expanduser().absolute() == right.expanduser().absolute()
    except OSError as exc:
        raise SdlcError(f"无法解析路径 {left} 与 {right}：{exc}", exit_code=1) from exc


def strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text.strip() + "\n"
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).strip() + "\n"
    return text.strip() + "\n"


def parse_description(text: str, fallback: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith("description:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'") or fallback
    return fallback


RETIRED_SDLC_SKILL_NAMES = {
    "sdlc-prepare",
    "sdlc-brief",
    "sdlc-brief-augment",
    "sdlc-brief-review",
}


def _read_skill_text(skill_file: Path, *, label: str = "技能文件") -> str:
    """把编码和文件读取错误转成稳定中文结果，正式命令不能直接抛出 Python 堆栈。"""
    try:
        return skill_file.read_text(encoding="utf-8")
    except UnicodeError:
        raise SdlcError(f"{label}不是有效的 UTF-8：{skill_file}", exit_code=1) from None
    except OSError as exc:
        raise SdlcError(f"无法读取{label}：{skill_file}（{exc}）", exit_code=1) from None


def discover_skills(source_home: Path) -> list[SkillEntry]:
    if not source_home.exists():
        raise SdlcError(f"找不到技能来源目录：{source_home}")
    if not source_home.is_dir():
        raise SdlcError(f"技能来源不是目录：{source_home}")
    try:
        children = sorted(source_home.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise SdlcError(f"无法读取技能来源目录：{source_home}（{exc}）", exit_code=1) from None
    entries: list[SkillEntry] = []
    for child in children:
        if (
            not child.is_dir()
            or not is_sdlc_agent_skill(child.name)
            or child.name in RETIRED_SDLC_SKILL_NAMES
        ):
            continue
        skill_file = child / "SKILL.md"
        if not skill_file.exists():
            continue
        raw_text = _read_skill_text(skill_file)
        entries.append(
            SkillEntry(
                name=child.name,
                source=child,
                description=parse_description(raw_text, child.name),
                kind=skill_kind(child.name),
                body=strip_frontmatter(raw_text),
            )
        )
    if not entries:
        raise SdlcError(f"技能来源目录里没有找到 sdlc-* 技能：{source_home}")
    return entries


def discover_non_sdlc_skill_names(skills_home: Path) -> list[str]:
    if not skills_home.exists():
        return []
    names: list[str] = []
    for child in sorted(skills_home.iterdir(), key=lambda item: item.name):
        if (
            not child.is_dir()
            or child.name.startswith(".")
            or ".backup-" in child.name
            or child.name.endswith(".backup")
            or is_sdlc_agent_skill(child.name)
        ):
            continue
        if (child / "SKILL.md").exists():
            names.append(child.name)
    return names


def discover_managed_shared_skills(
    source_home: Path,
    *,
    required: bool = False,
) -> list[SkillEntry]:
    if not source_home.exists():
        if required:
            raise SdlcError(f"找不到共享技能来源目录：{source_home}", exit_code=1)
        return []
    if not source_home.is_dir():
        raise SdlcError(f"共享技能来源不是目录：{source_home}", exit_code=1)
    try:
        children = sorted(source_home.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise SdlcError(
            f"无法读取共享技能来源目录：{source_home}（{exc}）",
            exit_code=1,
        ) from None
    entries: list[SkillEntry] = []
    for child in children:
        if (
            not child.is_dir()
            or child.name.startswith(".")
            or ".backup-" in child.name
            or child.name.endswith(".backup")
            or is_sdlc_agent_skill(child.name)
        ):
            continue
        skill_file = child / "SKILL.md"
        if not skill_file.exists():
            continue
        raw_text = _read_skill_text(skill_file, label="共享技能文件")
        entries.append(
            SkillEntry(
                name=child.name,
                source=child,
                description=parse_description(raw_text, child.name),
                kind="shared",
                body=strip_frontmatter(raw_text),
            )
        )
    return entries


def copy_tree(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True)


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    try:
        items = sorted(root.rglob("*"), key=lambda path: str(path.relative_to(root)))
    except OSError as exc:
        raise SdlcError(f"无法读取技能目录哈希：{root}（{exc}）", exit_code=1) from None
    for item in items:
        relative = str(item.relative_to(root))
        digest.update(relative.encode("utf-8"))
        try:
            if item.is_symlink():
                digest.update(b"\0link\0")
                digest.update(os.readlink(item).encode("utf-8"))
            elif item.is_dir():
                digest.update(b"\0dir\0")
            elif item.is_file():
                digest.update(b"\0file\0")
                digest.update(item.read_bytes())
        except OSError as exc:
            raise SdlcError(
                f"无法读取技能内容哈希：{item}（{exc}）",
                exit_code=1,
            ) from None
    return digest.hexdigest()


def tree_content_matches(source: Path, target: Path) -> bool:
    if not source.exists() or not target.exists():
        return False
    if not source.is_dir() or not target.is_dir() or source.is_symlink() or target.is_symlink():
        return False
    return tree_digest(source) == tree_digest(target)


@dataclass(frozen=True)
class _OwnedCloseResult:
    error: BaseException | None
    cleanup_errors: list[str]
    delivered_sigint: bool = False


def _close_owned_resource(
    *,
    resource: str,
    fd: int,
    path: Path,
    close_action,
) -> _OwnedCloseResult:
    """在屏蔽 SIGINT 的窗口内只关闭一次；异常后绝不按 fd 数字重试。"""

    try:
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    except BaseException as mask_error:
        text = str(mask_error) or repr(mask_error)
        return _OwnedCloseResult(mask_error, [
            f"resource={resource}，fd={fd}，路径={path.absolute()}，"
            f"无法进入 SIGINT 屏蔽关闭窗口（{type(mask_error).__name__}: "
            f"{text}；repr={mask_error!r}）"
        ])

    close_error: BaseException | None = None
    restore_error: BaseException | None = None
    restore_control_error: BaseException | None = None
    delivered_interrupt: BaseException | None = None
    try:
        close_action()
    except BaseException as exc:
        close_error = exc
    finally:
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        except KeyboardInterrupt as exc:
            # SIG_SETMASK 期间的 KeyboardInterrupt 一律视为恢复后交付的真实 SIGINT。
            delivered_interrupt = exc
        except Exception as exc:
            restore_error = exc
        except BaseException as exc:
            restore_control_error = exc

    if close_error is not None:
        close_text = str(close_error) or repr(close_error)
        detail = (
            f"resource={resource}，fd={fd}，路径={path.absolute()}，"
            f"资源关闭结果不确定/失败（{type(close_error).__name__}: "
            f"{close_text}；repr={close_error!r}）"
        )
        if restore_error is not None:
            detail += (
                f"；阶段=恢复SIGINT信号掩码，信号掩码恢复失败，当前线程状态不确定"
                f"（{type(restore_error).__name__}: "
                f"{str(restore_error) or repr(restore_error)}；repr={restore_error!r}）"
            )
        if restore_control_error is not None:
            detail += (
                f"；阶段=恢复SIGINT信号掩码，收到非SIGINT BaseException，当前线程状态不确定"
                f"（{type(restore_control_error).__name__}: "
                f"{str(restore_control_error) or repr(restore_control_error)}；"
                f"repr={restore_control_error!r}）"
            )
        if delivered_interrupt is not None:
            return _OwnedCloseResult(delivered_interrupt, [detail], delivered_sigint=True)
        return _OwnedCloseResult(close_error, [detail])
    if restore_error is not None:
        restore_text = str(restore_error) or repr(restore_error)
        return _OwnedCloseResult(restore_error, [
            f"resource={resource}，fd={fd}，路径={path.absolute()}，"
            f"阶段=恢复SIGINT信号掩码，信号掩码恢复失败，当前线程状态不确定"
            f"（{type(restore_error).__name__}: {restore_text}；repr={restore_error!r}）"
        ])
    if restore_control_error is not None:
        control_text = str(restore_control_error) or repr(restore_control_error)
        return _OwnedCloseResult(restore_control_error, [
            f"resource={resource}，fd={fd}，路径={path.absolute()}，"
            f"阶段=恢复SIGINT信号掩码，收到非SIGINT BaseException，当前线程状态不确定"
            f"（{type(restore_control_error).__name__}: {control_text}；"
            f"repr={restore_control_error!r}）"
        ])
    if delivered_interrupt is not None:
        return _OwnedCloseResult(delivered_interrupt, [], delivered_sigint=True)
    return _OwnedCloseResult(None, [])


def atomic_write_text(target: Path, content: str) -> None:
    """用同目录临时文件原子替换，并统一收口本函数拥有的所有 fd。"""

    tmp_path: Path | None = None
    raw_fd: int | None = None
    stream = None
    stream_fd: int | None = None
    dir_fd: int | None = None
    operation_error: BaseException | None = None
    resource_cleanup_errors: list[str] = []

    def keep_first(error: BaseException | None) -> None:
        nonlocal operation_error
        if error is not None and operation_error is None:
            operation_error = error

    def record_close(result: _OwnedCloseResult) -> None:
        nonlocal operation_error
        resource_cleanup_errors.extend(result.cleanup_errors)
        if result.delivered_sigint and isinstance(result.error, KeyboardInterrupt):
            if operation_error is not None and operation_error is not result.error:
                result.error.__cause__ = operation_error
                result.error.__suppress_context__ = True
            operation_error = result.error
        else:
            keep_first(result.error)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing_mode = stat.S_IMODE(os.lstat(target).st_mode)
            has_existing = True
        except FileNotFoundError:
            existing_mode = 0o644
            has_existing = False
        except OSError as exc:
            raise SdlcError(f"无法读取目标权限 {target}：{exc}", exit_code=1) from exc

        raw_fd, tmp_path_str = tempfile.mkstemp(dir=str(target.parent))
        tmp_path = Path(tmp_path_str)
        try:
            if operation_error is None:
                owned_raw_fd = raw_fd
                try:
                    stream = os.fdopen(owned_raw_fd, "w", encoding="utf-8")
                except BaseException as exc:
                    keep_first(exc)
                else:
                    # fdopen 成功后所有权立即从 raw 转给 stream，后续绝不能双关。
                    raw_fd = None
                    stream_fd = owned_raw_fd
                    try:
                        stream_fd = stream.fileno()
                    except BaseException as exc:
                        keep_first(exc)

            if operation_error is None and stream is not None:
                try:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException as exc:
                    keep_first(exc)
        finally:
            if stream is not None and stream_fd is not None:
                close_result = _close_owned_resource(
                    resource="stream",
                    fd=stream_fd,
                    path=tmp_path,
                    close_action=stream.close,
                )
                record_close(close_result)
                stream = None
                stream_fd = None
            elif raw_fd is not None:
                close_result = _close_owned_resource(
                    resource="raw",
                    fd=raw_fd,
                    path=tmp_path,
                    close_action=lambda fd=raw_fd: os.close(fd),
                )
                record_close(close_result)
                raw_fd = None

        if operation_error is not None:
            raise operation_error

        desired_mode = existing_mode if has_existing else 0o644
        try:
            os.chmod(tmp_path, desired_mode)
        except OSError as exc:
            raise SdlcError(f"无法设置临时文件权限 {tmp_path}：{exc}", exit_code=1) from exc

        os.replace(tmp_path, target)
        try:
            dir_fd = os.open(str(target.parent), os.O_DIRECTORY)
        except OSError as exc:
            raise SdlcError(f"无法打开原子写入父目录 {target.parent}：{exc}", exit_code=1) from exc

        try:
            if operation_error is None:
                try:
                    os.fsync(dir_fd)
                except OSError as exc:
                    keep_first(SdlcError(
                        f"原子替换后父目录 fsync 失败 {target.parent}：{exc}",
                        exit_code=1,
                    ))
                except BaseException as exc:
                    keep_first(exc)
        finally:
            close_result = _close_owned_resource(
                resource="dir",
                fd=dir_fd,
                path=target.parent,
                close_action=lambda fd=dir_fd: os.close(fd),
            )
            record_close(close_result)
            dir_fd = None

        if operation_error is not None:
            raise operation_error
    except BaseException as exc:
        keep_first(exc)

    if tmp_path is not None:
        try:
            tmp_path.unlink(missing_ok=True)
        except BaseException as cleanup_exc:
            cleanup_text = str(cleanup_exc) or type(cleanup_exc).__name__
            if isinstance(operation_error, SdlcError):
                operation_message = getattr(operation_error, "message", str(operation_error))
            elif operation_error is not None:
                error_text = str(operation_error) or type(operation_error).__name__
                operation_message = f"原子写入失败 {target}：{error_text}"
            else:
                operation_message = f"原子写入完成后清理临时文件失败：{target}"
            raise AtomicTempCleanupError(
                operation_message,
                {tmp_path.absolute(): cleanup_text},
                original_error=operation_error,
                resource_cleanup_errors=resource_cleanup_errors,
            ) from operation_error

    if resource_cleanup_errors:
        if isinstance(operation_error, SdlcError):
            operation_message = getattr(operation_error, "message", str(operation_error))
        elif operation_error is not None:
            operation_message = f"原子写入失败 {target}：{str(operation_error) or type(operation_error).__name__}"
        else:
            operation_message = f"原子写入完成后资源清理失败：{target}"
        raise AtomicResourceCleanupError(
            operation_message,
            resource_cleanup_errors,
            original_error=operation_error,
        ) from operation_error

    if operation_error is None:
        return
    if isinstance(operation_error, SdlcError):
        raise operation_error
    if isinstance(operation_error, OSError):
        raise SdlcError(f"原子写入失败 {target}：{operation_error}", exit_code=1) from operation_error
    raise operation_error

def snapshot_target(target: Path) -> dict:
    """记录三态、权限和内容；任何读取错误都必须在事务写入前终止。"""

    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return {"exists": False}
    except OSError as exc:
        raise SdlcError(f"无法读取快照 {target}：{exc}", exit_code=1) from exc
    info: dict = {
        "exists": True,
        "mode": stat.S_IMODE(st.st_mode),
        "is_symlink": stat.S_ISLNK(st.st_mode),
        "is_dir": stat.S_ISDIR(st.st_mode),
        "is_file": stat.S_ISREG(st.st_mode),
    }
    if stat.S_ISLNK(st.st_mode):
        try:
            info["link_target"] = os.readlink(target)
        except OSError as exc:
            raise SdlcError(f"无法读取快照链接 {target}：{exc}", exit_code=1) from exc
    elif stat.S_ISREG(st.st_mode):
        try:
            info["content"] = target.read_bytes()
        except OSError as exc:
            raise SdlcError(f"无法读取快照文件 {target}：{exc}", exit_code=1) from exc
    elif not stat.S_ISDIR(st.st_mode):
        # 特殊文件不读取内容，保留类型与权限，让回滚明确报告无法完整恢复。
        info["unsupported"] = True
    return info


def _cleanup_failure(path: Path, exc: BaseException) -> str:
    """保留清理路径、异常类型和可复核原值，空消息也不能丢失异常身份。"""

    text = str(exc) or repr(exc)
    return f"{path.absolute()}（{type(exc).__name__}: {text}；repr={exc!r}）"


def _restore_mode(
    target: Path,
    mode: int,
    *,
    symlink: bool = False,
    cleanup_errors: list[str] | None = None,
) -> bool:
    """恢复权限时不吞异常；权限失败必须让事务报告该路径未恢复。"""

    try:
        if symlink:
            try:
                os.chmod(target, mode, follow_symlinks=False)
            except (TypeError, NotImplementedError):
                lchmod = getattr(os, "lchmod", None)
                if lchmod is None:
                    return False
                lchmod(target, mode)
        else:
            os.chmod(target, mode)
    except BaseException as exc:
        if cleanup_errors is not None:
            cleanup_errors.append(_cleanup_failure(target, exc))
        return False
    return True


def _remove_path(target: Path) -> None:
    """不跟随符号链接删除一个事务目标。"""

    if not target.exists() and not target.is_symlink():
        return
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()


def restore_target(
    target: Path,
    snapshot: dict,
    backup_item: Path | None,
    cleanup_errors: list[str] | None = None,
) -> bool:
    """按快照精确恢复；原不存在的目标必须删除，权限失败必须返回 False。"""

    try:
        if not snapshot.get("exists"):
            _remove_path(target)
            return True

        is_symlink = bool(snapshot.get("is_symlink"))
        is_dir = bool(snapshot.get("is_dir"))
        is_file = bool(snapshot.get("is_file"))
        if is_symlink:
            link_target = snapshot.get("link_target")
            if link_target is None:
                return False
            _remove_path(target)
            os.symlink(link_target, target)
            return _restore_mode(
                target,
                int(snapshot["mode"]),
                symlink=True,
                cleanup_errors=cleanup_errors,
            )

        if is_dir:
            if backup_item is None or not backup_item.exists() or not backup_item.is_dir():
                return False
            _remove_path(target)
            shutil.copytree(backup_item, target, symlinks=True)
            return _restore_mode(target, int(snapshot["mode"]), cleanup_errors=cleanup_errors)

        if is_file:
            has_backup = backup_item is not None and backup_item.exists()
            has_content = "content" in snapshot
            if not has_backup and not has_content:
                return False
            _remove_path(target)
            if has_backup:
                if backup_item.is_dir():
                    shutil.copytree(backup_item, target, symlinks=True)
                else:
                    shutil.copy2(backup_item, target)
            else:
                target.write_bytes(snapshot["content"])
            return _restore_mode(target, int(snapshot["mode"]), cleanup_errors=cleanup_errors)

        # 特殊文件没有可安全复制的通用方案，明确报告未恢复。
        return False
    except BaseException as exc:
        if cleanup_errors is not None:
            cleanup_errors.append(_cleanup_failure(target, exc))
        return False


def backup_and_remove(item: Path, backup_dir: Path | None = None, label: str | None = None) -> bool:
    """删除已经在事务快照中备份的目标，避免同一目标产生第二个备份槽。"""

    if not item.exists() and not item.is_symlink():
        return False
    _remove_path(item)
    return True


def replace_tree_with_backup(source: Path, target: Path, backup_dir: Path, label: str) -> None:
    if same_path(source, target):
        return
    if tree_content_matches(source, target):
        return
    if target.exists() or target.is_symlink():
        backup_and_remove(target)
    copy_tree(source, target)


def render_claude_command(entry: SkillEntry) -> str:
    command_name = entry.name
    return "\n".join(
        [
            "---",
            f"description: {entry.description}",
            "argument-hint: 可选参数",
            "---",
            "",
            f"# /{command_name}",
            "",
            f"你正在 Claude Code 中执行本机 SDLC 指令：`/{command_name} $ARGUMENTS`。",
            "",
            "执行规则：",
            "",
            "1. 全程使用中文，文案直白清楚。",
            "2. 用户已经给出明确指令时，直接按当前指令推进，不做模板化二次确认。",
            "3. 只有遇到阻塞才停下来问用户：任务或文件不存在、候选不唯一且无法判断、依赖未满足、会覆盖或删除现有状态、测试范围缺失且无法从文档补齐。",
            f"4. 优先使用底层 CLI：`codex-sdlc`。如果系统 PATH 没有该命令，使用版本化仓库入口：`{versioned_cli_entry()}`。",
            "5. 面向用户推荐下一步时，只推荐 `/sdlc-*` 或 `$sdlc-*` 指令。",
            "6. Claude Code 的 `/sdlc-*` 和 Codex 的 `$sdlc-*` 是同一套语义，只是入口名字不同；不要拆成两套流程。",
            "7. 严格遵守本指令边界：本阶段做到哪里就停到哪里，不顺手推进下一阶段；如果下方 Skill 明确写了 Goal 模式或连续推进规则，按该 Skill 的循环规则执行。",
            "8. `$ARGUMENTS` 是用户传入的参数或补充说明，请结合下方 Skill 规则执行。",
            "",
            "## 对应 Skill 规则",
            "",
            entry.body.rstrip(),
            "",
        ]
    )


def claude_sync_block() -> str:
    return "\n".join(
        [
            SDLC_SYNC_START,
            "",
            "## 本机 SDLC 工作流规则",
            "",
            "- Claude Code 中优先使用 `/sdlc-*` Slash Commands；如果用户写 `$sdlc-*`，按同名 `/sdlc-*` 指令理解。",
            "- `/sdlc-*` 和 `$sdlc-*` 是同一套语义，只是 Claude Code 和 Codex 的入口名字不同；不要拆成两套流程，也不要让两边出现不同阶段边界。",
            f"- 底层 CLI 优先使用 `codex-sdlc`；如果 PATH 没有配置好，使用版本化仓库入口：`{versioned_cli_entry()}`。",
            "- 只有用户明确使用 `/sdlc-*`、`$sdlc-*`，或明确要继续需求、任务、验收、交接时，才进入 SDLC 流程。普通开发问答、排障解释、依赖文件说明和非 SDLC 开发不需要先执行 SDLC 交接。",
            "- SDLC 只使用自己的原生需求、设计、任务、测试和回归产物；不要引导用户接入、恢复、维护或继续使用其它流程目录。",
            "- 正式讨论输入默认先沉淀到当前 DRAFT；`DRAFT` 是 start 前统一确认稿，不是原始文档归档包。",
            "- `/sdlc-start` 默认消费已具备需求草稿和技术草稿的 DRAFT；只有已经整理好正式 JSON 包时，才走 `codex-sdlc start --file <json>` 兼容入口；当前存在未完成 DRAFT 时，JSON 必须写明 `source_draft_id` 并通过同一套 start 前审查。",
            "- DRAFT 章节别名、DRAFT 状态推荐、CAP/DES 归属和正式建档质量都按 CLI 核心合同判断；带 `source_draft_id` 的正式包必须保留 DRAFT 已确认事实；`start --file` 不自动补覆盖关系，每条 FR 必须显式提供编号、标题、说明、规则、输入、输出、触发条件、保存或改变的数据、权限、异常和边界，AC/TC 也必须有显式覆盖和可执行字段；已纳入当前 DRAFT 的 CAP 不再作为新需求线索。",
            "- 正式需求不再支持 `light-start` 一句话建档；小需求也走 DRAFT 主流程：discuss -> design -> start。已有结构化正式建档包时，可以使用 `codex-sdlc start --file <json>`。",
            "- 常用主线：`/sdlc-init` -> `/sdlc-discuss` -> `/sdlc-design` -> `/sdlc-design-accept` -> `/sdlc-start` -> `/sdlc-tasks` -> `/sdlc-task` -> `/sdlc-task-done` -> `/sdlc-regression` -> `/sdlc-accept` -> `/sdlc-docs`。",
            "- 需求和设计审核统一使用 `review` 合同；任务开工后使用读取清单、`task-read-confirm`、`task-run-check` 和 task-run 证据，不再增加单独的开工准备阶段。",
            "- 正式需求变化使用 `change-create` 建立结构化工作区，补齐资料后由 `change-package` 提交完整预计结果，再完成审核、任务保护和 `change-accept`。",
            "- 自然语言理解全部由 agent 完成，再调用 CLI。涉及需求意图、业务规则、任务拆分、任务和 FR/AC/TC/CHG 覆盖、经验级别、任务类型、文件范围、实现步骤或验收策略时，agent 必须先整理成显式结构化字段。CLI 只保存、校验、执行门禁和同步，不做关键词、正则、相似度或否定词判断。",
            "- CHG 没有模型给出的结构化任务时，只能标记待规划；agent 读取 CHG 和 current 文档后，用 `/sdlc-change-plan REQ-xxx --change CHG-xxx --task \"任务标题||任务目标||FR-001,FR-002\"` 写回结果。",
            "- CLI 拒绝结构化字段缺失或关系不唯一的输入。agent 应先补齐类型、编号、覆盖关系和验收记录，不能让 CLI 根据文字保底猜测。",
            "- 用户补充新规则、新 UI 状态、新展示口径、截图或设计稿，但不确定是普通任务还是需求变更时，优先用 `/sdlc-add`，不要直接用 `/sdlc-plan-add-task`。`/sdlc-add` 默认先复述理解、判断类型、列出预计任务和验收回归要求，用户确认后再实际执行 `codex-sdlc add`。",
            "- `/sdlc-grill` 只记录会影响业务目标、需求范围、技术方案、验收口径或任务实现方向的问题；不要为了“下一步是否开工”“是否继续下一阶段”这类流程确认单独记录质询。只有 Goal 模式允许主 agent 自问自答，普通需求、设计、任务规划和变更阶段确实需要质询时必须让用户回答。",
            "- Goal 模式启动后默认允许创建 Codex 桌面工作线程；普通模式不自动创建 Codex 线程，只提示用户可以要求开新线程执行下一步。",
            "- Goal 模式按当前工作类型选择主线程直接跑、单次工作线程或任务周期线程。主线程默认是调度员，只做状态确认、线程调度、结果复核、必要 SDLC 状态命令、用户确认和高风险动作；代码实现、长阅读、长排查、测试、验收和文档质量复核默认派工作线程。",
            "- 单次工作线程适合需求级回归、需求文档、复杂变更研究、任务外问题排查、长日志排查、只读审查、测试和验收；一个正式任务默认对应一个任务周期线程。",
            "- 同一任务的失败修复或验收反馈修复优先继续原任务周期线程；下一个任务必须新开任务周期线程，不能把两个任务混进同一条线程；第一版只允许一个工作线程运行。",
            "- 创建 Codex 桌面线程前，主线程先运行 `codex-sdlc status`。需要新线程时先用 `list_projects` 按当前仓库路径匹配项目，再用 `create_thread` 创建项目线程，传 `target.type = \"project\"`、项目 ID、`target.environment.type = \"local\"`、通用 SDLC 线程提示词和合适的 `model`。新开线程也必须考虑质量和成本平衡：高能力模型用于重思考、大范围判断、高风险实现和复杂返修；经济模型用于只读审查、日志整理、证据汇总和简单局部验证；不要默认所有新线程都用高能力模型。主线程必须记录计划模型、是否传入 model 字段、传入值或工具不支持的原因；工作线程必须在承接确认里说明自己看到或实际运行的模型，无法确认时写无法确认。`thinking` 按规则传：用户明确要求思考强度时传；任务需要复杂方案判断、多模块根因分析、大范围重构评估、高风险实现决策、前后矛盾梳理或长链路排查，并且工具支持时传。只读整理、日志归类、证据汇总、简单局部验证和明确小改动通常无需传 `thinking`；传 `thinking` 时必须在执行位置决策里写清原因。",
            "- 继续同一任务周期线程时用 `send_message_to_thread`，不要重新开线程；线程结束后用 `read_thread` 读取结果。线程结束前运行 `codex-sdlc status`，主线程读取线程结果后再次运行 `codex-sdlc status`。任务状态以 SDLC 状态为准，只按真实 SDLC 状态继续、停止或重新分流。",
            "- 工作线程遇到任务外问题时必须及时汇报主线程，不要把问题包装成普通限制或完成条件。任务外问题包括环境、账号、数据、依赖服务、工具链、设备、浏览器、IDE、模拟器、启动链路和权限等；只要影响当前任务验证、交付判断或下一步决策，就先收集最小证据、说明影响和建议分流。",
            "- 主线程读到任务外问题时，不要只按用户字面目标机械推进，也不要默认自己下场排查；先判断是否影响当前任务完成。如果影响且原任务线程已经卡住，默认开单次排查线程定位原因；排查后再决定继续原任务修复、记录正式修复任务、停止目标等待用户，还是补充证据后收口。必需验证缺失或结论不可信时，不允许直接 task-done。",
            "- 主线程准备向用户汇报目标完成前，必须亲自做最终质量检查，不再转派线程代查。检查 SDLC 状态、任务完成状态、工作线程上报的阻塞或限制、必需验证证据、Git 状态、是否误推进后续任务和是否还有用户确认点；发现问题就继续调度或停止目标说明原因，不能汇报完成。",
            "- 主线程派出工作线程后，必须按任务量和复杂度设置固定检查间隔，不要随机 sleep，也不要对所有线程都用同一个短间隔。固定档位：轻量状态或很短只读动作 2 分钟；普通只读审查、资料整理、日志初筛、小范围验证 5 分钟；正式任务开发、常规修复、包含构建或测试、多文件实现、UI/模拟器验收、复杂返修、长链路排查 10 分钟。只有线程明确接近收尾、正在等待很短命令结果或用户明确要求加快时，才允许临时缩短下一次检查间隔，并写清原因。",
            "- 工作线程和子代理都不能直接改 `.codex-sdlc/events.jsonl`、`.codex-sdlc/sdlc.db`、`.codex-sdlc/current.md` 或 `.codex-sdlc/requirements/**/tasks/T-xxx.md`；任务状态、验证、报告、变更、材料和经验必须通过 `/sdlc-*` 或 `codex-sdlc` 命令推进。",
            "- Goal 模式每个工作单元开始前都要分开写短决策。执行位置决策只由主线程输出，说明选择主线程、单次工作线程还是任务周期线程，并写清计划模型、是否传入 model 字段和传入结果；主线程直接执行时写子代理决策；创建或继续工作线程时只传子代理策略和授权，不把“使用 / 不使用”写死。",
            "- 工作线程收到的是主线程执行位置决策和子代理策略，不重新做执行位置决策。工作线程开工前必须先输出工作线程承接确认，说明承接线程类型、主线程计划模型、本线程实际模型或无法确认、模型一致性、检查间隔和范围；再自行输出子代理决策。主线程可以在策略里要求工作线程符合条件时必须使用子代理，工作线程不能把主线程未预派解释成“用户明确要求不使用子代理”。",
            "- 执行位置默认规则：主线程默认只做调度、状态确认和复核；轻量状态、用户确认、高风险动作、最终状态命令才由主线程亲自做；需要实现、长阅读、排查、测试、验收或文档质量复核时，优先派工作线程；单次耗时工作默认单次工作线程，正式任务默认任务周期线程。",
            "- 执行位置决策里必须写检查间隔：主线程写“无”；新开线程写 2 / 5 / 10 分钟并说明原因。正式任务线程没有依据时不允许 60 到 180 秒频繁轮询。",
            "- 子代理决策必须写使用或不使用、原因、模型、分工和复核方式；不使用子代理也要写明确原因。高能力模型用于重思考、大范围判断和高风险实现；经济模型用于文本查找、证据整理、简单修改和局部验证，不要默认全部使用高能力模型。",
            "- 主线程最终汇报必须写复核动作和复核结论，至少说明是否运行 `codex-sdlc status`，必要时检查 Git 状态和测试记录。汇报目标完成前，主线程必须亲自做最终质量检查；发现缺失验证、任务外问题未分流、线程结论不可信或状态不一致时，先继续调度处理，不要汇报完成。",
            "- 每个阶段只做自己阶段的事。`/sdlc-task` 只推进当前任务，不自动进入下一个任务；`/sdlc-task-done` 只完成当前任务的测试、状态同步和必要提交。",
            "- 当前任务保护覆盖 `doing`、`ready_for_user_check` 和 `test_failed`。新变更、新任务和修复任务默认不能抢走当前任务；除非用户明确要求或 agent 通过正式命令记录影响当前任务，否则不要把当前任务打回 `todo`。",
            "- 用户说“sdlc 当前需求测试发现”“验收发现”“之前任务有问题”“历史已完成任务有 bug”“回归发现问题”时，写代码前必须先用 `/sdlc-status` 判断归属：当前 doing 任务的问题继续当前任务；刚完成或待用户验收任务用 `/sdlc-task-restore`；历史已完成任务后来发现实现问题用 `/sdlc-fix`；需求目标变了用 `/sdlc-change`；技术方案错了回 `/sdlc-design`。普通模式先完成对应状态动作并停下，不要直接改代码。",
            "- Goal 模式必须评估是否使用子代理；进入实现、修复、测试或验收时，只要任务涉及大范围搜索、多文件、多页面、UI 验收、测试方案复核或主代理上下文较重，并且工具可用、范围可控，就必须派子代理，主代理负责复核和最终状态变更。不能因为当前工具列表没预加载子代理就直接判不可用；如果支持工具发现，先搜索 multi-agent/subagent 或 agent-task-dispatcher。只有纯状态读取、只执行状态命令、只提交 Git 或只写 SDLC 状态时，才由主代理单独处理。",
            "- 开发、测试、回归和交接只按当前 task.v2、读取清单、task-run、正式引用和前置交付物执行；`original/`、`versions/`、`DRAFT` 只用于追溯或建档前确认，不是开工后的正式执行依据。",
            "- 任务开工后先按读取清单核对全部正式原文和前置交付物，再用 `task-read-confirm` 确认清单；完成和回归只读取当前任务合同与 task-run 证据。",
            "- 当前需求包 `.codex-sdlc/requirements/<REQ>/tests/*.mjs` 里本轮新增或修改的专项脚本，必须用 `--test-script` 登记到当前任务测试契约；项目普通 `tests/`、`e2e/`、`__tests__/` 默认不触发这个门禁。",
            "- 任务开始时先复述当前任务目标、要实现的内容和完成后的预期效果；任务结束后详细说明落地情况、验证情况、测试证据和 Git 提交状态。",
            "- 任务开发时要给关键新增逻辑补中文解释性注释，说明为什么这样处理、兜底什么场景和后续维护要注意什么；不要只逐行翻译代码。",
            "- 任务完成后必须先测试，不自动继续下一个任务。没有合适 CLI 测试工具时，可以使用电脑操作、模拟器或 IDE 做人工/视觉验收；遇到卡点先判断归属并按任务外问题分流。必需验证缺失时，不要把命令成功或部分检查通过当成任务完成；只有补齐等价证据、确认问题与当前任务无关且不影响交付判断，或用户明确接受缺口后，才允许继续收口。",
            "- 需求被用户接受后，用 `/sdlc-docs REQ-xxx` 在项目 `docs/guide/` 生成面向后续维护的逻辑梳理文档；文档默认不提交 Git，只有客户明确要求时才用 `/sdlc-docs REQ-xxx --commit` 或单独提交。",
            "- 切换分支或工作树后，`.codex-sdlc/identity.json` 不匹配时必须停止，先用 `/sdlc-backup-list` 查看备份，再用 `/sdlc-restore ... --dry-run` 预览。需要保留当前需求包时，先切回原分支执行 `/sdlc-backup --pin`。",
            "- 恢复资料必须先 dry-run，再 confirm；不能静默覆盖。恢复指定快照时使用 `/sdlc-restore REQ-xxx --snapshot 快照名 --dry-run`。",
            "- 面向用户推荐下一步时，只推荐 `/sdlc-*` 或 `$sdlc-*` 指令。",
            SDLC_SYNC_END,
        ]
    )


def sync_claude_rules(claude_md: Path, backup_dir: Path | None = None) -> None:
    # 统一使用原子写，避免两套写入路径；backup_dir 参数保留兼容但不再在此处单独备份，事务快照已覆盖
    block = claude_sync_block()
    if claude_md.exists():
        original = _read_skill_text(claude_md, label="Claude 配置文件")
        if SDLC_SYNC_START in original and SDLC_SYNC_END in original:
            before, rest = original.split(SDLC_SYNC_START, 1)
            _old_block, after = rest.split(SDLC_SYNC_END, 1)
            updated = before.rstrip() + "\n\n" + block + after
        else:
            updated = original.rstrip() + "\n\n" + block + "\n"
        if updated != original:
            atomic_write_text(claude_md, updated)
        return
    claude_md.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(claude_md, "---\nname: base\napplyTo: '**'\n---\n\n" + block + "\n")


def stale_agent_skill_dirs(target_home: Path, expected_names: set[str], *, standard_home: bool = False) -> list[Path]:
    if not target_home.exists():
        return []
    stale: list[Path] = []
    for child in sorted(target_home.iterdir(), key=lambda item: item.name):
        if not child.is_dir() or not (child / "SKILL.md").exists():
            continue
        if child.name in expected_names:
            continue
        if standard_home or is_sdlc_agent_skill(child.name):
            stale.append(child)
    return stale


def duplicate_agent_skill_dirs(agents_skills_home: Path) -> list[Path]:
    if not agents_skills_home.exists():
        return []
    return [
        child
        for child in sorted(agents_skills_home.iterdir(), key=lambda item: item.name)
        if child.is_dir() and is_sdlc_agent_skill(child.name)
    ]


def build_manifest(
    paths: AgentSyncPaths,
    entries: list[SkillEntry],
    generated_at: str,
    claude_command_count: int,
    shared_skills: list[str],
    codex_local_skills: list[str],
) -> dict[str, object]:
    sdlc_names = [entry.name for entry in entries if entry.kind == "sdlc"]
    duplicate_names = sorted(set(shared_skills) & set(codex_local_skills))
    return {
        "schema": SYNC_SCHEMA,
        "generated_at": generated_at,
        "source_skills_home": str(paths.source_skills_home),
        "versioned_skills_home": str(versioned_skills_home()),
        "source_is_versioned": same_path(paths.source_skills_home, versioned_skills_home()),
        "shared_source_skills_home": str(paths.shared_source_skills_home),
        "versioned_shared_skills_home": str(versioned_shared_skills_home()),
        "shared_source_is_versioned": same_path(paths.shared_source_skills_home, versioned_shared_skills_home()),
        "standard_home": str(paths.agent_home),
        "skills": {
            "total": len(entries),
            "sdlc": len(sdlc_names),
            "names": [entry.name for entry in entries],
        },
        "adapters": {
            "codex": {
                "skills_home": str(paths.codex_skills_home),
                "skills": len(entries),
            },
            "agents": {
                "skills_home": str(paths.agents_skills_home),
                "shared_source": str(paths.standard_skills_home),
                "direct_skills": 0,
            },
            "claude": {
                "home": str(paths.claude_home),
                "commands_home": str(paths.claude_commands_home),
                "commands": claude_command_count,
            },
        },
        "capabilities": {
            "policy": "shared-skills-in-agents-skills; sdlc-adapters-generated-by-agent-sync",
            "shared_skills_home": str(paths.agents_skills_home),
            "managed_shared_source_home": str(paths.shared_source_skills_home),
            "managed_shared_skills": [
                entry.name for entry in discover_managed_shared_skills(paths.shared_source_skills_home)
            ],
            "shared_skills": shared_skills,
            "shared_skill_count": len(shared_skills),
            "codex_local_skills_home": str(paths.codex_skills_home),
            "codex_local_skills": codex_local_skills,
            "codex_local_skill_count": len(codex_local_skills),
            "duplicate_names": duplicate_names,
            "duplicate_count": len(duplicate_names),
        },
    }


# ---- 共享计划：供预览和正式同步复用，避免两套逻辑漂移 ----

def _gather_sync_state(paths: AgentSyncPaths):
    """收集同步所需的全部状态，仅标准库操作，供预览和正式共用"""
    entries = discover_skills(paths.source_skills_home)
    expected_names = {entry.name for entry in entries}
    sdlc_entries = [entry for entry in entries if entry.kind == "sdlc"]
    managed_shared_entries = discover_managed_shared_skills(paths.shared_source_skills_home)
    managed_shared_names = {entry.name for entry in managed_shared_entries}
    duplicates = duplicate_agent_skill_dirs(paths.agents_skills_home)
    stale_standard = stale_agent_skill_dirs(paths.standard_skills_home, expected_names, standard_home=True)
    stale_codex = [] if same_path(paths.source_skills_home, paths.codex_skills_home) else stale_agent_skill_dirs(paths.codex_skills_home, expected_names)
    shared_skills = sorted(set(discover_non_sdlc_skill_names(paths.agents_skills_home)) | managed_shared_names)
    codex_local_skills = discover_non_sdlc_skill_names(paths.codex_skills_home)
    duplicate_non_sdlc = sorted(set(shared_skills) & set(codex_local_skills))
    return {
        "entries": entries,
        "expected_names": expected_names,
        "sdlc_entries": sdlc_entries,
        "managed_shared_entries": managed_shared_entries,
        "managed_shared_names": managed_shared_names,
        "duplicates": duplicates,
        "stale_standard": stale_standard,
        "stale_codex": stale_codex,
        "shared_skills": shared_skills,
        "codex_local_skills": codex_local_skills,
        "duplicate_non_sdlc": duplicate_non_sdlc,
    }


def _build_preview_report(paths: AgentSyncPaths, state: dict, generated_at: str | None = None) -> dict[str, object]:
    """基于已收集状态构建只读预览报告，不创建任何文件或备份目录"""
    entries = state["entries"]
    sdlc_entries = state["sdlc_entries"]
    managed_shared_entries = state["managed_shared_entries"]
    duplicates = state["duplicates"]
    stale_standard = state["stale_standard"]
    stale_codex = state["stale_codex"]
    shared_skills = state["shared_skills"]
    codex_local_skills = state["codex_local_skills"]
    duplicate_non_sdlc = state["duplicate_non_sdlc"]
    # 预览不产生真实备份目录，返回占位路径以便展示
    preview_backup = str(paths.backup_home / "preview-no-write")
    report: dict[str, object] = {
        "mode": "preview",
        "paths": {
            "source_skills": str(paths.source_skills_home),
            "versioned_skills": str(versioned_skills_home()),
            "shared_source_skills": str(paths.shared_source_skills_home),
            "versioned_shared_skills": str(versioned_shared_skills_home()),
            "standard_home": str(paths.agent_home),
            "standard_skills": str(paths.standard_skills_home),
            "codex_skills": str(paths.codex_skills_home),
            "agents_skills": str(paths.agents_skills_home),
            "claude_commands": str(paths.claude_commands_home),
        },
        "skill_count": len(entries),
        "sdlc_count": len(sdlc_entries),
        "duplicate_count": len(duplicates),
        "stale_standard_count": len(stale_standard),
        "stale_codex_count": len(stale_codex),
        "claude_command_count": len(sdlc_entries),
        "shared_skill_count": len(shared_skills),
        "managed_shared_skill_count": len(managed_shared_entries),
        "codex_local_skill_count": len(codex_local_skills),
        "duplicate_non_sdlc_count": len(duplicate_non_sdlc),
        "duplicate_non_sdlc": duplicate_non_sdlc,
        "backup_dir": preview_backup,
        "source_is_versioned": same_path(paths.source_skills_home, versioned_skills_home()),
        "shared_source_is_versioned": same_path(paths.shared_source_skills_home, versioned_shared_skills_home()),
        # 兼容旧预览字段
        "source_skills": str(paths.source_skills_home),
        "shared_source": str(paths.shared_source_skills_home),
        "standard_home": str(paths.agent_home),
        "codex_skills": str(paths.codex_skills_home),
        "agents_skills": str(paths.agents_skills_home),
        "claude_home": str(paths.claude_home),
        "shared_count": len(managed_shared_entries),
        "source_is_versioned_old": same_path(paths.source_skills_home, versioned_skills_home()),
        "shared_is_versioned": same_path(paths.shared_source_skills_home, versioned_shared_skills_home()),
    }
    if generated_at:
        report["generated_at"] = generated_at
    return report


def preview_agent_sync_stdlib() -> dict[str, object]:
    """纯标准库预览：真实计算来源、目标、计划和校验，不依赖 jsonschema 等项目依赖；与 CLI dry-run 同一计划"""
    paths = default_paths()
    state = _gather_sync_state(paths)
    # 额外发现过期 Claude 命令以计入预览（ dry-run 同样会发现）
    sdlc_entries = state["sdlc_entries"]
    expected_claude_files = {entry.name + ".md" for entry in sdlc_entries}
    stale_claude = []
    if paths.claude_commands_home.exists():
        for f in sorted(paths.claude_commands_home.glob("sdlc-*.md")):
            if f.name not in expected_claude_files:
                stale_claude.append(f)
    # 将过期命令计入 stale_codex_count 的预览扩展？保持与 dry-run 一致：dry-run 报告包含过期命令数 via stale_codex 等，但 claude 需额外展示
    # 为便于预览校验，额外返回 stale_claude_count
    report = _build_preview_report(paths, state)
    report["stale_claude_count"] = len(stale_claude)
    report["stale_claude_files"] = [str(p) for p in stale_claude]
    return report


def _lexical_absolute(p: Path) -> str:
    """词法绝对路径，不跟随 symlink，解决 resolve() 截断和碰撞问题"""
    # os.path.abspath 只做当前目录拼接和 .. 词法归一化，不会解析符号链接。
    return os.path.abspath(os.fspath(p.expanduser()))


def _backup_path_for_target(target: Path, backup_dir: Path) -> Path:
    # 完整 SHA256，不截断，避免碰撞；不再依赖 basename 回退
    h = hashlib.sha256(_lexical_absolute(target).encode("utf-8")).hexdigest()
    return backup_dir / "targets" / h


def _register_target(
    targets: dict[str, Path],
    roles: dict[str, list[str]],
    target: Path,
    role: str,
) -> None:
    """按词法绝对路径合并目标；同一目标的多个业务角色共用一个备份槽。"""

    key = _lexical_absolute(target)
    if key not in targets:
        targets[key] = target
        roles[key] = [role]
        return
    # 目标可能同时被“待替换”和“待删除”等计划角色引用，显式合并而不是重复备份。
    if role not in roles[key]:
        roles[key].append(role)


def _parent_directory_chain(paths: list[Path]) -> list[Path]:
    """在任何 mkdir 前收集所有可能被事务创建的父目录，保留词法路径不跟随链接。"""

    collected: dict[str, Path] = {}
    for path in paths:
        current = Path(_lexical_absolute(path))
        while True:
            key = _lexical_absolute(current)
            collected[key] = current
            if current.parent == current:
                break
            current = current.parent
    return sorted(collected.values(), key=lambda item: (len(item.parts), str(item)))


def _copy_snapshot_to_backup(target: Path, snapshot: dict, backup_item: Path) -> None:
    """把已完成的快照复制到唯一目标槽，复制阶段也受同一事务保护。"""

    if not snapshot.get("exists"):
        return
    backup_item.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.get("is_symlink"):
        backup_item.write_text(str(snapshot.get("link_target", "")), encoding="utf-8")
        return
    if snapshot.get("is_dir"):
        shutil.copytree(target, backup_item, symlinks=True)
        return
    if snapshot.get("is_file"):
        shutil.copy2(target, backup_item)
        return
    raise SdlcError(f"无法备份不支持的目标类型：{target}", exit_code=1)


def _remove_transaction_dir(backup_dir: Path | None) -> list[str]:
    """删除失败轮次的事务目录，并保留清理失败路径供最终错误说明。"""

    if backup_dir is None:
        return []
    try:
        if not backup_dir.exists() and not backup_dir.is_symlink():
            return []
        _remove_path(backup_dir)
    except BaseException as exc:
        return [_cleanup_failure(backup_dir, exc)]
    return []


def _remove_absent_parent_dirs(parent_snapshots: dict[Path, dict]) -> list[str]:
    """只删除本轮创建且仍为空的父目录，并保留并发写入的任何目录项。"""

    errors: list[str] = []
    for path, snapshot in sorted(
        parent_snapshots.items(),
        key=lambda item: (len(item[0].parts), str(item[0])),
        reverse=True,
    ):
        try:
            if snapshot.get("exists") or (not path.exists() and not path.is_symlink()):
                continue
            # 并发进程可能把原不存在的路径创建成文件或链接；这些内容不属于本事务，必须原样保留。
            if path.is_symlink() or not path.is_dir():
                continue
            path.rmdir()
        except FileNotFoundError:
            continue
        except OSError as exc:
            # 从检查到 rmdir 之间可能出现并发文件或路径类型替换，这两类情况都安全保留。
            if exc.errno in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR}:
                continue
            errors.append(_cleanup_failure(path, exc))
        except BaseException as exc:
            errors.append(_cleanup_failure(path, exc))
    return errors


def sync_agent_entries(*, dry_run: bool = False, confirm: bool = False) -> dict[str, object]:
    if dry_run and confirm:
        raise SdlcError("`--dry-run` 和 `--confirm` 不能同时使用。", exit_code=1)
    if not dry_run and not confirm:
        raise SdlcError("默认只允许预览。请先使用 `codex-sdlc agent-sync --dry-run`，确认后再用 `--confirm` 写入。", exit_code=1)

    paths = default_paths()
    if same_path(paths.source_skills_home, paths.agents_skills_home):
        raise SdlcError("技能来源不能直接使用 `.agents/skills`，否则会把来源目录当重复入口清掉。", exit_code=1)
    if same_path(paths.shared_source_skills_home, paths.agents_skills_home):
        raise SdlcError("共享技能版本化来源不能直接使用 `.agents/skills`，否则会把运行时目录当来源。", exit_code=1)

    # 先完整生成只读计划；预览和确认共用这一份来源、目标和清理结果。
    state = _gather_sync_state(paths)
    entries: list[SkillEntry] = state["entries"]  # type: ignore
    sdlc_entries = state["sdlc_entries"]  # type: ignore
    managed_shared_entries = state["managed_shared_entries"]  # type: ignore
    duplicates = state["duplicates"]  # type: ignore
    stale_standard = state["stale_standard"]  # type: ignore
    stale_codex = state["stale_codex"]  # type: ignore
    shared_skills = state["shared_skills"]  # type: ignore
    codex_local_skills = state["codex_local_skills"]  # type: ignore
    duplicate_non_sdlc = state["duplicate_non_sdlc"]  # type: ignore
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    fail_point = os.environ.get("CODEX_SDLC_AGENT_SYNC_FAIL_POINT")

    expected_claude_files = {entry.name + ".md" for entry in sdlc_entries}
    stale_claude_files: list[Path] = []
    if paths.claude_commands_home.exists():
        for item in sorted(paths.claude_commands_home.glob("sdlc-*.md")):
            if item.name not in expected_claude_files:
                stale_claude_files.append(item)

    if dry_run:
        report = _build_preview_report(paths, state, generated_at)
        report["stale_claude_count"] = len(stale_claude_files)
        return report

    def _claude_md_needs_write() -> bool:
        claude_md = paths.claude_home / "CLAUDE.md"
        block = claude_sync_block()
        if not claude_md.exists():
            return True
        try:
            original = _read_skill_text(claude_md, label="Claude 配置文件")
        except SdlcError:
            return True
        if SDLC_SYNC_START in original and SDLC_SYNC_END in original:
            before, rest = original.split(SDLC_SYNC_START, 1)
            _old_block, after = rest.split(SDLC_SYNC_END, 1)
            updated = before.rstrip() + "\n\n" + block + after
        else:
            updated = original.rstrip() + "\n\n" + block + "\n"
        return updated != original

    def _manifest_needs_write() -> bool:
        manifest_path = paths.agent_home / "manifest.json"
        new_manifest = build_manifest(
            paths,
            entries,
            generated_at,
            len(sdlc_entries),
            shared_skills,
            codex_local_skills,
        )
        if not manifest_path.exists():
            return True
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return True
        existing_copy = dict(existing)
        new_copy = dict(new_manifest)
        existing_copy.pop("generated_at", None)
        new_copy.pop("generated_at", None)
        return existing_copy != new_copy

    # 没有实际变化时直接返回，不能为了确认命令创建 backup_home 或事务目录。
    has_changes = False
    for entry in entries:
        target = paths.standard_skills_home / entry.name
        if not same_path(entry.source, target) and not tree_content_matches(entry.source, target):
            has_changes = True
            break
    if not has_changes:
        for entry in managed_shared_entries:
            target = paths.agents_skills_home / entry.name
            if not tree_content_matches(entry.source, target):
                has_changes = True
                break
    if not has_changes and not same_path(paths.source_skills_home, paths.codex_skills_home):
        for entry in entries:
            target = paths.codex_skills_home / entry.name
            if not same_path(entry.source, target) and not tree_content_matches(entry.source, target):
                has_changes = True
                break
    if not has_changes and (stale_standard or stale_codex or duplicates or stale_claude_files):
        has_changes = True
    if not has_changes:
        for entry in sdlc_entries:
            command_file = paths.claude_commands_home / f"{entry.name}.md"
            if not command_file.exists():
                has_changes = True
                break
            try:
                if command_file.read_text(encoding="utf-8") != render_claude_command(entry):
                    has_changes = True
                    break
            except OSError:
                has_changes = True
                break
    if not has_changes and _claude_md_needs_write():
        has_changes = True
    if not has_changes and _manifest_needs_write():
        has_changes = True

    if not has_changes:
        report = _build_preview_report(paths, state, generated_at)
        report["mode"] = "confirmed"
        report["backup_dir"] = str(paths.backup_home / "no-changes-no-backup")
        report["backup_created"] = False
        report["manifest"] = str(paths.agent_home / "manifest.json")
        return report

    # 先按词法绝对路径建立唯一目标计划，再记录所有可能被 mkdir 创建的父目录状态。
    planned_targets: dict[str, Path] = {}
    target_roles: dict[str, list[str]] = {}

    def register_target(target: Path, role: str) -> None:
        _register_target(planned_targets, target_roles, target, role)

    for entry in entries:
        register_target(paths.standard_skills_home / entry.name, "standard-skill")
        if not same_path(paths.source_skills_home, paths.codex_skills_home):
            register_target(paths.codex_skills_home / entry.name, "codex-skill")
    for entry in managed_shared_entries:
        register_target(paths.agents_skills_home / entry.name, "managed-shared-skill")
    for item in stale_standard:
        register_target(item, "stale-standard-skill")
    for item in stale_codex:
        register_target(item, "stale-codex-skill")
    for item in duplicates:
        register_target(item, "duplicate-agent-skill")
    for entry in sdlc_entries:
        register_target(paths.claude_commands_home / f"{entry.name}.md", "claude-command")
    for item in stale_claude_files:
        register_target(item, "stale-claude-command")
    register_target(paths.claude_home / "CLAUDE.md", "claude-rules")
    register_target(paths.agent_home / "manifest.json", "manifest")

    parent_candidates = [
        paths.agent_home,
        paths.backup_home,
        paths.standard_skills_home,
        paths.codex_skills_home,
        paths.agents_skills_home,
        paths.claude_commands_home,
        paths.claude_home,
        *(target.parent for target in planned_targets.values()),
    ]
    # 这个读取阶段必须发生在任何 backup_home.mkdir、mkdtemp 或目标写入之前。
    parent_snapshots = {
        parent: snapshot_target(parent)
        for parent in _parent_directory_chain(parent_candidates)
    }

    backup_dir: Path | None = None
    snapshots: dict[str, tuple[Path, dict, Path]] = {}
    report: dict[str, object] | None = None
    committed = False
    backups_complete = False
    writes_started = False
    original_error: BaseException | None = None
    target_restore_errors: list[tuple[Path | None, dict | None, Path | None, str]] = []
    transaction_cleanup_errors: list[str] = []
    parent_cleanup_errors: list[str] = []
    owned_temp_cleanup_candidates: dict[Path, str] = {}
    owned_temp_cleanup_errors: list[str] = []
    atomic_resource_cleanup_errors: list[str] = []

    try:
        # 从 backup_home 创建开始，到事务目录、全量快照、备份复制和全部写入，都在同一保护范围内。
        paths.backup_home.mkdir(parents=True, exist_ok=True)
        if fail_point == "pre_backup_manifest":
            raise RuntimeError("注入故障：备份前阶段失败")
        backup_dir = Path(tempfile.mkdtemp(prefix="agent-sync-", dir=str(paths.backup_home)))

        all_targets = {
            key: (target, _backup_path_for_target(target, backup_dir))
            for key, target in planned_targets.items()
        }
        # 先完成所有目标的只读快照，任何 lstat/readlink/read_bytes 错误都发生在第一次备份复制前。
        for key, (target, backup_item) in all_targets.items():
            snapshots[key] = (target, snapshot_target(target), backup_item)
        # 快照全部成功后才复制旧内容；复制失败仍由同一 finally 轮次清理。
        for target, snapshot, backup_item in snapshots.values():
            _copy_snapshot_to_backup(target, snapshot, backup_item)
        backups_complete = True

        if fail_point == "pre_write":
            raise RuntimeError("注入故障：写入前失败")

        for entry in entries:
            target = paths.standard_skills_home / entry.name
            changed = not same_path(entry.source, target) and not tree_content_matches(entry.source, target)
            if changed:
                writes_started = True
            replace_tree_with_backup(entry.source, target, backup_dir, "standard-skills")
            if changed and fail_point == "after_skill_replace":
                raise RuntimeError("注入故障：完成技能替换后失败")
            if not same_path(paths.source_skills_home, paths.codex_skills_home):
                target = paths.codex_skills_home / entry.name
                changed = not same_path(entry.source, target) and not tree_content_matches(entry.source, target)
                if changed:
                    writes_started = True
                replace_tree_with_backup(entry.source, target, backup_dir, "codex-skills")
                if changed and fail_point == "after_skill_replace":
                    raise RuntimeError("注入故障：完成技能替换后失败")
        for entry in managed_shared_entries:
            target = paths.agents_skills_home / entry.name
            changed = not tree_content_matches(entry.source, target)
            if changed:
                writes_started = True
            replace_tree_with_backup(entry.source, target, backup_dir, "agents-shared-skills")
            if changed and fail_point == "after_skill_replace":
                raise RuntimeError("注入故障：完成技能替换后失败")

        for item in stale_standard:
            if item.exists() or item.is_symlink():
                writes_started = True
            backup_and_remove(item)
        for item in stale_codex:
            if item.exists() or item.is_symlink():
                writes_started = True
            backup_and_remove(item)
        for item in duplicates:
            if item.exists() or item.is_symlink():
                writes_started = True
            backup_and_remove(item)
        if fail_point == "claude":
            # 该故障点位于真实技能替换/清理之后，用于验证中段事务回滚，而不是替生产逻辑做假动作。
            raise RuntimeError("注入故障：Claude 阶段失败")

        paths.claude_commands_home.mkdir(parents=True, exist_ok=True)
        for item in stale_claude_files:
            if item.exists() or item.is_symlink():
                writes_started = True
            removed = backup_and_remove(item)
            if removed and fail_point == "after_stale_command":
                raise RuntimeError("注入故障：完成过期 Claude 命令删除后失败")
        for entry in sdlc_entries:
            command_file = paths.claude_commands_home / f"{entry.name}.md"
            new_text = render_claude_command(entry)
            needs_write = True
            if command_file.exists():
                try:
                    needs_write = command_file.read_text(encoding="utf-8") != new_text
                except OSError:
                    needs_write = True
            if needs_write:
                writes_started = True
                atomic_write_text(command_file, new_text)

        if _claude_md_needs_write():
            writes_started = True
        sync_claude_rules(paths.claude_home / "CLAUDE.md")
        if fail_point == "manifest":
            raise RuntimeError("注入故障：manifest 阶段失败")

        manifest = build_manifest(
            paths,
            entries,
            generated_at,
            len(sdlc_entries),
            shared_skills,
            codex_local_skills,
        )
        paths.agent_home.mkdir(parents=True, exist_ok=True)
        manifest_path = paths.agent_home / "manifest.json"
        if _manifest_needs_write():
            writes_started = True
            atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        report = {
            "mode": "confirmed",
            "paths": {
                "source_skills": str(paths.source_skills_home),
                "versioned_skills": str(versioned_skills_home()),
                "shared_source_skills": str(paths.shared_source_skills_home),
                "versioned_shared_skills": str(versioned_shared_skills_home()),
                "standard_home": str(paths.agent_home),
                "standard_skills": str(paths.standard_skills_home),
                "codex_skills": str(paths.codex_skills_home),
                "agents_skills": str(paths.agents_skills_home),
                "claude_commands": str(paths.claude_commands_home),
            },
            "skill_count": len(entries),
            "sdlc_count": len(sdlc_entries),
            "duplicate_count": len(duplicates),
            "stale_standard_count": len(stale_standard),
            "stale_codex_count": len(stale_codex),
            "claude_command_count": len(sdlc_entries),
            "shared_skill_count": len(shared_skills),
            "managed_shared_skill_count": len(managed_shared_entries),
            "codex_local_skill_count": len(codex_local_skills),
            "duplicate_non_sdlc_count": len(duplicate_non_sdlc),
            "duplicate_non_sdlc": duplicate_non_sdlc,
            "backup_dir": str(backup_dir),
            "backup_created": True,
            "manifest": str(manifest_path),
            "source_is_versioned": same_path(paths.source_skills_home, versioned_skills_home()),
            "shared_source_is_versioned": same_path(paths.shared_source_skills_home, versioned_shared_skills_home()),
        }
        committed = True
    except BaseException as exc:
        original_error = exc
        if isinstance(exc, AtomicTempCleanupError):
            owned_temp_cleanup_candidates.update(exc.cleanup_errors)
            atomic_resource_cleanup_errors.extend(exc.resource_cleanup_errors)
        elif isinstance(exc, AtomicResourceCleanupError):
            atomic_resource_cleanup_errors.extend(exc.cleanup_errors)
    finally:
        if not committed:
            # 备份没有全部完成时，目标还没有写入，绝不能拿半套备份覆盖原目标。
            if writes_started and backups_complete:
                for target, snapshot, backup_item in snapshots.values():
                    restore_errors: list[str] = []
                    try:
                        backup_exists = backup_item.exists() or backup_item.is_symlink()
                        restored = restore_target(
                            target,
                            snapshot,
                            backup_item if backup_exists else None,
                            restore_errors,
                        )
                    except BaseException as exc:
                        # restore_target 已兜住内部步骤；这里继续保护备份存在性检查等外围读取。
                        restore_errors.append(_cleanup_failure(target, exc))
                        restored = False
                    if not restored:
                        if snapshot.get("exists"):
                            reason = f"原目标未能恢复：{target}"
                        else:
                            reason = f"本轮新建目标未能删除：{target}"
                        if restore_errors:
                            reason += "（" + "；".join(restore_errors) + "）"
                        target_restore_errors.append((target, snapshot, backup_item, reason))
            elif writes_started:
                target_restore_errors.append(
                    (None, None, None, "事务在备份完成前已经开始写入")
                )

            # 原子写入第一次清理失败后，外层只重试自己登记的精确临时路径。
            for temp_path, first_error in owned_temp_cleanup_candidates.items():
                try:
                    temp_path.unlink(missing_ok=True)
                except BaseException as exc:
                    owned_temp_cleanup_errors.append(
                        f"{temp_path.absolute()}（首次清理：{first_error}；"
                        f"二次清理：{_cleanup_failure(temp_path, exc)}）"
                    )

            # 回滚完整成功后才能删除唯一事务备份；失败时保留它供人工恢复。
            if not target_restore_errors:
                transaction_cleanup_errors.extend(_remove_transaction_dir(backup_dir))
                if not transaction_cleanup_errors:
                    parent_cleanup_errors.extend(_remove_absent_parent_dirs(parent_snapshots))

    if not committed:
        if original_error is None:
            original_error = RuntimeError("未知事务错误")
        if isinstance(original_error, (AtomicTempCleanupError, AtomicResourceCleanupError)):
            original_message = original_error.operation_message
            underlying_error = original_error.original_error or original_error
        else:
            original_message = getattr(original_error, "message", str(original_error))
            underlying_error = original_error
        if target_restore_errors:
            detail = "、".join(item[3] for item in target_restore_errors)
            recovery_items: list[str] = []
            for target, snapshot, backup_item, _reason in target_restore_errors:
                if (
                    target is not None
                    and snapshot is not None
                    and snapshot.get("exists")
                    and backup_item is not None
                    and (backup_item.exists() or backup_item.is_symlink())
                ):
                    recovery_items.append(f"{target} -> {backup_item.absolute()}")
            recovery_hint = ""
            if recovery_items:
                recovery_hint = "；可人工恢复：" + "、".join(recovery_items)
            diagnostic_hint = ""
            if backup_dir is not None and (backup_dir.exists() or backup_dir.is_symlink()):
                diagnostic_hint = f"；事务诊断目录：{backup_dir.absolute()}"
            temp_hint = ""
            if owned_temp_cleanup_errors:
                temp_hint = "；事务拥有临时文件清理失败：" + "、".join(owned_temp_cleanup_errors)
            resource_hint = ""
            if atomic_resource_cleanup_errors:
                resource_hint = "；原子写入资源清理失败：" + "、".join(atomic_resource_cleanup_errors)
            transaction_hint = ""
            if transaction_cleanup_errors:
                transaction_hint = "；事务备份清理失败：" + "、".join(transaction_cleanup_errors)
            parent_hint = ""
            if parent_cleanup_errors:
                parent_hint = "；空父目录清理失败：" + "、".join(parent_cleanup_errors)
            raise SdlcError(
                f"同步失败且目标回滚失败：{detail}{recovery_hint}{diagnostic_hint}"
                f"{temp_hint}{resource_hint}{transaction_hint}{parent_hint}；原错：{original_message}",
                exit_code=1,
            ) from underlying_error
        if atomic_resource_cleanup_errors:
            detail = "、".join(atomic_resource_cleanup_errors)
            state = "目标已恢复" if writes_started else "未开始写入"
            temp_hint = ""
            if owned_temp_cleanup_errors:
                temp_hint = "；事务拥有临时文件清理失败：" + "、".join(owned_temp_cleanup_errors)
            transaction_hint = ""
            if transaction_cleanup_errors:
                transaction_hint = "；事务备份清理失败：" + "、".join(transaction_cleanup_errors)
            parent_hint = ""
            if parent_cleanup_errors:
                parent_hint = "；空父目录清理失败：" + "、".join(parent_cleanup_errors)
            raise SdlcError(
                f"同步失败，{state}，但原子写入资源清理失败："
                f"{detail}{temp_hint}{transaction_hint}{parent_hint}；原错：{original_message}",
                exit_code=1,
            ) from underlying_error
        if owned_temp_cleanup_errors:
            detail = "、".join(owned_temp_cleanup_errors)
            state = "目标已恢复" if writes_started else "未开始写入"
            transaction_hint = ""
            if transaction_cleanup_errors:
                transaction_hint = "；事务备份清理失败：" + "、".join(transaction_cleanup_errors)
            parent_hint = ""
            if parent_cleanup_errors:
                parent_hint = "；空父目录清理失败：" + "、".join(parent_cleanup_errors)
            raise SdlcError(
                f"同步失败，{state}，但事务拥有临时文件清理失败："
                f"{detail}{transaction_hint}{parent_hint}；原错：{original_message}",
                exit_code=1,
            ) from underlying_error
        if transaction_cleanup_errors:
            detail = "、".join(transaction_cleanup_errors)
            state = "目标已恢复" if writes_started else "未开始写入"
            cleanup_hint = ""
            if backup_dir is not None and (backup_dir.exists() or backup_dir.is_symlink()):
                cleanup_hint = f"；可手工清理事务备份：{backup_dir.absolute()}"
            raise SdlcError(
                f"同步失败，{state}，但事务备份清理失败：{detail}{cleanup_hint}；原错：{original_message}",
                exit_code=1,
            ) from underlying_error
        if parent_cleanup_errors:
            detail = "、".join(parent_cleanup_errors)
            state = "目标已恢复" if writes_started else "未开始写入"
            raise SdlcError(
                f"同步失败，{state}，但空父目录清理失败：{detail}；原错：{original_message}",
                exit_code=1,
            ) from underlying_error
        # 用户中断已经完成全部事务回滚与清理时，保持原异常类型和退出语义。
        if not isinstance(underlying_error, Exception):
            raise underlying_error
        if not writes_started:
            raise SdlcError(f"同步失败，未开始写入：{original_message}", exit_code=1) from underlying_error
        raise SdlcError(f"同步失败已回滚：{original_message}", exit_code=1) from underlying_error

    if report is None:
        raise SdlcError("同步事务没有生成结果。", exit_code=1)
    return report

def check_agent_entries() -> dict[str, object]:
    """只读核对版本化来源和三类运行时入口，不能用预览结果代替内容检查。"""

    paths = default_paths()
    issues: list[str] = []
    expected_source = versioned_skills_home()
    expected_shared_source = versioned_shared_skills_home()

    def recovery_issues() -> list[str]:
        install_script = expected_source.parent / "scripts" / "install_specstamp.py"
        return [
            "恢复版本化来源命令：unset CODEX_SDLC_SOURCE_SKILLS_HOME CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME",
            "同步预览命令：codex-sdlc agent-sync --dry-run",
            "确认同步命令：codex-sdlc agent-sync --confirm",
            "修复后复查命令：codex-sdlc agent-sync --check",
            f"重新安装命令：python3 {install_script}",
        ]

    def finish_report(
        entries: list[SkillEntry],
        managed_shared_entries: list[SkillEntry],
    ) -> dict[str, object]:
        sdlc_entries = [entry for entry in entries if entry.kind == "sdlc"]
        skill_hashes: dict[str, str] = {}
        shared_hashes: dict[str, str] = {}
        for entry in entries:
            try:
                skill_hashes[entry.name] = tree_digest(entry.source)
            except SdlcError as exc:
                issues.append(exc.message)
        for entry in managed_shared_entries:
            try:
                shared_hashes[entry.name] = tree_digest(entry.source)
            except SdlcError as exc:
                issues.append(exc.message)
        if issues:
            for issue in recovery_issues():
                if issue not in issues:
                    issues.append(issue)
        return {
            "mode": "check",
            "issues": issues,
            "skill_count": len(entries),
            "sdlc_count": len(sdlc_entries),
            "claude_command_count": len(sdlc_entries),
            "skill_hashes": skill_hashes,
            "managed_shared_skill_hashes": shared_hashes,
        }

    if not same_path(paths.source_skills_home, expected_source):
        issues.append(
            "SDLC 技能来源不是当前代码的版本化目录。"
            f"实际来源：{paths.source_skills_home}；版本化来源：{expected_source}"
        )
    if not same_path(paths.shared_source_skills_home, expected_shared_source):
        issues.append(
            "共享技能来源不是当前代码的版本化目录。"
            f"实际共享来源：{paths.shared_source_skills_home}；"
            f"版本化共享来源：{expected_shared_source}"
        )
    if issues:
        return finish_report([], [])

    try:
        entries = discover_skills(paths.source_skills_home)
        managed_shared_entries = discover_managed_shared_skills(
            paths.shared_source_skills_home,
            required=True,
        )
    except SdlcError as exc:
        issues.append(exc.message)
        return finish_report([], [])
    sdlc_entries = [entry for entry in entries if entry.kind == "sdlc"]

    def check_skill_tree(label: str, entry: SkillEntry, target: Path) -> None:
        if not target.exists():
            issues.append(f"{label}缺失：{target}")
        else:
            try:
                matches = tree_content_matches(entry.source, target)
            except SdlcError as exc:
                issues.append(exc.message)
            else:
                if not matches:
                    issues.append(f"{label}内容不同：{target}")

    for entry in entries:
        check_skill_tree("标准 Agent 运行时技能", entry, paths.standard_skills_home / entry.name)
        if not same_path(paths.source_skills_home, paths.codex_skills_home):
            check_skill_tree("Codex 技能", entry, paths.codex_skills_home / entry.name)
    for entry in managed_shared_entries:
        check_skill_tree("共享 Agent 技能", entry, paths.agents_skills_home / entry.name)

    expected_names = {entry.name for entry in entries}
    for target in stale_agent_skill_dirs(paths.standard_skills_home, expected_names, standard_home=True):
        issues.append(f"标准 Agent 运行时存在多余入口：{target}")
    if not same_path(paths.source_skills_home, paths.codex_skills_home):
        for target in stale_agent_skill_dirs(paths.codex_skills_home, expected_names):
            issues.append(f"Codex 技能存在多余入口：{target}")
    for target in duplicate_agent_skill_dirs(paths.agents_skills_home):
        issues.append(f"标准 Agent skills 存在重复 SDLC 入口：{target}")

    expected_commands = {entry.name + ".md" for entry in sdlc_entries}
    for entry in sdlc_entries:
        command_path = paths.claude_commands_home / f"{entry.name}.md"
        if not command_path.exists():
            issues.append(f"Claude 命令缺失：{command_path}")
        else:
            try:
                command_text = _read_skill_text(command_path, label="Claude 命令文件")
            except SdlcError as exc:
                issues.append(exc.message)
            else:
                if command_text != render_claude_command(entry):
                    issues.append(f"Claude 命令内容不同：{command_path}")
    if paths.claude_commands_home.exists():
        for command_path in sorted(paths.claude_commands_home.glob("sdlc-*.md")):
            if command_path.name not in expected_commands:
                issues.append(f"Claude 命令存在多余入口：{command_path}")

    manifest_path = paths.agent_home / "manifest.json"
    if not manifest_path.exists():
        issues.append(f"Agent 同步清单缺失：{manifest_path}")
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError):
            issues.append(f"Agent 同步清单无法读取：{manifest_path}")
        else:
            managed_shared_names = {entry.name for entry in managed_shared_entries}
            shared_skills = sorted(
                set(discover_non_sdlc_skill_names(paths.agents_skills_home)) | managed_shared_names
            )
            codex_local_skills = discover_non_sdlc_skill_names(paths.codex_skills_home)
            expected_manifest = build_manifest(
                paths,
                entries,
                str(manifest.get("generated_at") or ""),
                len(sdlc_entries),
                shared_skills,
                codex_local_skills,
            )
            informational_fields = {
                "capabilities.shared_skills",
                "capabilities.shared_skill_count",
                "capabilities.codex_local_skills",
                "capabilities.codex_local_skill_count",
                "capabilities.duplicate_names",
                "capabilities.duplicate_count",
            }

            def compare(expected: object, actual: object, field: str = "") -> None:
                if isinstance(expected, dict) and isinstance(actual, dict):
                    for key in sorted(set(expected) | set(actual)):
                        if key == "generated_at":
                            continue
                        path = f"{field}.{key}" if field else key
                        if path in informational_fields:
                            continue
                        if key not in expected or key not in actual:
                            issues.append(f"Agent 同步清单字段不一致：{path}")
                        else:
                            compare(expected[key], actual[key], path)
                    return
                if expected != actual:
                    issues.append(f"Agent 同步清单字段不一致：{field}")

            compare(expected_manifest, manifest)

    return finish_report(entries, managed_shared_entries)

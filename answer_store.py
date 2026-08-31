from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self, cast


class AnswerStoreError(RuntimeError):
    """题库存储不可读或待写数据无效。"""


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_MISSING = object()


def _thread_lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


class _ProcessFileLock:
    """仅依赖标准库的 Windows/POSIX 跨进程排他锁。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b", buffering=0)
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"\0")
        self._file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class AnswerStore:
    """原子、线程安全且跨进程安全的 JSON 题库存储。"""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        fallbacks: tuple[str | os.PathLike[str], ...] = (),
        validator: Callable[[Any], bool] | None = None,
    ) -> None:
        self.path = Path(path)
        self.backup_path = Path(f"{self.path}.bak")
        self.lock_path = Path(f"{self.path}.lock")
        self.fallbacks = tuple(Path(item) for item in fallbacks)
        self.validator = validator or (lambda value: isinstance(value, dict))
        self._thread_lock = _thread_lock_for(self.path)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """对一次完整的读改写事务加线程锁与文件锁。"""

        with self._thread_lock, _ProcessFileLock(self.lock_path):
            yield

    def _decode(self, path: Path) -> dict[str, Any]:
        with path.open(encoding="utf-8") as file:
            value = json.load(file)
        if not self.validator(value):
            raise AnswerStoreError(f"题库格式无效: {path}")
        return value

    def _load_candidates_unlocked(self) -> tuple[dict[str, Any], Path]:
        errors: list[str] = []
        seen: set[str] = set()
        for candidate in (self.path, self.backup_path, *self.fallbacks):
            key = os.path.normcase(str(candidate.resolve()))
            if key in seen:
                continue
            seen.add(key)
            if not candidate.exists():
                continue
            try:
                return self._decode(candidate), candidate
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                AnswerStoreError,
            ) as exc:
                errors.append(f"{candidate}: {exc}")
        detail = "；".join(errors) if errors else "没有可用文件"
        raise AnswerStoreError(f"无法加载题库：{detail}")

    def load(
        self,
        *,
        default: Mapping[str, Any] | object = _MISSING,
        recover: bool = True,
    ) -> dict[str, Any]:
        """读取题库；主文件损坏时从最近备份或只读候选恢复。"""

        with self.locked():
            try:
                value, source = self._load_candidates_unlocked()
            except AnswerStoreError:
                if default is _MISSING:
                    raise
                value = dict(cast(Mapping[str, Any], default))
                source = self.path
            if recover and source != self.path:
                self._atomic_write_unlocked(value, create_backup=False)
            return copy.deepcopy(value)

    def write(self, value: Mapping[str, Any]) -> None:
        """校验后原子写入，并在覆盖前保留一份最近有效版本。"""

        data = copy.deepcopy(dict(value))
        if not self.validator(data):
            raise AnswerStoreError("拒绝写入无效题库")
        with self.locked():
            self._atomic_write_unlocked(data, create_backup=True)

    def update(
        self,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
        *,
        default: Mapping[str, Any] | object = _MISSING,
    ) -> dict[str, Any]:
        """在同一跨进程锁内完成读取、修改和原子写入。"""

        with self.locked():
            try:
                current, _ = self._load_candidates_unlocked()
            except AnswerStoreError:
                if default is _MISSING:
                    raise
                current = dict(cast(Mapping[str, Any], default))
            working = copy.deepcopy(current)
            changed = mutator(working)
            result = dict(changed) if changed is not None else working
            if not self.validator(result):
                raise AnswerStoreError("题库更新结果无效")
            self._atomic_write_unlocked(result, create_backup=True)
            return copy.deepcopy(result)

    def _atomic_write_unlocked(
        self,
        value: Mapping[str, Any],
        *,
        create_backup: bool,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if create_backup and self.path.exists():
            try:
                current = self._decode(self.path)
            except (OSError, UnicodeError, json.JSONDecodeError, AnswerStoreError):
                current = None
            if current is not None:
                self._write_temp_and_replace(
                    self.backup_path,
                    json.dumps(
                        current,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                )

        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self._write_temp_and_replace(self.path, text)

    @staticmethod
    def _write_temp_and_replace(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
            AnswerStore._fsync_directory(path.parent)
        except BaseException:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            fd = os.open(path, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

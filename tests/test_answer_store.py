from __future__ import annotations

import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import answer_store as answer_store_module
from answer_store import AnswerStore


def _increment_store(path: str, rounds: int) -> None:
    store = AnswerStore(path)
    for _ in range(rounds):
        store.update(lambda data: data.update(count=int(data.get("count", 0)) + 1))


def test_atomic_write_keeps_previous_version_and_recovers_damage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "answer.json"
    store = AnswerStore(path)
    first = {"题目": {"version": 1}}
    second = {"题目": {"version": 2}}

    store.write(first)
    store.write(second)

    assert json.loads(path.read_text(encoding="utf-8")) == second
    assert json.loads(store.backup_path.read_text(encoding="utf-8")) == first

    path.write_text('{"broken":', encoding="utf-8")
    assert store.load() == first
    assert json.loads(path.read_text(encoding="utf-8")) == first


def test_interrupted_replace_never_truncates_existing_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "answer.json"
    store = AnswerStore(path)
    original = {"题目": {"version": 1}}
    store.write(original)
    real_replace = answer_store_module.os.replace

    def fail_main_replace(source: Path, destination: Path) -> None:
        if Path(destination) == path:
            raise OSError("simulated interruption")
        real_replace(source, destination)

    monkeypatch.setattr(answer_store_module.os, "replace", fail_main_replace)

    with pytest.raises(OSError, match="simulated interruption"):
        store.write({"题目": {"version": 2}})

    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert not list(tmp_path.glob(".answer.json.*.tmp"))


def test_threaded_updates_are_serialized(tmp_path: Path) -> None:
    path = tmp_path / "answer.json"
    AnswerStore(path).write({"count": 0})

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_increment_store, str(path), 8) for _ in range(6)]
        for future in futures:
            future.result()

    assert AnswerStore(path).load()["count"] == 48


def test_process_updates_are_serialized(tmp_path: Path) -> None:
    path = tmp_path / "answer.json"
    AnswerStore(path).write({"count": 0})
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_increment_store, args=(str(path), 5)) for _ in range(3)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    assert AnswerStore(path).load()["count"] == 15

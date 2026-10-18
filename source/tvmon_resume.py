"""Owned, locked segment checkpoints for interrupted TVMON downloads."""

from __future__ import annotations

import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import stat
import threading
import time


class ResumeError(Exception):
    pass


def _check_path(path: Path) -> None:
    """Reject directory junctions/symlinks before resolving an owned cache path."""
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ResumeError("이어받기 폴더에 연결된 경로가 있어 작업할 수 없습니다.")


def cache_path(folder: Path, resume_id: str) -> Path:
    if not isinstance(resume_id, str) or not re.fullmatch(r"[0-9a-f]{32}", resume_id):
        raise ResumeError("이어받기 작업 번호가 올바르지 않습니다.")
    folder = folder.expanduser().absolute()
    _check_path(folder)
    container = folder.resolve() / "이어받기 임시"
    root = container / (".tvmon-resume-" + resume_id)
    _check_path(root)
    if container.resolve().parent != folder.resolve() or root.resolve().parent != container.resolve():
        raise ResumeError("이어받기 폴더가 저장 위치를 벗어났습니다.")
    return root


class ResumeCache:
    def __init__(self, folder: Path, resume_id: str):
        self.root = cache_path(folder, resume_id)
        self.folder = self.root.parent.parent
        self.resume_id = resume_id
        self.state = None
        self._guard = threading.Condition()
        self._writing = set()
        self._pending_checkpoints = {}
        self._checkpointing = False
        self._lock_file = None

    def _file(self, name: str) -> Path:
        if not name or Path(name).name != name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
            raise ResumeError("이어받기 파일 이름이 올바르지 않습니다.")
        path = self.root / name
        _check_path(path)
        if path.exists() and not path.is_file():
            raise ResumeError("이어받기 파일 위치에 다른 폴더가 있습니다.")
        return path

    def _read_json(self, name: str):
        path = self._file(name)
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ResumeError("이어받기 기록의 크기가 올바르지 않습니다.")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ResumeError("이어받기 기록을 읽지 못했습니다. 해당 작업을 삭제한 뒤 다시 추가해 주세요.") from None
        if not isinstance(value, dict):
            raise ResumeError("이어받기 기록이 올바르지 않습니다.")
        return value

    def _verify_owner(self) -> None:
        try:
            owner = self._read_json("owner.json")
        except FileNotFoundError:
            raise ResumeError("기존 폴더의 소유 정보를 확인하지 못했습니다. 다른 파일을 보호하기 위해 작업을 중단했습니다.") from None
        if owner != {"owner": "TVMON resume cache", "version": 1, "resume_id": self.resume_id}:
            raise ResumeError("이 폴더는 해당 다운로드의 이어받기 폴더가 아닙니다.")

    def _check_children(self) -> list[Path]:
        files = list(self.root.iterdir())
        for path in files:
            if (path.name not in {"owner.json", "state.json", ".state.json.tmp", ".lock", "_merge.ts.part"}
                    and not re.fullmatch(r"[0-9]{6}\.(?:ts|tmp)", path.name)):
                raise ResumeError("이어받기 폴더에 다른 파일이 있어 작업을 중단했습니다.")
            self._file(path.name)
        return files

    def migrate_legacy(self) -> None:
        legacy = self.folder / self.root.name
        _check_path(legacy)
        if not legacy.exists():
            return
        if self.root.exists():
            raise ResumeError("기존 위치와 새 위치에 이어받기 폴더가 모두 있어 이전하지 않았습니다.")
        destination = self.root
        self.root = legacy
        try:
            self._open()
        finally:
            self.close()
            self.root = destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        _check_path(legacy)
        _check_path(destination)
        if legacy.resolve().parent != self.folder or destination.resolve().parent != self.folder / "이어받기 임시":
            raise ResumeError("이어받기 폴더가 저장 위치를 벗어났습니다.")
        legacy.rename(destination)

    def __enter__(self):
        self.migrate_legacy()
        return self._open()

    def _open(self):
        _check_path(self.root)
        self.root.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.root.mkdir()
            created = True
        except FileExistsError:
            created = False
        if created:
            owner = {"owner": "TVMON resume cache", "version": 1, "resume_id": self.resume_id}
            with self._file("owner.json").open("xb") as output:
                output.write(json.dumps(owner).encode("utf-8"))
                output.flush()
                os.fsync(output.fileno())
        self._verify_owner()
        self._check_children()
        lock_file = self._file(".lock").open("a+b")
        try:
            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ResumeError("이 다운로드는 다른 창에서 사용 중입니다. 먼저 해당 작업을 멈춰 주세요.") from None
            self._lock_file = lock_file
            self._verify_owner()
            self._check_children()
            if self._file("state.json").exists():
                self.state = self._read_json("state.json")
            return self
        except BaseException:
            self.close()
            lock_file.close()
            raise

    def close(self) -> None:
        if self._lock_file is not None:
            try:
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._lock_file.close()
                self._lock_file = None

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _write_state(self, state=None) -> dict[str, float]:
        started = time.perf_counter()
        temporary = self._file(".state.json.tmp")
        destination = self._file("state.json")
        payload = json.dumps(self.state if state is None else state,
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        prepared = time.perf_counter()
        with temporary.open("wb") as output:
            output.write(payload)
            output.flush()
            written = time.perf_counter()
            os.fsync(output.fileno())
            synced = time.perf_counter()
        os.replace(temporary, destination)
        return {"state_prepare_seconds": prepared - started, "state_write_seconds": written - prepared,
                "state_sync_seconds": synced - written, "state_replace_seconds": time.perf_counter() - synced}

    def bind(self, page: str, fingerprint: str, title: str, total: int, target_name: str) -> None:
        if self.state is None:
            self.state = {"version": 1, "resume_id": self.resume_id, "page": page,
                          "fingerprint": fingerprint, "title": title, "total": total,
                          "target": target_name, "parts": {}, "publication": None}
            self._write_state()
        elif (self.state.get("version") != 1 or self.state.get("resume_id") != self.resume_id
              or self.state.get("page") != page or self.state.get("fingerprint") != fingerprint
              or self.state.get("total") != total):
            raise ResumeError("영상의 조각이나 암호화 정보가 이전과 달라 이어받을 수 없습니다. 기존 작업을 삭제한 뒤 새로 추가해 주세요.")
        if not isinstance(self.state.get("parts"), dict):
            raise ResumeError("이어받기 조각 기록이 올바르지 않습니다.")
        self.target_path()

    def target_path(self) -> Path:
        name = self.state.get("target")
        if (not isinstance(name, str) or Path(name).name != name
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or not name.endswith(".ts")):
            raise ResumeError("이어받기 저장 파일 이름이 올바르지 않습니다.")
        target = self.folder / name
        _check_path(target)
        return target

    def recover_publication(self, check_cancel) -> Path | None:
        publication = self.state.get("publication")
        if publication is None:
            return None
        if (not isinstance(publication, dict) or publication.get("phase") not in ("ready", "published")
                or not isinstance(publication.get("size"), int) or publication["size"] <= 0
                or not isinstance(publication.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", publication["sha256"])):
            raise ResumeError("이어받기 완료 기록이 올바르지 않습니다.")
        target = self.target_path()
        if not target.is_file() or target.stat().st_size != publication["size"]:
            return None
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                check_cancel()
                digest.update(block)
        if digest.hexdigest() != publication["sha256"]:
            return None
        self.finish_publication()
        return target

    def verified_parts(self, validate_ts, check_cancel) -> dict[int, int]:
        verified = {}
        records = {}
        for index_text, record in self.state["parts"].items():
            check_cancel()
            if (not isinstance(index_text, str) or not re.fullmatch(r"0|[1-9][0-9]*", index_text)
                    or not 0 <= int(index_text) < self.state["total"]
                    or not isinstance(record, dict) or not isinstance(record.get("size"), int)
                    or not 0 < record["size"] <= 32 * 1024 * 1024
                    or not isinstance(record.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
                raise ResumeError("이어받기 조각 기록이 올바르지 않습니다.")
            index = int(index_text)
            path = self._file(f"{index:06d}.ts")
            if not path.is_file() or path.stat().st_size != record["size"]:
                continue
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                continue
            try:
                validate_ts(data)
            except Exception:
                continue
            verified[index] = len(data)
            records[index_text] = record
        self.state["parts"] = records
        self.state["publication"] = None
        self._write_state()
        return verified

    def store(self, index: int, data: bytes, *, on_event=None) -> None:
        started = time.perf_counter()
        with self._guard:
            # Different pieces have independent files; the same piece must still have one writer.
            while index in self._writing:
                self._guard.wait()
            self._writing.add(index)
        entered = time.perf_counter()
        phase = "piece_write"
        timings = {}
        try:
            temporary = self._file(f"{index:06d}.tmp")
            destination = self._file(f"{index:06d}.ts")
            record = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            with temporary.open("wb") as output:
                output.write(data)
                output.flush()
                written = time.perf_counter()
                phase = "piece_sync"
                os.fsync(output.fileno())
                synced = time.perf_counter()
            phase = "piece_replace"
            os.replace(temporary, destination)
            persisted = time.perf_counter()
            phase = "checkpoint"
            request = {"record": record}
            batch = None
            with self._guard:
                self._pending_checkpoints[index] = request
                while self._checkpointing and "completed" not in request:
                    self._guard.wait()
                if "completed" not in request:
                    batch = self._pending_checkpoints
                    self._pending_checkpoints = {}
                    self._checkpointing = True
                    acquired = time.perf_counter()
            if batch is not None:
                error = None
                try:
                    state = {**self.state, "parts": {**self.state["parts"],
                             **{str(part): pending["record"] for part, pending in batch.items()}}}
                    # While this snapshot is made durable, other pieces can finish
                    # writing and join the next checkpoint without holding the guard.
                    timings = self._write_state(state)
                except BaseException as exc:
                    error = exc
                finally:
                    with self._guard:
                        if error is None:
                            self.state = state
                        completed = time.perf_counter()
                        for pending in batch.values():
                            pending.update(completed=completed, error=error, parts=len(batch))
                        self._checkpointing = False
                        self._guard.notify_all()
            else:
                # A different writer made this piece durable. Count that interval
                # as waiting, without counting the shared disk work more than once.
                acquired = completed = request["completed"]
                timings = {name: 0.0 for name in ("state_prepare_seconds", "state_write_seconds",
                                                  "state_sync_seconds", "state_replace_seconds")}
            if request["error"] is not None:
                raise request["error"]
            timings.update(entry_wait_seconds=entered - started, piece_write_seconds=written - entered,
                           piece_sync_seconds=synced - written, piece_replace_seconds=persisted - synced,
                           checkpoint_wait_seconds=acquired - persisted, checkpoint_seconds=completed - acquired,
                           seconds=completed - started)
        except BaseException:
            if on_event is not None:
                on_event("segment_storage_error", phase=phase, seconds=round(time.perf_counter() - started, 6))
            raise
        finally:
            with self._guard:
                self._writing.remove(index)
                self._guard.notify_all()
        if on_event is not None:
            finished = time.perf_counter()
            timings.update(completion_wait_seconds=finished - completed, seconds=finished - started)
            on_event("segment_storage", **{name: round(seconds, 6) for name, seconds in timings.items()},
                     checkpoint_writer=batch is not None, checkpoint_parts=request["parts"])

    def begin_publication(self, size: int, digest: str) -> None:
        self.state["publication"] = {"phase": "ready", "size": size, "sha256": digest}
        self._write_state()

    def finish_publication(self) -> None:
        self.state["publication"]["phase"] = "published"
        self._write_state()
        # The published receipt remains, so a crash before the GUI event is recoverable.
        for index in range(self.state["total"]):
            path = self._file(f"{index:06d}.ts")
            path.unlink(missing_ok=True)

    def merge_path(self) -> Path:
        return self._file("_merge.ts.part")


def discard_cache(folder: Path, resume_id: str) -> None:
    cache = ResumeCache(folder, resume_id)
    cache.migrate_legacy()
    if not cache.root.exists():
        return
    with cache:
        files = cache._check_children()
        for path in files:
            if path.name not in ("owner.json", ".lock"):
                path.unlink()
        # Keep ownership if a locked segment prevents deletion, allowing a safe retry.
        cache._file("owner.json").unlink()
    (cache.root / ".lock").unlink()
    cache.root.rmdir()

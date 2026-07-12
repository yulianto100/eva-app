import os
import time

from cleanup import cleanup_directory, cleanup_old_files


def test_cleanup_directory_removes_expired_files_and_empty_snapshot(tmp_path):
    snapshot = tmp_path / "20260710-000000"
    snapshot.mkdir()
    expired = snapshot / "archive.zip"
    expired.write_bytes(b"old")
    old_time = time.time() - 7200
    os.utime(expired, (old_time, old_time))
    current = tmp_path / "current.zip"
    current.write_bytes(b"new")

    assert cleanup_directory(tmp_path, 3600) == 1
    assert not expired.exists()
    assert not snapshot.exists()
    assert current.exists()


def test_cleanup_old_files_preserves_required_output_directories(tmp_path):
    upload_dir = tmp_path / "uploads"
    output_dir = tmp_path / "outputs"
    upload_dir.mkdir()
    (output_dir / "boq").mkdir(parents=True)
    (output_dir / "hpdb").mkdir()
    disposable = output_dir / "disposable"
    disposable.mkdir()

    cleanup_old_files(upload_dir, output_dir, 3600)

    assert (output_dir / "boq").is_dir()
    assert (output_dir / "hpdb").is_dir()
    assert not disposable.exists()

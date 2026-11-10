"""scripts/export_public.sh, run for real on a small git repository, with stand-ins for the release
check and gitleaks: one commit and a tag in a new folder, no history, no private files, nothing pushed."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AUTHOR = "Relay Owner <owner@example.com>"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "private"
    (src / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "export_public.sh", src / "scripts")
    (src / "scripts" / "hygiene_check.sh").write_text('#!/bin/bash\nexit "${FAKE_HYGIENE:-0}"\n')
    (src / "scripts" / "hygiene_check.sh").chmod(0o755)
    (src / "README.md").write_text("relay\n")
    (src / ".gitignore").write_text("_import/\n.env\n")
    _git(src, "init", "-q", "-b", "main")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "old history one")
    (src / "README.md").write_text("relay, later\n")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-am", "old history two")
    (src / "_import").mkdir()
    (src / "_import" / "notes.md").write_text("private\n")
    (src / ".env").write_text("RELAY_API_KEY=x\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gitleaks").write_text('#!/bin/bash\nexit "${FAKE_LEAKS:-0}"\n')
    (bin_dir / "gitleaks").chmod(0o755)
    return src, bin_dir, tmp_path


def run(repo, *args, **env):
    src, bin_dir, tmp = repo
    e = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp), **env}
    return subprocess.run(["bash", "scripts/export_public.sh", *args], cwd=src, env=e, capture_output=True,
                          text=True, check=False, timeout=60)


def test_one_commit_and_a_tag_without_the_history_or_private_files(repo):
    src, _, tmp = repo
    out = tmp / "public"
    r = run(repo, str(out), "v0.1.0", AUTHOR)
    assert r.returncode == 0, r.stderr + r.stdout
    assert _git(out, "rev-list", "--count", "HEAD") == "1"
    assert _git(out, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%s") == f"{AUTHOR}|{AUTHOR}|Quanti Relay v0.1.0"
    assert _git(out, "tag") == "v0.1.0" and _git(out, "rev-parse", "v0.1.0^{commit}") == _git(out, "rev-parse", "HEAD")
    assert (out / "README.md").read_text() == "relay, later\n"
    assert not (out / "_import").exists() and not (out / ".env").exists()
    assert "old history" not in _git(out, "log", "--all", "--format=%s")
    assert _git(out, "remote") == ""                       # nothing to push to: that is the owner's step


def test_a_private_email_is_warned_about(repo):
    r = run(repo, str(repo[2] / "public"), "v0.1.0", "Relay Owner <owner@example.com>")
    assert r.returncode == 0 and "WARNING" in r.stdout and "will be public" in r.stdout


@pytest.mark.parametrize(("args", "env", "why"), [
    (("v0.1",), {}, "not a version"),
    (("v0.1.0", "no email"), {}, "Name <email>"),
    (("v0.1.0",), {"FAKE_HYGIENE": "1"}, "release check failed"),
    (("v0.1.0",), {"FAKE_LEAKS": "1"}, "gitleaks found"),
])
def test_it_stops_when_it_cannot_do_it_right(repo, args, env, why):
    out = repo[2] / "public"
    r = run(repo, str(out), *args, *(() if len(args) > 1 else (AUTHOR,)), **env)
    assert r.returncode == 1 and why in r.stderr


def test_uncommitted_changes_or_an_existing_folder_stop_it(repo):
    src, _, tmp = repo
    (tmp / "taken").mkdir()
    r = run(repo, str(tmp / "taken"), "v0.1.0", AUTHOR)
    assert r.returncode == 1 and "exists already" in r.stderr
    (src / "README.md").write_text("changed\n")
    r = run(repo, str(tmp / "public"), "v0.1.0", AUTHOR)
    assert r.returncode == 1 and "commit or stash" in r.stderr and not (tmp / "public").exists()

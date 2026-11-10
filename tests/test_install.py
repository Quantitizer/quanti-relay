"""deploy/install.sh, run for real against a copy of the repository, with stand-ins for the system
tools (apt-get, docker, ufw, curl, ...). It checks the logic: the address, .env, what it starts, and
that running it again keeps what is there."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from relay import envfile

ROOT = Path(__file__).resolve().parent.parent
STUBS = {
    "apt-get": 'echo "apt-get $*" >> "$LOG"',
    "systemctl": 'echo "systemctl $*" >> "$LOG"',
    "ufw": 'echo "ufw $*" >> "$LOG"',
    "chown": 'echo "chown $*" >> "$LOG"',
    "id": 'echo "${FAKE_UID:-0}"',
    "getent": 'echo "${FAKE_RESOLVES:-203.0.113.7} STREAM $2"',
    "sleep": "true",
    "sshd": '[ "$1" = -t ] && exit "${FAKE_SSHD_BAD:-0}"; echo "port ${FAKE_SSH_PORT:-22}"',
    "install": 'echo "install $*" >> "$LOG"; cp "$3" "$4"',
    "docker": 'echo "docker $*" >> "$LOG"; [ "$1 $2" = "compose version" ] && echo "Docker Compose version v2"; true',
    # the public address lookup, and the relay's /health over https
    "curl": 'for a in "$@"; do case "$a" in *checkip*|*ipify*) [ -n "$FAKE_IP" ] && echo "$FAKE_IP" && exit 0; exit 7 ;;'
            ' */health) echo "curl $a" >> "$LOG"; exit 0 ;; esac; done; exit 1',
}


@pytest.fixture
def box(tmp_path):
    repo = tmp_path / "repo"
    (repo / "relay").mkdir(parents=True)
    shutil.copy(ROOT / "compose.yaml", repo)
    shutil.copytree(ROOT / "deploy", repo / "deploy")
    shutil.copy(ROOT / "relay" / "envfile.py", repo / "relay")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        (bin_dir / name).write_text(f"#!/bin/bash\n{body}\n")
        (bin_dir / name).chmod(0o755)
    return repo, bin_dir, tmp_path


def run(box, *args, ip="203.0.113.7", **env):
    repo, bin_dir, tmp = box
    e = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "LOG": str(tmp / "log"), "FAKE_IP": ip,
         "RELAY_INSTALL_ROOT": str(tmp / "root"), "HOME": str(tmp), "RELAY_DIR_OWNER": str(os.getuid()), **env}
    return subprocess.run(["bash", str(repo / "deploy" / "install.sh"), *args], env=e, capture_output=True, check=False,
                          text=True, timeout=60)


def _env(box):
    return envfile.get, box[0] / ".env"


def test_a_fresh_server_gets_https_on_its_ip_paper_and_a_hidden_key(box):
    repo, _, tmp = box
    r = run(box)
    assert r.returncode == 0, r.stderr + r.stdout
    get, f = _env(box)
    assert get(f, "RELAY_PUBLIC_HOST") == "203.0.113.7" and get(f, "RELAY_ACME_PROFILE") == "shortlived"
    assert get(f, "RELAY_PUBLIC_IP") == "203.0.113.7" and get(f, "RELAY_TRUSTED_PROXIES") == "172.30.77.10"
    assert get(f, "RELAY_DRY_RUN") == "true"
    key = get(f, "RELAY_API_KEY")
    assert len(key) >= 32 and key not in r.stdout + r.stderr
    assert oct(f.stat().st_mode & 0o777) == oct(0o600)
    log = (tmp / "log").read_text()
    assert "docker compose --profile tls up -d --build" in log and "curl https://203.0.113.7/health" in log
    # 80 and 443 over IPv4 only; SSH stays open
    assert "ufw allow proto tcp from 0.0.0.0/0 to any port 443" in log and "ufw --force enable" in log
    assert "ufw allow OpenSSH" in log and "ufw allow 443/tcp" not in log
    assert all("DPkg::Lock::Timeout=600" in line for line in log.splitlines() if line.startswith("apt-get"))
    assert (tmp / "root/usr/local/bin/relay").exists()
    assert (tmp / "root/etc/default/quanti-relay").read_text().strip() == f"RELAY_DIR={repo}"
    assert "Unattended-Upgrade" in (tmp / "root/etc/apt/apt.conf.d/20auto-upgrades").read_text()
    for sub in ("deployments", "sources", "sinks"):
        assert (repo / "config" / sub).is_dir()
    assert "https://203.0.113.7/console" in r.stdout


def test_running_it_again_keeps_the_key_and_the_owners_live_choice(box):
    assert run(box).returncode == 0
    get, f = _env(box)
    key = get(f, "RELAY_API_KEY")
    envfile.put(f, "RELAY_DRY_RUN", "false")              # the owner went live
    assert run(box).returncode == 0
    assert get(f, "RELAY_API_KEY") == key and get(f, "RELAY_DRY_RUN") == "false"
    names = [line.split("=")[0] for line in f.read_text().splitlines() if "=" in line]
    assert len(names) == len(set(names))


def test_own_domain_and_sslip(box):
    assert run(box, "--domain", "relay.example.com").returncode == 0
    get, f = _env(box)
    assert get(f, "RELAY_PUBLIC_HOST") == "relay.example.com" and get(f, "RELAY_ACME_PROFILE") == "classic"
    r = run(box, "--domain", "relay.example.com", FAKE_RESOLVES="198.51.100.4")
    assert "WARNING" in r.stdout and "198.51.100.4" in r.stdout
    assert run(box, "--sslip").returncode == 0
    assert get(f, "RELAY_PUBLIC_HOST") == "203-0-113-7.sslip.io"


def test_it_stops_when_it_cannot_do_it_right(box):
    r = run(box, FAKE_UID="1000")
    assert r.returncode == 1 and "as root" in r.stderr
    r = run(box, ip="")
    assert r.returncode == 1 and "--public-ip" in r.stderr
    assert run(box, ip="", RELAY_PUBLIC_IP="198.51.100.4").returncode == 0
    r = run(box, "--domain", "not a domain")
    assert r.returncode == 1 and "not a domain name" in r.stderr
    assert run(box, "--bogus").returncode == 2
    assert not (box[0] / ".env").read_text().count("not a domain")


def test_ssh_on_another_port_stays_open(box):
    assert run(box, FAKE_SSH_PORT="2222").returncode == 0
    assert "ufw allow 2222/tcp" in (box[2] / "log").read_text()


def test_ssh_takes_keys_only(box):
    # a server made without an SSH key would take passwords from the whole internet
    _, _, tmp = box
    assert run(box).returncode == 0
    conf = tmp / "root/etc/ssh/sshd_config.d/00-quanti-relay.conf"
    assert conf.read_text().splitlines() == ["PasswordAuthentication no", "KbdInteractiveAuthentication no"]
    assert "systemctl try-reload-or-restart ssh" in (tmp / "log").read_text()
    # settings the server's SSH does not accept are taken back, so SSH cannot break
    conf.unlink()
    (tmp / "log").unlink()
    r = run(box, FAKE_SSHD_BAD="1")
    assert r.returncode == 0 and "SSH is left as it was" in r.stdout and not conf.exists()
    assert "try-reload-or-restart" not in (tmp / "log").read_text()


def test_a_folder_someone_else_can_change_is_refused(box):
    repo, _, _ = box
    r = run(box, RELAY_DIR_OWNER="0")                       # the test's files are not root's
    assert r.returncode == 1 and "clone the relay as root" in r.stderr and not (repo / ".env").exists()
    (repo / "deploy" / "relay").chmod(0o777)                # anyone may write it
    r = run(box)
    assert r.returncode == 1 and "deploy/relay" in r.stderr


def test_user_data_and_the_wrapper_are_shell_that_parses():
    for name in ("install.sh", "update.sh", "relay", "user-data.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / "deploy" / name)], check=False).returncode == 0, name
    user_data = (ROOT / "deploy" / "user-data.sh").read_text()
    assert 'REPOSITORY="https://github.com/Quantitizer/quanti-relay.git"' in user_data              # the public repository, filled in
    assert 'VERSION="<VERSION>"' in user_data           # the owner picks the release


@pytest.mark.parametrize(("args", "docker"), [
    (["doctor", "--json"], "compose --profile tls exec -T relay relay doctor --json"),
    (["setup", "show"], "compose --profile tls exec -T relay relay setup show"),
    (["restart"], "compose --profile tls up -d --force-recreate relay"),
    (["logs"], "compose --profile tls logs --tail=200 -f relay"),
    (["status"], "compose --profile tls ps"),
])
def test_the_relay_command_reaches_docker_compose(box, args, docker):
    """Run, not only parsed: v0.1.0 had `exec compose` on a shell function, which bash cannot exec."""
    repo, bin_dir, tmp = box
    e = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "LOG": str(tmp / "log"), "RELAY_DIR": str(repo)}
    r = subprocess.run(["bash", str(repo / "deploy" / "relay"), *args], env=e, capture_output=True, check=False,
                       text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert (tmp / "log").read_text().splitlines() == [f"docker {docker}"]


def test_the_tls_proxy_trusts_the_network_the_relay_trusts():
    import yaml
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    import ipaddress
    subnet = compose["networks"]["default"]["ipam"]["config"][0]["subnet"]
    caddy = compose["services"]["caddy"]
    proxy = caddy["networks"]["default"]["ipv4_address"]
    # the relay trusts the proxy's own address, not the network (its gateway carries local programs)
    assert f'PROXY="{proxy}"' in (ROOT / "deploy" / "install.sh").read_text()
    assert ipaddress.ip_address(proxy) in ipaddress.ip_network(subnet) and not proxy.endswith(".1")
    others = compose["networks"]["default"]["ipam"]["config"][0]["ip_range"]
    assert ipaddress.ip_address(proxy) not in ipaddress.ip_network(others)
    assert caddy["profiles"] == ["tls"] and "@sha256:" in caddy["image"]
    assert set(caddy["environment"]) == {"RELAY_HOST", "RELAY_ACME_PROFILE"}           # none of the relay's keys
    assert "env_file" not in caddy


# ---- relay update ----------------------------------------------------------------------------

@pytest.fixture
def clone(tmp_path):
    """A repository with a release tag and a branch, and the server's clone of it."""
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main"]
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run([*git, "init", "-q"], cwd=origin, check=True)
    shutil.copytree(ROOT / "deploy", origin / "deploy")
    (origin / "v").write_text("1")
    subprocess.run([*git, "add", "."], cwd=origin, check=True)
    subprocess.run([*git, "commit", "-qm", "one"], cwd=origin, check=True)
    subprocess.run([*git, "tag", "v1.0.0"], cwd=origin, check=True)
    server = tmp_path / "server"
    subprocess.run(["git", "clone", "-q", str(origin), str(server)], check=True)
    (origin / "v").write_text("2")                       # main moves on after the server cloned it
    subprocess.run([*git, "commit", "-qam", "two"], cwd=origin, check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {**STUBS, "docker": 'echo "docker $*" >> "$LOG"; case "$*" in *"db backup"*) '
                                          'exit "${FAKE_BACKUP_FAILS:-0}" ;; esac; true'}.items():
        (bin_dir / name).write_text(f"#!/bin/bash\n{body}\n")
        (bin_dir / name).chmod(0o755)
    (tmp_path / "root" / "usr" / "local" / "bin").mkdir(parents=True)
    return server, bin_dir, tmp_path


def update(clone, *args, **env):
    server, bin_dir, tmp = clone
    e = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "LOG": str(tmp / "log"), "RELAY_DIR": str(server),
         "RELAY_INSTALL_ROOT": str(tmp / "root"), "HOME": str(tmp), **env}
    return subprocess.run(["bash", str(server / "deploy" / "update.sh"), *args], check=False, env=e,
                          capture_output=True, text=True, timeout=60)


def test_update_takes_a_branch_as_the_repository_has_it_now(clone):
    server, _, tmp = clone
    r = update(clone, "main", "--yes")
    assert r.returncode == 0, r.stderr + r.stdout
    assert (server / "v").read_text() == "2"                 # not the stale local main
    log = (tmp / "log").read_text()
    assert "relay db backup" in log and "up -d --build --force-recreate" in log
    assert (tmp / "root/usr/local/bin/relay").exists()      # the relay command of the new version
    assert update(clone, "v1.0.0", "--yes").returncode == 0 and (server / "v").read_text() == "1"


def test_update_says_what_it_would_do_and_stops_without_a_copy(clone):
    server, _, tmp = clone
    r = update(clone, "main")
    assert r.returncode == 0 and "--yes" in r.stdout and (server / "v").read_text() == "1"
    r = update(clone, "main", "--yes", FAKE_BACKUP_FAILS="2")
    assert r.returncode == 1 and "STOP" in r.stdout and (server / "v").read_text() == "1"
    assert "up -d" not in (tmp / "log").read_text()
    assert update(clone, "main", "--yes", "--no-backup", FAKE_BACKUP_FAILS="2").returncode == 0
    assert update(clone, "nonsense", "--yes").returncode == 2

"""The tools an AI agent sets the relay up with (R7.2): the key check, the secrets file, relay doctor
and relay setup."""
import json
import os
import pty
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from relay import cli, doctor, envfile, setup
from relay.config import Settings
from relay.venue import keycheck
from relay.venue.keycheck import (
    KeyCheck,
    check_key,
    read_binance,
    read_bybit,
    read_kucoin,
    read_okx,
)

# ---- what the key may do -------------------------------------------------------------------------

BYBIT_OK = {"readOnly": 0, "ips": ["203.0.113.7"],
            "permissions": {"ContractTrade": ["Order", "Position"], "Spot": [], "Wallet": ["AccountTransfer"]}}


def test_bybit_answers_are_read():
    k = read_bybit(BYBIT_OK)
    assert (k.trade, k.withdraw, k.ip_bound, k.ips) == (True, False, True, ["203.0.113.7"])
    k = read_bybit({**BYBIT_OK, "readOnly": 1})
    assert k.trade is False
    k = read_bybit({**BYBIT_OK, "permissions": {"Wallet": ["Withdraw"], "Derivatives": ["DerivativesTrade"]},
                    "ips": ["*"]})
    assert (k.trade, k.withdraw, k.ip_bound, k.ips) == (True, True, False, [])
    assert read_bybit({**BYBIT_OK, "permissions": {"Spot": ["SpotTrade"]}}, "spot").trade is True
    assert read_bybit({**BYBIT_OK, "permissions": {"Spot": ["SpotTrade"]}}).trade is False   # a perpetual needs contracts
    assert read_bybit({"permissions": "?"}).error and read_bybit({"permissions": "?"}).trade is None


def test_binance_okx_and_kucoin_answers_are_read():
    k = read_binance({"enableFutures": True, "enableWithdrawals": False, "ipRestrict": True,
                      "enableSpotAndMarginTrading": False})
    assert (k.trade, k.withdraw, k.ip_bound, k.ips) == (True, False, True, None)
    assert read_binance({"enableFutures": True, "enableWithdrawals": False,
                         "enableSpotAndMarginTrading": False}, "spot").trade is False
    assert read_binance({"enableFutures": True, "enableWithdrawals": True}).withdraw is True
    assert read_binance({"enableWithdrawals": "yes"}).error
    k = read_okx({"data": [{"perm": "read_only,trade", "ip": "203.0.113.7, 198.51.100.4"}]})
    assert (k.trade, k.withdraw, k.ip_bound, k.ips) == (True, False, True, ["203.0.113.7", "198.51.100.4"])
    k = read_okx({"data": [{"perm": "read_only,trade,withdraw", "ip": ""}]})
    assert (k.withdraw, k.ip_bound) == (True, False)
    assert read_okx({"data": []}).error
    k = read_kucoin({"data": {"permission": "General,Futures", "ipWhitelist": "203.0.113.7"}})
    assert (k.trade, k.withdraw, k.ip_bound) == (True, False, True)
    k = read_kucoin({"data": {"permission": "General,Spot,Withdrawal", "ipWhitelist": ""}})
    assert (k.trade, k.withdraw, k.ip_bound) == (False, True, False)
    # KuCoin also calls its withdrawal permission "Transfer"; a name the relay does not know is
    # "unknown", never "cannot withdraw"; no address list in the answer: not known either
    assert read_kucoin({"data": {"permission": "General,Futures,Transfer"}}).withdraw is True
    assert read_kucoin({"data": {"permission": "General,Futures,InnerTransfer,Unified"}}).withdraw is False
    k = read_kucoin({"data": {"permission": "General,Futures,SomethingNew"}})
    assert k.withdraw is None and k.ip_bound is None


def _status(verdict):
    return {c["check"]: c["status"] for c in verdict}


def test_the_verdict_fails_a_withdrawing_or_non_trading_key_and_one_locked_elsewhere():
    good = KeyCheck("okx", trade=True, withdraw=False, ip_bound=True, ips=["203.0.113.7"])
    assert set(_status(good.verdict("203.0.113.7")).values()) == {"ok"}
    assert _status(good.verdict("198.51.100.4"))["key_ip_bound"] == "fail"
    assert _status(KeyCheck("okx", trade=True, withdraw=True, ip_bound=True, ips=[]).verdict())["key_no_withdrawal"] == "fail"
    assert _status(KeyCheck("okx", trade=False, withdraw=False).verdict())["key_trades"] == "fail"
    unknown = _status(KeyCheck("x").verdict())
    assert unknown == {"key_trades": "warn", "key_no_withdrawal": "warn", "key_ip_bound": "warn"}
    assert _status(KeyCheck("okx", trade=True, withdraw=False, ip_bound=False).verdict())["key_ip_bound"] == "warn"
    assert _status(KeyCheck("x", error="boom").verdict())["key_readable"] == "fail"
    # binance: bound, but to which addresses it does not say: not a failure
    assert _status(KeyCheck("binance", trade=True, withdraw=False, ip_bound=True).verdict("203.0.113.7"))[
        "key_ip_bound"] == "ok"


class _X:
    def __init__(self, exchange, answer=None, fail=None):
        self.id, self.answer, self.fail, self.calls = exchange, answer, fail, []

    def __getattr__(self, name):
        async def call(*a):
            self.calls.append(name)
            if self.fail:
                raise self.fail
            return self.answer
        return call


async def test_check_key_asks_the_exchanges_own_call_and_never_raises():
    x = _X("okx", {"data": [{"perm": "trade", "ip": "203.0.113.7"}]})
    k = await check_key(SimpleNamespace(x=x))
    assert x.calls == ["privateGetAccountConfig"] and k.exchange == "okx" and k.trade is True
    x = _X("bybit", {"retCode": 0, "result": BYBIT_OK})
    assert (await check_key(SimpleNamespace(x=x))).ip_bound is True
    k = await check_key(SimpleNamespace(x=_X("okx", fail=RuntimeError("down https://www.okx.com/api?sign=abc"))))
    assert k.error and "sign=abc" not in k.error and k.trade is None
    k = await check_key(SimpleNamespace(x=_X("gate")))
    assert k.error and "check them on the exchange" in k.error
    assert (k.trade, k.withdraw, k.ip_bound) == (None, None, None)          # unknown, never a guess



async def test_a_hyperliquid_key_is_an_agent_wallet_or_it_can_withdraw():
    eth_account = pytest.importorskip("eth_account")
    main_key, agent_key = "0x" + "11" * 32, "0x" + "22" * 32
    main = eth_account.Account.from_key(main_key).address
    agent = eth_account.Account.from_key(agent_key).address
    assert keycheck.eth_address(agent_key) == agent.lower()

    def hl(private_key, answer=None, fail=None):
        x = _X("hyperliquid", answer, fail)
        x.__dict__.update(walletAddress=main, privateKey=private_key)
        return x
    # the account's own key: it can withdraw, whatever else is said
    x = hl(main_key)
    k = await check_key(SimpleNamespace(x=x))
    assert (k.trade, k.withdraw) == (True, True) and x.calls == []
    # an agent wallet the account approved: it trades and cannot withdraw
    x = hl(agent_key, {"role": "agent", "data": {"user": main.lower()}})
    k = await check_key(SimpleNamespace(x=x))
    assert (k.trade, k.withdraw, k.error) == (True, False, None) and x.calls == ["publicPostInfo"]
    # an agent of another account, a plain wallet, no answer, no key: unknown, never "cannot withdraw"
    for x in (hl(agent_key, {"role": "agent", "data": {"user": "0x" + "ab" * 20}}),
              hl(agent_key, {"role": "user"}), hl(agent_key, fail=RuntimeError("down")), hl("not a key")):
        k = await check_key(SimpleNamespace(x=x))
        assert k.withdraw is None and k.error, x.answer


async def test_the_native_bybit_client_is_asked_directly():
    class Native:
        async def get_api_key_info(self):
            return BYBIT_OK
    assert (await check_key(Native())).ips == ["203.0.113.7"]

    class Broken:
        async def get_api_key_info(self):
            raise RuntimeError("10003 invalid key")
    assert (await check_key(Broken())).error


# ---- the secrets file ---------------------------------------------------------------------------

def test_a_secret_is_set_without_touching_the_other_lines(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# my notes\nRELAY_API_KEY=old\nOTHER='x' \nexport RELAY_API_KEY=older\n")
    assert envfile.put(f, "RELAY_API_KEY", "n3w-v@lue with space")
    text = f.read_text()
    assert text == "# my notes\nRELAY_API_KEY='n3w-v@lue with space'\nOTHER='x' \n"
    assert envfile.get(f, "RELAY_API_KEY") == "n3w-v@lue with space" and envfile.get(f, "OTHER") == "x"
    assert oct(f.stat().st_mode & 0o777) == oct(0o600)
    assert envfile.put(f, "RELAY_API_KEY", "n3w-v@lue with space") is False
    assert envfile.names(f) == {"RELAY_API_KEY": True, "OTHER": True}
    assert envfile.put(f, "NEW_ONE", "v") and envfile.get(f, "NEW_ONE") == "v"


def test_the_last_line_for_a_name_wins_as_in_docker_compose(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=first\nA=second\n")
    assert envfile.get(f, "A") == "second"


def test_values_and_names_that_cannot_be_written_safely_are_refused(tmp_path):
    f = tmp_path / ".env"
    for bad in ("", "it's", "two\nlines"):
        with pytest.raises(envfile.EnvFileError):
            envfile.put(f, "RELAY_X", bad)
    for name in ("lower", "X", "1ABC", "A-B"):
        with pytest.raises(envfile.EnvFileError):
            envfile.put(f, name, "v")
    assert not f.exists()


def test_values_as_docker_compose_and_dotenv_write_them_are_read(tmp_path):
    f = tmp_path / ".env"
    f.write_text('A="double"\nB=plain # a comment\nC=\nexport D=d\n')
    assert (envfile.get(f, "A"), envfile.get(f, "B"), envfile.get(f, "C"), envfile.get(f, "D")) == (
        "double", "plain", "", "d")
    assert envfile.names(f) == {"A": True, "B": True, "C": False, "D": True}


def test_secret_set_asks_the_terminal_and_never_prints_the_value(tmp_path, monkeypatch, capsys):
    f = tmp_path / ".env"
    monkeypatch.setattr(envfile, "ask_hidden", lambda prompt: "s3cret-value")
    assert cli.main(["secret", "--env-file", str(f), "set", "BYBIT_API_KEY"]) == 0
    out = capsys.readouterr()
    assert "s3cret-value" not in out.out + out.err and "12 characters" in out.out
    assert envfile.get(f, "BYBIT_API_KEY") == "s3cret-value"


def test_generate_writes_a_long_value_unseen_and_never_replaces_one(tmp_path, capsys):
    f = tmp_path / ".env"
    assert envfile.main(["--env-file", str(f), "generate", "RELAY_API_KEY"]) == 0
    value = envfile.get(f, "RELAY_API_KEY")
    assert len(value) >= 32 and value not in capsys.readouterr().out
    assert envfile.main(["--env-file", str(f), "generate", "RELAY_API_KEY"]) == 2
    assert envfile.get(f, "RELAY_API_KEY") == value


ENVFILE = Path(envfile.__file__)


def test_without_a_terminal_set_and_reveal_refuse(tmp_path):
    """A process with no terminal at all (an agent's command), not a stand-in for one."""
    f = tmp_path / ".env"
    f.write_text("RELAY_API_KEY='abc'\n")
    for action in ("set", "reveal"):
        r = subprocess.run([sys.executable, "-I", str(ENVFILE), "--env-file", str(f), action, "RELAY_API_KEY"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, start_new_session=True,
                           timeout=30, check=False)
        assert r.returncode == 2 and "person at a terminal" in r.stderr and "abc" not in r.stdout + r.stderr
    assert envfile.get(f, "RELAY_API_KEY") == "abc"


def _at_a_terminal(f, action, typed=b""):
    """Runs `relay secret <action>` with a real (pseudo) terminal, as an SSH session or a provider's
    console gives it; what is typed goes to the terminal. Returns the exit code and the screen."""
    pid, fd = pty.fork()
    if pid == 0:                                                        # the child: the terminal is its own
        os.execv(sys.executable, [sys.executable, "-I", str(ENVFILE), "--env-file", str(f), action,
                                  "RELAY_API_KEY"])
    screen = b""
    try:
        if typed:
            while b"hidden" not in screen:                              # wait for the prompt, then type
                try:
                    chunk = os.read(fd, 1024)
                except OSError:
                    chunk = b""
                if not chunk:                                           # it ended without asking
                    break
                screen += chunk
            else:
                os.write(fd, typed)
        while True:
            try:
                chunk = os.read(fd, 1024)
            except OSError:                                             # the child closed the terminal
                break
            if not chunk:
                break
            screen += chunk
    finally:
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    return os.waitstatus_to_exitcode(status), screen.decode()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs a pseudo-terminal")
def test_at_a_real_terminal_set_and_reveal_work(tmp_path):
    f = tmp_path / ".env"
    code, screen = _at_a_terminal(f, "set", b"s3cr3t-typed-value\n")
    assert code == 0, screen
    assert envfile.get(f, "RELAY_API_KEY") == "s3cr3t-typed-value"
    assert "s3cr3t" not in screen                                       # typing is hidden
    code, screen = _at_a_terminal(f, "reveal")
    assert code == 0 and "s3cr3t-typed-value" in screen, screen


def test_reveal_writes_to_the_terminal_not_to_the_output(tmp_path, monkeypatch, capsys):
    f = tmp_path / ".env"
    f.write_text("RELAY_API_KEY='abc123'\n")
    shown = []
    monkeypatch.setattr(envfile, "show_on_tty", shown.append)
    assert envfile.main(["--env-file", str(f), "reveal", "RELAY_API_KEY"]) == 0
    assert shown == ["abc123"] and "abc123" not in capsys.readouterr().out


def test_a_plain_setting_is_written_and_a_secret_sounding_one_refused(tmp_path, capsys):
    f = tmp_path / ".env"
    assert envfile.main(["--env-file", str(f), "setting", "RELAY_PUBLIC_HOST", "relay.example.com"]) == 0
    assert envfile.get(f, "RELAY_PUBLIC_HOST") == "relay.example.com"
    for name in ("RELAY_API_KEY", "BYBIT_API_SECRET", "RELAY_SINK_T_BOT_TOKEN", "RELAY_HEARTBEAT_URL",
                 "RELAY_OKX_API_PASSPHRASE", "DB_PASSWORD"):
        assert envfile.main(["--env-file", str(f), "setting", name, "x"]) == 2, name
        assert envfile.get(f, name) is None
    assert "relay secret set" in capsys.readouterr().err


def test_doctor_checks_the_key_against_the_installed_address(tmp_path, monkeypatch, capsys):
    from relay import config as config_mod
    seen = {}

    async def fake(dep, settings, server_ip):
        seen["ip"] = server_ip
        return []
    monkeypatch.setattr(doctor, "key_checks", fake)
    monkeypatch.setattr(config_mod, "settings", _settings(tmp_path, public_ip="203.0.113.7"))
    cli.main(["doctor", "--online", "--json"])
    assert seen["ip"] == "203.0.113.7"
    cli.main(["doctor", "--online", "--json", "--server-ip", "198.51.100.4"])
    assert seen["ip"] == "198.51.100.4"
    capsys.readouterr()


def test_list_shows_names_only(tmp_path, capsys):
    f = tmp_path / ".env"
    f.write_text("RELAY_API_KEY='abc123'\nRELAY_EMPTY=\n")
    assert envfile.main(["--env-file", str(f), "list"]) == 0
    out = capsys.readouterr().out
    assert "RELAY_API_KEY: set" in out and "RELAY_EMPTY: empty" in out and "abc123" not in out


def test_the_secrets_file_runs_with_the_standard_library_alone():
    src = Path(envfile.__file__).read_text()
    imports = {line.split()[1].split(".")[0] for line in src.splitlines()
               if line.startswith(("import ", "from ")) and not line.startswith("from __future__")}
    assert imports <= {"argparse", "io", "os", "re", "secrets", "sys", "tempfile", "pathlib", "termios"}


# ---- relay doctor -------------------------------------------------------------------------------

def _settings(tmp_path, **kw):
    (tmp_path / "config").mkdir(exist_ok=True)
    base = {"api_key": "k" * 40, "config_dir": str(tmp_path / "config"),
            "database_url": f"sqlite+aiosqlite:///{tmp_path / 'relay.db'}"}
    return Settings(_env_file=None, **{**base, **kw})


def _by_name(checks):
    return {c["check"]: c for c in checks}


def test_doctor_on_a_fresh_folder(tmp_path):
    checks, dep = doctor.offline_checks(_settings(tmp_path), environ={})
    c = _by_name(checks)
    assert dep is None
    assert c["api_key"]["status"] == "ok" and c["mode"]["status"] == "ok"
    assert c["deployment"]["status"] == "warn" and c["sources"]["status"] == "warn"
    assert c["exchange_keys"]["status"] == "warn" and "relay secret set BYBIT_API_KEY" in c["exchange_keys"]["fix"]
    assert c["heartbeat"]["status"] == "warn" and c["sinks"]["status"] == "warn"
    assert not any(x["status"] == "fail" for x in checks)


def test_doctor_fails_what_stops_the_relay(tmp_path):
    c = _by_name(doctor.offline_checks(_settings(tmp_path, api_key=""), environ={})[0])
    assert c["api_key"]["status"] == "fail" and "generate" in c["api_key"]["fix"]
    c = _by_name(doctor.offline_checks(_settings(tmp_path, api_key="short"), environ={})[0])
    assert c["api_key"]["status"] == "fail"
    c = _by_name(doctor.offline_checks(_settings(tmp_path, api_readonly_key="short"), environ={})[0])
    assert c["api_readonly_key"]["status"] == "fail"
    c = _by_name(doctor.offline_checks(_settings(tmp_path, trusted_proxies="nonsense"), environ={})[0])
    assert c["network_settings"]["status"] == "fail"
    c = _by_name(doctor.offline_checks(_settings(tmp_path, config_dir=str(tmp_path / "none")), environ={})[0])
    assert c["config_folder"]["status"] == "fail"
    (tmp_path / "config" / "deployments").mkdir()
    (tmp_path / "config" / "deployments" / "x.yaml").write_text("venue: {exchange: okx}\n")
    c = _by_name(doctor.offline_checks(_settings(tmp_path), environ={})[0])
    assert c["deployment"]["status"] == "fail"
    (tmp_path / "config" / "deployments" / "x.yaml").unlink()
    (tmp_path / "config" / "sources").mkdir()
    (tmp_path / "config" / "sources" / "bot.yaml").write_text(
        "auth: hmac\nsecret_env: RELAY_SOURCE_BOT_SECRET\ndeployments: [default]\n")
    c = _by_name(doctor.offline_checks(_settings(tmp_path), environ={})[0])
    assert c["sources"]["status"] == "fail"
    c = _by_name(doctor.offline_checks(_settings(tmp_path), environ={"RELAY_SOURCE_BOT_SECRET": "s" * 40})[0])
    assert c["sources"]["status"] == "ok"


def test_doctor_on_a_live_deployment_without_keys_fails_and_says_live(tmp_path):
    _settings(tmp_path)
    setup.write_config(tmp_path / "config", "deployment", "main",
                       {"venue": {"exchange": "okx", "environment": "testnet"}})
    p = tmp_path / "config" / "deployments" / "main.yaml"
    p.write_text(p.read_text().replace("dry_run: true", "dry_run: false"))
    c = _by_name(doctor.offline_checks(_settings(tmp_path, dry_run=False), environ={})[0])
    assert c["mode"]["status"] == "warn" and "LIVE" in c["mode"]["detail"]
    assert c["exchange_keys"]["status"] == "fail" and "RELAY_OKX_API_KEY" in c["exchange_keys"]["detail"]
    # either key says paper: paper
    c = _by_name(doctor.offline_checks(_settings(tmp_path, dry_run=True), environ={})[0])
    assert c["mode"]["status"] == "ok" and c["exchange_keys"]["status"] == "warn"
    # no file and RELAY_DRY_RUN=false: the relay runs the built-in deployment LIVE, and says so
    p.unlink()
    c = _by_name(doctor.offline_checks(_settings(tmp_path, dry_run=False), environ={})[0])
    assert c["mode"]["status"] == "warn" and "LIVE" in c["mode"]["detail"]
    assert c["exchange_keys"]["status"] == "fail"


def test_doctor_database_states(tmp_path):
    from relay.runtime import upgrade_schema
    s = _settings(tmp_path)
    upgrade_schema(s.database_url)
    assert "latest" in _by_name(doctor.offline_checks(s, environ={})[0])["database"]["detail"]
    db = tmp_path / "relay.db"
    con = sqlite3.connect(db)
    con.execute("UPDATE alembic_version SET version_num = '0001'")
    con.commit()
    c = _by_name(doctor.offline_checks(s, environ={})[0])["database"]
    assert c["status"] == "ok" and "upgraded" in c["detail"]
    con.execute("UPDATE alembic_version SET version_num = '9999_from_the_future'")     # after a rollback
    con.commit()
    c = _by_name(doctor.offline_checks(s, environ={})[0])["database"]
    assert c["status"] == "fail" and "does not know" in c["detail"]
    con.execute("DROP TABLE alembic_version")
    con.commit()
    con.close()
    c = _by_name(doctor.offline_checks(s, environ={})[0])["database"]
    assert c["status"] == "warn" and "without a copy" in c["detail"]
    db.write_bytes(b"not a database at all, just bytes" * 10)
    assert _by_name(doctor.offline_checks(s, environ={})[0])["database"]["status"] == "fail"


def test_doctor_cli_json_and_exit_code(tmp_path, monkeypatch, capsys):
    from relay import config as config_mod
    monkeypatch.setattr(config_mod, "settings", _settings(tmp_path))
    assert cli.main(["doctor", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and {c["check"] for c in out["checks"]} >= {"api_key", "deployment", "database"}
    monkeypatch.setattr(config_mod, "settings", _settings(tmp_path, api_key=""))
    assert cli.main(["doctor"]) == 1 and "FAIL api_key" in capsys.readouterr().out
    monkeypatch.setattr(config_mod, "settings", _settings(tmp_path))
    assert cli.main(["doctor", "--json", "--public-url", "http://plain.example"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert _by_name(out["checks"])["public_address"]["status"] == "fail"


async def test_doctor_http_check(monkeypatch):
    import httpx
    real = httpx.AsyncClient
    me = {"service": "quanti-relay"}
    answers = {"https://ok.example/health": httpx.Response(200, json={**me, "status": "ok"}),
               "https://stuck.example/health": httpx.Response(503, json={**me, "status": "stuck"}),
               "https://other.example/health": httpx.Response(200, json={"status": "ok"}),
               "https://html.example/health": httpx.Response(200, text="<html>", headers={"content-type": "text/html"}),
               "https://broken.example/health": httpx.Response(
                   200, content=b"{not json", headers={"content-type": "application/json"}),
               "https://list.example/health": httpx.Response(200, json=[1]),
               "https://odd.example/health": httpx.Response(200, json={**me, "status": "starting"})}

    def handler(req):
        if str(req.url) not in answers:
            raise httpx.ConnectError("no")
        return answers[str(req.url)]
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert (await doctor.http_check("x", "https://ok.example/"))["status"] == "ok"
    bad = await doctor.http_check("x", "https://stuck.example")
    assert bad["status"] == "fail" and "stuck" in bad["detail"]
    assert (await doctor.http_check("x", "https://gone.example"))["status"] == "fail"
    assert (await doctor.http_check("x", "https://odd.example"))["status"] == "fail"     # 200, but not ok
    for other in ("other", "html", "broken", "list"):                  # not a relay: never "ok", never a crash
        c = await doctor.http_check("x", f"https://{other}.example")
        assert c["status"] == "fail" and "not as a Quanti Relay" in c["detail"], other


async def test_doctor_key_checks_skip_without_keys_and_read_the_verdict(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    assert await doctor.key_checks(None, s, None) == []
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")

    async def fake(client, market):
        assert client.base_url == "https://api-testnet.bybit.com"         # the settings' host
        return KeyCheck("bybit", trade=True, withdraw=True, ip_bound=False)
    monkeypatch.setattr(keycheck, "check_key", fake)
    checks = _by_name(await doctor.key_checks(None, s, "203.0.113.7"))
    assert checks["key_no_withdrawal"]["status"] == "fail" and "203.0.113.7" in checks["key_ip_bound"]["detail"]


# ---- relay setup --------------------------------------------------------------------------------

def _run(capsys, *argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = cli.main(["setup", *argv])
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from relay import config as config_mod
    s = _settings(tmp_path)
    monkeypatch.setattr(config_mod, "settings", s)
    return Path(s.config_dir)


def test_setup_write_is_checked_repeatable_and_never_live(cfg, capsys, monkeypatch):
    body = json.dumps({"venue": {"exchange": "okx", "environment": "testnet"}, "dry_run": False})
    code, out = _run(capsys, "write", "deployment", "main", "--file", "-", stdin=body, monkeypatch=monkeypatch)
    assert code == 0 and out["changed"] and out["dry_run"] is True             # never live from here
    assert yaml.safe_load((cfg / "deployments" / "main.yaml").read_text())["dry_run"] is True
    code, out = _run(capsys, "write", "deployment", "main", "--file", "-", stdin=body, monkeypatch=monkeypatch)
    assert code == 0 and out["changed"] is False and out["restart_needed"] is False
    assert not (cfg / ".history").exists()                                    # nothing to keep: nothing changed
    code, out = _run(capsys, "write", "deployment", "main", "--file", "-", stdin="risk: {max_positions: 1.5}",
                     monkeypatch=monkeypatch)
    assert code == 1 and out["ok"] is False and "max_positions" in out["error"]


def test_setup_show_tells_which_variables_are_set_never_their_values(cfg, capsys, monkeypatch, tmp_path):
    f = tmp_path / "src.yaml"
    f.write_text("auth: hmac\nsecret_env: RELAY_SOURCE_BOT_SECRET\ndeployments: [default]\n")
    code, out = _run(capsys, "write", "source", "bot", "--file", str(f))
    assert code == 0
    monkeypatch.setenv("RELAY_SOURCE_BOT_SECRET", "a-very-secret-value-" * 3)
    code, out = _run(capsys, "show")
    assert out["sources"][0]["variables"] == {"RELAY_SOURCE_BOT_SECRET": True}
    assert out["paper"] is True and out["deployment"] is None
    assert "a-very-secret-value" not in json.dumps(out)
    code, out = _run(capsys, "remove", "source", "bot")
    assert code == 0 and not (cfg / "sources" / "bot.yaml").exists()
    code, out = _run(capsys, "remove", "source", "bot")
    assert code == 1 and "no source" in out["error"]


def test_setup_catalog_lists_the_exchanges(cfg, capsys):
    code, out = _run(capsys, "catalog")
    assert code == 0 and out["native"]["exchange"] == "bybit" and any(p["exchange"] == "okx" for p in out["profiled"])


def test_a_repeated_console_write_keeps_no_extra_history(tmp_path):
    data = {"venue": {"exchange": "okx", "environment": "testnet"}}
    setup.write_config(tmp_path, "deployment", "main", data)
    setup.write_config(tmp_path, "deployment", "main", data)
    assert not (tmp_path / ".history").exists()
    assert os.access(tmp_path / "deployments" / "main.yaml", os.R_OK)


def test_the_relay_reports_the_version_it_is_released_as():
    import tomllib

    import relay
    pyproject = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    assert relay.__version__ == pyproject["project"]["version"]

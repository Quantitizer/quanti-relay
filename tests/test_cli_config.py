"""Signal rate limit per source, `relay check-config` and `relay test-signal`."""
import json

import httpx
import pytest

from relay import cli
from relay.config import settings
from relay.signals.contract import parse_signal
from relay.signals.sources import SourceConfigError, load_sources
from tests.test_signal_intake import SECRET, _post, _settled, _signal, config_dir, relay  # noqa: F401
from tests.test_signal_sources import GOOD, _write

# ---- rate limit ---------------------------------------------------------------------------------


def test_the_rate_is_counted_over_the_last_minute(tmp_path):
    _write(tmp_path, "bot.yaml", GOOD + "max_signals_per_minute: 2\n")
    src = load_sources(tmp_path, environ={"BOT_SECRET": SECRET})["bot"]
    assert [src.over_rate(now=t) for t in (0, 10, 20)] == [False, False, True]
    assert src.over_rate(now=60) is False                    # the one at 0 is a minute old
    assert src.over_rate(now=61) is True                     # 10 and 60 are still in the window
    unlimited = load_sources(tmp_path, environ={"BOT_SECRET": SECRET})["bot"].__class__(
        name="x", auth="hmac", secret_env="X", deployments=("d",))
    assert not any(unlimited.over_rate(now=0) for _ in range(1000))


@pytest.mark.parametrize("value", ["0", "true", "'5'", "2.5"])
def test_an_unusable_rate_stops_the_start(tmp_path, value):
    _write(tmp_path, "bot.yaml", GOOD + f"max_signals_per_minute: {value}\n")
    with pytest.raises(SourceConfigError, match="max_signals_per_minute"):
        load_sources(tmp_path, environ={"BOT_SECRET": SECRET})


async def test_signals_over_the_rate_are_a_429_and_unsigned_ones_do_not_count(relay, config_dir):  # noqa: F811
    client, _, rt = relay
    rt.sources["mybot"] = rt.sources["mybot"].__class__(
        name="mybot", auth="hmac", secret_env="MYBOT_SECRET", deployments=("default",),
        max_signals_per_minute=2, _secret=SECRET)
    for _ in range(5):                                       # forged: refused before they are counted
        assert (await _post(client, _signal(), secret="x" * 40)).status_code == 401
    close = {k: v for k, v in _signal().items() if k not in ("side", "size", "stop")} | {"action": "close"}
    assert (await _post(client, {**close, "id": "a"})).status_code == 202
    assert (await _post(client, {**close, "id": "b"})).status_code == 202
    for _ in range(5):                                       # repeats (a retry, a replay): not counted
        again = await _post(client, {**close, "id": "a", "sent_at": _signal()["sent_at"]})
        assert again.json()["status"] == "duplicate"
    third = await _post(client, {**close, "id": "c"})
    assert third.status_code == 429 and "max_signals_per_minute" in third.json()["detail"]
    assert await rt.signal_log.get_by_source_id("mybot", "c") is None       # not logged: resend later
    await _settled(rt)


# ---- relay check-config -----------------------------------------------------------------------

def _run(argv, capsys):
    code = cli.main(argv)
    return code, capsys.readouterr()


def test_check_config_reports_what_the_relay_would_run(tmp_path, monkeypatch, capsys):
    (tmp_path / "deployments").mkdir()
    (tmp_path / "deployments" / "desk.yaml").write_text("enabled: false\n")
    _write(tmp_path, "bot.yaml", GOOD.replace("[default]", "[desk]"))
    _write(tmp_path, "old.yaml", GOOD.replace("BOT_SECRET", "OLD_SECRET"))
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    monkeypatch.setenv("BOT_SECRET", SECRET)
    monkeypatch.setenv("OLD_SECRET", SECRET)
    code, out = _run(["check-config"], capsys)
    assert code == 0
    assert "deployment: desk - entries DISABLED\n" in out.out
    assert "mode: paper (dry-run): no order reaches the venue - venue: bybit, host: testnet" in out.out
    assert "source: bot -> desk\n" in out.out
    assert "source: old -> default (not bound to the running deployment desk)" in out.out
    assert out.out.rstrip().endswith("config OK")


def test_check_config_fails_on_a_bad_file(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "bot.yaml", GOOD)
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    monkeypatch.delenv("BOT_SECRET", raising=False)
    code, out = _run(["check-config"], capsys)
    assert code == 1 and "sources: PROBLEM" in out.out and "BOT_SECRET" in out.out
    assert "will not start" in out.out


def test_check_config_without_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    code, out = _run(["check-config"], capsys)
    assert code == 0 and "the RELAY_* environment settings apply" in out.out and "refuses everything" in out.out
    assert "mode: paper (dry-run)" in out.out


@pytest.mark.parametrize("env_dry, file_dry, testnet, demo, line", [
    (False, None, True, False, "mode: LIVE - venue: bybit, host: testnet"),         # no file: the environment decides
    (False, None, False, False, "mode: LIVE - venue: bybit, host: MAINNET (real money)"),
    (False, None, False, True, "mode: LIVE - venue: bybit, host: demo"),
    (True, False, False, False, "mode: paper (dry-run)"),
    (False, True, False, False, "mode: paper (dry-run)"),
    (False, False, False, False, "mode: LIVE - venue: bybit, host: MAINNET (real money)"),
])
def test_check_config_always_says_paper_or_live_like_the_relay(tmp_path, monkeypatch, capsys,
                                                                env_dry, file_dry, testnet, demo, line):
    if file_dry is not None:
        (tmp_path / "deployments").mkdir()
        (tmp_path / "deployments" / "d.yaml").write_text(f"dry_run: {str(file_dry).lower()}\n")
    for k, v in (("config_dir", str(tmp_path)), ("dry_run", env_dry), ("testnet", testnet), ("bybit_demo", demo)):
        monkeypatch.setattr(settings, k, v)
    code, out = _run(["check-config"], capsys)
    assert code == 0 and line in out.out


def test_the_module_runs_as_a_script(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    r = subprocess.run([sys.executable, "-m", "relay.cli", "check-config"], check=False, capture_output=True,
                       text=True, cwd=Path(__file__).resolve().parents[1],
                       env={**os.environ, "RELAY_CONFIG_DIR": str(tmp_path)})
    assert r.returncode == 0 and "config OK" in r.stdout, r.stderr


# ---- relay test-signal ------------------------------------------------------------------------

@pytest.fixture
def bot(tmp_path, monkeypatch):
    _write(tmp_path, "bot.yaml", GOOD)
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    monkeypatch.setenv("BOT_SECRET", SECRET)
    return load_sources(tmp_path, environ={"BOT_SECRET": SECRET})["bot"]


OPEN_ARGS = ["test-signal", "--source", "bot", "--symbol", "BTCUSDT", "--side", "long", "--notional", "100",
             "--stop-pct", "2"]


def test_test_signal_is_a_valid_signed_dry_run_signal(bot, capsys):
    code, out = _run([*OPEN_ARGS, "--print-only"], capsys)
    assert code == 0
    _, header_line, raw = out.out.strip().split("\n")
    header = header_line.split(": ", 1)[1]
    assert bot.verify(header, raw.encode()) is None
    sig = parse_signal(json.loads(raw))
    assert (sig.dry_run, sig.source, sig.deployment, sig.action, sig.stop.pct) == (True, "bot", "default", "open", 2)


def test_test_signal_sends_it_and_reports_the_answer(bot, monkeypatch, capsys):
    sent = {}

    def fake_post(url, content, headers, timeout):
        sent.update(url=url, body=content, header=headers["X-Relay-Signature"])
        return httpx.Response(202, json={"status": "accepted", "signal": 1})
    monkeypatch.setattr(httpx, "post", fake_post)
    code, out = _run([*OPEN_ARGS, "--url", "http://relay:9000"], capsys)
    assert code == 0 and sent["url"] == "http://relay:9000/v1/signals" and out.out.startswith("202")
    assert bot.verify(sent["header"], sent["body"]) is None and json.loads(sent["body"])["dry_run"] is True


def test_test_signal_refuses_conflicting_options(bot, capsys):
    for extra in (["--stop-price", "95"], ):
        with pytest.raises(SystemExit):
            cli.main([*OPEN_ARGS, *extra])
    with pytest.raises(SystemExit):
        cli.main([*OPEN_ARGS, "--close-pct", "20"])
    capsys.readouterr()


def test_test_signal_warns_before_sending_unencrypted_to_another_host(bot, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(202, json={"status": "accepted"}))
    _, out = _run([*OPEN_ARGS, "--url", "http://relay.example:8080"], capsys)
    assert "unencrypted" in out.err
    _, out = _run([*OPEN_ARGS, "--url", "https://relay.example"], capsys)
    assert "unencrypted" not in out.err
    _, out = _run(OPEN_ARGS, capsys)                                  # localhost
    assert "unencrypted" not in out.err


def test_test_signal_refuses_what_would_not_be_a_signal(bot, capsys):
    code, out = _run(["test-signal", "--source", "bot", "--symbol", "BTCUSDT"], capsys)     # open without side
    assert code == 2 and "not a valid signal" in out.err
    code, out = _run(["test-signal", "--source", "nobody"], capsys)
    assert code == 2 and "no source named 'nobody'" in out.err


def test_test_signal_reports_an_unreachable_relay(bot, monkeypatch, capsys):
    def down(*a, **kw):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "post", down)
    code, out = _run(OPEN_ARGS, capsys)
    assert code == 1 and "could not reach the relay" in out.err


def test_test_signal_sends_a_target_book(bot, capsys):
    code, out = _run(["test-signal", "--source", "bot", "--weights", "BTCUSDT=0.05,ETHUSDT=-0.03",
                      "--stop-pct", "2", "--gross-leverage", "1.5", "--print-only"], capsys)
    assert code == 0
    sig = parse_signal(json.loads(out.out.strip().split("\n")[2]))
    assert (sig.kind, sig.weights, sig.stop_pct, sig.gross_leverage, sig.dry_run) == (
        "target", {"BTC/USDT:USDT": 0.05, "ETH/USDT:USDT": -0.03}, 2, 1.5, True)


@pytest.mark.parametrize("extra, message", [
    (["--weights", "BTCUSDT:0.05"], "SYMBOL=WEIGHT"),
    (["--weights", "BTCUSDT=0.05", "--symbol", "ETHUSDT"], "do not belong"),
])
def test_test_signal_refuses_a_malformed_target_book(bot, capsys, extra, message):
    code, out = _run(["test-signal", "--source", "bot", *extra], capsys)
    assert code == 2 and message in out.err


@pytest.mark.parametrize("extra, message", [
    (["--weights", "BTCUSDT=0.05,BTCUSDT=0.02"], "listed twice"),
    (["--weights", "BTCUSDT=0.05", "--action", "close"], "--action does not belong"),
])
def test_test_signal_refuses_ambiguous_target_options(bot, capsys, extra, message):
    code, out = _run(["test-signal", "--source", "bot", *extra], capsys)
    assert code == 2 and message in out.err

"""Sources: the source files, their secrets (from the environment only) and HMAC signatures."""
import pytest

from relay.signals.sources import (
    MIN_SECRET_LENGTH,
    SIGNATURE_WINDOW_S,
    SourceConfigError,
    load_sources,
    sign,
    verify_signature,
)

SECRET = "s" * MIN_SECRET_LENGTH
BODY = b'{"version":"1","id":"a"}'


def _write(tmp_path, name, text):
    folder = tmp_path / "sources"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(text)


GOOD = "auth: hmac\nsecret_env: BOT_SECRET\ndeployments: [default]\n"


def test_a_signed_body_is_accepted_inside_the_window():
    header = sign(SECRET, BODY, t=1_000)
    assert verify_signature(SECRET, header, BODY, now=1_000) is None
    assert verify_signature(SECRET, header, BODY, now=1_000 + SIGNATURE_WINDOW_S) is None
    assert verify_signature(SECRET, header, BODY, now=1_000 - SIGNATURE_WINDOW_S) is None


@pytest.mark.parametrize("header, body, secret, now, match", [
    (None, BODY, SECRET, 1_000, "missing"),
    ("", BODY, SECRET, 1_000, "missing"),
    ("v1=abc", BODY, SECRET, 1_000, "malformed"),
    ("t=1000", BODY, SECRET, 1_000, "malformed"),
    ("t=abc,v1=abc", BODY, SECRET, 1_000, "malformed"),
    (sign(SECRET, BODY, t=1_000), BODY + b" ", SECRET, 1_000, "does not match"),     # body changed
    (sign(SECRET, BODY, t=1_000), BODY, "x" * MIN_SECRET_LENGTH, 1_000, "does not match"),  # other secret
    (sign(SECRET, BODY, t=1_000).replace("t=1000", "t=1001"), BODY, SECRET, 1_000, "does not match"),
    (sign(SECRET, BODY, t=1_000), BODY, SECRET, 1_000 + SIGNATURE_WINDOW_S + 1, "window"),   # replayed later
    (sign(SECRET, BODY, t=1_000), BODY, SECRET, 1_000 - SIGNATURE_WINDOW_S - 1, "window"),   # from the future
    (sign(SECRET, BODY, t=1_000), BODY, "", 1_000, "no secret"),
])
def test_signatures_that_are_refused(header, body, secret, now, match):
    assert match in verify_signature(secret, header, body, now=now)


def test_one_matching_signature_of_several_is_enough():
    good = sign(SECRET, BODY, t=1_000).split("v1=")[1]
    assert verify_signature(SECRET, f"t=1000,v1=deadbeef,v1={good}", BODY, now=1_000) is None
    assert verify_signature(SECRET, f"t=1000, v1={good.upper()}", BODY, now=1_000) is None


def test_sources_are_read_with_their_secret_from_the_environment(tmp_path):
    _write(tmp_path, "mybot.yaml", GOOD)
    _write(tmp_path, "other.yml", "name: desk\nauth: hmac\nsecret_env: DESK\ndeployments: [a, b]\nenabled: false\n")
    sources = load_sources(tmp_path, environ={"BOT_SECRET": SECRET, "DESK": "d" * 40})
    bot, desk = sources["mybot"], sources["desk"]
    assert (bot.deployments, bot.enabled, desk.deployments, desk.enabled) == (("default",), True, ("a", "b"), False)
    assert bot.may_reach("default") and not bot.may_reach("a")
    assert bot.verify(sign(SECRET, BODY), BODY) is None
    assert desk.verify(sign(SECRET, BODY), BODY) == "signature does not match"
    assert SECRET not in repr(bot)                                 # never in a log line


def test_no_folder_means_no_sources(tmp_path):
    assert load_sources(tmp_path, environ={}) == {}


@pytest.mark.parametrize("text, env, match", [
    (GOOD + "secret: abc\n", {"BOT_SECRET": SECRET}, "never goes in a config file"),
    (GOOD, {}, "is not set"),
    (GOOD, {"BOT_SECRET": "short"}, f"at least {MIN_SECRET_LENGTH}"),
    (GOOD.replace("hmac", "none"), {"BOT_SECRET": SECRET}, "auth must be hmac"),
    (GOOD.replace("deployments: [default]", "deployments: []"), {"BOT_SECRET": SECRET}, "deployments"),
    (GOOD.replace("deployments: [default]", "deployments: default"), {"BOT_SECRET": SECRET}, "deployments"),
    (GOOD.replace("secret_env: BOT_SECRET\n", ""), {"BOT_SECRET": SECRET}, "secret_env"),
    (GOOD + "enabled: yes please\n", {"BOT_SECRET": SECRET}, "enabled"),
    (GOOD + "deployment: [x]\n", {"BOT_SECRET": SECRET}, "unknown setting"),
    ("auth: [hmac\n", {}, "not valid YAML"),
    ("- a\n- b\n", {}, "mapping"),
])
def test_a_bad_source_file_stops_the_relay(tmp_path, text, env, match):
    _write(tmp_path, "mybot.yaml", text)
    with pytest.raises(SourceConfigError, match=match):
        load_sources(tmp_path, environ=env)


def test_two_sources_may_not_share_a_name(tmp_path):
    _write(tmp_path, "a.yaml", "name: bot\n" + GOOD)
    _write(tmp_path, "b.yaml", "name: bot\n" + GOOD)
    with pytest.raises(SourceConfigError, match="second source named 'bot'"):
        load_sources(tmp_path, environ={"BOT_SECRET": SECRET})

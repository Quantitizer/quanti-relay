from relay.execution.error_messages import humanize_order_error as h


def test_codes_match_only_as_whole_numbers():
    assert h("price 0.4290 out of range") == "price 0.4290 out of range"
    assert h("HTTP error: 429").startswith("Server overloaded")


def test_the_venues_own_text_is_always_kept():
    raw = "Bybit API error 110007: ab not enough for new order"
    assert h(raw).endswith(f"(venue: {raw})")


def test_vague_words_do_not_trigger_unrelated_messages():
    assert h("order blocked by the user") == "order blocked by the user"
    assert h("Too many stop orders 110009").startswith("Too many stop orders")


def test_unknown_errors_pass_through_unchanged():
    assert h("something new") == "something new" and h("") == "Unknown error"

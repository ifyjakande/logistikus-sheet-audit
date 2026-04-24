"""Tests for the parse_date / parse_amount helpers in audit_sheet.

These run in CI before the audit touches the sheet. If any assertion fails
the audit job is skipped, so the live sheet is only ever mutated by a
parser whose accuracy was just re-verified on this run.
"""

from audit_sheet import parse_amount, parse_date


def test_date_typo_ap_becomes_apr():
    assert parse_date("15-Ap-2026") == "15-Apr-2026"


def test_date_typo_long_prefix_apri_becomes_apr():
    assert parse_date("15-Apri-2026") == "15-Apr-2026"


def test_date_non_prefix_typo_is_flagged_not_guessed():
    # 'Arp' is not a valid prefix of any month, so we must not guess it.
    assert parse_date("15-Arp-2026") is None


def test_date_full_month_name():
    assert parse_date("15-April-2026") == "15-Apr-2026"


def test_date_canonical_roundtrip():
    assert parse_date("15-Apr-2026") == "15-Apr-2026"


def test_date_with_ordinal_suffix():
    assert parse_date("15th Apr 2026") == "15-Apr-2026"
    assert parse_date("1st January 2026") == "1-Jan-2026"


def test_date_mixed_case():
    assert parse_date("15-apr-2026") == "15-Apr-2026"
    assert parse_date("15-APR-2026") == "15-Apr-2026"


def test_date_slash_separator_dayfirst():
    assert parse_date("15/04/2026") == "15-Apr-2026"


def test_date_dot_separator():
    assert parse_date("15.04.2026") == "15-Apr-2026"


def test_date_iso_format():
    assert parse_date("2026-04-15") == "15-Apr-2026"


def test_date_us_style_parses_by_dayfirst_fallback():
    # When day can't be first (e.g. 04/15), dateutil falls back sensibly.
    assert parse_date("04/15/2026") == "15-Apr-2026"


def test_date_word_month_first_with_comma():
    assert parse_date("Apr 15 2026") == "15-Apr-2026"


def test_date_short_year_is_expanded():
    assert parse_date("15-Apr-26") == "15-Apr-2026"


def test_date_extra_whitespace_trimmed():
    assert parse_date("  15-Apr-2026  ") == "15-Apr-2026"


def test_date_ambiguous_ma_rejected():
    # 'Ma' could be Mar or May — must not guess.
    assert parse_date("15-Ma-2026") is None


def test_date_ambiguous_ju_rejected():
    # 'Ju' could be Jun or Jul — must not guess.
    assert parse_date("15-Ju-2026") is None


def test_date_invalid_day_rejected():
    assert parse_date("32-Apr-2026") is None
    assert parse_date("31-Feb-2026") is None


def test_date_insufficient_info_rejected():
    # Year alone must not become "today in 2026".
    assert parse_date("2026") is None
    # Month + year only (missing day) must be rejected.
    assert parse_date("Apr 2026") is None
    # Day + month only (missing year) must be rejected.
    assert parse_date("15 Apr") is None


def test_date_pure_text_rejected():
    assert parse_date("pending") is None
    assert parse_date("N/A") is None
    assert parse_date("") is None


def test_date_year_out_of_range_rejected():
    assert parse_date("15-Apr-1800") is None
    assert parse_date("15-Apr-2200") is None


def test_date_non_string_input_rejected():
    assert parse_date(None) is None  # type: ignore[arg-type]
    assert parse_date(46107) is None  # type: ignore[arg-type]


def test_amount_plain_integer():
    assert parse_amount("5000") == 5000


def test_amount_with_naira_symbol_and_commas():
    assert parse_amount("₦5,000") == 5000
    assert parse_amount("₦ 5,000.00") == 5000
    assert parse_amount("₦1,500.50") == 1500.5


def test_amount_with_generic_currency_symbols():
    assert parse_amount("$1,234") == 1234
    assert parse_amount("€ 100.25") == 100.25


def test_amount_preserves_int_when_integer_valued():
    assert isinstance(parse_amount("1000.00"), int)
    assert parse_amount("1000.00") == 1000


def test_amount_preserves_float_when_fractional():
    assert isinstance(parse_amount("1500.50"), float)
    assert parse_amount("1500.50") == 1500.5


def test_amount_whitespace_cell_clears():
    assert parse_amount(" ") == ""
    assert parse_amount("   ") == ""
    assert parse_amount("") == ""


def test_amount_non_numeric_text_rejected():
    assert parse_amount("N/A") is None
    assert parse_amount("pending") is None
    assert parse_amount("5k") is None


def test_amount_scientific_notation_rejected():
    # '5e3' would float() to 5000.0, but it's more likely a typo.
    assert parse_amount("5e3") is None


def test_amount_leading_currency_with_trailing_garbage_rejected():
    assert parse_amount("₦5,000abc") is None


def test_amount_non_string_rejected():
    assert parse_amount(1234) is None  # type: ignore[arg-type]
    assert parse_amount(None) is None  # type: ignore[arg-type]

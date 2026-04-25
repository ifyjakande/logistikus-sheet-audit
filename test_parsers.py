"""Tests for the parse_date / parse_amount helpers in audit_sheet.

These run in CI before the audit touches the sheet. If any assertion fails
the audit job is skipped, so the live sheet is only ever mutated by a
parser whose accuracy was just re-verified on this run.
"""

import pytest

from audit_sheet import (
    MONTHS_LONG,
    MONTHS_SHORT,
    analyze_date,
    infer_month,
    parse_amount,
    parse_date,
    serial_to_month,
)


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


def test_date_missing_separator_between_month_and_year():
    # '24-Ap2026' — the month-to-year boundary has no separator.
    assert parse_date("24-Ap2026") == "24-Apr-2026"
    assert parse_date("15-Apr2026") == "15-Apr-2026"
    assert parse_date("15-April2026") == "15-Apr-2026"


def test_date_missing_separator_between_day_and_month():
    assert parse_date("24Ap-2026") == "24-Apr-2026"
    assert parse_date("15Apr-2026") == "15-Apr-2026"
    assert parse_date("15April-2026") == "15-Apr-2026"


def test_date_no_separators_at_all():
    assert parse_date("24Ap2026") == "24-Apr-2026"
    assert parse_date("15Apr2026") == "15-Apr-2026"
    assert parse_date("15April2026") == "15-Apr-2026"


def test_date_missing_separator_short_year_expanded():
    assert parse_date("24Ap26") == "24-Apr-2026"
    assert parse_date("15Apr26") == "15-Apr-2026"


def test_date_missing_separator_invalid_day_rejected():
    assert parse_date("32Ap2026") is None
    assert parse_date("31Feb2026") is None


def test_date_missing_separator_unknown_month_rejected():
    # 'Xy' is not a prefix of any month — must not guess.
    assert parse_date("15Xy2026") is None
    # 'Arp' is not a valid prefix even when separators are stripped.
    assert parse_date("15Arp2026") is None


def test_date_missing_separator_ambiguous_prefix_still_flagged():
    # '15Ma2026' is still ambiguous (Mar/May) — no separators must not relax that.
    canonical, cands, day, year = analyze_date("15Ma2026")
    assert canonical is None
    assert set(cands) == {"Mar", "May"}
    assert (day, year) == (15, 2026)


def test_date_missing_separator_pure_digits_rejected():
    # No alpha block at all — must not be coerced into a date.
    assert parse_date("242026") is None
    assert parse_date("15042026") is None


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


# -- analyze_date --------------------------------------------------------


def test_analyze_date_returns_candidates_for_ambiguous_ma():
    canonical, cands, day, year = analyze_date("15-Ma-2026")
    assert canonical is None
    assert set(cands) == {"Mar", "May"}
    assert (day, year) == (15, 2026)


def test_analyze_date_returns_candidates_for_ambiguous_ju():
    canonical, cands, day, year = analyze_date("15-Ju-2026")
    assert canonical is None
    assert set(cands) == {"Jun", "Jul"}
    assert (day, year) == (15, 2026)


def test_analyze_date_day_31_disambiguates_ju_to_jul():
    # Only Jul has 31 days among {Jun, Jul}, so no ambiguity remains.
    canonical, cands, day, year = analyze_date("31-Ju-2026")
    assert canonical == "31-Jul-2026"
    assert cands == []
    assert (day, year) == (31, 2026)


def test_analyze_date_unambiguous_returns_canonical_with_no_candidates():
    canonical, cands, day, year = analyze_date("15-Ap-2026")
    assert canonical == "15-Apr-2026"
    assert cands == []
    assert (day, year) == (15, 2026)


def test_analyze_date_unparseable_returns_empty():
    canonical, cands, day, year = analyze_date("pending")
    assert (canonical, cands, day, year) == (None, [], None, None)


def test_analyze_date_invalid_day_returns_empty():
    # Day 32 is invalid for every month; no candidates survive.
    canonical, cands, day, year = analyze_date("32-Ma-2026")
    assert (canonical, cands, day, year) == (None, [], None, None)


# -- infer_month --------------------------------------------------------


def test_infer_month_mar_support_on_both_sides():
    # 3 Mar before + 3 Mar after = clear, infers Mar.
    assert infer_month(["Mar", "May"], [3, 3, 3], [3, 3, 3]) == "Mar"


def test_infer_month_may_support_on_both_sides():
    assert infer_month(["Mar", "May"], [5, 5], [5, 5, 5]) == "May"


def test_infer_month_only_before_support_flags():
    # The 'first row of a new month typo'd' trap: all support is above,
    # none below. Must flag, not infer.
    assert infer_month(["Mar", "May"], [3, 3, 3, 3, 3], []) is None


def test_infer_month_only_after_support_flags():
    # Symmetrical: support only from below also flags.
    assert infer_month(["Mar", "May"], [], [5, 5, 5, 5, 5]) is None


def test_infer_month_insufficient_combined_support_flags():
    # 1 before + 1 after = 2, below threshold 3 even though both sides exist.
    assert infer_month(["Mar", "May"], [3], [3]) is None


def test_infer_month_mixed_support_flags():
    # Neighbours span both candidates — refuse to pick.
    assert infer_month(["Mar", "May"], [3, 3], [5]) is None


def test_infer_month_non_candidate_neighbours_ignored():
    # April (month 4) isn't a candidate here, so it's irrelevant noise.
    assert infer_month(["Mar", "May"], [3, 3, 4], [3, 4, 4]) == "Mar"


def test_infer_month_no_candidate_neighbours_flags():
    # All neighbours are in April; neither candidate has any support.
    assert infer_month(["Mar", "May"], [4, 4], [4, 4]) is None


def test_infer_month_empty_candidates_returns_none():
    assert infer_month([], [3, 3, 3], [3, 3, 3]) is None


def test_infer_month_both_sides_empty_returns_none():
    assert infer_month(["Mar", "May"], [], []) is None


def test_infer_month_month_boundary_mar_to_apr_typo_in_mar_side():
    # Ambiguous cell sits near the end of March data; April rows follow.
    # Before: 5 March neighbours. After: 5 April neighbours (non-candidate).
    # Only one side has candidate support -> flag (do not guess).
    assert infer_month(["Mar", "May"], [3, 3, 3, 3, 3], [4, 4, 4, 4, 4]) is None


def test_infer_month_month_boundary_with_later_data_flags_if_sides_disagree():
    # After a month boundary, user typed the first new-month row as "Ju"
    # and later filled in more July rows. Before = all Jun, After = all Jul.
    # Two candidates each have one-sided support -> flag.
    assert infer_month(["Jun", "Jul"], [6, 6, 6], [7, 7, 7]) is None


# -- serial_to_month ----------------------------------------------------


def test_serial_to_month_known_dates():
    # 1899-12-30 is day 0; 2026-04-15 is a valid April day.
    from datetime import datetime
    apr_15_2026 = (datetime(2026, 4, 15) - datetime(1899, 12, 30)).days
    assert serial_to_month(apr_15_2026) == 4
    mar_1_2026 = (datetime(2026, 3, 1) - datetime(1899, 12, 30)).days
    assert serial_to_month(mar_1_2026) == 3


# -- Exhaustive per-month coverage --------------------------------------
#
# These parametrized tests exercise every month of the year against the
# parser and the inference rule, so no month can silently regress.


@pytest.mark.parametrize("short", MONTHS_SHORT)
def test_every_short_month_name_parses_canonically(short):
    assert parse_date(f"15-{short}-2026") == f"15-{short}-2026"


@pytest.mark.parametrize("short,long_name", list(zip(MONTHS_SHORT, MONTHS_LONG)))
def test_every_long_month_name_normalises_to_short(short, long_name):
    assert parse_date(f"15-{long_name}-2026") == f"15-{short}-2026"


@pytest.mark.parametrize("short", MONTHS_SHORT)
def test_every_month_mixed_case(short):
    assert parse_date(f"15-{short.upper()}-2026") == f"15-{short}-2026"
    assert parse_date(f"15-{short.lower()}-2026") == f"15-{short}-2026"


# Unique 2-letter prefixes: the 8 months without a same-letter sibling.
UNIQUE_TWO_LETTER = [
    ("Ja", "Jan"), ("Fe", "Feb"), ("Ap", "Apr"), ("Au", "Aug"),
    ("Se", "Sep"), ("Oc", "Oct"), ("No", "Nov"), ("De", "Dec"),
]


@pytest.mark.parametrize("prefix,expected", UNIQUE_TWO_LETTER)
def test_every_unique_two_letter_prefix_resolves(prefix, expected):
    # These 8 prefixes match exactly one month, so they auto-correct.
    assert parse_date(f"15-{prefix}-2026") == f"15-{expected}-2026"


@pytest.mark.parametrize("prefix,expected_candidates", [
    ("Ma", {"Mar", "May"}),
    ("Ju", {"Jun", "Jul"}),
    ("J",  {"Jan", "Jun", "Jul"}),
    ("M",  {"Mar", "May"}),
    ("A",  {"Apr", "Aug"}),
])
def test_ambiguous_prefixes_return_all_candidates(prefix, expected_candidates):
    canonical, cands, day, year = analyze_date(f"15-{prefix}-2026")
    assert canonical is None
    assert set(cands) == expected_candidates
    assert (day, year) == (15, 2026)


@pytest.mark.parametrize("day_in_31,expected", [
    (31, "Jul"),  # only Jul has 31 days among {Jun, Jul}
])
def test_day_31_resolves_ju_to_jul(day_in_31, expected):
    assert parse_date(f"{day_in_31}-Ju-2026") == f"{day_in_31}-{expected}-2026"


@pytest.mark.parametrize("short", MONTHS_SHORT)
def test_every_month_inferred_from_symmetric_neighbours(short):
    """For each month, confirm neighbour inference fires when that month's
    candidate group has symmetric same-month support on both sides."""
    month_num = MONTHS_SHORT.index(short) + 1
    # Build plausible candidate groups that include this month as ambiguous.
    # We fabricate a 2-element candidate list by pairing with a different
    # month to exercise the inference path.
    other_num = 1 if month_num != 1 else 2
    other = MONTHS_SHORT[other_num - 1]
    candidates = [short, other]
    # Symmetric strong support for `short`: should pick it.
    assert infer_month(candidates, [month_num, month_num],
                       [month_num, month_num]) == short
    # Symmetric support for the other candidate: should pick the other.
    assert infer_month(candidates, [other_num, other_num],
                       [other_num, other_num]) == other


@pytest.mark.parametrize("short", MONTHS_SHORT)
def test_every_month_rejects_one_sided_support(short):
    """For each month, confirm the boundary-trap rule rejects one-sided support."""
    month_num = MONTHS_SHORT.index(short) + 1
    other_num = 1 if month_num != 1 else 2
    other = MONTHS_SHORT[other_num - 1]
    candidates = [short, other]
    # All support on the 'before' side only (e.g. last row of old month).
    assert infer_month(candidates, [month_num]*5, []) is None
    # All support on the 'after' side only (e.g. first row of new month).
    assert infer_month(candidates, [], [month_num]*5) is None

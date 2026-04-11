"""
Unit tests for coupon_parser.py

Run: python test_coupon_parser.py
"""

import sys
from coupon_parser import parse_coupon_from_bbg_name, coupon_precision_gain

CASES = [
    # (bbg_name, expected_coupon)
    ("ARAMCO 4 3/8 04/16/39",           4.375),
    ("PEMEX 7 5/8 07/01/35",            7.625),
    ("TURKEY 5 1/4 03/13/30",           5.25),
    ("COLOMBIA 3 7/8 02/15/61",         3.875),
    ("T 2 7/8 05/15/32",                2.875),       # US Treasury
    ("T 4 1/2 11/15/33",                4.5),
    ("COLOMBIA 5 02/15/29",             5.0),          # integer coupon
    ("EIB 3 3/10 02/03/28",             3.3),          # thirds
    ("IBRD 1 1/10 11/18/30",            1.1),
    ("GACI 5 1/4 10/17/34",             5.25),
    ("SAUDI 5 1/4 01/16/50",            5.25),
    ("BGK 3 3/4 10/07/31",              3.75),
    ("KOREA 1 1/4 01/21/26",            1.25),
    ("PEMEX 6 7/8 08/04/26",            6.875),
    ("PERTM 3 1/2 01/01/28",            3.5),
    # Edge cases
    ("EIB MTN RegS 3 3/8 02/07/28",     3.375),        # suffix before coupon token
    ("SAUDI ARABIAN OIL CO 4 3/8 04/16/39", 4.375),    # multi-word issuer
    (None,                              None),
    ("",                                None),
    ("T",                               None),          # no coupon at all
]

PRECISION_CASES = [
    # (stored, parsed, expect_upgrade)
    (4.38,  4.375,  True),
    (4.375, 4.375,  False),   # same — no upgrade
    (5.0,   5.0,    False),
    (7.63,  7.625,  True),
    (None,  4.375,  False),   # can't upgrade without stored value
    (4.38,  None,   False),   # can't upgrade without parsed value
]


def run():
    passed = failed = 0

    print("── coupon parsing ─────────────────────────────────────────────")
    for name, want in CASES:
        got = parse_coupon_from_bbg_name(name)
        ok = (got == want) if want is not None else (got is None)
        status = "✓" if ok else "✗"
        if not ok:
            failed += 1
            print(f"  {status} {name!r:50s}  want={want}  got={got}")
        else:
            passed += 1
            print(f"  {status} {name!r:50s}  → {got}")

    print()
    print("── precision gain ─────────────────────────────────────────────")
    for stored, parsed, expect in PRECISION_CASES:
        got = coupon_precision_gain(stored, parsed)
        ok = got == expect
        status = "✓" if ok else "✗"
        if not ok:
            failed += 1
        print(f"  {status} stored={stored}  parsed={parsed}  expect={expect}  got={got}")

    print()
    print(f"{'PASS' if failed == 0 else 'FAIL'}  {passed}/{passed+failed} tests passed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    run()

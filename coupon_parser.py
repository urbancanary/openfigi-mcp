"""
Parse fractional coupons from Bloomberg security names.

Bloomberg stores coupons in its name field as fractions:
    "ARAMCO 4 3/8 04/16/49"   →  4.375
    "PEMEX 7 5/8 07/01/35"    →  7.625
    "TURKEY 5 1/4 03/13/30"   →  5.25
    "COLOMBIA 3 7/8 02/15/61" →  3.875
    "T 2 7/8 05/15/32"        →  2.875 (US Treasury)

This matters because bond analytics (QuantLib accrued interest, duration)
require exact coupon precision.  Storing "4.38" instead of "4.375" causes
real accrued interest errors — a full reconciliation cycle to diagnose.

The Bloomberg name format is:
    TICKER [WHOLE] NUMERATOR/DENOMINATOR DATE [SUFFIX]
or:
    TICKER INTEGER DATE [SUFFIX]   (for whole-number coupons like 5%)

The WHOLE + FRACTION part is called the "broken fraction" or "mixed number".
Common denominators in bond markets: 2, 4, 8, 16, 32 (Treasury-fractions).
"""

import re
from typing import Optional

# Matches: optional whole-number + fraction  e.g. "4 3/8", "3/4", "7 5/8", "10"
# followed by a date token (MM/DD/YY or MM/DD/YYYY or YYYY-MM-DD)
# or end-of-coupon (space or end of string) to avoid false positives on date slashes.
#
# Group layout:
#   (1) whole part (may be empty for pure fractions like "3/4")
#   (2) numerator
#   (3) denominator
#   OR
#   (4) integer-only coupon (e.g. "5" in "COLOMBIA 5 01/25/30")
_FRAC_RE = re.compile(
    r"""
    (?:^|\s)                # word boundary
    (?:
        (\d+)\s+(\d+)/(\d+) # whole + fraction: "4 3/8"
        |
        (\d+)/(\d+)         # pure fraction: "3/4" (uncommon but valid)
        |
        (\d+)               # integer coupon: "5"
    )
    (?=\s|$)                # must be followed by space or end (not a date slash)
    """,
    re.VERBOSE,
)


def parse_coupon_from_bbg_name(name: Optional[str]) -> Optional[float]:
    """
    Extract coupon from a Bloomberg security name string.

    Returns the coupon as a float (e.g. 4.375), or None if not found / ambiguous.

    Design rules:
    - Returns None rather than guessing when the pattern is ambiguous
    - Never returns 0.0 (would indicate an error, not a zero-coupon bond)
    - Handles "MTN RegS" / "144A" / other suffixes safely
    - The first fraction/integer after the issuer token wins
    """
    if not name:
        return None

    name = name.strip()

    # Skip the first token (ticker/issuer). Bloomberg names start with the
    # issuer short name, then the coupon, then the maturity date.
    parts = name.split()
    if len(parts) < 2:
        return None

    # The coupon appears at positions 1 or 2 (after ticker, optionally after
    # a country-code prefix). We try from position 1 to avoid the ticker.
    # Re-join from position 1 and run the regex.
    remainder = " ".join(parts[1:])

    for m in _FRAC_RE.finditer(remainder):
        whole_str, num_str, den_str, pf_num, pf_den, int_str = (
            m.group(1), m.group(2), m.group(3),
            m.group(4), m.group(5),
            m.group(6),
        )

        if whole_str is not None:
            # whole + fraction: "4 3/8"
            whole = int(whole_str)
            num   = int(num_str)
            den   = int(den_str)
            if den == 0:
                continue
            coupon = whole + num / den
        elif pf_num is not None:
            # pure fraction: "3/4"
            num = int(pf_num)
            den = int(pf_den)
            if den == 0:
                continue
            coupon = num / den
        elif int_str is not None:
            coupon = float(int_str)
        else:
            continue

        # Sanity: bonds have coupons between 0 (excl) and 20%
        if coupon <= 0 or coupon > 20:
            continue

        # Round to 10 decimal places to avoid floating-point noise
        return round(coupon, 10)

    return None


def coupon_precision_gain(stored: Optional[float], parsed: Optional[float]) -> bool:
    """
    Return True if the parsed coupon is materially more precise than what
    is stored (i.e. worth updating bond_reference).

    Threshold: difference > 0.001 (1/1000th of a percent = 0.01 bps).
    This catches the common "4.38 stored, 4.375 correct" error while
    ignoring genuine floating-point noise.
    """
    if parsed is None or stored is None:
        return False
    return abs(parsed - stored) > 0.001

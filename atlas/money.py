"""Currencies and storefront regions for price observations (CONTRACTS-v1 C8.1).

Money is an integer number of *minor units* with the exponent ISO 4217 gives the currency: `USD` has 2
(999 = 9.99), `JPY` 0 (1980 = 1980), `KWD` 3 (1500 = 1.500). Atlas never stores, accepts or returns a float
amount; `decimal()` formats one for display as a string.

The tables are the active ISO 4217 alphabetic codes with their minor-unit exponents, and the officially
assigned ISO 3166-1 alpha-2 country codes, both as published at the end of 2025. Codes whose minor unit is
"N.A." in ISO 4217 (precious metals, `XDR`, the testing codes `XTS`/`XXX`) are not currencies a store charges
in and are refused. Withdrawn codes (`HRK`, `ANG`, `ZWL`, `SLL`, `CUC`) are refused; a node only keeps price
history for at most 365 days (C8.2), so no stored observation can need them. Updating a table is a code change
reviewed like any other: adding a code makes more input valid and changes nothing stored.
"""

from __future__ import annotations

from decimal import Decimal

#: Minor-unit exponent of every currency whose exponent is not 2.
_EXPONENT_NOT_2: dict[str, int] = {
    # 0: no minor unit
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0, "RWF": 0,
    "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    # 3: thousandths
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    # 4: units of account
    "CLF": 4, "UYW": 4,
}
_EXPONENT_2 = (
    "AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BGN BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE "
    "CHF CHW CNY COP COU CRC CUP CVE CZK DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GTQ GYD HKD "
    "HNL HTG HUF IDR ILS INR IRR JMD KES KGS KHR KPW KYD KZT LAK LBP LKR LRD LSL MAD MDL MGA MKD MMK MNT MOP "
    "MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD PAB PEN PGK PHP PKR PLN QAR RON RSD RUB SAR SBD "
    "SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TOP TRY TTD TWD TZS UAH USD USN UYU UZS "
    "VED VES WST XCD XCG YER ZAR ZMW ZWG"
).split()
#: ISO 4217 alphabetic code -> minor-unit exponent.
CURRENCY_EXPONENT: dict[str, int] = {**{code: 2 for code in _EXPONENT_2}, **_EXPONENT_NOT_2}

#: ISO 3166-1 alpha-2, lower case (C8.1). There is no `ww`: a price is always regional.
REGIONS: frozenset[str] = frozenset((
    "ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bl bm bn bo bq br bs bt bv bw "
    "by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do dz ec ee eg eh er es et fi "
    "fj fk fm fo fr ga gb gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il im in "
    "io iq ir is it je jm jo jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me "
    "mf mg mh mk ml mm mn mo mp mq mr ms mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf "
    "pg ph pk pl pm pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd se sg sh si sj sk sl sm sn so sr ss st sv "
    "sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw tz ua ug um us uy uz va vc ve vg vi vn vu wf ws ye "
    "yt za zm zw"
).split())

#: Largest amount accepted in minor units: below 2**53, so every JSON client reads it exactly.
MAX_MINOR = 10**15


def exponent(currency: str) -> int:
    """The ISO 4217 minor-unit exponent. KeyError for a code Atlas does not accept."""
    return CURRENCY_EXPONENT[currency]


def valid_currency(value: object) -> bool:
    return isinstance(value, str) and value in CURRENCY_EXPONENT


def valid_region(value: object) -> bool:
    return isinstance(value, str) and value in REGIONS


def decimal(amount_minor: int | None, currency: str) -> str | None:
    """`999, "USD"` -> "9.99"; `1980, "JPY"` -> "1980"; `1500, "KWD"` -> "1.500". Exact (no float)."""
    if amount_minor is None:
        return None
    places = exponent(currency)
    value = Decimal(amount_minor).scaleb(-places)
    return f"{value:.{places}f}"


def discount_pct(amount_minor: int, base_amount_minor: int) -> int:
    """The discount a current price is of its list price, in whole percent, rounded half up (Steam shows 33%
    for 1999 -> 1339). Integer arithmetic only."""
    if base_amount_minor <= 0:
        return 0
    return ((base_amount_minor - amount_minor) * 200 + base_amount_minor) // (2 * base_amount_minor)

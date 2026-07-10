#!/usr/bin/env python3
"""
6C TOC (Toll Operators Coalition) AVI Standard Tag Decoder
Based on: 6C TOC AVI Standard v3.1, Revision 1 (May 11, 2017)

This decoder handles ISO 18000-6C/63 toll transponders using the
official 6C TOC memory mapping specification.

DSFID: 0x3E (Data Storage Format Identifier assigned by ISO to 6C TOC)
AFI: 0xB0 (Application Family Identifier for tolling)
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple


# 6C TOC Agency Codes. The NTTA assignments include all three codes in the
# April 10, 2025 6C Coalition Agency Code Master (41, 53, and 69).
# Format: agency_code -> (acronym, name, state/region)
AGENCY_CODES = {
    0: ("Reserved", "Reserved", "N/A"),
    17: ("SC", "Southern Connector", "SC"),
    33: ("NCTA", "North Carolina Toll Authority", "NC"),
    35: ("FTE", "Florida's Turnpike Enterprise", "FL"),
    41: ("NTTA", "North Texas Tollway Authority", "TX"),
    53: ("NTTA", "North Texas Tollway Authority", "TX"),
    55: ("OTA", "Oklahoma Turnpike Authority", "OK"),
    61: ("HDBC", "Halifax Dartmouth Bridge Commission", "NS"),
    64: ("FTE", "Florida's Turnpike Enterprise", "FL"),
    65: ("FTE", "Florida's Turnpike Enterprise", "FL"),
    70: ("AR", "American Roads", "US"),
    71: ("DWT", "Detroit Windsor Tunnel", "MI"),
    77: ("WSDOT", "Washington State Department of Transportation", "WA"),
    78: ("WSDOT", "Washington State Department of Transportation (Legacy)", "WA"),
    79: ("PlusPass", "PlusPass", "US"),
    99: ("CFX", "Central Florida Expressway", "FL"),
    101: ("BATA", "Bay Area Toll Authority", "CA"),
    102: ("Cobequid", "Cobequid Pass - Western Alignment Corporation", "NS"),
    103: ("TCA", "Transportation Corridor Agencies", "CA"),
    104: ("Caltrans", "California Department of Transportation", "CA"),
    105: ("GGBHTD", "Golden Gate Bridge, Highway and Tunnel District", "CA"),
    106: ("LACMTA", "Los Angeles County Metropolitan Transportation Authority", "CA"),
    107: ("SR91", "SR 91 Express Lanes", "CA"),
    108: ("RCTC", "Riverside County Transportation Commission", "CA"),
    109: ("SANDAG", "San Diego Association of Governments", "CA"),
    110: ("VTA", "Santa Clara Valley Transportation Authority", "CA"),
    111: ("SBX", "South Bay Expressway, LLC", "CA"),
    112: ("Sunol JPA", "Sunol SMART Carpool Lanes Joint Powers Authority", "CA"),
    113: ("SFCTA", "San Francisco County Transportation Authority", "CA"),
    114: ("SANBAG", "San Bernardino Associated Governments", "CA"),
    115: ("A25", "Concession A25 sec", "QC"),
    116: ("POHR", "Port of Hood River", "OR"),
    117: ("Neology", "Neology", "US"),  # Toll transponder vendor
    118: ("MHAB", "McAllen-Hidalgo & Anzalduas Bridges", "TX"),
    119: ("PRIB", "Pharr-Reynosa International Bridge", "TX"),
    120: ("TransCore", "TransCore", "US"),  # Toll transponder vendor
    121: ("CCRMA", "Cameron County Regional Mobility Authority", "TX"),
    122: ("Starr", "Starr County International Bridge", "TX"),
    # 123: CONFLICT WITH SEGO
    124: ("MBA", "Mackinac Bridge Authority", "MI"),
    125: ("BestPass", "BestPass", "US"),  # Fleet management
    126: ("Kapsch", "Kapsch", "US"),  # Toll transponder vendor
    # 127: HOLD for CA Transition
    140: ("LAWA", "Los Angeles World Airports", "CA"),
    194: ("E-470", "E-470 Public Highway Authority", "CO"),
    321: ("SRTA", "State Road & Tollway Authority", "GA"),
    448: ("PRHTA", "Puerto Rico Highway and Transportation Authority", "PR"),
    449: ("LSIORB", "Ohio River Bridges (Louisville)", "KY"),
    450: ("LADOTD", "Louisiana Department of Transportation and Development", "LA"),
    451: ("GNOEC", "Greater New Orleans Expressway Commission", "LA"),
    69: ("NTTA", "North Texas Tollway Authority", "TX"),
    1409: ("UDOT", "Utah Department of Transportation", "UT"),
    2305: ("TI Corp", "Transportation Investment Corporation", "BC"),
    2306: ("Merseyflow", "Merseyflow", "UK"),
    4000: ("ATL", "Hartsfield-Jackson Atlanta International Airport", "GA"),
    # 4080-4095: Reserved for testing
}

# Vehicle Classification from EZPass spec (Appendix C)
VEHICLE_TYPES = {
    0: "Undefined",
    1: "Automobile",
    2: "Motorcycle",
    3: "Pickup Truck",
    4: "Van (seats 1-9)",
    5: "Minibus (seats 10-15)",
    6: "Bus (seats 16+)",
    7: "Recreational Vehicle",
    8: "Truck",
    9: "Auto Transporter (≤65')",
    10: "Auto Transporter (>65')",
    11: "Tractor & Trailer (≤48')",
    12: "Tractor & Trailer (>48')",
    13: "Tractor & Dual Trailers (each ≤28.5')",
    14: "Tractor & Dual Trailers (each >28.5')",
    15: "Tractor & Dual Trailers (mixed)",
    16: "Undefined",
    17: "Tractor/Mobile Home Combination",
}

# HOV Declaration values
HOV_DECLARATIONS = {
    0: "Single Mode (default)",
    1: "SOV (non-carpool)",
    2: "HOV 2+",
    3: "HOV 3+",
    4: "Carpool (as defined by roadway)",
    5: "Reserved",
    6: "Reserved",
    7: "Reserved",
}

# Programming Version
VERSIONS = {
    0: "Unassigned",
    1: "Version 1.0",
    2: "Version 2.0",
    3: "Version 3.0",
}


@dataclass
class Decoded6CTOC:
    """Container for decoded 6C TOC tag data."""
    raw_hex: str
    pc_bits: Optional[str]
    uii: str
    
    # Header info
    dsfid: int
    is_6ctoc: bool
    
    # Decoded fields
    agency_use: int
    classification: Dict[str, Any]
    hov_declaration: int
    hov_description: str
    version: int
    version_description: str
    agency_code: int
    agency_info: Tuple[str, str, str]  # (acronym, name, state)
    transponder_serial_number: int
    uii_validation_hash: int
    
    # Computed values
    check_digit: int
    printed_number: str
    barcode_content: str


@dataclass(frozen=True)
class ClassifiedTag:
    """Normalized reader output ready for lookup and display."""

    lookup_value: str
    display_value: str
    protocol: str
    agency_code: Optional[int] = None


def hex_to_bits(hex_str: str) -> str:
    """Convert hex string to binary string."""
    return bin(int(hex_str, 16))[2:].zfill(len(hex_str) * 4)


def extract_bits(bits: str, start: int, length: int) -> int:
    """Extract bits from a binary string (0-indexed)."""
    return int(bits[start:start + length], 2) if length > 0 else 0


def calculate_luhn_check_digit(digits: str) -> int:
    """
    Calculate Luhn (mod10) check digit.
    Per 6C TOC spec: calculated on last 2 digits of 4-digit agency + all 10 TSN digits
    
    Note: 6C TOC uses a Luhn variant that doubles ODD positions from LEFT (0-indexed),
    not the standard algorithm that doubles from the right.
    """
    total = 0
    for i, digit in enumerate(digits):
        d = int(digit)
        if i % 2 == 1:  # Double odd positions (1, 3, 5, ...) from left
            d *= 2
            if d > 9:
                d -= 9  # Sum digits (same as subtract 9 for single digit doubling)
        total += d
    
    return (10 - (total % 10)) % 10


def clean_hex_input(hex_string: str) -> Tuple[Optional[str], str]:
    """
    Clean and validate hex input.
    Returns (pc_bits, uii) tuple.
    """
    hex_string = hex_string.strip().lstrip('#').upper()
    
    if len(hex_string) == 28:
        # Includes PC (Protocol Control) bits - 4 hex chars
        return hex_string[:4], hex_string[4:]
    elif len(hex_string) == 24:
        # UII only (96 bits)
        return None, hex_string
    else:
        raise ValueError(f"Invalid hex length: {len(hex_string)}. Expected 24 (UII only) or 28 (PC + UII).")


def decode_6ctoc(hex_string: str) -> Decoded6CTOC:
    """
    Decode a 6C TOC toll transponder tag.
    
    Memory Map (96-bit UII):
    - Bits 0-7:   DSFID (0x3E for 6C TOC)
    - Bits 8-20:  Agency Use (13 bits)
    - Bits 21-32: Classification (12 bits)
    - Bits 33-35: HOV Declaration (3 bits)
    - Bits 36-39: Version (4 bits)
    - Bits 40-51: Agency Code (12 bits)
    - Bits 52-79: Transponder Serial Number (28 bits)
    - Bits 80-95: UII Validation Hash (16 bits)
    """
    pc_bits, uii = clean_hex_input(hex_string)
    
    # Convert UII to binary
    uii_bits = hex_to_bits(uii)
    
    if len(uii_bits) != 96:
        raise ValueError(f"UII must be 96 bits, got {len(uii_bits)}")
    
    # Extract DSFID (Data Storage Format Identifier)
    dsfid = extract_bits(uii_bits, 0, 8)
    is_6ctoc = (dsfid == 0x3E)
    
    if not is_6ctoc:
        raise ValueError(f"Not a 6C TOC tag. DSFID is 0x{dsfid:02X}, expected 0x3E")
    
    # Extract Agency Use (13 bits) - for agency-specific info
    agency_use = extract_bits(uii_bits, 8, 13)
    
    # Extract Classification (12 bits)
    class_assigned = extract_bits(uii_bits, 21, 1)
    vehicle_type = extract_bits(uii_bits, 22, 5)
    vehicle_axles = extract_bits(uii_bits, 27, 4)
    vehicle_weight = extract_bits(uii_bits, 31, 1)
    rear_tires = extract_bits(uii_bits, 32, 1)
    
    classification = {
        "assigned": bool(class_assigned),
        "vehicle_type_code": vehicle_type,
        "vehicle_type": VEHICLE_TYPES.get(vehicle_type, f"Unknown ({vehicle_type})"),
        "axles": vehicle_axles if vehicle_axles >= 2 else "Undefined",
        "weight": "> 7,000 lbs" if vehicle_weight else "≤ 7,000 lbs",
        "rear_tires": "Dual" if rear_tires else "Single",
    }
    
    # Extract HOV Declaration (3 bits)
    hov_declaration = extract_bits(uii_bits, 33, 3)
    hov_description = HOV_DECLARATIONS.get(hov_declaration, f"Unknown ({hov_declaration})")
    
    # Extract Version (4 bits)
    version = extract_bits(uii_bits, 36, 4)
    version_description = VERSIONS.get(version, f"Unknown ({version})")
    
    # Extract Agency Code (12 bits)
    agency_code = extract_bits(uii_bits, 40, 12)
    agency_info = AGENCY_CODES.get(agency_code, ("Unknown", f"Agency {agency_code}", "??"))
    
    # Extract Transponder Serial Number (28 bits)
    tsn = extract_bits(uii_bits, 52, 28)
    
    # Extract UII Validation Hash (16 bits)
    uii_hash = extract_bits(uii_bits, 80, 16)
    
    # Calculate check digit using Luhn algorithm
    # Per spec: last 2 digits of 4-digit agency code + all 10 digits of TSN
    agency_4digit = f"{agency_code:04d}"
    tsn_10digit = f"{tsn:010d}"
    luhn_input = agency_4digit[2:4] + tsn_10digit  # Last 2 digits of agency + TSN
    check_digit = calculate_luhn_check_digit(luhn_input)
    
    # Format printed number (per spec section 3.3)
    # Format: <AA>AA  TTTTTTTTTT  L (agency without leading zeros, TSN with leading zeros)
    printed_number = f"{agency_code} {tsn:010d} {check_digit}"
    
    # Barcode content (per spec): AAAATTTTTTTTTTL
    barcode_content = f"{agency_code:04d}{tsn:010d}{check_digit}"
    
    return Decoded6CTOC(
        raw_hex=hex_string.upper(),
        pc_bits=pc_bits,
        uii=uii,
        dsfid=dsfid,
        is_6ctoc=is_6ctoc,
        agency_use=agency_use,
        classification=classification,
        hov_declaration=hov_declaration,
        hov_description=hov_description,
        version=version,
        version_description=version_description,
        agency_code=agency_code,
        agency_info=agency_info,
        transponder_serial_number=tsn,
        uii_validation_hash=uii_hash,
        check_digit=check_digit,
        printed_number=printed_number,
        barcode_content=barcode_content,
    )


# Reverse lookup: agency acronym -> code. Built once at import.
# For acronyms with multiple codes (e.g. FTE), the lowest code wins; encoding
# is only used for matching against a scanned EPC, and the match function
# ignores the agency bits if multiple codes share an acronym -- see below.
_ACRONYM_TO_CODES: Dict[str, list] = {}
for _code, (_acr, _name, _state) in AGENCY_CODES.items():
    _ACRONYM_TO_CODES.setdefault(_acr.upper(), []).append(_code)


def encode_6ctoc(printed: str, version: int = 3, include_pc: bool = False) -> str:
    """
    Encode a printed 6C tag number (e.g. "NTTA 0000480314") into a hex EPC.

    Only the bits derivable from the printed form are set:
      - DSFID (0x3E)
      - Version (default 3.0 = 3)
      - Agency Code (looked up by acronym)
      - Transponder Serial Number

    Agency Use, Classification, HOV Declaration, and the UII Validation
    Hash are zeroed -- they are not part of the printed form and the hash
    cannot be computed without the agency's 32-byte key and the chip TID.

    Use `match_6ctoc()` to compare the result against a scanned EPC; it
    masks out the bits the printed form does not encode.

    If `include_pc=True`, the standard 6C-TOC PC word "31B0" is prepended
    (yielding 28 hex chars total).
    """
    parts = printed.strip().upper().split()
    if len(parts) < 2:
        raise ValueError(f"Expected 'AGENCY TSN', got: {printed!r}")

    acronym, tsn_str = parts[0], parts[1]

    if acronym.isdigit():                  # accept numeric agency code directly
        agency_code = int(acronym)
    elif acronym in _ACRONYM_TO_CODES:
        agency_code = _ACRONYM_TO_CODES[acronym][0]
    else:
        raise ValueError(f"Unknown agency acronym: {acronym!r}")

    try:
        tsn = int(tsn_str)
    except ValueError:
        raise ValueError(f"TSN must be numeric, got: {tsn_str!r}")

    if not 0 <= tsn < (1 << 28):
        raise ValueError(f"TSN {tsn} out of 28-bit range (max {(1 << 28) - 1})")
    if not 0 <= version < (1 << 4):
        raise ValueError(f"Version {version} out of 4-bit range (0-15)")

    uii = 0
    uii |= 0x3E << 88                          # DSFID @ bits 0-7
    uii |= (version & 0xF) << 56               # Version @ bits 36-39
    uii |= (agency_code & 0xFFF) << 44         # Agency Code @ bits 40-51
    uii |= (tsn & 0x0FFFFFFF) << 16            # TSN @ bits 52-79
    # Bits 80-95 (UII Validation Hash) remain 0 -- not computable without key+TID.

    hex_str = f"{uii:024X}"
    return f"31B0{hex_str}" if include_pc else hex_str


def match_6ctoc(printed: str, scanned_hex: str) -> bool:
    """
    Return True if `printed` (e.g. "NTTA 0000480314") matches the scanned
    EPC `scanned_hex` (24 or 28 hex chars, optional leading '#').

    Compares only the bits the printed form encodes: DSFID, Agency Code,
    and Transponder Serial Number. All other bits (issuer-specific or
    derived) are ignored.
    """
    try:
        decoded = decode_6ctoc(scanned_hex)
        parts = printed.strip().upper().split()
        if len(parts) < 2:
            return False

        agency, serial = parts[0], int(parts[1])
        if agency.isdigit():
            expected_codes = [int(agency)]
        else:
            expected_codes = _ACRONYM_TO_CODES.get(agency, [])
    except (ValueError, KeyError):
        return False

    return (
        decoded.agency_code in expected_codes
        and decoded.transponder_serial_number == serial
    )


def classify_tag_output(body: str, legacy_serial_max: int = 10) -> Optional[ClassifiedTag]:
    """
    Classify normalized serial-reader output without losing legacy tag support.

    Dotted legacy identifiers are accepted when the serial portion is at most
    ``legacy_serial_max`` characters. Plain identifiers up to that length are
    also treated as legacy TransCore output. Longer plain values must decode as
    6C TOC and resolve to a known, non-reserved agency; everything else is
    discarded before access lookup or logging.
    """
    value = body.strip().upper()
    if not value:
        return None

    if "." in value:
        prefix, separator, serial = value.partition(".")
        if (
            separator
            and prefix
            and serial
            and "." not in serial
            and prefix.isalnum()
            and serial.isalnum()
            and len(serial) <= legacy_serial_max
        ):
            return ClassifiedTag(value, value, "legacy")
        return None

    if len(value) <= legacy_serial_max:
        if value.isalnum():
            return ClassifiedTag(value, value, "legacy")
        return None

    try:
        decoded = decode_6ctoc(value)
    except ValueError:
        return None

    if decoded.agency_code not in AGENCY_CODES:
        return None

    acronym = decoded.agency_info[0].upper()
    if acronym in {"UNKNOWN", "RESERVED"}:
        return None

    display = f"{acronym} {decoded.transponder_serial_number:010d}"
    return ClassifiedTag(value, display, "6c", decoded.agency_code)


def is_6ctoc_tag(hex_string: str) -> bool:
    """Check if a tag is a 6C TOC toll tag (DSFID = 0x3E)."""
    try:
        _, uii = clean_hex_input(hex_string)
        dsfid = int(uii[:2], 16)
        return dsfid == 0x3E
    except:
        return False


def print_decoded(result: Decoded6CTOC, verbose: bool = True):
    """Pretty print decoded 6C TOC tag."""
    print(f"\n{'='*70}")
    print(f"6C TOC TOLL TAG DECODER")
    print(f"{'='*70}")
    print(f"Input:           {result.raw_hex}")
    print(f"UII (96-bit):    {result.uii}")
    if result.pc_bits:
        print(f"PC Bits:         {result.pc_bits}")
    print(f"{'='*70}")
    
    print(f"\n>>> DECODED TAG NUMBER:")
    print(f"    {result.printed_number}")
    print(f"\n>>> BARCODE CONTENT:")
    print(f"    {result.barcode_content}")
    
    print(f"\n{'='*70}")
    print(f"FIELD BREAKDOWN")
    print(f"{'='*70}")
    
    agency_acronym, agency_name, agency_state = result.agency_info
    print(f"  DSFID:              0x{result.dsfid:02X} (6C TOC Standard)")
    print(f"  Agency Code:        {result.agency_code} (0x{result.agency_code:03X})")
    print(f"  Agency:             {agency_acronym} - {agency_name} ({agency_state})")
    print(f"  Serial Number:      {result.transponder_serial_number}")
    print(f"  Check Digit:        {result.check_digit} (Luhn)")
    print(f"  Version:            {result.version_description}")
    print(f"  HOV Declaration:    {result.hov_description}")
    print(f"  UII Hash:           0x{result.uii_validation_hash:04X}")
    
    if verbose and result.classification["assigned"]:
        print(f"\n  VEHICLE CLASSIFICATION:")
        print(f"    Type:             {result.classification['vehicle_type']}")
        print(f"    Axles:            {result.classification['axles']}")
        print(f"    Weight:           {result.classification['weight']}")
        print(f"    Rear Tires:       {result.classification['rear_tires']}")

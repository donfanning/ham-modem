"""CCIR 476 (ITU-R M.476-5 table 1 and 2): the 7-unit code and its conversion from/to text.

A code is an int whose 7 bits are sent least significant bit first, 1 = B (higher tone), 0 = Y (lower tone); every
valid code has four 1s and three 0s. (Wikipedia and most tables print these as hex in the same value; the ITU table
prints the emitted B/Y string in time order, which is the same number read LSB first.)
"""

from __future__ import annotations

# service information signals (table 2)
CS1, CS2, CS3 = 0x65, 0x6A, 0x59
IDLE_B, IDLE_A, RQ = 0x33, 0x0F, 0x66            # idle signal b / idle signal a / signal repetition
PHASE_1, PHASE_2 = IDLE_A, RQ                    # mode B: phasing signal 1 (RX position), 2 (DX position)
LTRS, FIGS = 0x5A, 0x36
CR, LF, SPACE, BLANK = 0x78, 0x6C, 0x5C, 0x6A    # BLANK: ITA2 'unperforated tape' (shares CS2's code)

# code -> (letter case, figure case), "" for none; control characters as in ITA2
_TABLE = {
    0x17: ("J", "\a"), 0x1B: ("F", "!"), 0x1D: ("C", ":"), 0x1E: ("K", "("),
    0x27: ("W", "2"), 0x2B: ("Y", "6"), 0x2D: ("P", "0"), 0x2E: ("Q", "1"),
    0x35: ("G", "&"), 0x39: ("M", "."), 0x3A: ("X", "/"), 0x3C: ("V", "="),
    0x47: ("A", "-"), 0x4B: ("S", "'"), 0x4D: ("I", "8"), 0x4E: ("U", "7"),
    0x53: ("D", "\x05"), 0x55: ("R", "4"), 0x56: ("E", "3"), 0x59: ("N", ","),
    0x5C: (" ", " "), 0x63: ("Z", "+"), 0x65: ("L", ")"), 0x69: ("H", "#"),
    0x6C: ("\n", "\n"), 0x71: ("O", "9"), 0x72: ("B", "?"), 0x74: ("T", "5"), 0x78: ("\r", "\r"),
}
VALID = frozenset(_TABLE) | {CS1, CS2, CS3, IDLE_B, IDLE_A, RQ, LTRS, FIGS, BLANK}
assert all(bin(c).count("1") == 4 for c in VALID) and len(VALID) == 35

_LETTERS = {ch[0]: c for c, ch in _TABLE.items()}
_FIGURES = {ch[1]: c for c, ch in _TABLE.items() if ch[1]}


def is_valid(code: int) -> bool:
    return code in VALID


def bits(code: int) -> list[int]:
    """The 7 bits in transmission order (LSB first)."""
    return [(code >> i) & 1 for i in range(7)]


def from_bits(b) -> int:
    return sum(int(x) << i for i, x in enumerate(b))


def encode_text(text: str, figures: bool = False) -> tuple[list[int], bool]:
    """Text -> character codes with LTRS/FIGS inserted where the case changes; returns (codes, case at the end).
    Characters outside the alphabet are dropped; lower case is upper-cased."""
    out: list[int] = []
    for ch in text.upper():
        if ch in ("\r", "\n", " "):
            out.append({"\r": CR, "\n": LF, " ": SPACE}[ch])
            continue
        if ch in _LETTERS and ch.isalpha():
            if figures:
                out.append(LTRS)
                figures = False
            out.append(_LETTERS[ch])
        elif ch in _FIGURES:
            if not figures:
                out.append(FIGS)
                figures = True
            out.append(_FIGURES[ch])
    return out, figures


class Decoder:
    """Character codes -> text, tracking the letter/figure case (a mutilated character prints as '_' only when asked)."""

    def __init__(self) -> None:
        self.figures = False

    def char(self, code: int | None) -> str:
        if code is None:
            return "_"
        if code == LTRS:
            self.figures = False
            return ""
        if code == FIGS:
            self.figures = True
            return ""
        entry = _TABLE.get(code)
        if entry is None:
            return ""
        return entry[1 if self.figures else 0]

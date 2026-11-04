"""Detects the cleanup-protection phrase "Radhe Radhe" in a post's text.

A separate channel-cleanup script leaves posts that contain "Radhe Radhe"
alone. The scheduler NEVER adds the phrase (or anything else) to a post: it
only looks at the text the admin wrote and reports the post as protected
(🛡 Protection: ON) when that text already contains the phrase. Detection
and message content are completely separate — nothing in this module
returns or stores modified text; the normalised copy below exists only for
matching and is thrown away.

This module has no dependency on the bot (standard library only), so the
cleanup script can use the very same rules.

What counts (``has_radhe_radhe``)
---------------------------------
* English — "Radhe" twice, in any capitalisation ("RADHE RADHE",
  "rAdHe rAdHe"): Unicode case folding, not ``str.lower``. Styled Unicode
  letters such as "𝐑𝐚𝐝𝐡𝐞 𝐑𝐚𝐝𝐡𝐞" count too (NFKC normalisation).
* Hindi — "राधे राधे".
* Urdu — "رادھے رادھے"; the aspirated ھ is also accepted when typed as ہ or ه.
* Between the two words: spaces (or a line break), or one punctuation mark
  with optional spaces around it — a hyphen or dash, a comma (, or ،), a full
  stop (. or ۔), a Devanagari danda (। or ॥) or an underscore:
  "राधे-राधे", "राधे, राधे", "राधे। राधे", "رادھے، رادھے", "Radhe_Radhe".
* Anywhere in the text — at the start, in the middle of a sentence, at the
  end, on its own line, between emoji or next to punctuation.
* Only as whole words: "Radheshyam", "राधेश्याम", "xRadhe Radhe" or a lone
  "Radhe" do not count. Both words must be in the same script.

Invisible characters that keyboards slip into words (zero-width joiners,
bidi marks, soft hyphens, the Arabic tatweel) and Arabic short-vowel marks
are ignored, so they cannot hide the phrase.
"""

import re
import unicodedata
from typing import Optional

__all__ = ["has_radhe_radhe", "normalize_for_matching"]

# Removed from the matching copy only (never from the post itself).
_IGNORED = dict.fromkeys(
    [
        0x00AD,                     # soft hyphen
        0x200B, 0x200C, 0x200D,     # zero-width space / non-joiner / joiner
        0x200E, 0x200F,             # left-to-right / right-to-left mark
        0x2060, 0xFEFF,             # word joiner, zero-width no-break space
        0x0640,                     # Arabic tatweel (kashida)
        0x0670,                     # Arabic superscript alef
        *range(0x202A, 0x202F),     # bidi embeddings / overrides
        *range(0x2066, 0x206A),     # bidi isolates
        *range(0x064B, 0x0660),     # Arabic short vowels and other harakat
    ],
    None,
)

# Between the two words: whitespace, or ONE punctuation mark (hyphens and
# dashes, Latin/Arabic comma, Latin/Urdu full stop, danda, double danda,
# underscore) with optional whitespace around it.
_SEPARATOR = r"(?:\s*[\-\u2010-\u2015\u2212_,\u060C.\u06D4\u0964\u0965]\s*|\s+)"

_ENGLISH = "radhe"                                   # compared after case folding
_HINDI = "\u0930\u093E\u0927\u0947"                  # राधे
_URDU = "\u0631\u0627\u062F[\u06BE\u06C1\u0647]\u06D2"   # رادھے (ھ / ہ / ه)

# A character of this class right next to the phrase would make it part of
# a longer word in that script.
_ENGLISH_EDGE = r"\w"
_HINDI_EDGE = r"[\u0900-\u0963\u0966-\u096F\u0971-\u097F\uA8E0-\uA8FF]"   # not । ॥ ॰
_URDU_EDGE = r"[\u0620-\u065F\u066E-\u06D3\u06D5-\u06EF\u06FA-\u06FF\u0750-\u077F\u08A0-\u08FF]"


def _phrase(word: str, edge: str) -> str:
    return rf"(?<!{edge}){word}{_SEPARATOR}{word}(?!{edge})"


_PATTERN = re.compile("|".join([
    _phrase(_ENGLISH, _ENGLISH_EDGE),
    _phrase(_HINDI, _HINDI_EDGE),
    _phrase(_URDU, _URDU_EDGE),
]))


def normalize_for_matching(text: Optional[str]) -> str:
    """A throw-away copy of ``text`` for matching: NFKC-normalised, with
    ignorable characters removed and case folded. Never send or store it."""
    folded = unicodedata.normalize("NFKC", str(text or "")).translate(_IGNORED)
    return folded.casefold()


def has_radhe_radhe(text: Optional[str]) -> bool:
    """True if ``text`` contains "Radhe Radhe" in English, Hindi or Urdu."""
    if not text:
        return False
    return _PATTERN.search(normalize_for_matching(text)) is not None

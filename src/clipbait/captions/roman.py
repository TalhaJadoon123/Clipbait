"""Hindi and Urdu in Roman letters ("Hinglish"), for burned-in captions.

Ported from PeakCut's ``studio/roman.py`` (MIT, (c) 2026 PeakCut contributors)
into Clipbait's idiom. Clipbait draws captions with Latin-only display fonts
(Anton, Montserrat) while its regional playbooks cover India and Pakistan, so a
Devanagari or Urdu transcript would reach the screen as empty boxes. This module
spells the same words the way people type them in chat — ``चूना जो उसको``
becomes ``chuna jo usko``, ``یار کیا ہے`` becomes ``yaar kya hai`` — so a caption
is always drawable.

It is not a translation. Urdu writes without most of its vowels, so the common
words come from a list and the rest are spelled from their letters: readable,
if not always exactly how a person would type it. Russian-style (Cyrillic),
Greek and Korean letters are handled the same way for the same reason.
"""

from __future__ import annotations

import re
import unicodedata

from ..schema import Word

# Scripts the caption fonts cannot draw, and therefore the scripts worth
# romanising. The Arabic range is written out cleanly: it must not contain a
# literal space, or every English caption would look like it needs romanising.
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
MORE_SCRIPTS = re.compile(r"[\u0370-\u03FF\u0400-\u04FF\u1F00-\u1FFF\uAC00-\uD7A3]")

# Ranges are runs of one script, so a whole word is romanised in one call and
# the rules that depend on letter position still see the whole word.
_HINDI_RUN = re.compile(r"[\u0900-\u097F]+")
_URDU_RUN = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]+")
_KOREAN_RUN = re.compile(r"[\uAC00-\uD7A3]+")
# Greek and Cyrillic, plus the combining accents a decomposed Greek word leaves
# behind: those are dropped rather than spelled.
_LETTER_RUN = re.compile(r"[\u0370-\u03FF\u0400-\u04FF\u0300-\u036F]+")


# ---------------------------------------------------------------- Devanagari
_VOWELS = {
    "अ": "a",
    "आ": "aa",
    "इ": "i",
    "ई": "ee",
    "उ": "u",
    "ऊ": "oo",
    "ऋ": "ri",
    "ए": "e",
    "ऐ": "ai",
    "ओ": "o",
    "औ": "au",
    "ऑ": "o",
    "ऍ": "e",
}
_SIGNS = {
    "ा": "aa",
    "ि": "i",
    "ी": "ee",
    "ु": "u",
    "ू": "oo",
    "ृ": "ri",
    "े": "e",
    "ै": "ai",
    "ो": "o",
    "ौ": "au",
    "ॉ": "o",
    "ॅ": "e",
}
_CONS = {
    "क": "k",
    "ख": "kh",
    "ग": "g",
    "घ": "gh",
    "ङ": "n",
    "च": "ch",
    "छ": "chh",
    "ज": "j",
    "झ": "jh",
    "ञ": "n",
    "ट": "t",
    "ठ": "th",
    "ड": "d",
    "ढ": "dh",
    "ण": "n",
    "त": "t",
    "थ": "th",
    "द": "d",
    "ध": "dh",
    "न": "n",
    "प": "p",
    "फ": "ph",
    "ब": "b",
    "भ": "bh",
    "म": "m",
    "य": "y",
    "र": "r",
    "ल": "l",
    "व": "v",
    "श": "sh",
    "ष": "sh",
    "स": "s",
    "ह": "h",
    "ळ": "l",
    "क़": "q",
    "ख़": "kh",
    "ग़": "gh",
    "ज़": "z",
    "फ़": "f",
    "ड़": "r",
    "ढ़": "rh",
    "य़": "y",
}
_NUKTA = {"क": "q", "ख": "kh", "ग": "gh", "ज": "z", "फ": "f", "ड": "r", "ढ": "rh", "य": "y"}
_VIRAMA, _NUKTA_SIGN = "\u094d", "\u093c"
_NASAL = {"ं", "ँ"}
_OTHER = {"ः": "h", "।": ".", "॥": ".", "ॐ": "om", **{chr(0x0966 + i): str(i) for i in range(10)}}
# Common words whose everyday spelling the letter rules would not guess.
_WORDS = {
    "में": "mein",
    "मैं": "main",
    "हैं": "hain",
    "है": "hai",
    "नहीं": "nahi",
    "नही": "nahi",
    "हूँ": "hoon",
    "हूं": "hoon",
    "क्या": "kya",
    "क्यों": "kyun",
    "क्यूँ": "kyun",
    "यह": "yeh",
    "वह": "woh",
    "और": "aur",
    "भी": "bhi",
    "ही": "hi",
    "की": "ki",
    "जी": "ji",
    "तो": "to",
    "हाँ": "haan",
    "हां": "haan",
    "यहाँ": "yahan",
    "यहां": "yahan",
    "वहाँ": "wahan",
    "वहां": "wahan",
    "कहाँ": "kahan",
    "कहां": "kahan",
    "थी": "thi",
    "रही": "rahi",
    "कभी": "kabhi",
    "अभी": "abhi",
    "सभी": "sabhi",
    "भाई": "bhai",
    "मुझे": "mujhe",
    "तुम": "tum",
    "हम": "hum",
    "आप": "aap",
    "बहुत": "bahut",
    "अच्छा": "accha",
    "ठीक": "theek",
    "लिए": "liye",
}


# ---------------------------------------------------------------- Urdu / Arabic
_UR_WORDS = {
    "ہے": "hai",
    "ہیں": "hain",
    "ہوں": "hoon",
    "ہو": "ho",
    "ہی": "hi",
    "یہ": "ye",
    "وہ": "wo",
    "کیا": "kya",
    "کیوں": "kyun",
    "نہیں": "nahi",
    "نہ": "na",
    "میں": "main",
    "میرا": "mera",
    "میری": "meri",
    "میرے": "mere",
    "تو": "to",
    "کہ": "ke",
    "کی": "ki",
    "کا": "ka",
    "کے": "ke",
    "کو": "ko",
    "سے": "se",
    "پر": "par",
    "اور": "aur",
    "بھی": "bhi",
    "تھا": "tha",
    "تھی": "thi",
    "تھے": "the",
    "گیا": "gaya",
    "گئی": "gayi",
    "رہا": "raha",
    "رہی": "rahi",
    "رہے": "rahe",
    "بہت": "bahut",
    "اچھا": "achha",
    "یار": "yaar",
    "بھائی": "bhai",
    "آپ": "aap",
    "ہم": "hum",
    "تم": "tum",
    "مجھے": "mujhe",
    "تمہیں": "tumhein",
    "اب": "ab",
    "کر": "kar",
    "کرو": "karo",
    "کرنا": "karna",
    "دیکھو": "dekho",
    "چلو": "chalo",
    "ابھی": "abhi",
    "کبھی": "kabhi",
    "جی": "ji",
    "ہاں": "haan",
    "نا": "na",
    "اللہ": "Allah",
    "ٹھیک": "theek",
    "پتا": "pata",
    "پتہ": "pata",
    "لیکن": "lekin",
    "پھر": "phir",
    "سب": "sab",
    "کچھ": "kuch",
    "کون": "kaun",
    "کہاں": "kahan",
    "یہاں": "yahan",
    "وہاں": "wahan",
    "ایک": "ek",
    "دو": "do",
    "اپنا": "apna",
    "اپنی": "apni",
    "لوگ": "log",
    "بات": "baat",
    "کام": "kaam",
    "دن": "din",
    "آج": "aaj",
    "کل": "kal",
    "بس": "bas",
    "شکریہ": "shukriya",
    "واہ": "wah",
    "گھر": "ghar",
    "ہوا": "hua",
    "ہوئی": "hui",
    "ہوئے": "hue",
    "اس": "us",
    "ان": "un",
    "جو": "jo",
    "جب": "jab",
    "تب": "tab",
    "مگر": "magar",
    "پاکستان": "Pakistan",
    "زندگی": "zindagi",
    "پیسے": "paise",
    "روپے": "rupay",
}
_UR_CONS = {
    "ب": "b",
    "پ": "p",
    "ت": "t",
    "ٹ": "t",
    "ث": "s",
    "ج": "j",
    "چ": "ch",
    "ح": "h",
    "خ": "kh",
    "د": "d",
    "ڈ": "d",
    "ذ": "z",
    "ر": "r",
    "ڑ": "r",
    "ز": "z",
    "ژ": "zh",
    "س": "s",
    "ش": "sh",
    "ص": "s",
    "ض": "z",
    "ط": "t",
    "ظ": "z",
    "غ": "gh",
    "ف": "f",
    "ق": "q",
    "ک": "k",
    "ك": "k",
    "گ": "g",
    "ل": "l",
    "م": "m",
    "ن": "n",
    "ہ": "h",
    "ه": "h",
    "ة": "a",
    "ھ": "h",
}
_UR_MARKS = {"َ": "a", "ِ": "i", "ُ": "u", "ّ": "", "ْ": "", "ً": "an", "ٰ": "a"}
_UR_DIGITS = {
    **{chr(0x06F0 + i): str(i) for i in range(10)},
    **{chr(0x0660 + i): str(i) for i in range(10)},
}
# Arabic punctuation, including the Urdu full stop, which the caption fonts lack.
_AR_PUNCT = {"،": ",", "؟": "?", "؛": ";", "٪": "%", "۔": "."}


# ---------------------------------------------------------------- Cyrillic, Greek, Korean
_CYR = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "є": "ye",
    "і": "i",
    "ї": "yi",
    "ґ": "g",
    "ў": "u",
    "ђ": "dj",
    "ј": "j",
    "љ": "lj",
    "њ": "nj",
    "ћ": "c",
    "џ": "dz",
    "ә": "a",
    "ғ": "g",
    "қ": "q",
    "ң": "n",
    "ө": "o",
    "ұ": "u",
    "ү": "u",
    "һ": "h",
}
_GRK = {
    "α": "a",
    "β": "v",
    "γ": "g",
    "δ": "d",
    "ε": "e",
    "ζ": "z",
    "η": "i",
    "θ": "th",
    "ι": "i",
    "κ": "k",
    "λ": "l",
    "μ": "m",
    "ν": "n",
    "ξ": "x",
    "ο": "o",
    "π": "p",
    "ρ": "r",
    "σ": "s",
    "ς": "s",
    "τ": "t",
    "υ": "y",
    "φ": "f",
    "χ": "ch",
    "ψ": "ps",
    "ω": "o",
}
_K_FIRST = [
    "g",
    "kk",
    "n",
    "d",
    "tt",
    "r",
    "m",
    "b",
    "pp",
    "s",
    "ss",
    "",
    "j",
    "jj",
    "ch",
    "k",
    "t",
    "p",
    "h",
]
_K_VOWEL = [
    "a",
    "ae",
    "ya",
    "yae",
    "eo",
    "e",
    "yeo",
    "ye",
    "o",
    "wa",
    "wae",
    "oe",
    "yo",
    "u",
    "wo",
    "we",
    "wi",
    "yu",
    "eu",
    "ui",
    "i",
]
_K_LAST = [
    "",
    "k",
    "k",
    "k",
    "n",
    "n",
    "n",
    "t",
    "l",
    "k",
    "m",
    "l",
    "l",
    "l",
    "p",
    "l",
    "m",
    "p",
    "p",
    "t",
    "t",
    "ng",
    "t",
    "t",
    "k",
    "t",
    "p",
    "t",
]


class _Unit:
    """One spoken unit: a consonant with its vowel, a standalone vowel, or a glyph.

    Hindi letters carry an inherent "a" unless a sign or virama replaces it, and
    two of the rules below (the dropped word-final "a", the syllable-final long
    vowel) depend on that distinction, so it is tracked instead of inferred.
    """

    __slots__ = ("roman", "vowel", "consonant", "inherent", "nasal")

    def __init__(
        self,
        roman: str,
        vowel: str | None,
        consonant: bool,
        *,
        inherent: bool = False,
    ) -> None:
        self.roman = roman
        self.vowel = vowel
        self.consonant = consonant
        self.inherent = inherent
        self.nasal = False

    def spell(self) -> str:
        return self.roman + (self.vowel or "")


# ---------------------------------------------------------------- Devanagari spelling
def _hindi_word(word: str) -> str:
    """One Hindi word, spelled the way people type it."""
    if word in _WORDS:
        return _WORDS[word]

    units: list[_Unit] = []
    index = 0
    while index < len(word):
        char = word[index]
        following = word[index + 1] if index + 1 < len(word) else ""

        if char in _CONS:
            consonant = _CONS[char]
            if following == _NUKTA_SIGN:
                consonant = _NUKTA.get(char, consonant)
                index += 1
                following = word[index + 1] if index + 1 < len(word) else ""
            if following == _VIRAMA:
                units.append(_Unit(consonant, None, True))  # a conjunct: no vowel
                index += 2
                continue
            if following in _SIGNS:
                units.append(_Unit(consonant, _SIGNS[following], True))
                index += 2
                continue
            units.append(_Unit(consonant, "a", True, inherent=True))
        elif char in _VOWELS:
            units.append(_Unit("", _VOWELS[char], False))
        elif char in _NASAL:
            if units:
                units[-1].vowel = (units[-1].vowel or "") + "n"
                units[-1].inherent = False
                units[-1].nasal = True
            else:
                units.append(_Unit("n", None, True))
        elif char in _OTHER:
            units.append(_Unit(_OTHER[char], None, False))
        elif char in {_NUKTA_SIGN, _VIRAMA}:
            pass  # a stray mark: nothing to draw
        else:
            units.append(_Unit(char, None, False))
        index += 1

    spoken = [unit for unit in units if unit.consonant or unit.vowel]
    # Hindi drops the carried "a" at the end of a word, and between a vowel and
    # another vowel-carrying letter (कहना -> kahna, not kahana). Read right to
    # left, the way the rules apply.
    if len(spoken) > 1 and spoken[-1].inherent:
        spoken[-1].vowel = None
    for position in range(len(spoken) - 2, 0, -1):
        unit, before, after = spoken[position], spoken[position - 1], spoken[position + 1]
        if unit.inherent and before.vowel and after.consonant and after.vowel and not before.nasal:
            unit.vowel = None
    # Long vowels the way people type them: doubled only before a consonant that
    # ends a syllable (baat, kaam, aaj), single elsewhere (hamara, pata, ki, tu).
    short = {"aa": "a", "ee": "i", "oo": "u"}
    for position, unit in enumerate(spoken):
        if unit.vowel in short:
            after = spoken[position + 1] if position + 1 < len(spoken) else None
            if after is None or after.vowel:
                unit.vowel = short[unit.vowel]
    return "".join(unit.spell() for unit in units)


# ---------------------------------------------------------------- Urdu spelling
def _urdu_word(word: str) -> str:
    """One Urdu/Arabic word, spelled from its letters.

    Urdu leaves most short vowels unwritten, so a word that comes out as bare
    consonants gets the one "a" the script omitted and a final ``ہ`` after a
    consonant is read as the vowel it stands for (کہ -> ka, بچہ -> bacha).
    """
    if word in _UR_WORDS:
        return _UR_WORDS[word]

    out: list[str] = []
    after_consonant = False
    for index, char in enumerate(word):
        first, last = index == 0, index == len(word) - 1
        following = word[index + 1] if index + 1 < len(word) else ""

        if char in _UR_MARKS:
            out.append(_UR_MARKS[char])
            after_consonant = False
            continue
        if char in _UR_DIGITS:
            out.append(_UR_DIGITS[char])
            after_consonant = False
            continue
        if char == "ھ":  # do chashmi he: the "h" of bh, ph, kh, chh...
            out.append("h")
            continue
        if char in _AR_PUNCT:
            out.append(_AR_PUNCT[char])
            after_consonant = False
            continue
        if char in "اآأإٱ":  # alef, and alef with hamza or wasla
            out.append("aa" if char == "آ" else "i" if char == "إ" else "a")
            after_consonant = False
            continue
        if char in "وؤ":
            if first or not after_consonant:
                out.append("w")
                after_consonant = True
            else:
                out.append("o")
                after_consonant = False
            continue
        if char in "یيئى":
            if first:
                out.append("y")
                after_consonant = True
            else:
                out.append(
                    "i" if last else "ee" if after_consonant and following not in "اآوؤ" else "y"
                )
                after_consonant = out[-1] == "y"
            continue
        if char == "ے":
            out.append("e")
            after_consonant = False
            continue
        if char == "ں":  # noon ghunna: the nasal a word ends on
            out.append("n")
            continue
        if char in "عء":  # ayn and hamza carry no sound of their own here
            continue
        consonant = _UR_CONS.get(char)
        if consonant is None:
            out.append(char)
            after_consonant = False
            continue
        if char in "ہه" and last and after_consonant:
            out.append("a")
            after_consonant = False
            continue
        if (after_consonant and index == 1) or (after_consonant and index == 2 and word[1] == "ھ"):
            out.append(
                "a"
            )  # the short "a" Urdu does not write after a word's first letter: گھر -> ghar
        out.append(consonant)
        after_consonant = True

    spelled = "".join(out)
    if re.fullmatch(r"[a-z]{2,}", spelled) and not re.search(r"[aeiou]", spelled):
        spelled = spelled[:-1] + "a" + spelled[-1]
    return spelled


# ---------------------------------------------------------------- Cyrillic / Greek / Korean
def _letters(run: str) -> str:
    """Cyrillic and Greek letters spelled in Latin, accents dropped."""
    out: list[str] = []
    for char in unicodedata.normalize("NFD", run):
        if unicodedata.combining(char):
            continue
        low = char.lower()
        roman = _CYR.get(low, _GRK.get(low))
        if roman is None:
            out.append(char)
        else:
            out.append(roman.capitalize() if char != low and roman else roman)
    return "".join(out)


def _korean(run: str) -> str:
    """Korean syllables spelled from their initial, medial and final jamo."""
    out: list[str] = []
    for char in run:
        offset = ord(char) - 0xAC00
        if 0 <= offset < 11172:
            out.append(
                _K_FIRST[offset // 588] + _K_VOWEL[offset % 588 // 28] + _K_LAST[offset % 28]
            )
        else:
            out.append(char)
    return "".join(out)


# ---------------------------------------------------------------- public API
def needs_roman(text: str) -> bool:
    """True when the text contains letters the caption fonts cannot draw.

    Covers Hindi, Urdu/Arabic, Russian-style (Cyrillic), Greek and Korean.
    """
    text = text or ""
    return bool(DEVANAGARI.search(text) or ARABIC.search(text) or MORE_SCRIPTS.search(text))


def drawable(text: str) -> bool:
    """True when the Latin caption fonts can draw every letter of the text.

    They carry Latin letters (including accented forms), digits, punctuation and
    symbols; anything else renders as an empty box.
    """
    return not any(
        char.isalpha() and not (ord(char) < 0x250 or 0x1E00 <= ord(char) <= 0x1EFF)
        for char in text or ""
    )


def to_roman(text: str) -> str:
    """Every Hindi, Urdu, Cyrillic, Greek and Korean word spelled in Roman letters.

    Anything else — English, emoji, punctuation, digits — is left exactly as it
    is, so this is safe to run over every caption.
    """
    text = text or ""
    if DEVANAGARI.search(text):
        text = _HINDI_RUN.sub(lambda match: _hindi_word(match.group(0)), text)
    if ARABIC.search(text):
        text = _URDU_RUN.sub(lambda match: _urdu_word(match.group(0)), text)
    if MORE_SCRIPTS.search(text):
        # Decompose Greek and Cyrillic first, so accents can be dropped, then
        # recompose whatever Latin text was already there untouched.
        text = "".join(
            unicodedata.normalize("NFD", char)
            if "\u0370" <= char <= "\u04ff" or "\u1f00" <= char <= "\u1fff"
            else char
            for char in text
        )
        text = _KOREAN_RUN.sub(lambda match: _korean(match.group(0)), text)
        text = unicodedata.normalize(
            "NFC", _LETTER_RUN.sub(lambda match: _letters(match.group(0)), text)
        )
    return text


def caption_words(words: list[Word]) -> list[Word]:
    """The same words with any Hindi or Urdu spelled in Roman letters.

    Only ``text`` changes, and only for the words that need it, so timings stay
    exactly where the transcript put them and callers that already have a
    drawable word get their object back untouched.
    """
    return [
        word.model_copy(update={"text": to_roman(word.text)}) if needs_roman(word.text) else word
        for word in words
    ]

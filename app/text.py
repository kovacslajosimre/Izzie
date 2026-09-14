"""Mondathatár-felismerés a token-folyamban.

A TTS-nek mondatokra van szuksege, hogy az elsot mar mondhassa,
mikozben a tobbi meg generalodik. Ez a modul a beerkezo szovegdarabokat
puffereli, es amint egy teljes mondat osszeallt, kiadja.
"""

import re
from typing import List, Tuple

# Mondatzaro irasjel + opcionalis zaro idezojel/zarojel + whitespace
_BOUNDARY = re.compile(r'([.!?…]+)(["\'»”’\)\]]*)(\s+)')

# Magyar roviditesek, amik utan a pont NEM mondathatar
_ABBREV = {
    "pl", "ill", "stb", "kb", "vö", "vo", "ún", "un", "kb", "ti", "azaz",
    "dr", "prof", "id", "ifj", "sz", "szt", "u", "krt", "em", "hrsz",
    "min", "max", "kft", "bt", "zrt", "nyrt", "ker", "tel", "ker",
    "jan", "febr", "márc", "marc", "ápr", "apr", "jún", "jun", "júl", "jul",
    "aug", "szept", "okt", "nov", "dec",
}

_LAST_WORD = re.compile(r'([\wáéíóöőúüűÁÉÍÓÖŐÚÜŰ]+)$')


def _is_real_boundary(text_before: str) -> bool:
    """Eldonti, hogy a pont valodi mondatvege-e."""
    m = _LAST_WORD.search(text_before)
    if not m:
        return True
    word = m.group(1)

    # "2026." vagy "3." - magyarban a sorszam utan pont all, nem mondatveg
    if word.isdigit():
        return False

    # "pl." / "stb." / "dr."
    if word.lower() in _ABBREV:
        return False

    # "K." - egybetus kezdobetu (nev roviditese)
    if len(word) == 1 and word.isupper():
        return False

    return True


def split_sentences(buffer: str) -> Tuple[List[str], str]:
    """A pufferbol kiszedi a kesz mondatokat.

    Visszaad: (kesz mondatok listaja, a maradek puffer)
    """
    sentences: List[str] = []
    start = 0

    for m in _BOUNDARY.finditer(buffer):
        end_of_text = m.end(2)  # irasjel + zarojel vege, whitespace elott
        if not _is_real_boundary(buffer[start:m.start(1)]):
            continue

        sentence = buffer[start:end_of_text].strip()
        if sentence:
            sentences.append(sentence)
        start = m.end(3)

    return sentences, buffer[start:]

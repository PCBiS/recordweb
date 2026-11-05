"""Replace only the two upstream navigation blocks; retain intervening content."""
import re
from pathlib import Path

NAMES = ('index', 'recording', 'config', 'channels', 'cookies', 'files', 'register')
INCLUDE = "{% include 'partials/navigation.html' %}"


def _block(text, marker):
    matches = list(re.finditer(marker, text))
    if len(matches) != 1:
        raise ValueError(f'expected exactly one block for {marker!r}')
    start = matches[0].start()
    depth = 0
    for tag in re.finditer(r'</?div\b[^>]*>', text[start:], re.I):
        depth += -1 if tag.group().lower().startswith('</') else 1
        if depth == 0:
            return start, start + tag.end()
    raise ValueError('unbalanced navigation div')


def transform(name, text):
    if Path(name).stem not in NAMES:
        raise ValueError(f'unexpected template: {name}')
    a, b = _block(text, r'<div\s+class="navbar-container">')
    c, d = _block(text, r'<div\s+id="mySidenav"\s+class="sidenav">')
    if not a < b <= c < d or INCLUDE in text:
        raise ValueError('unexpected navigation layout')
    return text[:a] + INCLUDE + text[b:c] + text[d:]

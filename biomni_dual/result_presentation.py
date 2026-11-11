"""Safe, non-destructive rendering of worker artifacts for the Web control center.

Original result bytes/text and SHA256 remain authoritative. This module is a VIEW only:
never use its output for verification or write it back to the ledger.
"""
from __future__ import annotations

import json
import re

_UNICODE_ESCAPE = re.compile(r'(?:\\u[0-9a-fA-F]{4})+')
_TEXT_FIELDS = ('answer', 'result', 'output', 'final', 'text', 'content', 'message', 'stdout', 'response')


def _extract(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks = [_extract(item) for item in value]
        return '\n\n'.join(x for x in chunks if x is not None)
    if isinstance(value, dict):
        for key in _TEXT_FIELDS:
            if key in value:
                part = _extract(value[key])
                if part is not None:
                    return part
        return json.dumps(value, ensure_ascii=False, indent=2)
    return None


def present_result(raw: str | None) -> dict:
    """Return readable UTF-8 display text without changing original evidence."""
    if raw is None:
        return {'text': '', 'format': 'empty', 'transformed': False}
    if not isinstance(raw, str):
        raise TypeError('Worker result must be text')
    display = raw
    fmt = 'text'
    for _ in range(4):
        stripped = display.strip()
        if not stripped or stripped[0] not in ('{', '[', '"'):
            break
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            break
        extracted = _extract(parsed)
        if extracted is None:
            break
        if extracted == display:
            break
        display = extracted
        fmt = 'json'
    # Some workers send literal Unicode escape sequences without JSON quotes.
    # Decode only complete escaped codepoint groups, never run unicode_escape over
    # the whole string: that would corrupt already-valid Chinese UTF-8.
    def decode_unicode(match):
        try:
            return json.loads('"' + match.group(0) + '"')
        except (ValueError, json.JSONDecodeError):
            return match.group(0)
    decoded = _UNICODE_ESCAPE.sub(decode_unicode, display)
    if decoded != display:
        display = decoded
        fmt = 'unicode_escape' if fmt == 'text' else 'json_unicode_escape'
    # A small subset of legacy adapters emit UTF-8 bytes as literal \xHH sequences.
    # Only decode contiguous, valid UTF-8 runs containing non-ASCII bytes.
    def decode_hex_bytes(match):
        try:
            payload=bytes.fromhex(match.group(0).replace('\\x',''))
            text=payload.decode('utf-8','strict')
            return text if any(ord(ch)>127 for ch in text) else match.group(0)
        except (UnicodeError,ValueError):
            return match.group(0)
    hexadecimal=re.sub(r'(?:\\x[0-9A-Fa-f]{2}){2,}',decode_hex_bytes,display)
    if hexadecimal!=display:
        display=hexadecimal
        fmt='utf8_hex_escape' if fmt=='text' else fmt+'_utf8_hex_escape'
    return {'text': display, 'format': fmt, 'transformed': display != raw}

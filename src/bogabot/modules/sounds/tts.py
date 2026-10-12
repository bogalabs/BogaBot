"""Texto a voz para las frases de evento (ver voice_lines.py).

Usa gTTS (la voz del traductor de Google): no necesita key, solo salida a
internet. gTTS es bloqueante, así que corre en un thread. Si falla (sin red,
Google cambió algo) devuelve None y el evento sigue sin voz.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 20


def _save(text: str, out: Path, lang: str, tld: str) -> None:
    from gtts import gTTS  # import tardío: si falta la librería, solo se pierde la voz

    gTTS(text, lang=lang, tld=tld).save(str(out))


async def synthesize(text: str, out: Path, lang: str = "es", tld: str = "com.mx") -> Path | None:
    """Genera el mp3 de `text` en `out`. None si no se pudo."""
    try:
        await asyncio.wait_for(asyncio.to_thread(_save, text, out, lang, tld), timeout=_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - sin voz no es motivo para romper nada
        log.warning("No pude generar la voz (%s: %s).", type(exc).__name__, exc)
        out.unlink(missing_ok=True)
        return None
    return out if out.exists() and out.stat().st_size > 0 else None

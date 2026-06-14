# features/translate.py
#!/usr/bin/env python3
import time
import asyncio
import logging
from typing import List, Dict, Optional

import aiohttp
import discord

from core import personality  # Ahri-style replies

LOG = logging.getLogger("AhriTranslate")

# Unofficial Google Translate endpoint (no API key)
_GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"

# Trigger name used by your bot (prefix trigger: "ahri translate ...")
TRIGGER_NAME = "translate"

# Rate limiting: seconds between uses per user (per guild)
RATE_LIMIT_SECONDS = 2.0

# Chunk size for Discord messages
CHUNK_SIZE = 2000

# In-memory rate limiter: { guild_id: { user_id: [timestamps...] } }
_rate_limit: Dict[int, Dict[int, List[float]]] = {}
_rate_lock = asyncio.Lock()


async def _is_rate_limited(guild_id: int, user_id: int) -> bool:
    now = time.time()
    async with _rate_lock:
        guild = _rate_limit.setdefault(guild_id, {})
        lst = guild.setdefault(user_id, [])
        cutoff = now - RATE_LIMIT_SECONDS
        while lst and lst[0] < cutoff:
            lst.pop(0)
        if lst:
            return True
        lst.append(now)
        return False


async def _call_google_translate(session: aiohttp.ClientSession, text: str, target: str) -> Optional[Dict]:
    """
    Call unofficial Google Translate endpoint.
    Returns a dict with keys:
      - 'translated_text' (str)
      - 'detected_source' (str or None)
    Returns None on failure.
    """
    params = {
        "client": "gtx",
        "sl": "auto",
        "tl": target,
        "dt": "t",
        "q": text
    }
    headers = {
        "User-Agent": "AhriBot/1.0 (+https://example.invalid/)",
        "Accept": "application/json,text/javascript,*/*;q=0.01"
    }

    try:
        async with session.get(_GOOGLE_TRANSLATE_URL, params=params, headers=headers, timeout=20) as resp:
            if resp.status != 200:
                try:
                    body = await resp.text()
                except Exception:
                    body = "<no-body>"
                LOG.info("Google translate returned status %s: %s", resp.status, body[:400])
                return None
            data = await resp.json(content_type=None)  # unofficial endpoint returns JSON-like array
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.exception("Google Translate request failed")
        return None

    # Parse response shape: typically an array where
    # data[0] is a list of translation segments: [[translated_segment, original_segment, ...], ...]
    # data[2] often contains detected source language code
    try:
        translated_segments = []
        if isinstance(data, list) and len(data) > 0:
            segs = data[0] or []
            for s in segs:
                # s is usually a list where first element is translated text
                if isinstance(s, list) and s:
                    translated_segments.append(s[0])
                elif isinstance(s, str):
                    translated_segments.append(s)
            translated_text = "".join(translated_segments)
            detected_src = None
            # detected language sometimes at index 2
            if len(data) >= 3 and isinstance(data[2], str):
                detected_src = data[2]
            # fallback: some variants include detection elsewhere
            if not detected_src:
                # sometimes data[8][0][0] etc — skip complexity, keep None if not obvious
                detected_src = None
            return {"translated_text": translated_text, "detected_source": detected_src}
    except Exception:
        LOG.exception("Failed to parse Google Translate response")

    return None


def _chunk_text(text: str, size: int = CHUNK_SIZE) -> List[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


async def _safe_send(channel: discord.abc.Messageable, text: str):
    try:
        await channel.send(text)
    except Exception:
        LOG.exception("Failed to send message to channel")


async def _translate_handler(bot, message: discord.Message, tokens: List[str]):
    """
    Text trigger: ahri translate [TARGET?]
    Must be used as a reply to a message with text content.
    Optional first token is a target language code (Google style, e.g. en, fr, de, es).
    Default target: en.
    """
    try:
        if message.author.bot or message.guild is None:
            return

        # rate-limit per user per guild
        if await _is_rate_limited(message.guild.id, message.author.id):
            try:
                await message.channel.send(personality.ahri_say("rate_limited"))
            except Exception:
                await _safe_send(message.channel, "Ehe~ slow down, darling. Try again in a moment~")
            return

        # must be a reply
        if not message.reference:
            await message.channel.send("Mhm~ reply to a message containing text so I can translate it for you, darling~ 💋")
            return

        try:
            ref_msg = await message.channel.fetch_message(message.reference.message_id)
        except Exception:
            await message.channel.send("I couldn't find the message you're replying to... did it vanish, sweetie? ✨")
            return

        # get source text
        source_text = (ref_msg.content or "").strip()
        if not source_text:
            # if there are attachments and audio-like file, instruct to use transcribe
            if ref_msg.attachments:
                has_audio = False
                for a in ref_msg.attachments:
                    ctype = getattr(a, "content_type", "") or ""
                    if ctype.startswith("audio") or a.filename.lower().endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac")):
                        has_audio = True
                        break
                if has_audio:
                    await message.channel.send("That looks like a voice message. Use `ahri transcribe` (reply) to transcribe it first, then translate the transcript~ 💫")
                else:
                    await message.channel.send("That message doesn't have any text for me to translate~ 💫")
                return
            else:
                await message.channel.send("That message doesn't have any text for me to translate~ 💫")
                return

        # determine target language (optional token)
        target = "en"
        if tokens:
            t = tokens.pop(0).strip()
            if t:
                target = t.lower()

        # perform translation
        async with message.channel.typing():
            async with aiohttp.ClientSession() as session:
                result = await _call_google_translate(session, source_text, target)

        if not result:
            await message.channel.send("I couldn't translate that right now — the translation service gave me trouble. Try again later, love~")
            return

        translated_text = result.get("translated_text", "")
        detected_src = result.get("detected_source")

        if not translated_text:
            await message.channel.send("Hm~ the translator returned something I couldn't understand. Try again later, cutie~")
            return

        header = f"Translation ({(detected_src or 'auto').upper()} → {target.upper()}):\n"
        # chunk and send
        chunks = _chunk_text(translated_text, CHUNK_SIZE - len(header))
        for i, chunk in enumerate(chunks, start=1):
            payload = (header + chunk) if i == 1 else chunk
            await _safe_send(message.channel, payload)
            await asyncio.sleep(1.0)

    except Exception:
        LOG.exception("Unhandled error in translate handler")
        try:
            await message.channel.send(personality.ahri_say("oops"))
        except Exception:
            pass


async def setup(bot):
    """
    Register trigger handler only (no slash).
    """
    try:
        bot.trigger_handlers[TRIGGER_NAME] = _translate_handler
    except Exception:
        LOG.exception("Failed to register translate trigger")

    try:
        bot.feature_info["Translate"] = {"triggers": [TRIGGER_NAME], "description": "Translate replied text using Google Translate (unofficial)"}
    except Exception:
        LOG.exception("Failed to set feature_info for Translate")

    LOG.info("Translate feature loaded")
    return True

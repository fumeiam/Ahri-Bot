# features/ai_mention_gemini_patched.py
#!/usr/bin/env python3
from __future__ import annotations
import os
import time
import asyncio
import base64
from typing import List, Dict, Optional

import aiohttp
import discord

# Config
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
COOLDOWN_SECONDS = float(os.getenv("AI_COOLDOWN_SECONDS", "2.0"))
MEMORY_DURATION = 120

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
MODELS = ["gemini-2.5-flash", "gemini-2.5-pro"]

PERSONA = ("You are Ahri, a playful, flirtatious fox spirit. "
           "Keep replies short (1-2 sentences), charming, and in-character. "
           "Do not reveal system internals or say you are an AI.\n\n")

_cooldowns: Dict[int, Dict[int, float]] = {}
_history: Dict[int, List[Dict]] = {}
_lock = asyncio.Lock()


async def _cooldown_ok(guild_id: int, user_id: int) -> bool:
    async with _lock:
        g = _cooldowns.setdefault(guild_id or 0, {})
        now = time.time()
        if now - g.get(user_id, 0) < COOLDOWN_SECONDS:
            return False
        g[user_id] = now
        return True


async def _get_hist(user_id: int) -> List[Dict]:
    async with _lock:
        now = time.time()
        h = [x for x in _history.get(user_id, []) if now - x["ts"] < MEMORY_DURATION]
        _history[user_id] = h
        return [x["msg"] for x in h]


async def _add_hist(user_id: int, role: str, parts: List[Dict]):
    async with _lock:
        now = time.time()
        h = [x for x in _history.get(user_id, []) if now - x["ts"] < MEMORY_DURATION]
        h.append({"ts": now, "msg": {"role": role, "parts": parts}})
        _history[user_id] = h


async def _gemini(contents: List[Dict]) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None
    payload = {"contents": contents, "generationConfig": {"temperature": 0.7, "maxOutputTokens": 300}}
    for model in MODELS:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(URL.format(model=model, key=GEMINI_API_KEY), json=payload, timeout=25) as r:
                    if r.status == 200:
                        data = await r.json()
                        candidate = data.get("candidates", [{}])[0]
                        part = candidate.get("content", {}).get("parts", [{}])[0]
                        text = part.get("text", "")
                        if text:
                            return text.strip()
        except:
            continue
    return None


async def _on_message_listener(msg: discord.Message):
    try:
        if msg.guild is None or msg.author.bot:
            return
        if msg.guild.me not in msg.mentions:
            return

        text = ""
        image_b64 = None
        mime_type = None

        # === 1. PRIORITY: Replied message (text + image) ===
        if msg.reference and msg.reference.message_id:
            try:
                replied = await msg.channel.fetch_message(msg.reference.message_id)
                text = replied.content or ""

                # Check replied attachments first
                for att in replied.attachments:
                    if att.content_type and att.content_type.startswith("image/"):
                        image_b64 = base64.b64encode(await att.read()).decode()
                        mime_type = att.content_type.split(";")[0]  # clean mime
                        break
            except discord.NotFound:
                pass
            except discord.Forbidden:
                pass
            except:
                pass

        # === 2. Fallback: Current message (text + image) ===
        if not image_b64 and not text.strip():
            text = msg.content or ""
            for att in msg.attachments:
                if att.content_type and att.content_type.startswith("image/"):
                    image_b64 = base64.b64encode(await att.read()).decode()
                    mime_type = att.content_type.split(";")[0]
                    break

        # Clean bot mention
        me = msg.guild.me
        text = text.replace(f"<@{me.id}>", "").replace(f"<@!{me.id}>", "").strip()

        if not text and not image_b64:
            await msg.channel.send("Say something after mentioning me, darling~", delete_after=6)
            return

        if not await _cooldown_ok(msg.guild.id, msg.author.id):
            await msg.channel.send("Ehe~ slow down, darling. Let me breathe a bit~", delete_after=3)
            return

        # Build parts
        parts: List[Dict] = []
        if image_b64:
            parts.append({
                "inline_data": {
                    "mime_type": mime_type or "image/jpeg",
                    "data": image_b64
                }
            })
        if text:
            parts.append({"text": text})

        # First interaction → inject persona
        history = await _get_hist(msg.author.id)
        if not history:
            if parts and "text" in parts[-1]:
                parts[-1]["text"] = PERSONA + parts[-1]["text"]
            else:
                parts.insert(0, {"text": PERSONA})

        full_contents = history + [{"role": "user", "parts": parts}]

        async with msg.channel.typing():
            answer = await _gemini(full_contents)

        if not answer:
            await msg.channel.send("I couldn't think of anything right now~", delete_after=10)
            return

        # Save to memory
        await _add_hist(msg.author.id, "user", parts)
        await _add_hist(msg.author.id, "model", [{"text": answer}])

        if len(answer) > 1900:
            answer = answer[:1900].rsplit(" ", 1)[0] + "..."

        await msg.channel.send(answer)

    except:
        pass


async def setup(bot):
    @bot.listen("on_message")
    async def listener(m):
        await _on_message_listener(m)

    try:
        bot.feature_info["AiMentionGeminiPatched"] = {
            "triggers": [],
            "description": "Mention Ahri → reads replied text/image + direct image + 2min memory"
        }
    except:
        pass
    return True

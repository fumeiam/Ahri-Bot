#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import json
import aiohttp
import discord
from typing import Dict, Optional

from discord import app_commands

# ---------------- CONFIG ----------------

DATA_DIR = "data"
EMOJI_STORE = os.path.join(DATA_DIR, "stolen_emojis.json")

EMOJI_REGEX = re.compile(r"<(a?):([a-zA-Z0-9_]+):([0-9]{17,20})>")

# ---------------- UTILS ----------------

def _ensure_store():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(EMOJI_STORE):
        with open(EMOJI_STORE, "w", encoding="utf-8") as f:
            json.dump({}, f)

def _load_store() -> Dict:
    _ensure_store()
    with open(EMOJI_STORE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_store(data: Dict):
    with open(EMOJI_STORE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

async def _download_emoji(emoji_id: str, animated: bool) -> Optional[bytes]:
    ext = "gif" if animated else "png"
    url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?quality=lossless"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            return await resp.read()

# ---------------- CORE LOGIC ----------------

async def steal_from_text(text: str, user_id: int) -> Optional[str]:
    match = EMOJI_REGEX.search(text)
    if not match:
        return None

    animated = bool(match.group(1))
    name = match.group(2)
    emoji_id = match.group(3)

    img = await _download_emoji(emoji_id, animated)
    if not img:
        return None

    store = _load_store()
    store[str(user_id)] = {
        "name": name,
        "id": emoji_id,
        "animated": animated,
        "data": img.hex()
    }
    _save_store(store)
    return name

async def add_to_guild(guild: discord.Guild, user_id: int) -> bool:
    store = _load_store()
    data = store.get(str(user_id))
    if not data:
        return False

    emoji_bytes = bytes.fromhex(data["data"])
    await guild.create_custom_emoji(
        name=data["name"],
        image=emoji_bytes
    )

    del store[str(user_id)]
    _save_store(store)
    return True

# ---------------- TRIGGER COMMANDS ----------------

async def emoji_root(bot, message: discord.Message, args):
    if not args:
        await message.channel.send("Usage: `ahri emoji steal` or `ahri emoji add`")
        return

    sub = args[0].lower()

    # ---- steal (reply-based) ----
    if sub == "steal":
        if not message.reference or not message.reference.message_id:
            await message.channel.send("Reply to a message containing an emoji~")
            return

        try:
            replied = await message.channel.fetch_message(message.reference.message_id)
        except Exception:
            await message.channel.send("Couldn't read that message~")
            return

        name = await steal_from_text(replied.content, message.author.id)
        if not name:
            await message.channel.send("No custom emoji found there~")
            return

        await message.channel.send(f"Stolen~ 🦊 `{name}`")

    # ---- add ----
    elif sub == "add":
        if not message.guild:
            return

        try:
            ok = await add_to_guild(message.guild, message.author.id)
        except discord.Forbidden:
            await message.channel.send("I need **Manage Emojis** permission~")
            return
        except Exception:
            await message.channel.send("That emoji won't fit here~")
            return

        if not ok:
            await message.channel.send("You haven't stolen any emoji yet~")
            return

        await message.channel.send("Emoji added successfully~ ✨")

    else:
        await message.channel.send("Unknown emoji command~")

# ---------------- SLASH COMMAND ----------------

@app_commands.command(
    name="steal",
    description="Steal an emoji using <:name:id> format"
)
@app_commands.describe(
    emoji="Paste the emoji like <:name:id>"
)
async def steal_slash(interaction: discord.Interaction, emoji: str):
    await interaction.response.defer(ephemeral=True)

    name = await steal_from_text(emoji, interaction.user.id)
    if not name:
        await interaction.followup.send("That doesn't look like a valid emoji~")
        return

    await interaction.followup.send(f"Stolen~ 🦊 `{name}`")

# ---------------- SETUP ----------------

def register(bot, key, func):
    if not hasattr(bot, "trigger_handlers"):
        bot.trigger_handlers = {}
    bot.trigger_handlers[key] = func

async def setup(bot):
    # trigger-word command
    register(bot, "emoji", emoji_root)

    # slash command (global)
    try:
        bot.tree.add_command(steal_slash)
    except Exception:
        pass

    try:
        bot.feature_info["EmojiStealer"] = {
            "triggers": ["emoji steal", "emoji add", "/steal"],
            "description": "Steal emojis globally and add them to servers"
        }
    except Exception:
        pass

    return True

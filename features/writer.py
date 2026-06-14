# features/channel_writer_auto.py
import logging
from typing import List, Dict

import discord
from core import personality, permissions, utils

LOG = logging.getLogger("channel_writer_auto")

# In-memory storage: guild_id -> channel_id
TARGET_CHANNELS: Dict[int, int] = {}

# ===== Trigger Handlers =====
async def settarget(bot, message: discord.Message, tokens: List[str]):
    """Set the target channel for this server"""
    guild_id = message.guild.id
    if not await permissions.is_guild_admin(message.author, guild_id):
        await message.channel.send(personality.ahri_say("no_permission"))
        return

    if not tokens:
        await message.channel.send(personality.ahri_say("oops") + " You need to mention a channel.")
        return

    # parse channel mention
    channel = utils.get_channel_from_mention(message.guild, tokens[0])
    if not channel:
        await message.channel.send(personality.ahri_say("oops") + " Invalid channel.")
        return

    TARGET_CHANNELS[guild_id] = channel.id
    await message.channel.send(personality.ahri_say("activated") + f" Target channel set to {channel.mention} ✅")

settarget._needs_admin = True  # admin-only


async def write(bot, message: discord.Message, tokens: List[str]):
    """Send a message to the target channel"""
    if not tokens:
        await message.channel.send(personality.ahri_say("oops") + " You need to provide a message.")
        return

    guild_id = message.guild.id
    channel_id = TARGET_CHANNELS.get(guild_id)
    if not channel_id:
        await message.channel.send(personality.ahri_say("oops") + " No target channel set. Use `ahri settarget` first.")
        return

    channel = message.guild.get_channel(channel_id)
    if not channel:
        await message.channel.send(personality.ahri_say("oops") + " Target channel not found.")
        return

    content = " ".join(tokens)
    try:
        await channel.send(content)
        await message.channel.send(personality.ahri_say("activated") + f" Message sent to {channel.mention} ✨")
    except Exception as e:
        LOG.exception("Failed to send message: %s", e)
        await message.channel.send(personality.ahri_say("oops") + " Could not send the message.")


# ===== Auto-registration =====
def register(bot):
    triggers = {
        "settarget": settarget,
        "write": write
    }
    for name, func in triggers.items():
        bot.trigger_handlers[name] = func
        bot.feature_info[name] = {
            "triggers": [name],
            "description": func.__doc__ or "—"
        }

# Automatically register when imported
try:
    import sys
    import __main__ as main
    if hasattr(main, "bot"):
        register(main.bot)
        LOG.info("channel_writer_auto registered triggers successfully!")
except Exception as e:
    LOG.warning("Failed to auto-register channel_writer_auto: %s", e)

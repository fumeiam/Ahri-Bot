import asyncio
import discord
from discord.ext import commands
from gtts import gTTS
import tempfile
import os
import time
from typing import Tuple, Dict

# --------------------------
# In-memory guild defaults
# --------------------------
guild_defaults: Dict[int, Dict[str, float]] = {}  # {guild_id: {"voice": str, "volume": float}}
tts_queue_counts: Dict[int, int] = {}  # {guild_id: number of active TTS messages}
user_rate_limit: Dict[Tuple[int, int], float] = {}  # {(guild_id, user_id): last_ts}
RATE_LIMIT_SECONDS = 4.0  # 4s per user

def get_guild_defaults(guild_id: int):
    if guild_id not in guild_defaults:
        guild_defaults[guild_id] = {"voice": "en", "volume": 1.0}
    return guild_defaults[guild_id]

# --------------------------
# Utility
# --------------------------
def _ahri_text(msg: str) -> str:
    return f"✨ **Ahri**: {msg}"

def check_rate_limit(guild_id: int, user_id: int) -> Tuple[bool, float]:
    now = time.time()
    last_ts = user_rate_limit.get((guild_id, user_id), 0.0)
    elapsed = now - last_ts
    if elapsed < RATE_LIMIT_SECONDS:
        return False, RATE_LIMIT_SECONDS - elapsed
    user_rate_limit[(guild_id, user_id)] = now
    return True, 0.0

async def _play_tts_audio(vc: discord.VoiceClient, text: str, lang: str, volume: float):
    """Generate TTS, play in VC, then return when finished."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmpfile:
        tts = gTTS(text=text, lang=lang)
        tts.save(tmpfile.name)
        tmp_path = tmpfile.name

    source = discord.FFmpegPCMAudio(
        tmp_path,
        executable="/home/container/ffmpeg/ffmpeg"
    )
    vc.play(source)
    vc.source = discord.PCMVolumeTransformer(vc.source, volume=volume)

    while vc.is_playing():
        await asyncio.sleep(0.5)

    os.remove(tmp_path)

# --------------------------
# SAY HANDLER
# --------------------------
async def say_handler(bot, message: discord.Message, tokens):
    if not message.author.voice or not message.author.voice.channel:
        await message.channel.send(_ahri_text("You must be in a voice channel to use this command!"))
        return

    allowed, wait_time = check_rate_limit(message.guild.id, message.author.id)
    if not allowed:
        await message.channel.send(_ahri_text(f"⏱️ Wait {int(wait_time)}s before using TTS again."))
        return

    if not tokens:
        await message.channel.send(_ahri_text("Usage: ahri say <text> [--voice en] [--volume 1.0]"))
        return

    defaults = get_guild_defaults(message.guild.id)
    voice = defaults["voice"]
    volume = defaults["volume"]

    # Parse flags
    text_parts = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--voice"):
            if "=" in token:
                voice = token.split("=", 1)[1]
            elif i + 1 < len(tokens):
                voice = tokens[i + 1]
                i += 1
        elif token.startswith("--volume"):
            if "=" in token:
                try:
                    volume = float(token.split("=", 1)[1])
                except:
                    pass
            elif i + 1 < len(tokens):
                try:
                    volume = float(tokens[i + 1])
                except:
                    pass
                i += 1
        else:
            text_parts.append(token)
        i += 1

    text = " ".join(text_parts)
    if not text:
        await message.channel.send(_ahri_text("Please provide text to speak!"))
        return

    vc: discord.VoiceClient = message.guild.voice_client
    if not vc or not vc.is_connected():
        vc = await message.author.voice.channel.connect()

    # Increment TTS queue counter
    tts_queue_counts[message.guild.id] = tts_queue_counts.get(message.guild.id, 0) + 1

    # Pause music if not already paused
    was_playing = False
    if vc.is_playing():
        was_playing = True
        vc.pause()

    await message.channel.send(_ahri_text(f"🔊 Speaking now (lang={voice}, vol={volume})..."))

    try:
        await _play_tts_audio(vc, text, voice, volume)
    except Exception as e:
        await message.channel.send(_ahri_text(f"❌ Failed to play TTS: {e}"))
    finally:
        # Decrement TTS queue counter
        tts_queue_counts[message.guild.id] -= 1
        # Resume music only if no other TTS is active
        if was_playing and tts_queue_counts[message.guild.id] == 0:
            vc.resume()

# --------------------------
# VOLUME HANDLER
# --------------------------
async def tts_volume_handler(bot, message: discord.Message, tokens):
    if not tokens:
        await message.channel.send(_ahri_text("Usage: ahri tts volume <number between 0.1 and 2.0>"))
        return
    try:
        volume = float(tokens[0])
        if volume <= 0 or volume > 2.0:
            await message.channel.send(_ahri_text("❌ Volume must be between 0.1 and 2.0"))
            return
        defaults = get_guild_defaults(message.guild.id)
        defaults["volume"] = volume
        await message.channel.send(_ahri_text(f"✅ Default TTS volume set to {volume}"))
    except ValueError:
        await message.channel.send(_ahri_text("❌ Invalid volume value"))

# --------------------------
# LANGUAGE HANDLER
# --------------------------
async def tts_language_handler(bot, message: discord.Message, tokens):
    if not tokens:
        await message.channel.send(_ahri_text("Usage: ahri tts language <lang_code> (e.g., en, fr, es)"))
        return
    lang = tokens[0].lower()
    defaults = get_guild_defaults(message.guild.id)
    defaults["voice"] = lang
    await message.channel.send(_ahri_text(f"✅ Default TTS language set to `{lang}`"))

# --------------------------
# REGISTER HANDLERS
# --------------------------
def setup(bot):
    bot.trigger_handlers["say"] = say_handler
    bot.feature_info["Say"] = {"triggers": ["say"]}

    bot.trigger_handlers["tts volume"] = tts_volume_handler
    bot.feature_info["TTS Volume"] = {"triggers": ["tts volume"]}

    bot.trigger_handlers["tts language"] = tts_language_handler
    bot.feature_info["TTS Language"] = {"triggers": ["tts language"]}

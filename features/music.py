# music_lavalink_full.py - Fixed Wavelink 3.2.0 Event Handlers
import asyncio
import json
import logging
import os
import random
import time
from typing import Optional, List, Tuple

import discord
from discord import Message
from discord.ext import commands
import wavelink

# -----------------------
# CONFIG — Lavalink server
# -----------------------
LAVALINK_HOST = "lava-v4.ajieblogs.eu.org"
LAVALINK_PORT = 443
LAVALINK_PASSWORD = "https://dsc.gg/ajidevserver"
USE_SECURE = True

# local persistence file
_DATA_FILE = os.path.join(os.getcwd(), "music_lavalink.json")

_log = logging.getLogger("music_lavalink")
if not _log.handlers:
    _log.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _log.addHandler(handler)

# -----------------------
# In-memory state
# -----------------------
guild_setup = {}       # guild_id -> {"channel_id": int, "message_id": int}
guild_state = {}       # guild_id -> {"queue": list, "volume": float, "vc_id": int, "pause": bool}

# rate limiting per user per guild
_button_timestamps: dict = {}  # {(guild_id, user_id): last_ts}
_BUTTON_RATE_LIMIT_SECONDS = 5
_FEEDBACK_TTL_SECONDS = 10

TRENDING_QUERIES = [
    "lofi beats", "kpop", "lofi hip hop", "relaxing music", "pop hits 2025",
    "gaming music", "anime OST", "chillhop", "trending songs"
]

# -----------------------
# Persistence helpers
# -----------------------
def _load_guild_state():
    try:
        if os.path.exists(_DATA_FILE):
            with open(_DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for gid, state in data.items():
                        try:
                            guild_state[int(gid)] = state
                        except Exception:
                            continue
    except Exception as e:
        _log.exception("Failed to load guild state: %s", e)

def _save_guild_state():
    try:
        data = {str(gid): state for gid, state in guild_state.items()}
        tmp = _DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _DATA_FILE)
    except Exception:
        _log.exception("Failed to save guild state")

# -----------------------
# Utility
# -----------------------
def _ahri_text(msg: str) -> str:
    return f"✨ **Ahri**: {msg}"

def _is_admin(member: discord.Member) -> bool:
    try:
        return member.guild_permissions.administrator
    except Exception:
        return False

def _rate_limit_key(interaction: discord.Interaction) -> Tuple[int, int]:
    guild_id = interaction.guild.id if interaction.guild else 0
    user_id = interaction.user.id
    return (guild_id, user_id)

def _check_and_update_rate_limit(interaction: discord.Interaction) -> Tuple[bool, float]:
    try:
        key = _rate_limit_key(interaction)
        now = time.time()
        last = _button_timestamps.get(key, 0.0)
        elapsed = now - last
        if elapsed < _BUTTON_RATE_LIMIT_SECONDS:
            return False, _BUTTON_RATE_LIMIT_SECONDS - elapsed
        _button_timestamps[key] = now
        return True, 0.0
    except Exception:
        return True, 0.0

async def _delete_later(message: discord.Message, delay: float):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass

async def _send_temporary_response(interaction: discord.Interaction, content: str, ephemeral: bool = False):
    try:
        await interaction.response.send_message(content, ephemeral=ephemeral)
    except discord.InteractionResponded:
        try:
            await interaction.followup.send(content, ephemeral=ephemeral)
        except Exception:
            return
    except Exception:
        try:
            await interaction.followup.send(content, ephemeral=ephemeral)
        except Exception:
            return
    if not ephemeral:
        try:
            msg = await interaction.original_response()
            asyncio.create_task(_delete_later(msg, _FEEDBACK_TTL_SECONDS))
        except Exception:
            pass

# -----------------------
# Lavalink initialization for Wavelink 3.2.0
# -----------------------
# -----------------------
# Lavalink node startup for Wavelink 3.x
# -----------------------
async def start_nodes(bot: commands.Bot):
    """Connects to Lavalink node using Wavelink 3.x syntax"""
    await bot.wait_until_ready()
    
    try:
        # Create node
        node = wavelink.Node(
            uri=f"{'https' if USE_SECURE else 'http'}://{LAVALINK_HOST}:{LAVALINK_PORT}",
            password=LAVALINK_PASSWORD,
            identifier="MAIN",
            retries=5,           # retry attempts if failed
            resume_timeout=60
        )

        # Connect node via Pool
        await wavelink.Pool.connect(client=bot, nodes=[node])
        _log.info(f"Connected to Lavalink node {LAVALINK_HOST}:{LAVALINK_PORT}")

    except Exception as e:
        _log.exception(f"Failed to connect to Lavalink node: {e}")


# -----------------------
# Modals
# -----------------------
class PlayUrlModal(discord.ui.Modal, title="Play URL or search"):
    url_or_query = discord.ui.TextInput(
        label="YouTube URL or search query",
        style=discord.TextStyle.short,
        placeholder="https://www.youtube.com/watch?v=xxx or lofi beats",
        required=True,
        max_length=512,
    )
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        
    async def on_submit(self, interaction: discord.Interaction):
        allowed, rem = _check_and_update_rate_limit(interaction)
        if not allowed:
            await _send_temporary_response(interaction, _ahri_text(f"You're pressing buttons too fast — wait {int(rem)}s"), ephemeral=True)
            return
        if not interaction.user.voice or not interaction.user.voice.channel:
            await _send_temporary_response(interaction, _ahri_text("You must be in a voice channel."), ephemeral=True)
            return
        try:
            # Get or create player
            player = interaction.guild.voice_client
            if not player or not isinstance(player, wavelink.Player):
                player = await interaction.user.voice.channel.connect(cls=wavelink.Player)
                
            query = self.url_or_query.value.strip()
            
            # Search for tracks using Wavelink 3.x syntax
            tracks = await wavelink.Playable.search(query)
            if not tracks:
                await _send_temporary_response(interaction, _ahri_text("No results found."), ephemeral=True)
                return
                
            track = tracks[0]
            
            # Play the track
            await player.play(track)

            # Store in guild state
            guild_state.setdefault(self.guild_id, {}).setdefault("queue", []).append({
                "title": track.title,
                "url": track.uri,
                "duration": track.length
            })
            _save_guild_state()

            # Apply stored volume
            try:
                vol = guild_state.get(self.guild_id, {}).get("volume")
                if vol is not None:
                    await player.set_volume(int(vol * 100))  # Convert to 0-100 scale
            except Exception as e:
                _log.debug("Volume set failed: %s", e)

            await _send_temporary_response(interaction, _ahri_text(f"Now playing: {track.title} 🎶"))
        except Exception as e:
            _log.exception("PlayUrlModal error: %s", e)
            await _send_temporary_response(interaction, _ahri_text("Failed to play that URL."), ephemeral=True)

class VolumeModal(discord.ui.Modal, title="Set playback volume (0.1 - 5.0)"):
    volume_value = discord.ui.TextInput(
        label="Volume (e.g. 0.8)",
        style=discord.TextStyle.short,
        placeholder="0.8",
        required=True,
        max_length=6,
    )
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        
    async def on_submit(self, interaction: discord.Interaction):
        allowed, rem = _check_and_update_rate_limit(interaction)
        if not allowed:
            await _send_temporary_response(interaction, _ahri_text(f"You're pressing buttons too fast — wait {int(rem)}s"), ephemeral=True)
            return
        try:
            vol = float(self.volume_value.value.strip())
            if vol <= 0 or vol > 5.0:
                await _send_temporary_response(interaction, _ahri_text("Volume must be between 0.1 and 5.0."), ephemeral=True)
                return

            # Store per-guild volume
            guild_state.setdefault(self.guild_id, {})["volume"] = vol
            _save_guild_state()

            # Apply to player if connected
            player = interaction.guild.voice_client
            if player and isinstance(player, wavelink.Player):
                await player.set_volume(int(vol * 100))  # Convert to 0-100 scale

            await _send_temporary_response(interaction, _ahri_text(f"Volume set to {vol} ✅"))
        except ValueError:
            await _send_temporary_response(interaction, _ahri_text("Invalid volume input. Please enter a number."), ephemeral=True)
        except Exception:
            await _send_temporary_response(interaction, _ahri_text("Invalid volume input."), ephemeral=True)

# -----------------------
# Music Control View
# -----------------------
class MusicControlView(discord.ui.View):
    def __init__(self, guild_id: int, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id

    async def get_player(self, interaction: discord.Interaction) -> Optional[wavelink.Player]:
        try:
            if not interaction.user.voice or not interaction.user.voice.channel:
                return None

            player = interaction.guild.voice_client
            if not player or not isinstance(player, wavelink.Player):
                player = await interaction.user.voice.channel.connect(cls=wavelink.Player)
            elif player.channel.id != interaction.user.voice.channel.id:
                await player.move_to(interaction.user.voice.channel)

            # Apply stored volume
            try:
                vol = guild_state.get(self.guild_id, {}).get("volume")
                if vol is not None:
                    await player.set_volume(int(vol * 100))
            except Exception as e:
                _log.debug("Volume set failed: %s", e)

            return player
        except Exception as e:
            _log.exception("get_player error: %s", e)
            return None

    @discord.ui.button(label="▶️ Play (autoplay)", style=discord.ButtonStyle.success, custom_id="music_play_autoplay")
    async def play_autoplay(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, rem = _check_and_update_rate_limit(interaction)
        if not allowed:
            await _send_temporary_response(interaction, _ahri_text(f"You're pressing buttons too fast — wait {int(rem)}s"), ephemeral=True)
            return
        try:
            query = random.choice(TRENDING_QUERIES)
            player = await self.get_player(interaction)
            if not player:
                await _send_temporary_response(interaction, _ahri_text("Could not connect to voice channel."), ephemeral=True)
                return
                
            tracks = await wavelink.Playable.search(query)
            if not tracks:
                await _send_temporary_response(interaction, _ahri_text("No autoplay tracks found."), ephemeral=True)
                return
                
            track = tracks[0]
            await player.play(track)
            
            # Add to queue
            guild_state.setdefault(self.guild_id, {}).setdefault("queue", []).append({
                "title": track.title,
                "url": track.uri,
                "duration": track.length
            })
            _save_guild_state()
            
            await _send_temporary_response(interaction, _ahri_text(f"Autoplay started: {track.title} 🎶"))
        except Exception as e:
            _log.exception("play_autoplay error: %s", e)
            await _send_temporary_response(interaction, _ahri_text("Failed to start autoplay."), ephemeral=True)

    @discord.ui.button(label="▶️ Play (URL/search)", style=discord.ButtonStyle.primary, custom_id="music_play_url")
    async def play_url(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, rem = _check_and_update_rate_limit(interaction)
        if not allowed:
            await _send_temporary_response(interaction, _ahri_text(f"You're pressing buttons too fast — wait {int(rem)}s"), ephemeral=True)
            return
        if not interaction.user.voice or not interaction.user.voice.channel:
            await _send_temporary_response(interaction, _ahri_text("Join a voice channel first."), ephemeral=True)
            return
        modal = PlayUrlModal(self.guild_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="music_skip")
    async def skip_track(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, rem = _check_and_update_rate_limit(interaction)
        if not allowed:
            await _send_temporary_response(interaction, _ahri_text(f"Wait {int(rem)}s"), ephemeral=True)
            return
        
        player = interaction.guild.voice_client
        if not player or not player.playing:
            await _send_temporary_response(interaction, _ahri_text("Nothing is playing."), ephemeral=True)
            return
        
        # Remove current track from queue
        if self.guild_id in guild_state and guild_state[self.guild_id].get("queue"):
            queue = guild_state[self.guild_id]["queue"]
            if queue:
                queue.pop(0)
                _save_guild_state()
        
        await player.stop()
        await _send_temporary_response(interaction, _ahri_text("Skipped track ⏭️"))

    @discord.ui.button(label="⏯️ Pause/Resume", style=discord.ButtonStyle.secondary, custom_id="music_pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, rem = _check_and_update_rate_limit(interaction)
        if not allowed:
            await _send_temporary_response(interaction, _ahri_text(f"Wait {int(rem)}s"), ephemeral=True)
            return
        
        player = interaction.guild.voice_client
        if not player:
            await _send_temporary_response(interaction, _ahri_text("Not connected to voice."), ephemeral=True)
            return
        
        if player.paused:
            await player.resume()
            guild_state.setdefault(self.guild_id, {})["pause"] = False
            await _send_temporary_response(interaction, _ahri_text("Resumed playback ▶️"))
        else:
            await player.pause()
            guild_state.setdefault(self.guild_id, {})["pause"] = True
            await _send_temporary_response(interaction, _ahri_text("Paused playback ⏸️"))
        
        _save_guild_state()

    @discord.ui.button(label="🔊 Volume", style=discord.ButtonStyle.primary, custom_id="music_volume")
    async def volume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, rem = _check_and_update_rate_limit(interaction)
        if not allowed:
            await _send_temporary_response(interaction, _ahri_text(f"Wait {int(rem)}s"), ephemeral=True)
            return
        
        modal = VolumeModal(self.guild_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="📋 Queue", style=discord.ButtonStyle.secondary, custom_id="music_queue")
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, rem = _check_and_update_rate_limit(interaction)
        if not allowed:
            await _send_temporary_response(interaction, _ahri_text(f"Wait {int(rem)}s"), ephemeral=True)
            return
        
        queue = guild_state.get(self.guild_id, {}).get("queue", [])
        if not queue:
            await _send_temporary_response(interaction, _ahri_text("Queue is empty."), ephemeral=True)
            return
        
        queue_text = "\n".join([f"{i+1}. {track['title']}" for i, track in enumerate(queue[:10])])
        if len(queue) > 10:
            queue_text += f"\n... and {len(queue) - 10} more"
        
        await _send_temporary_response(interaction, _ahri_text(f"Current queue:\n{queue_text}"))

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, custom_id="music_stop")
    async def stop_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed, rem = _check_and_update_rate_limit(interaction)
        if not allowed:
            await _send_temporary_response(interaction, _ahri_text(f"Wait {int(rem)}s"), ephemeral=True)
            return
        
        player = interaction.guild.voice_client
        if player:
            await player.disconnect()
            # Clear queue but keep settings
            if self.guild_id in guild_state:
                guild_state[self.guild_id]["queue"] = []
                guild_state[self.guild_id]["pause"] = False
            _save_guild_state()
        
        await _send_temporary_response(interaction, _ahri_text("Stopped playback and disconnected ⏹️"))

# -----------------------
# Setup persistent panel
# -----------------------
async def music_setup_handler(bot, message: Message, tokens):
    author = message.author
    if not isinstance(author, discord.Member) or not _is_admin(author):
        await message.channel.send(_ahri_text("Admin permission required."))
        return

    if not tokens:
        await message.channel.send(_ahri_text("Usage: `ahri setupmusic <#channel|channel_id>`"))
        return

    channel_token = " ".join(tokens)
    target = None

    # 1️⃣ Check if it’s a mention like <#123456789012345678>
    if channel_token.startswith("<#") and channel_token.endswith(">"):
        try:
            target = message.guild.get_channel(int(channel_token[2:-1]))
        except Exception:
            pass

    # 2️⃣ Check if it’s a numeric ID
    if not target and channel_token.isdigit():
        try:
            target = message.guild.get_channel(int(channel_token))
        except Exception:
            pass

    # 3️⃣ Check for exact name match
    if not target:
        for ch in message.guild.text_channels:
            if ch.name.lower() == channel_token.lower():
                target = ch
                break

    # 4️⃣ Fallback: check for partial match
    if not target:
        for ch in message.guild.text_channels:
            if channel_token.lower() in ch.name.lower():
                target = ch
                break

    if not target:
        await message.channel.send(_ahri_text("Could not find that channel."))
        return

    content = "✨ **Ahri Music Player** — click buttons to control music.\nPlay, Skip, Pause/Resume, Volume."
    view = MusicControlView(message.guild.id)
    sent = await target.send(content, view=view)
    guild_setup[message.guild.id] = {"channel_id": target.id, "message_id": sent.id}
    _save_guild_state()
    bot.add_view(view, message_id=sent.id)
    await message.channel.send(_ahri_text(f"Music panel posted in {target.mention} ✅"))

# -----------------------
# Event Handlers for Wavelink 3.2.0 (Corrected)
# -----------------------
async def setup_event_handlers(bot: commands.Bot):
    """Setup Wavelink 3.2.0 event handlers with correct syntax"""
    
    @bot.event
    async def on_wavelink_track_end(player: wavelink.Player, track: wavelink.Playable, reason: str):
        """Handle track completion - Wavelink 3.2.0 syntax"""
        try:
            if not player or not player.guild:
                return
                
            guild_id = player.guild.id
            # Remove finished track from queue
            if guild_id in guild_state and guild_state[guild_id].get("queue"):
                queue = guild_state[guild_id]["queue"]
                if queue:
                    queue.pop(0)
                    _save_guild_state()
                    
            _log.info(f"Track ended in guild {guild_id}, reason: {reason}")
        except Exception as e:
            _log.error(f"Error in track_end handler: {e}")

    @bot.event
    async def on_wavelink_node_ready(payload):
        node = payload.node # <-- get the actual Node object
        _log.info(f"Node {node.identifier} is ready!")

    @bot.event 
    async def on_wavelink_track_exception(player: wavelink.Player, track: wavelink.Playable, error: Exception):
        """Handle track exceptions - Wavelink 3.2.0 syntax"""
        try:
            guild_id = player.guild.id if player.guild else "Unknown"
            _log.error(f"Track error in guild {guild_id}: {error}")
        except Exception as e:
            _log.error(f"Error in track_exception handler: {e}")

    @bot.event
    async def on_wavelink_track_stuck(player: wavelink.Player, track: wavelink.Playable, threshold: float):
        """Handle stuck tracks - Wavelink 3.2.0 syntax"""
        try:
            guild_id = player.guild.id if player.guild else "Unknown"
            _log.warning(f"Track stuck in guild {guild_id}, threshold: {threshold}")
        except Exception as e:
            _log.error(f"Error in track_stuck handler: {e}")

# -----------------------
# Module setup
# -----------------------
def setup(bot):
    bot.trigger_handlers["setupmusic"] = music_setup_handler
    bot.loop.create_task(start_nodes(bot))
    bot.loop.create_task(setup_event_handlers(bot))
    _load_guild_state()

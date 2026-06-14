# logging.py
from __future__ import annotations

import os
import json
import time
import asyncio
from typing import Dict, Any, Optional, Tuple

import discord
from discord import Object
from discord.ext import commands

from core import db, permissions  # keep using your core helpers for guild load + perms

# ---- Configuration ----
DATA_DIR = os.path.join("data")
SETTINGS_FILE = os.path.join(DATA_DIR, "logging_settings.json")
_INVITE_CACHE: Dict[int, Dict[str, Dict[str, Any]]] = {}  # guild_id -> invite_code -> {"uses": int, "inviter": id}
_FILE_LOCK = asyncio.Lock()
_CACHE_LOCK = asyncio.Lock()

FEATURE_INFO = {
    "name": "unified_logger",
    "triggers": ["log"],
    "description": "Unified logging system: message / server / moderation events with per-category & per-event toggles."
}

# ---- Base-Ahri inline responses (module-local; no personality.py) ----
AHRI = {
    "ok_set": "Alright~ I’ll keep watch there. 🔍",
    "no_permission": "Hmm~ you’re not allowed to tell me that, sweetie. Ask an admin~",
    "no_channel": "Mmh — I’d love to log that, but where should I whisper the secrets? Mention a channel (like `#logs`) within 30s.",
    "no_channel_final": "Alright, I’ll wait. If you change your mind, set a channel with `ahri log setchannel #channel`~",
    "toggled_on": "Done — logging toggled. I’ll keep an eye. ✨",
    "toggled_off": "Fine~ I’ll stop watching that for now.",
    "help_header": "Ahri Logger — I’ll quietly watch and report things for you~",
    "error": "Oops~ something slipped through my foxfire. Try again later, okay?",
    "already": "Hehe~ that’s already set like that.",
    "setchannel_prompt": "Mention the channel to use for logs (reply with a channel mention in 30s).",
    "setchannel_done": "Log channel set. I’ll be watching from there~",
}

# ---- Default settings template per guild ----
DEFAULT_TEMPLATE = {
    "log_channel": None,
    "categories": {
        "message": {
            "enabled": True,
            "delete": True,
            "edit": True
        },
        "server": {
            "enabled": True,
            "join": True,
            "leave": True
        },
        "moderation": {
            "enabled": True,
            "ban": True,
            "kick": True,
            "mute": True
        }
    }
}

# ---- Helper: file persistence ----
async def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

async def _load_all_settings() -> Dict[str, Any]:
    await _ensure_data_dir()
    async with _FILE_LOCK:
        if not os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, indent=2)
            return {}
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data or {}
        except Exception:
            # If file is corrupt, rename it and start fresh
            try:
                ts = int(time.time())
                os.rename(SETTINGS_FILE, f"{SETTINGS_FILE}.bak.{ts}")
            except Exception:
                pass
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, indent=2)
            return {}

async def _save_all_settings(all_settings: Dict[str, Any]) -> None:
    await _ensure_data_dir()
    async with _FILE_LOCK:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_settings, f, indent=2, sort_keys=True)

# ensure a guild has defaults and return its dict
async def _ensure_guild_settings(guild_id: int) -> Dict[str, Any]:
    all_sets = await _load_all_settings()
    sid = str(guild_id)
    if sid not in all_sets:
        all_sets[sid] = DEFAULT_TEMPLATE.copy()
        # deepcopy categories
        all_sets[sid]["categories"] = json.loads(json.dumps(DEFAULT_TEMPLATE["categories"]))
        await _save_all_settings(all_sets)
    return all_sets[sid]

# update a guild's settings and persist
async def _save_guild_settings(guild_id: int, settings: Dict[str, Any]) -> None:
    all_sets = await _load_all_settings()
    all_sets[str(guild_id)] = settings
    await _save_all_settings(all_sets)

# get a setting with fallback
async def _get_setting(guild_id: int) -> Dict[str, Any]:
    try:
        s = await _ensure_guild_settings(guild_id)
        return s
    except Exception:
        return DEFAULT_TEMPLATE.copy()

# ---- Invite caching (accurate invite detection) ----
async def _cache_guild_invites(guild: discord.Guild):
    try:
        invites = await guild.invites()
    except Exception:
        return
    async with _CACHE_LOCK:
        data = {}
        for inv in invites:
            try:
                data[inv.code] = {"uses": inv.uses, "inviter": inv.inviter.id if inv.inviter else None}
            except Exception:
                data[str(getattr(inv, "code", "unknown"))] = {"uses": getattr(inv, "uses", 0), "inviter": None}
        _INVITE_CACHE[guild.id] = data

async def _refresh_guild_invites_safe(guild: discord.Guild):
    try:
        await _cache_guild_invites(guild)
    except Exception:
        pass

async def _find_inviter_on_join(guild: discord.Guild) -> Optional[Tuple[int, str]]:
    """
    Compare current invites to cached invites to find which invite increased in 'uses'.
    Returns (inviter_id, invite_code) or None
    """
    try:
        invites = await guild.invites()
    except Exception:
        return None
    async with _CACHE_LOCK:
        old = _INVITE_CACHE.get(guild.id, {})
        winner = None
        for inv in invites:
            code = inv.code
            old_uses = old.get(code, {}).get("uses", 0)
            if getattr(inv, "uses", 0) > old_uses:
                winner = (inv.inviter.id if inv.inviter else None, code)
                break
        # update cache to latest snapshot
        newmap = {}
        for inv in invites:
            newmap[inv.code] = {"uses": inv.uses, "inviter": inv.inviter.id if inv.inviter else None}
        _INVITE_CACHE[guild.id] = newmap
        return winner

# ---- Bot-sent log message tracking (so we can watch deletions of Ahri's log messages) ----
# in-memory only; small TTL for cleanup
_BOT_LOG_MSGS: Dict[int, Dict[str, Any]] = {}
_BOT_LOG_LOCK = asyncio.Lock()
_BOT_LOG_TTL_SECONDS = 24 * 3600

async def _store_bot_log_message(sent: discord.Message, title: Optional[str], snippet: Optional[str]):
    """Store metadata about a message the bot sent to a log channel so we can detect its deletion later."""
    try:
        async with _BOT_LOG_LOCK:
            cutoff = time.time() - _BOT_LOG_TTL_SECONDS
            for mid in list(_BOT_LOG_MSGS.keys()):
                if _BOT_LOG_MSGS[mid].get("sent_at", 0) < cutoff:
                    del _BOT_LOG_MSGS[mid]
            _BOT_LOG_MSGS[sent.id] = {
                "guild": sent.guild.id if sent.guild else None,
                "channel": sent.channel.id if sent.channel else None,
                "title": title or "",
                "snippet": (snippet or "")[:900],
                "sent_at": time.time()
            }
    except Exception:
        # don't crash the main logger if tracking fails
        try:
            # best-effort logging to console
            print("BOTLOG: _store_bot_log_message failed", sent.id if sent else None)
        except Exception:
            pass

async def _pop_bot_log_message(message_id: int) -> Optional[Dict[str, Any]]:
    async with _BOT_LOG_LOCK:
        return _BOT_LOG_MSGS.pop(message_id, None)

async def _get_bot_log_message(message_id: int) -> Optional[Dict[str, Any]]:
    async with _BOT_LOG_LOCK:
        return _BOT_LOG_MSGS.get(message_id)

# ---- Logging helpers ----
async def _send_to_channel(bot: "discord.Client", guild_id: int, content: Optional[str] = None, embed: Optional[discord.Embed] = None, mention_allowed: bool = False, store_sent: bool = True):
    """
    Send to configured log channel. If store_sent is True, messages authored by the bot that are posted into the log channel
    will be tracked in-memory so deletions of Ahri's log messages can be detected and reported.
    """
    try:
        s = await _get_setting(guild_id)
        ch_id = s.get("log_channel")
        if not ch_id:
            return False
        ch = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
        if not ch:
            return False
        allowed = discord.AllowedMentions.none() if not mention_allowed else discord.AllowedMentions.all()
        sent = await ch.send(content=content, embed=embed, allowed_mentions=allowed, suppress_embeds=False)
        # track if required and the message was authored by this bot and posted into log channel
        if store_sent and sent and sent.author and sent.author.bot and sent.author.id == bot.user.id:
            try:
                title = embed.title if embed else None
                snippet = embed.description[:900] if embed and embed.description else None
                await _store_bot_log_message(sent, title, snippet)
            except Exception:
                # swallow — tracking failure shouldn't break logging
                pass
        return True
    except Exception:
        return False

# ---- Format helpers ----
def _fmt_when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))

def _base_embed(title: str, description: str = "", color: int = 0xE56BFF) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    return e

# ---- Report a tracked bot-sent message deletion ----
async def _report_tracked_bot_message_deletion(bot: "discord.Client", stored: Dict[str, Any], deleter_text: str, matched_via: str = "id"):
    """
    Send a deletion report to the log channel. We call _send_to_channel with store_sent=False
    to avoid tracking the deletion-report message itself.
    """
    try:
        guild_id = stored.get("guild")
        ch_id = stored.get("channel")
        title = stored.get("title") if False else None  # defensive no-op to avoid linter noise
    except Exception:
        guild_id = stored.get("guild") if stored else None
    try:
        title = stored.get("title") if stored else "Log message"
        snippet = stored.get("snippet") if stored else "(no snippet)"
        desc = (
            f"**Logged Message Title:** {title}\n"
            f"**Original Channel:** <#{stored.get('channel')}> (`{stored.get('channel')}`)\n"
            f"**Deleted at:** {_fmt_when(time.time())}\n"
            f"**Deleted by:** {deleter_text}\n\n"
            f"**Original snippet:** {snippet}\n\n"
            f"*matched_via: {matched_via}*"
        )
        embed = _base_embed("Bot Log Message Deleted", desc, color=0xFF5555)
        # ensure the deletion-report does NOT get tracked (store_sent=False)
        await _send_to_channel(bot, guild_id, embed=embed, store_sent=False)
    except Exception:
        # do not crash the logger if reporting fails
        try:
            print("BOTLOG: failed to report tracked bot message deletion", stored)
        except Exception:
            pass

# ---- Event listeners / core logic ----
async def _on_message_delete(bot: "discord.Client", message: discord.Message):
    try:
        if message.guild is None:
            return

        # If the deleted message was authored by our bot (Ahri), let raw_message_delete handle it
        if message.author and message.author.bot and message.author.id == bot.user.id:
            # This is our bot's message - let raw_message_delete handle the logging
            # It has better fallback logic for uncached messages
            return

        # Otherwise behavior for *user* message deletions (original logger behavior)
        if message.author and message.author.bot:
            # some other bot; ignore for user deletions
            return

        s = await _get_setting(message.guild.id)
        cat = s.get("categories", {}).get("message", {})
        if not cat.get("enabled", True) or not cat.get("delete", True):
            return

        desc = f"**Author:** {message.author} (`{message.author.id}`)\n**Channel:** {message.channel.mention} (`{message.channel.id}`)\n"
        content = (message.content or "(no text)")[:1900]
        desc += f"**Content:** {content}\n"
        urls = ", ".join(getattr(a, "url", "") for a in getattr(message, "attachments", [])) or "(no attachments)"
        desc += f"**Attachments:** {urls}"
        embed = _base_embed("Message Deleted", desc)
        # When sending this log entry, it will be stored automatically by _send_to_channel
        await _send_to_channel(bot, message.guild.id, embed=embed)
    except Exception:
        # soft-fail
        return

async def _on_message_edit(bot: "discord.Client", before: discord.Message, after: discord.Message):
    try:
        if before.guild is None:
            return
        if before.author and before.author.bot:
            return

        s = await _get_setting(before.guild.id)
        cat = s.get("categories", {}).get("message", {})
        if not cat.get("enabled", True) or not cat.get("edit", True):
            return

        # If content didn't change (only embeds/attachments), optionally ignore; we log only content changes
        if (before.content or "").strip() == (after.content or "").strip():
            return

        desc = f"**Author:** {before.author} (`{before.author.id}`)\n**Channel:** {before.channel.mention} (`{before.channel.id}`)\n"
        desc += f"**Before:** {(before.content or '(no text)')[:900]}\n**After:** {(after.content or '(no text)')[:900]}"
        embed = _base_embed("Message Edited", desc)
        await _send_to_channel(bot, before.guild.id, embed=embed)
    except Exception:
        return

async def _on_member_join(bot: "discord.Client", member: discord.Member):
    try:
        if member.guild is None:
            return
        s = await _get_setting(member.guild.id)
        cat = s.get("categories", {}).get("server", {})
        if not cat.get("enabled", True) or not cat.get("join", True):
            # still refresh invites cache
            await _refresh_guild_invites_safe(member.guild)
            return

        inviter_info = await _find_inviter_on_join(member.guild)
        inviter_text = "(unknown)"
        if inviter_info:
            inv_id, code = inviter_info
            if inv_id:
                inviter_text = f"<@{inv_id}> (`{inv_id}`) via code `{code}`"
            else:
                inviter_text = f"(unknown inviter) via code `{code}`"

        desc = f"**Member:** {member.mention} (`{member.id}`)\n**Account created:** {_fmt_when(member.created_at.timestamp())}\n**Invited by:** {inviter_text}"
        embed = _base_embed("Member Joined", desc)
        await _send_to_channel(bot, member.guild.id, embed=embed, mention_allowed=False)
    except Exception:
        await _refresh_guild_invites_safe(member.guild)
        return

async def _on_member_remove(bot: "discord.Client", member: discord.Member):
    try:
        if member.guild is None:
            return
        s = await _get_setting(member.guild.id)
        cat = s.get("categories", {}).get("server", {})
        if not cat.get("enabled", True) or not cat.get("leave", True):
            return

        desc = f"**Member:** {member} (`{member.id}`)\n**Left at:** {_fmt_when(time.time())}"
        embed = _base_embed("Member Left", desc)
        await _send_to_channel(bot, member.guild.id, embed=embed)
    except Exception:
        return

async def _on_member_ban(bot: "discord.Client", guild: discord.Guild, user: discord.User):
    try:
        s = await _get_setting(guild.id)
        cat = s.get("categories", {}).get("moderation", {})
        if not cat.get("enabled", True) or not cat.get("ban", True):
            return

        # Try to get audit log info for who banned
        banner = "(unknown)"
        reason = None
        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
                if getattr(entry.target, "id", None) == user.id:
                    banner = f"{entry.user} (`{entry.user.id}`)"
                    reason = entry.reason
                    break
        except Exception:
            pass

        desc = f"**User banned:** {user} (`{user.id}`)\n**By:** {banner}\n"
        if reason:
            desc += f"**Reason:** {reason}"
        embed = _base_embed("User Banned", desc)
        await _send_to_channel(bot, guild.id, embed=embed)
    except Exception:
        return

async def _on_member_kick(bot: "discord.Client", guild: discord.Guild, user: discord.User):
    """There is no direct 'on_member_kick' event. We detect kick via audit logs when a member leaves and there's a recent kick entry."""
    try:
        s = await _get_setting(guild.id)
        cat = s.get("categories", {}).get("moderation", {})
        if not cat.get("enabled", True) or not cat.get("kick", True):
            return

        # try find a recent kick for this user
        kicker = "(unknown)"
        reason = None
        try:
            async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.kick):
                if getattr(entry.target, "id", None) == user.id and (time.time() - entry.created_at.timestamp()) < 30:
                    kicker = f"{entry.user} (`{entry.user.id}`)"
                    reason = entry.reason
                    break
        except Exception:
            pass

        desc = f"**User kicked:** {user} (`{user.id}`)\n**By:** {kicker}\n"
        if reason:
            desc += f"**Reason:** {reason}"
        embed = _base_embed("User Kicked", desc)
        await _send_to_channel(bot, guild.id, embed=embed)
    except Exception:
        return

# Detect timeouts (mutes) via communication_disabled_until field in Member on update
async def _on_member_update(bot: "discord.Client", before: discord.Member, after: discord.Member):
    try:
        if before.guild is None:
            return
        s = await _get_setting(before.guild.id)
        cat = s.get("categories", {}).get("moderation", {})
        if not cat.get("enabled", True) or not cat.get("mute", True):
            return

        be = getattr(before, "communication_disabled_until", None)
        af = getattr(after, "communication_disabled_until", None)

        # if a timeout was newly set
        if (be is None or be.timestamp() if be else None) != (af.timestamp() if af else None):
            # timeout started
            if (be is None or be < af if be else True) and af is not None:
                # member muted (timed out)
                until = af.strftime("%Y-%m-%d %H:%M:%S UTC")
                desc = f"**Member:** {after} (`{after.id}`)\n**Muted until:** {until}"
                embed = _base_embed("Member Timed Out (Mute)", desc)
                await _send_to_channel(bot, before.guild.id, embed=embed)
            # timeout removed
            elif af is None and be is not None:
                desc = f"**Member:** {after} (`{after.id}`)\n**Mute removed**"
                embed = _base_embed("Mute Removed", desc)
                await _send_to_channel(bot, before.guild.id, embed=embed)
    except Exception:
        return

# ---- Registration helper (compat with your loader) ----
def register(bot, key: str, func):
    if not hasattr(bot, "trigger_handlers") or bot.trigger_handlers is None:
        bot.trigger_handlers = {}
    bot.trigger_handlers[key] = func

# ---- Command handler: ahri log ... ----
async def _log_root(bot: "discord.Client", message: discord.Message, args):
    # require guild
    if message.guild is None:
        return

    # allow help
    if not args or args[0].lower() in ("help", "h"):
        # show help + statuses
        try:
            s = await _get_setting(message.guild.id)
            cats = s.get("categories", {})
            lines = [AHRI["help_header"], ""]
            for k, v in cats.items():
                lines.append(f"**{k}** → {'ON' if v.get('enabled', False) else 'OFF'}")
                for ev, state in v.items():
                    if ev == "enabled":
                        continue
                    lines.append(f"  • {ev} → {'ON' if state else 'OFF'}")
            ch = s.get("log_channel")
            lines.append("")
            lines.append(f"Log channel: {f'<#{ch}>' if ch else '(not set)'}")
            await message.channel.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            await message.channel.send(AHRI["error"])
        return

    sub = args[0].lower()

    # setchannel
    if sub == "setchannel":
        # require admin
        if not await permissions.is_guild_admin(message.author, message.guild.id):
            await message.channel.send(AHRI["no_permission"])
            return

        if message.channel_mentions:
            ch = message.channel_mentions[0]
            s = await _ensure_guild_settings(message.guild.id)
            s["log_channel"] = ch.id
            await _save_guild_settings(message.guild.id, s)
            await message.channel.send(AHRI["ok_set"])
            return

        # interactive prompt (D behaviour)
        await message.channel.send(AHRI["setchannel_prompt"])
        try:
            def check(m: discord.Message):
                return m.author.id == message.author.id and m.guild is not None and m.channel is message.channel

            reply = await bot.wait_for("message", check=check, timeout=30.0)
            if reply.channel_mentions:
                ch = reply.channel_mentions[0]
                s = await _ensure_guild_settings(message.guild.id)
                s["log_channel"] = ch.id
                await _save_guild_settings(message.guild.id, s)
                await message.channel.send(AHRI["setchannel_done"])
            else:
                await message.channel.send(AHRI["no_channel_final"])
            return
        except asyncio.TimeoutError:
            await message.channel.send(AHRI["no_channel_final"])
            return
        except Exception:
            await message.channel.send(AHRI["error"])
            return

    # toggle command: usage
    # ahri log toggle <category> [<event>]
    if sub == "toggle":
        # require at least category
        if len(args) < 2:
            await message.channel.send("Usage: `ahri log toggle <category>` or `ahri log toggle <category> <event>`")
            return
        if not await permissions.is_guild_admin(message.author, message.guild.id):
            await message.channel.send(AHRI["no_permission"])
            return

        category = args[1].lower()
        event = args[2].lower() if len(args) >= 3 else None

        # load settings
        s = await _ensure_guild_settings(message.guild.id)
        cats = s.setdefault("categories", {})

        if category not in cats:
            await message.channel.send(f"I don't know that category `{category}`. Try `ahri log help`.")
            return

        # toggle whole category if no event given
        if not event:
            current = bool(cats[category].get("enabled", False))
            cats[category]["enabled"] = not current
            await _save_guild_settings(message.guild.id, s)
            if cats[category]["enabled"]:
                # if turning on but no log channel, interactively ask to set
                if not s.get("log_channel"):
                    await message.channel.send(AHRI["no_channel"])
                    try:
                        def check(m: discord.Message):
                            return m.author.id == message.author.id and m.guild is not None and m.channel is message.channel
                        reply = await bot.wait_for("message", check=check, timeout=30.0)
                        if reply.channel_mentions:
                            ch = reply.channel_mentions[0]
                            s["log_channel"] = ch.id
                            await _save_guild_settings(message.guild.id, s)
                            await message.channel.send(AHRI["ok_set"])
                        else:
                            await message.channel.send(AHRI["no_channel_final"])
                    except asyncio.TimeoutError:
                        await message.channel.send(AHRI["no_channel_final"])
                else:
                    await message.channel.send(AHRI["toggled_on"])
            else:
                await message.channel.send(AHRI["toggled_off"])
            return

        # toggle specific event
        if event not in cats[category]:
            await message.channel.send(f"I don't know that event `{event}` under `{category}`. Try `ahri log help`.")
            return

        current_ev = bool(cats[category].get(event, False))
        cats[category][event] = not current_ev
        await _save_guild_settings(message.guild.id, s)
        # if turning on but category disabled, remind user to enable category
        if cats[category][event] and not cats[category].get("enabled", False):
            await message.channel.send(f"Okay~ `{event}` is ON, but the category `{category}` is currently OFF. Turn the category on with `ahri log toggle {category}`.")
            return
        await message.channel.send(AHRI["toggled_on"] if cats[category][event] else AHRI["toggled_off"])
        return

    await message.channel.send("Unknown subcommand. Try `ahri log help`.")

# ---- Setup entrypoint ----
async def setup(bot: "discord.Client"):
    # register trigger root
    register(bot, "log", _log_root)

    # ensure settings file exists and load defaults for all guilds bot is in
    try:
        # load all existing settings
        _ = await _load_all_settings()
    except Exception:
        pass

    # cache invites for all guilds asynchronously
    for g in list(bot.guilds):
        try:
            await _cache_guild_invites(g)
        except Exception:
            pass

    # Listeners
    @bot.listen("on_message_delete")
    async def __on_message_delete(message: discord.Message):
        await _on_message_delete(bot, message)

    @bot.listen("on_message_edit")
    async def __on_message_edit(before: discord.Message, after: discord.Message):
        await _on_message_edit(bot, before, after)

    @bot.listen("on_member_join")
    async def __on_member_join(member: discord.Member):
        await _on_member_join(bot, member)

    @bot.listen("on_member_remove")
    async def __on_member_remove(member: discord.Member):
        await _on_member_remove(bot, member)

    @bot.listen("on_member_ban")
    async def __on_member_ban(guild: discord.Guild, user: discord.User):
        await _on_member_ban(bot, guild, user)

    # there is no on_member_kick; detect via on_member_remove + audit logs.
    # We'll also hook on_member_remove to try detect kicks via audit log:
    @bot.listen("on_member_remove")
    async def __on_member_remove_for_kick(member: discord.Member):
        # attempt to detect kick immediately
        try:
            guild = member.guild
            # check for recent kick audit log entry
            try:
                async for entry in guild.audit_logs(limit=6, action=discord.AuditLogAction.kick):
                    if getattr(entry.target, "id", None) == member.id and (time.time() - entry.created_at.timestamp()) < 30:
                        # log kick separately
                        await _on_member_kick(bot, guild, member)
                        break
            except Exception:
                pass
        except Exception:
            pass

    @bot.listen("on_member_update")
    async def __on_member_update(before: discord.Member, after: discord.Member):
        await _on_member_update(bot, before, after)

    # Keep invite cache updated on invite create/delete
    @bot.listen("on_invite_create")
    async def __on_inv_create(invite: discord.Invite):
        try:
            async with _CACHE_LOCK:
                gmap = _INVITE_CACHE.setdefault(invite.guild.id, {})
                gmap[invite.code] = {"uses": invite.uses, "inviter": invite.inviter.id if invite.inviter else None}
        except Exception:
            pass

    @bot.listen("on_invite_delete")
    async def __on_inv_delete(invite: discord.Invite):
        try:
            async with _CACHE_LOCK:
                gmap = _INVITE_CACHE.setdefault(invite.guild.id, {})
                if invite.code in gmap:
                    del gmap[invite.code]
        except Exception:
            pass

    # Keep in sync when bot joins a guild
    @bot.listen("on_guild_join")
    async def __on_guild_join(guild: discord.Guild):
        try:
            await _cache_guild_invites(guild)
            # ensure settings exist for the guild
            await _ensure_guild_settings(guild.id)
        except Exception:
            pass

    # When bot is removed from a guild, remove cached invites to save memory
    @bot.listen("on_guild_remove")
    async def __on_guild_remove(guild: discord.Guild):
        try:
            async with _CACHE_LOCK:
                if guild.id in _INVITE_CACHE:
                    del _INVITE_CACHE[guild.id]
        except Exception:
            pass

    # Also handle uncached message deletes so we can detect deletions even when message not in cache
    @bot.listen("on_raw_message_delete")
    async def __on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
        try:
            message_id = getattr(payload, "message_id", None)
            guild_id = getattr(payload, "guild_id", None)
            channel_id = getattr(payload, "channel_id", None)
            if not message_id or not guild_id:
                return

            # If a tracked bot-sent log message was removed (uncached), try to match by id or fallback to recent message in same channel
            stored = await _get_bot_log_message(message_id)
            matched_via = "id"
            if not stored:
                # fallback: find recent tracked message in same channel (within 90s)
                cutoff = time.time() - 90
                candidate_mid = None
                candidate_info = None
                async with _BOT_LOG_LOCK:
                    for mid, info in _BOT_LOG_MSGS.items():
                        try:
                            if info.get("guild") == guild_id and info.get("channel") == channel_id and info.get("sent_at", 0) >= cutoff:
                                if candidate_info is None or info.get("sent_at", 0) > candidate_info.get("sent_at", 0):
                                    candidate_mid = mid
                                    candidate_info = info
                        except Exception:
                            continue
                if candidate_info:
                    stored = candidate_info
                    matched_via = f"fallback_channel_time(mid={candidate_mid})"
                else:
                    # nothing to do
                    return

            # find deleter via audit logs
            deleter_text = "(unknown)"
            try:
                guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
                now_ts = time.time()
                async for entry in guild.audit_logs(limit=30, action=discord.AuditLogAction.message_delete):
                    try:
                        entry_ts = entry.created_at.timestamp()
                    except Exception:
                        entry_ts = None
                    extra = getattr(entry, "extra", None)
                    chan = getattr(extra, "channel", None) if extra else None
                    channel_ok = (chan and getattr(chan, "id", None) == channel_id)
                    if entry_ts and (now_ts - entry_ts) < 25 and channel_ok:
                        user = getattr(entry, "user", None)
                        if user:
                            deleter_text = f"{user} (`{user.id}`)"
                        else:
                            deleter_text = "(unknown)"
                        break
            except Exception:
                pass

            # report and cleanup stored entry
            await _report_tracked_bot_message_deletion(bot, stored, deleter_text, matched_via=matched_via)
            try:
                async with _BOT_LOG_LOCK:
                    if message_id in _BOT_LOG_MSGS:
                        del _BOT_LOG_MSGS[message_id]
                    else:
                        for mid, info in list(_BOT_LOG_MSGS.items()):
                            if info.get("guild") == guild_id and info.get("channel") == channel_id:
                                del _BOT_LOG_MSGS[mid]
                                break
            except Exception:
                pass
        except Exception:
            return

    # Also refresh invite cache periodically in background (light)
    async def _invite_refresh_loop():
        await bot.wait_until_ready()
        while not bot.is_closed():
            try:
                for g in list(bot.guilds):
                    try:
                        await _refresh_guild_invites_safe(g)
                    except Exception:
                        pass
            except Exception:
                pass
            await asyncio.sleep(300)  # refresh every 5 minutes

    bot.loop.create_task(_invite_refresh_loop())

    # graceful shutdown: nothing to close, but ensure settings persist
    return

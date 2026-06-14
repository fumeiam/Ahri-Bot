from __future__ import annotations

import os
import random
import time
import json
import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from dotenv import load_dotenv
from discord.ext import commands  # to properly catch CommandNotFound

from core import db, utils, personality, permissions

AHRI_FEEDBACK_RESPONSES = [
    "Mmm~ that was a little too spicy for here ♥ I’ll be taking it down~",
    "Oh my~ naughty naughty… I’ll clean this up for you ♥",
    "Ehehe~ that one’s a bit too much for the den, let’s keep it safe ♥"
]

# Load environment variables
load_dotenv()

FEATURE_INFO = {
    "name": "nsfw_moderator",
    "triggers": ["nsfw"],
    "description": "Scan images for NSFW content, move/delete/report them, and provide admin trigger commands."
}

# --- Provider interface (Sightengine workflow) ---
class NSFWProvider:
    async def check_image(self, session: aiohttp.ClientSession, url: str, workflow_id: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError()


class SightengineWorkflowProvider(NSFWProvider):
    def __init__(self, api_user: str, api_secret: str, workflow_id: Optional[str]):
        self.api_user = api_user
        self.api_secret = api_secret
        self.default_workflow_id = workflow_id
        self.endpoint = "https://api.sightengine.com/1.0/check-workflow.json"

    async def check_image(self, session: aiohttp.ClientSession, url: str, workflow_id: Optional[str] = None) -> Dict[str, Any]:
        wf = workflow_id or self.default_workflow_id
        if not wf:
            return {"ok": False, "error": "No workflow id configured"}

        params = {
            "url": url,
            "workflow": wf,
            "api_user": self.api_user,
            "api_secret": self.api_secret,
        }
        try:
            async with session.get(self.endpoint, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                # use resp.text() and parse because Sightengine sometimes sends non-json wrappers
                text = await resp.text()
                if resp.status != 200:
                    return {"ok": False, "error": f"Sightengine HTTP {resp.status}: {text[:300]}"}
                try:
                    data = json.loads(text)
                except Exception:
                    return {"ok": False, "error": "Sightengine returned non-JSON"}
                return {"ok": True, "data": data}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Sightengine timeout"}
        except Exception as e:
            return {"ok": False, "error": f"Sightengine error: {e}"}


# --- (legacy) parsing kept for compatibility, no longer used with workflows ---
async def _parse_sightengine_scores(data: Dict[str, Any]) -> Tuple[float, float, str]:
    # Kept intentionally (unused with workflows) to minimize removal of original content
    def _as_float(v: Any) -> float:
        try:
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, dict):
                nums = [float(x) for x in v.values() if isinstance(x, (int, float))]
                return max(nums) if nums else 0.0
            return 0.0
        except Exception:
            return 0.0

    try:
        nud = data.get("nudity", {}) or {}
        sexual_activity = _as_float(nud.get("sexual_activity", 0.0))
        sexual_display = _as_float(nud.get("sexual_display", 0.0))
        erotica = _as_float(nud.get("erotica", 0.0))
        suggestive_raw = data.get("suggestive", nud.get("suggestive", 0.0))
        suggestive = _as_float(suggestive_raw)
        explicit = max(sexual_activity, sexual_display, erotica)

        media_type = "photo"
        t = data.get("type")
        if isinstance(t, dict) and t:
            media_type = max(t, key=lambda k: (t.get(k) if isinstance(t.get(k), (int, float)) else 0.0))
        elif isinstance(t, str):
            media_type = t

        return explicit, suggestive, media_type
    except Exception:
        return 0.0, 0.0, "photo"


# --- module-level session + semaphore ---
_session: Optional[aiohttp.ClientSession] = None
_scan_sem = asyncio.Semaphore(4)


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def _get_env_provider() -> Optional[NSFWProvider]:
    """
    Sightengine Workflows. Expects SIGHTENGINE_USER, SIGHTENGINE_SECRET, SIGHTENGINE_WORKFLOW (optional if set via command).
    """
    se_user = os.getenv("SIGHTENGINE_USER")
    se_secret = os.getenv("SIGHTENGINE_SECRET")
    se_workflow = os.getenv("SIGHTENGINE_WORKFLOW")

    if se_user and se_secret:
        print("Using Sightengine Workflow provider")
        return SightengineWorkflowProvider(se_user, se_secret, se_workflow)

    print("Warning: No NSFW provider configured. Set SIGHTENGINE_USER and SIGHTENGINE_SECRET")
    return None


# --- per-guild config namespace inside core db ---
NSFW_KEY = "nsfw_moderator"


def _ensure_nsfw_cfg(guild_data: Dict[str, Any]) -> Dict[str, Any]:
    part = guild_data.setdefault(NSFW_KEY, {})
    part.setdefault("enabled", True)
    part.setdefault("log_channel_id", None)
    part.setdefault("active_channel_ids", [])
    part.setdefault("whitelist_user_ids", [])
    part.setdefault("blacklist_user_ids", [])
    part.setdefault("whitelist_role_id", None)
    part.setdefault("blacklist_role_id", None)
    part.setdefault("everyone_blacklisted", False)
    # NEW for workflows:
    part.setdefault("workflow_id", os.getenv("SIGHTENGINE_WORKFLOW") or None)
    # NEW: where to move detected NSFW messages (channel id)
    part.setdefault("target_channel_id", None)
    # Mention string (display) + optional user id (for a real ping)
    part.setdefault("review_mention", "@fumeiam")
    part.setdefault("review_mention_id", None)
    part.setdefault("last_updated", None)
    return part


# --- attachment heuristic ---

def _is_image_attachment(att: discord.Attachment) -> bool:
    ct = getattr(att, "content_type", None)
    if ct and ct.startswith("image/"):
        return True
    fn = getattr(att, "filename", "") or ""
    return bool(re.search(r"\.(png|jpe?g|gif|webp)$", fn, re.I))


# --- internal logging to configured log channel (plain text, no embeds) ---
async def _log_action(bot: "discord.Client", guild_id: int, text: str) -> None:
    try:
        g = await db.load_guild(guild_id)
        ns = _ensure_nsfw_cfg(g)
        cid = ns.get("log_channel_id")
        if not cid:
            return
        ch = bot.get_channel(cid)
        if not ch:
            return

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        log_message = f"`[{timestamp}]` {text}"  # keep link only, no embeds
        await ch.send(
            log_message,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress_embeds=True
        )
    except Exception as e:
        try:
            print(f"[NSFW Logger Error] {e}")
        except Exception:
            pass
        return


# --- Helper: move a message into target channel via webhook (preserve author look) and delete original ---
async def _move_message_to_target_and_delete(bot: "discord.Client", message: discord.Message, guild_data: Dict[str, Any]) -> bool:
    """Attempt to repost the entire message (files, embeds, content) into the configured target channel
    using a temporary webhook so it looks like the original author posted there. Then delete the original.

    Returns True if the original message was successfully deleted (moved & deleted), False otherwise.
    """
    try:
        ns = _ensure_nsfw_cfg(guild_data)
        target_id = ns.get("target_channel_id")
        if not target_id:
            return False

        # fetch target channel
        try:
            target_channel = bot.get_channel(target_id) or await bot.fetch_channel(target_id)
        except Exception as e:
            await _log_action(bot, message.guild.id if message.guild else 0, f"⚠️ Failed to fetch target channel {target_id}: {e}")
            return False

        # prepare files and embeds
        files: List[discord.File] = []
        for att in getattr(message, "attachments", []):
            try:
                f = await att.to_file()
                files.append(f)
            except Exception as e:
                await _log_action(bot, message.guild.id, f"⚠️ Failed to download attachment {getattr(att, 'url', '')}: {e}")

        embeds = getattr(message, "embeds", None) or None
        content = message.content or None

        moved = False
        webhook = None
        try:
            # try webhook first (best appearance)
            try:
                # create webhook with a stable name length
                webhook_name = (message.author.display_name or str(message.author))[:80]
                webhook = await target_channel.create_webhook(name=webhook_name, reason="NSFW oversight: moving message for moderation")
                await webhook.send(
                    content=content,
                    username=message.author.display_name if getattr(message.author, 'display_name', None) else str(message.author),
                    avatar_url=getattr(getattr(message.author, 'display_avatar', None), 'url', None),
                    files=files if files else None,
                    embeds=embeds if embeds else None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                moved = True
            except Exception:
                # fallback to bot send if webhook creation or send fails
                await target_channel.send(
                    f"📦 Archived message from {message.author.mention} in {message.channel.mention}:\n{content or ''}",
                    files=files if files else None,
                    embeds=embeds if embeds else None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                moved = True
        finally:
            if webhook:
                try:
                    await webhook.delete()
                except Exception:
                    pass

        # attempt delete original message
        deleted = False
        try:
            await message.delete()
            deleted = True
        except Exception as e:
            await _log_action(bot, message.guild.id, f"❌ Failed to delete original message after move: {e}")
            deleted = False

        # log movement (mirrors previous delete logging but indicates move)
        try:
            urls = ", ".join(getattr(att, "url", "") for att in getattr(message, "attachments", [])) or "(no attachments)"
            await _log_action(bot, message.guild.id, f"🚨 Moved NSFW message from {message.author} in <#{message.channel.id}> to <#{target_id}> (urls={urls}).")
        except Exception:
            pass

        # update timestamp and persist
        ns["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            await db.save_guild(message.guild.id, guild_data)
        except Exception as e:
            await _log_action(bot, message.guild.id, f"❌ Failed to save guild settings after move: {e}")

        return deleted

    except Exception as e:
        try:
            await _log_action(bot, message.guild.id if message.guild else 0, f"❌ Unexpected error in move helper: {e}")
        except Exception:
            pass
        return False

# --- review UI (Approve / Reject) ---
class ReviewView(discord.ui.View):
    def __init__(self, bot: discord.Client, guild_id: int, channel_id: int, message_id: int, img_url: str):
        super().__init__(timeout=3600)
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id = message_id
        self.img_url = img_url

    async def _is_moderator(self, user: discord.User, guild: discord.Guild) -> bool:
        try:
            member = guild.get_member(user.id) or await guild.fetch_member(user.id)
            if member is None:
                return False
            if await permissions.is_guild_admin(member, guild.id):
                return True
            perms = getattr(member, "guild_permissions", None)
            return bool(perms and (perms.manage_messages or perms.administrator))
        except Exception:
            return False

    async def _disable(self, interaction: discord.Interaction, status: str):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        content = f"{interaction.message.content}\n**[{status}]**"
        try:
            await interaction.message.edit(content=content, view=self)
        except Exception:
            pass

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if not guild or not await self._is_moderator(interaction.user, guild):
            await interaction.response.send_message("You don't have permission to moderate this.", ephemeral=True)
            return
        # optionally log approval
        try:
            await _log_action(self.bot, self.guild_id, f"✅ Approved image via review by {interaction.user} in <#{self.channel_id}> (url={self.img_url})")
        except Exception:
            pass
        await self._disable(interaction, f"Approved by {interaction.user.mention}")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if not guild or not await self._is_moderator(interaction.user, guild):
            await interaction.response.send_message("You don't have permission to moderate this.", ephemeral=True)
            return

        # Try move the original message to the configured target and delete it
        deleted = False
        try:
            ch = self.bot.get_channel(self.channel_id) or await self.bot.fetch_channel(self.channel_id)
            msg = await ch.fetch_message(self.message_id)

            # load guild settings and attempt move
            try:
                g = await db.load_guild(self.guild_id)
            except Exception:
                g = {}

            deleted = await _move_message_to_target_and_delete(self.bot, msg, g)

            # Ahri-style feedback in channel (no user mention)
            try:
                response = random.choice(AHRI_FEEDBACK_RESPONSES)
                await ch.send(personality.ahri_say("oops") + f" {response}", delete_after=12)
            except Exception:
                pass

            # _move_message_to_target_and_delete already logs movement; no need to duplicate too much
        except Exception as e:
            await _log_action(self.bot, self.guild_id, f"⚠️ Review reject pressed, but failed to move/delete in <#{self.channel_id}> (url={self.img_url}): {e}")

        await self._disable(interaction, f"Rejected by {interaction.user.mention}{' — moved & deleted' if deleted else ''}")


# --- core scanning routine ---
async def _scan_message(bot: "discord.Client", message: discord.Message, provider: Optional[NSFWProvider]) -> bool:
    if message.guild is None or message.author.bot:
        return False

    try:
        gdata = await db.load_guild(message.guild.id)
    except Exception as e:
        await _log_action(bot, message.guild.id if message.guild else 0, f"❌ Failed to load guild data: {e}")
        return False

    if not gdata.get("activated", False):
        return False

    ns = _ensure_nsfw_cfg(gdata)
    if not ns.get("enabled", True):
        return False

    author_id = message.author.id

    # --- role-based gating (primary), lists as fallback (legacy) ---
    wl_role_id = ns.get("whitelist_role_id")
    bl_role_id = ns.get("blacklist_role_id")
    member: Optional[discord.Member] = message.author if isinstance(message.author, discord.Member) else None

    has_wl_role = False
    has_bl_role = False
    if member:
        if wl_role_id:
            has_wl_role = any(r.id == wl_role_id for r in getattr(member, "roles", []))
        if bl_role_id:
            has_bl_role = any(r.id == bl_role_id for r in getattr(member, "roles", []))

    is_whitelisted = has_wl_role or (author_id in ns.get("whitelist_user_ids", []))
    is_blacklisted = has_bl_role or (author_id in ns.get("blacklist_user_ids", []))

    if is_whitelisted:
        return False

    attachments = [a for a in message.attachments if _is_image_attachment(a)]
    if not attachments:
        return False

    # --- FIX: Strict channel gating ---
    active_channels: List[int] = ns.get("active_channel_ids", []) or []
    # If no monitored channels are configured, do not scan anywhere
    if not active_channels:
        return False
    # Only scan inside configured monitored channels
    if message.channel.id not in active_channels:
        return False

    # Within monitored channels, apply "everyone_blacklisted" switch:
    everyone_blacklisted = ns.get("everyone_blacklisted", False)
    if not everyone_blacklisted and not is_blacklisted:
        # When global lock is OFF, only blacklisted users are scanned in monitored channels
        return False

    if provider is None:
        await _log_action(bot, message.guild.id, "⚠️ Provider not configured; skipping image scan.")
        return False

    session = await _get_session()
    async with _scan_sem:
        for att in attachments:
            try:
                res = await provider.check_image(session, att.url, workflow_id=ns.get("workflow_id"))
                if not res.get("ok"):
                    await _log_action(
                        bot, message.guild.id,
                        f"❌ Scan failed for image `{att.url}` — {res.get('error', 'Unknown error')}`"
                    )
                    continue

                data = res.get("data", {})
                if data.get("status") != "success":
                    await _log_action(
                        bot, message.guild.id,
                        f"❌ Sightengine returned failure for `{att.url}` — {data.get('error') or 'unknown'}`"
                    )
                    continue

                summary = data.get("summary", {}) or {}
                action = str(summary.get("action", "accept")).lower()
                reason_list = summary.get("reject_reason", []) or []
                # Convert reason list to short text
                if isinstance(reason_list, list):
                    reasons_txt = ", ".join((r.get("text") if isinstance(r, dict) else str(r)) for r in reason_list) or "unspecified"
                else:
                    reasons_txt = str(reason_list)

                # Delete immediately if reject -> now move to target channel and then delete original
                if action == "reject":
                    deleted = False
                    try:
                        # Attempt to move message to configured target channel (webhook preserved) and delete
                        moved_deleted = await _move_message_to_target_and_delete(bot, message, gdata)
                        deleted = bool(moved_deleted)
                    except Exception as e:
                        await _log_action(
                            bot, message.guild.id,
                            f"❌ Error moving message for image `{att.url}` in <#{message.channel.id}>: {e}"
                        )

                    # Ahri feedback (channel), independent of logs; no user mention in logs
                    try:
                        response = random.choice(AHRI_FEEDBACK_RESPONSES)
                        await message.channel.send(
                            personality.ahri_say("oops") + f" {response}",
                            delete_after=12
                        )
                    except Exception:
                        pass

                    if deleted:
                        await _log_action(
                            bot, message.guild.id,
                            f"🚨 Moved NSFW image from {message.author} in <#{message.channel.id}> (url={att.url}) to configured target channel."
                        )
                    else:
                        await _log_action(
                            bot, message.guild.id,
                            f"⚠️ Flagged image from {message.author} in <#{message.channel.id}> (url={att.url}, reason={reasons_txt}) — not moved."
                        )

                    ns["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    try:
                        await db.save_guild(message.guild.id, gdata)
                    except Exception as e:
                        await _log_action(bot, message.guild.id, f"❌ Failed to save guild settings: {e}")

                    return deleted

                # Review → send log with buttons + mention reviewer (configurable), DO NOT delete yet
                if action == "review":
                    try:
                        g = await db.load_guild(message.guild.id)
                        ns2 = _ensure_nsfw_cfg(g)
                        cid = ns2.get("log_channel_id")
                        if cid:
                            ch = bot.get_channel(cid)
                            if ch:
                                review_text = (
                                    f"`[{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}]` "
                                    f"🟠 **Review required** for image in <#{message.channel.id}> (url={att.url}, reason={reasons_txt})"
                                )

                                # Build allowed mentions (only the configured reviewer)
                                allowed = discord.AllowedMentions.none()
                                content_prefix = ""
                                mention_id = ns2.get("review_mention_id")
                                if mention_id:
                                    # Real ping
                                    content_prefix = f"<@{int(mention_id)}>\n"
                                    allowed = discord.AllowedMentions(users=[discord.Object(id=int(mention_id))])
                                else:
                                    # Fallback text (won't ping)
                                    content_prefix = f"{ns2.get('review_mention')}\n"

                                view = ReviewView(bot, message.guild.id, message.channel.id, message.id, att.url)
                                await ch.send(content_prefix + review_text, view=view, allowed_mentions=allowed, suppress_embeds=True)
                            else:
                                await _log_action(bot, message.guild.id, f"🟠 Review required in <#{message.channel.id}> (url={att.url}, reason={reasons_txt})")
                        else:
                            await _log_action(bot, message.guild.id, f"🟠 Review required in <#{message.channel.id}> (url={att.url}, reason={reasons_txt})")
                    except Exception as e:
                        await _log_action(bot, message.guild.id, f"❌ Failed to post review log: {e}")
                    return False

                # Accept → do nothing
            except Exception as e:
                await _log_action(
                    bot, message.guild.id,
                    f"❌ Exception while scanning image in <#{message.channel.id}>: {e}"
                )
                continue
    return False


# --- trigger handler registration helper (matches other features) ---
def register(bot, key: str, func):
    # ensure dict exists to avoid AttributeError
    if not hasattr(bot, "trigger_handlers") or bot.trigger_handlers is None:
        bot.trigger_handlers = {}
    bot.trigger_handlers[key] = func


# --- the main setup entrypoint called by loader ---
async def setup(bot: "discord.Client"):
    provider = _get_env_provider()

    # --- helpers that need bot closure context ---
    async def _add_role(member: discord.Member, role_id: Optional[int]) -> bool:
        if not role_id:
            return False
        role = member.guild.get_role(role_id)
        if not role:
            await _log_action(bot, member.guild.id, f"⚠️ Configured role {role_id} not found.")
            return False
        try:
            await member.add_roles(role, reason="NSFW moderator: assign role")
            return True
        except Exception as e:
            await _log_action(bot, member.guild.id, f"❌ Failed to add role {role} to {member}: {e}")
            return False

    async def _remove_role(member: discord.Member, role_id: Optional[int]) -> bool:
        if not role_id:
            return False
        role = member.guild.get_role(role_id)
        if not role:
            return False
        try:
            await member.remove_roles(role, reason="NSFW moderator: remove role")
            return True
        except Exception as e:
            await _log_action(bot, member.guild.id, f"❌ Failed to remove role {role} from {member}: {e}")
            return False

    # ---- message listener ----
    @bot.listen("on_message")
    async def _on_message_listener(message: discord.Message):
        try:
            if message.author.bot or message.guild is None:
                return
            gdata = await db.load_guild(message.guild.id)
            if not gdata.get("activated", False):
                return
            await _scan_message(bot, message, provider)
        except commands.CommandNotFound:
            # suppress CommandNotFound console spam
            return
        except Exception as e:
            try:
                await _log_action(bot, message.guild.id if message.guild else 0, f"❌ on_message error: {e}")
            except Exception:
                pass
            return

    # --- suppress "CommandNotFound: Command 'nsfw' is not found" globally ---
    @bot.listen("on_command_error")
    async def _ignore_cmd_not_found(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return

    # ---- trigger root: ahri nsfw <sub> ----
    async def nsfw_root(bot: "discord.Client", message: discord.Message, args: List[str]):
        if message.guild is None:
            return

        if not args:
            await message.channel.send("Usage: `ahri nsfw help` or `ahri nsfw <subcommand>`")
            return

        sub = args[0].lower()
        gdata = await db.load_guild(message.guild.id)
        ns = _ensure_nsfw_cfg(gdata)

        async def _save_and_ack(text: str, suppress=False):
            ns["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            await db.save_guild(message.guild.id, gdata)
            await message.channel.send(
                personality.ahri_say("done") + " " + text,
                allowed_mentions=discord.AllowedMentions.none() if suppress else discord.AllowedMentions.all()
            )

        admin_subs = {
            "help", "h",
            "enable", "on", "disable", "off",
            "setlogchannel",
            "addchannel", "monitor", "removechannel", "unmonitor",
            "whitelist", "unwhitelist", "allow", "unallow",
            "blacklist", "unblacklist", "watch", "unwatch",
            "toggleglobal", "globallock",
            "viewsettings", "settings",
            "viewwhitelist", "viewblacklist",
            # NEW role-based admin commands:
            "setwhitelistrole", "setblacklistrole",
            "clearwhitelistrole", "clearblacklistrole",
            # NEW workflow/review controls:
            "setreviewmention", "clearreviewmention",
            # NEW target channel controls:
            "settarget", "cleartarget",
        }
        if sub in admin_subs:
            if not await permissions.is_guild_admin(message.author, message.guild.id):
                await message.channel.send(personality.ahri_say("no_permission"))
                return

        # HELP
        if sub in ("help", "h"):
            help_text = """**Ahri NSFW Moderator — admin commands**
`ahri nsfw help` — show this help

`ahri nsfw enable|disable` — toggle scanning
`ahri nsfw setlogchannel #channel` — where logs are sent
`ahri nsfw setreviewmention @user` — who to ping for REVIEW cases (or `ahri nsfw clearreviewmention`)
`ahri nsfw addchannel #channel` / `ahri nsfw removechannel #channel`
`ahri nsfw setwhitelistrole @role` / `ahri nsfw setblacklistrole @role` — configure special roles
`ahri nsfw clearwhitelistrole` / `ahri nsfw clearblacklistrole` — unset special roles
`ahri nsfw whitelist @user` / `ahri nsfw unwhitelist @user`
`ahri nsfw blacklist @user` / `ahri nsfw unblacklist @user`
`ahri nsfw toggleglobal` — treat everyone as blacklisted in monitored channels (whitelist still bypasses)
`ahri nsfw viewsettings` — view current settings (mentions suppressed)
`ahri nsfw viewwhitelist` — show whitelisted users
`ahri nsfw viewblacklist` — show blacklisted users
`ahri nsfw settarget #channel` — set the channel where detected NSFW messages will be moved
`ahri nsfw cleartarget` — clear the target channel
"""
            await message.channel.send(help_text)
            return

        # enable/disable
        if sub in ("enable", "on"):
            ns["enabled"] = True
            await _save_and_ack("NSFW scanning enabled.")
            return
        if sub in ("disable", "off"):
            ns["enabled"] = False
            await _save_and_ack("NSFW scanning disabled.")
            return

        # setlogchannel
        if sub == "setlogchannel":
            if not message.channel_mentions:
                await message.channel.send("Mention the channel: `ahri nsfw setlogchannel #logs`")
                return
            ch = message.channel_mentions[0]
            ns["log_channel_id"] = ch.id
            await _save_and_ack(f"Logging to {ch.mention}.")
            return

        # NEW: setreviewmention / clearreviewmention
        if sub == "setreviewmention":
            # Prefer a real user mention for proper ping
            if message.mentions:
                u = message.mentions[0]
                ns["review_mention_id"] = int(u.id)
                ns["review_mention"] = f"<@{u.id}>"
                await _save_and_ack(f"Review mention set to {u.mention}.")
                return
            # Else accept a bare string (won't ping)
            if len(args) >= 2:
                ns["review_mention_id"] = None
                ns["review_mention"] = " ".join(args[1:])
                await _save_and_ack(f"Review mention text set to `{ns['review_mention']}` (note: this will not ping).", suppress=True)
                return
            await message.channel.send("Usage: `ahri nsfw setreviewmention @user` or `ahri nsfw setreviewmention some text`")
            return

        if sub == "clearreviewmention":
            ns["review_mention_id"] = None
            ns["review_mention"] = "@fumeiam"
            await _save_and_ack("Review mention cleared (defaulting to `@fumeiam` text; will not ping).", suppress=True)
            return

        # addchannel / removechannel
        if sub in ("addchannel", "monitor"):
            if not message.channel_mentions:
                await message.channel.send("Mention the channel to monitor: `ahri nsfw addchannel #channel`")
                return
            ch = message.channel_mentions[0]
            if ch.id not in ns.get("active_channel_ids", []):
                ns["active_channel_ids"].append(ch.id)
                await _save_and_ack(f"Monitoring {ch.mention}.")
            else:
                await message.channel.send(f"I'm already watching {ch.mention}~")
            return

        if sub in ("removechannel", "unmonitor"):
            if not message.channel_mentions:
                await message.channel.send("Mention the channel to stop: `ahri nsfw removechannel #channel`")
                return
            ch = message.channel_mentions[0]
            if ch.id in ns.get("active_channel_ids", []):
                ns["active_channel_ids"].remove(ch.id)
                await _save_and_ack(f"Stopped monitoring {ch.mention}.")
            else:
                await message.channel.send(f"I wasn't watching {ch.mention}~")
            return

        # --- NEW: set/clear whitelist/blacklist roles (one of each) ---
        if sub == "setwhitelistrole":
            if not message.role_mentions:
                await message.channel.send("Mention the role: `ahri nsfw setwhitelistrole @role`")
                return
            r = message.role_mentions[0]
            ns["whitelist_role_id"] = r.id
            await _save_and_ack(f"Whitelist role set to <@&{r.id}>", suppress=True)
            return

        if sub == "setblacklistrole":
            if not message.role_mentions:
                await message.channel.send("Mention the role: `ahri nsfw setblacklistrole @role`")
                return
            r = message.role_mentions[0]
            ns["blacklist_role_id"] = r.id
            await _save_and_ack(f"Blacklist role set to <@&{r.id}>", suppress=True)
            return

        if sub == "clearwhitelistrole":
            ns["whitelist_role_id"] = None
            await _save_and_ack("Whitelist role cleared.")
            return

        if sub == "clearblacklistrole":
            ns["blacklist_role_id"] = None
            await _save_and_ack("Blacklist role cleared.")
            return

        # whitelist / unwhitelist (assign/remove role and keep lists in sync)
        if sub in ("whitelist", "allow"):
            if not message.mentions:
                await message.channel.send("Mention the user to whitelist: `ahri nsfw whitelist @user`")
                return
            u = message.mentions[0]
            if u.id not in ns.get("whitelist_user_ids", []):
                ns["whitelist_user_ids"].append(u.id)
            if u.id in ns.get("blacklist_user_ids", []):
                ns["blacklist_user_ids"].remove(u.id)

            if isinstance(u, discord.Member):
                await _remove_role(u, ns.get("blacklist_role_id"))
                added = await _add_role(u, ns.get("whitelist_role_id"))
                suffix = " (role assigned)" if added else ""
            else:
                suffix = ""

            await _save_and_ack(f"{u.mention} can bypass scans.{suffix}", suppress=True)
            return

        if sub in ("unwhitelist", "unallow"):
            if not message.mentions:
                await message.channel.send("Mention the user to remove from whitelist.")
                return
            u = message.mentions[0]
            if u.id in ns.get("whitelist_user_ids", []):
                ns["whitelist_user_ids"].remove(u.id)
            if isinstance(u, discord.Member):
                await _remove_role(u, ns.get("whitelist_role_id"))
            await _save_and_ack(f"{u.mention} removed from whitelist.", suppress=True)
            return

        # blacklist / unblacklist (assign/remove role and keep lists in sync)
        if sub in ("blacklist", "watch"):
            if not message.mentions:
                await message.channel.send("Mention the user to blacklist: `ahri nsfw blacklist @user`")
                return
            u = message.mentions[0]
            if u.id not in ns.get("blacklist_user_ids", []):
                ns["blacklist_user_ids"].append(u.id)
            if u.id in ns.get("whitelist_user_ids", []):
                ns["whitelist_user_ids"].remove(u.id)

            if isinstance(u, discord.Member):
                await _remove_role(u, ns.get("whitelist_role_id"))
                added = await _add_role(u, ns.get("blacklist_role_id"))
                suffix = " (role assigned)" if added else ""
            else:
                suffix = ""

            await _save_and_ack(f"{u.mention} added to watchlist.{suffix}", suppress=True)
            return

        if sub in ("unblacklist", "unwatch"):
            if not message.mentions:
                await message.channel.send("Mention the user to remove from blacklist.")
                return
            u = message.mentions[0]
            if u.id in ns.get("blacklist_user_ids", []):
                ns["blacklist_user_ids"].remove(u.id)
            if isinstance(u, discord.Member):
                await _remove_role(u, ns.get("blacklist_role_id"))
            await _save_and_ack(f"{u.mention} removed from watchlist.", suppress=True)
            return

        # toggleglobal
        if sub in ("toggleglobal", "globallock"):
            ns["everyone_blacklisted"] = not ns.get("everyone_blacklisted", False)
            state = "ENABLED (monitored channels only) 🔒" if ns["everyone_blacklisted"] else "DISABLED 🔓"
            await _save_and_ack(f"Global 'everyone blacklisted' is now {state}")
            return

        # viewsettings (mentions suppressed)
        if sub in ("viewsettings", "settings"):
            monitored = ", ".join(f"<#{c}>" for c in ns.get("active_channel_ids", [])) or "(none)"
            logc = f"<#{ns['log_channel_id']}>" if ns.get("log_channel_id") else "(not set)"

            wl_role_id = ns.get("whitelist_role_id")
            bl_role_id = ns.get("blacklist_role_id")
            wl_role_str = f"<@&{wl_role_id}>" if wl_role_id else "(not set)"
            bl_role_str = f"<@&{bl_role_id}>" if bl_role_id else "(not set)"

            whitelist = " ".join(f"<@{uid}>" for uid in ns.get("whitelist_user_ids", [])) or "(empty)"
            blacklist = " ".join(f"<@{uid}>" for uid in ns.get("blacklist_user_ids", [])) or "(empty)"
            last_updated = ns.get("last_updated") or "(never)"
            wf = ns.get("workflow_id") or "(not set)"
            review_m = ns.get("review_mention")
            review_m_id = ns.get("review_mention_id")
            review_str = f"<@{review_m_id}>" if review_m_id else f"{review_m} (text only)"
            target_str = f"<#{ns.get('target_channel_id')}>" if ns.get('target_channel_id') else "(not set)"

            settings_text = f"""Enabled: {'YES' if ns.get('enabled', True) else 'NO'}
Log channel: {logc}
Workflow ID: `{wf}`
Target channel: {target_str}
Monitored: {monitored}
Global everyone-blacklisted: {'ON (monitored only)' if ns.get('everyone_blacklisted') else 'OFF'}
Roles → Whitelist: {wl_role_str} | Blacklist: {bl_role_str}
Review mention: {review_str}
Whitelist IDs: {whitelist}
Blacklist IDs: {blacklist}
Last updated: {last_updated}"""

            await message.channel.send(settings_text, allowed_mentions=discord.AllowedMentions.none())
            return

        # viewwhitelist (mentions suppressed)
        if sub == "viewwhitelist":
            wl = ns.get("whitelist_user_ids", [])
            if not wl:
                await message.channel.send("Whitelist is empty.")
            else:
                users = " ".join(f"<@{uid}>" for uid in wl)
                await message.channel.send(f"Whitelisted users: {users}", allowed_mentions=discord.AllowedMentions.none())
            return

        # viewblacklist (mentions suppressed)
        if sub == "viewblacklist":
            bl = ns.get("blacklist_user_ids", [])
            if not bl:
                await message.channel.send("Blacklist is empty.")
            else:
                users = " ".join(f"<@{uid}>" for uid in bl)
                await message.channel.send(f"Blacklisted users: {users}", allowed_mentions=discord.AllowedMentions.none())
            return

        # NEW: settarget / cleartarget
        if sub == "settarget":
            if not message.channel_mentions:
                await message.channel.send("Mention the channel to move NSFW messages to: `ahri nsfw settarget #channel`")
                return
            ch = message.channel_mentions[0]
            ns["target_channel_id"] = ch.id
            await _save_and_ack(f"NSFW target channel set to {ch.mention}.")
            return

        if sub == "cleartarget":
            ns["target_channel_id"] = None
            await _save_and_ack("NSFW target channel cleared.")
            return

        await message.channel.send("I don't recognize that subcommand. Try `ahri nsfw help`.")

    # register handler
    register(bot, "nsfw", nsfw_root)

    # --- graceful aiohttp session cleanup on bot shutdown ---
    original_close = bot.close

    async def wrapped_close():
        global _session
        if _session and not _session.closed:
            try:
                await _session.close()
            except Exception:
                pass
            _session = None
        await original_close()

    bot.close = wrapped_close
    return

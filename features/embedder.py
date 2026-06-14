# features/instagram_x_embedder.py
import re
import time
import asyncio
import logging
from typing import List, Dict, Optional, Tuple

import discord

from core import db, permissions, personality, utils  # utils used for tokenize in command handlers if needed

LOG = logging.getLogger("InstagramXEmbedder")

# Match any instagram.com URL (posts, reels, stories, profiles, etc.)
INSTAGRAM_PATTERN = re.compile(
    r'https?://(?:www\.)?instagram\.com[^\s<>"]+',
    re.IGNORECASE,
)

# Match X/twitter status/permalink links and general x.com links
X_PATTERN = re.compile(
    r'https?://(?:(?:www|m)\.)?(?:x\.com|twitter\.com)[^\s<>"]+',
    re.IGNORECASE,
)

# Defaults for rate limiting / spam prevention
MAX_LINKS_PER_MESSAGE = 5                # ignore messages with > this many matched links
MAX_ACTIONS_PER_USER_PER_MIN = 6         # how many embed fixups a user may cause per minute (per guild)
ACTION_WINDOW_SECONDS = 60               # window size for the above limit
GLOBAL_MESSAGE_COOLDOWN = 1.0            # seconds between bot sends in the same channel (throttle)

# Feature names used for toggles
FEATURE_INSTA = "instaembedder"
FEATURE_X = "xembedder"

# In-memory storage for feature states (cache)
# Structure: { guild_id: {FEATURE_INSTA: bool, FEATURE_X: bool} }
_feature_state: Dict[int, Dict[str, bool]] = {}
_feature_lock = asyncio.Lock()

# Per-guild per-user rate limiter: { guild_id: { user_id: [timestamps...] } }
_rate_limit: Dict[int, Dict[int, List[float]]] = {}
_rate_limit_lock = asyncio.Lock()

# Channel-level last send time for global throttle
_channel_last_send: Dict[int, float] = {}
_channel_lock = asyncio.Lock()

# Missing-permission warn cache to avoid repeating warnings every message:
# { (guild_id, feature): last_warn_ts }
_missing_perm_warned: Dict[Tuple[int, str], float] = {}
_MISSING_PERM_WARN_TTL = 300.0  # seconds to suppress repeated "missing perms" warnings

# Whether to send a small ephemeral success confirmation after replacing links.
SEND_SUCCESS_CONFIRMATION = False

# Timeouts / delays (tunable)
# When reel: wait this long AFTER message.edit(suppress=True) before posting modified link(s)
REEL_POST_DELAY_AFTER_SUPPRESS = 1.5  # seconds (you requested 1.5s)
# After posting modified link(s), wait this long and check if Discord created an embed
CHECK_EMBED_DELAY_AFTER_POST = 1.5  # seconds (you requested check after 1.5s)

# ---- Persistence helpers (top-level guild keys) ----
async def _get_guild_feature_state(guild_id: int, feature: str) -> bool:
    """
    Read feature state from in-memory cache; if missing, try to load guild record and read
    the top-level feature key (e.g. g['instaembedder']). Default is DISABLED (False).
    """
    async with _feature_lock:
        guild_map = _feature_state.setdefault(guild_id, {})
        if feature in guild_map:
            return bool(guild_map[feature])

    # Not cached: try DB load
    try:
        if hasattr(db, "load_guild"):
            g = await db.load_guild(guild_id)
            if isinstance(g, dict) and feature in g:
                val = bool(g.get(feature))
                async with _feature_lock:
                    _feature_state.setdefault(guild_id, {})[feature] = val
                return val
    except Exception:
        LOG.exception("DB read failed while getting feature %s for guild %s", feature, guild_id)

    # Default: disabled
    async with _feature_lock:
        _feature_state.setdefault(guild_id, {})[feature] = False
    return False

async def _set_guild_feature_state(guild_id: int, feature: str, enabled: bool):
    """
    Update in-memory cache and persist to DB if possible.
    Persist strategy (best-effort):
      1) If db.set_feature_state exists, call it.
      2) Else if db.load_guild + db.save_guild exists, load -> modify top-level key -> save.
      3) Else try to set g['features'] dict and save if possible.
    Failures are logged but do not raise.
    """
    async with _feature_lock:
        _feature_state.setdefault(guild_id, {})[feature] = bool(enabled)

    try:
        if hasattr(db, "set_feature_state"):
            await db.set_feature_state(guild_id, feature, bool(enabled))
            return

        if hasattr(db, "load_guild") and hasattr(db, "save_guild"):
            g = await db.load_guild(guild_id)
            if not isinstance(g, dict):
                g = {}
            g[feature] = bool(enabled)
            await db.save_guild(guild_id, g)
            return

        if hasattr(db, "load_guild"):
            g = await db.load_guild(guild_id)
            if isinstance(g, dict):
                features = g.get("features", {})
                if not isinstance(features, dict):
                    features = {}
                features[feature] = bool(enabled)
                g["features"] = features
                if hasattr(db, "save_guild"):
                    await db.save_guild(guild_id, g)
            return
    except Exception:
        LOG.exception("Failed to persist feature state %s=%s for guild %s", feature, enabled, guild_id)

# ---- URL helpers ----
def to_kkinstagram(url: str) -> str:
    """
    Convert any instagram.com URL to kkinstagram.com and strip query string
    """
    try:
        new = re.sub(r'(?i)^https?://(?:www\.)?instagram\.com', 'https://kkinstagram.com', url, count=1)
        new = re.split(r'[?#]', new, maxsplit=1)[0]
        return new
    except Exception:
        LOG.exception("to_kkinstagram failed for url: %s", url)
        return url

def to_fixupx(url: str) -> str:
    try:
        m = re.match(r'(?i)^https?://(?:www\.|m\.)?[^/]+(/.*)?', url)
        path = m.group(1) if m else ""
        path = re.split(r'[?#]', path, maxsplit=1)[0]
        prox = f"https://fixupx.com{path}"
        return prox
    except Exception:
        LOG.exception("to_fixupx failed for url: %s", url)
        return url

# ---- rate / throttle helpers ----
async def _record_action_and_check_rate(guild_id: int, user_id: int) -> bool:
    now = time.time()
    async with _rate_limit_lock:
        guild_entry = _rate_limit.setdefault(guild_id, {})
        user_list = guild_entry.setdefault(user_id, [])
        cutoff = now - ACTION_WINDOW_SECONDS
        while user_list and user_list[0] < cutoff:
            user_list.pop(0)
        if len(user_list) >= MAX_ACTIONS_PER_USER_PER_MIN:
            return False
        user_list.append(now)
        return True

async def _throttle_channel_send(channel_id: int) -> bool:
    now = time.time()
    async with _channel_lock:
        last = _channel_last_send.get(channel_id, 0.0)
        if now - last < GLOBAL_MESSAGE_COOLDOWN:
            return False
        _channel_last_send[channel_id] = now
        return True

# ---- Helpers for Ahri-style feedback and permission warnings ----
async def _safe_send(channel: discord.abc.Messageable, key: Optional[str] = None, fallback: Optional[str] = None, delete_after: Optional[float] = None, **kwargs):
    msg_text = None
    if key:
        try:
            msg_text = personality.ahri_say(key, **kwargs)
        except Exception:
            LOG.debug("personality.ahri_say failed for key=%s; falling back", key)
    if not msg_text:
        msg_text = fallback
    if not msg_text:
        return
    try:
        await channel.send(msg_text, delete_after=delete_after)
    except Exception:
        LOG.exception("Failed to send user-facing message for key=%s", key)

def _should_warn_missing_perm(guild_id: int, feature: str) -> bool:
    now = time.time()
    key = (guild_id, feature)
    last = _missing_perm_warned.get(key, 0.0)
    if now - last > _MISSING_PERM_WARN_TTL:
        _missing_perm_warned[key] = now
        return True
    return False

# ---- Main embedder class ----
class InstagramXEmbedder:
    def __init__(self, bot: discord.Client):
        self.bot = bot

    @staticmethod
    def _extract_instagram_urls(text: str) -> List[str]:
        return [m.group(0) for m in INSTAGRAM_PATTERN.finditer(text or "")]

    @staticmethod
    def _extract_x_urls(text: str) -> List[str]:
        return [m.group(0) for m in X_PATTERN.finditer(text or "")]

    async def _process_instagram(self, message: discord.Message, urls: List[str]) -> None:
        try:
            guild_id = message.guild.id
            # check per-guild activation (main bot activation)
            g = await db.load_guild(guild_id)
            if not g.get("activated", False):
                return

            # check feature toggle (persistent)
            if not await _get_guild_feature_state(guild_id, FEATURE_INSTA):
                return

            # simple spam control: ignore messages with too many links
            if len(urls) > MAX_LINKS_PER_MESSAGE:
                LOG.info("Ignoring instagram message with %d links from %s in guild %s", len(urls), message.author.id, guild_id)
                await _safe_send(
                    message.channel,
                    key="too_many_links",
                    fallback=f"Mmm~ that's a lot of Instagram links, cutie. I can only handle {MAX_LINKS_PER_MESSAGE} at once ♥",
                    delete_after=8
                )
                return

            # rate limit per-user
            allowed = await _record_action_and_check_rate(guild_id, message.author.id)
            if not allowed:
                LOG.info("Rate-limited instagram actions for user %s in guild %s", message.author.id, guild_id)
                await _safe_send(
                    message.channel,
                    key="rate_limited",
                    fallback="Mmm~ slow down, darling. One link at a time for me ♥",
                    delete_after=8
                )
                return

            # prepare proxies
            proxied = [to_kkinstagram(u) for u in urls]

            # Detect profile links and ignore them (no preview exists)
            # Profile pattern on proxied domain: https://kkinstagram.com/<username> or trailing slash
            profile_pattern = re.compile(r'^https?://(?:www\.)?kkinstagram\.com/[^/]+/?$', re.IGNORECASE)
            non_profile = [u for u in proxied if not profile_pattern.match(u)]
            if not non_profile:
                LOG.info("Only instagram profile links found in guild %s; ignoring", guild_id)
                return

            # classify reels vs others (apply delay only to reels)
            reel_pattern = re.compile(r'/reel(s)?/', re.IGNORECASE)
            reel_links = [u for u in non_profile if reel_pattern.search(u)]
            other_links = [u for u in non_profile if u not in reel_links]

            # disguised markdown links (kept as original)
            disguised_links = [f"[instagram]({p})" for p in (reel_links + other_links)]

            # attempt to suppress original embed (may fail if missing perms)
            suppressed = False
            try:
                await message.edit(suppress=True)
                suppressed = True
            except discord.Forbidden:
                LOG.info("Missing Manage Messages permission to suppress embed in guild %s", guild_id)
                if _should_warn_missing_perm(guild_id, FEATURE_INSTA):
                    await _safe_send(
                        message.channel,
                        key="missing_manage_messages_insta",
                        fallback="Tsk… I can't hide that preview without a little more power. Grant me *Manage Messages*, won't you?~",
                        delete_after=20
                    )
            except Exception:
                LOG.exception("Failed to suppress instagram embed")

            # throttle channel sends
            if not await _throttle_channel_send(message.channel.id):
                LOG.info("Channel %s throttled; not sending instagram replacement", message.channel.id)
                return

            # If there are reels, apply the reel-specific timings:
            #  - wait REEL_POST_DELAY_AFTER_SUPPRESS after suppress (only if suppression succeeded)
            #  - post markdown disguised links
            #  - wait CHECK_EMBED_DELAY_AFTER_POST and check embed; if none, post raw proxied links (no markdown)
            try:
                if reel_links and suppressed:
                    # wait for Discord to process suppression before posting the modified markdown links
                    await asyncio.sleep(REEL_POST_DELAY_AFTER_SUPPRESS)

                # send markdown disguised links (for both reels and non-reel cases)
                sent_msg = await message.channel.send("\n".join(disguised_links))

                # If there were any reel links, check after a delay whether Discord created embeds;
                # if not, post raw proxied links (no markdown) so Discord can unfurl them.
                if reel_links:
                    await asyncio.sleep(CHECK_EMBED_DELAY_AFTER_POST)
                    try:
                        fetched = await message.channel.fetch_message(sent_msg.id)
                        has_embed = bool(getattr(fetched, "embeds", None))
                    except Exception:
                        # fallback to checking sent_msg.embeds if fetch fails
                        has_embed = bool(getattr(sent_msg, "embeds", None))

                    if not has_embed:
                        # send raw proxied links (one per line), not markdown
                        try:
                            await message.channel.send("\n".join(reel_links + other_links))
                        except Exception:
                            LOG.exception("Failed to send raw proxied links as fallback for instagram reels")
                else:
                    # no reels: behave exactly as original (no special checking)
                    pass

                # optional cute confirmation (delete after short time)
                if SEND_SUCCESS_CONFIRMATION:
                    await _safe_send(
                        message.channel,
                        key="fix_success_instagram",
                        fallback="Here you go~ a prettier Instagram link, just for you ♥",
                        delete_after=6
                    )
            except Exception:
                LOG.exception("Failed to send instagram replacement message")
                await _safe_send(
                    message.channel,
                    key="send_failed_instagram",
                    fallback="Mmm… something went wrong hiding that Instagram link. Forgive me, fox’s honor~ 🦊",
                    delete_after=10
                )
        except Exception:
            LOG.exception("Unhandled exception in _process_instagram")

    async def _process_x(self, message: discord.Message, urls: List[str]) -> None:
        try:
            guild_id = message.guild.id
            # check global activation
            g = await db.load_guild(guild_id)
            if not g.get("activated", False):
                return

            # check per-feature toggle
            if not await _get_guild_feature_state(guild_id, FEATURE_X):
                return

            if len(urls) > MAX_LINKS_PER_MESSAGE:
                LOG.info("Ignoring x message with %d links from %s in guild %s", len(urls), message.author.id, guild_id)
                await _safe_send(
                    message.channel,
                    key="too_many_links_x",
                    fallback=f"Ooh~ that's a lot of X links. I can only handle {MAX_LINKS_PER_MESSAGE} at once, sweetie ♥",
                    delete_after=8
                )
                return

            allowed = await _record_action_and_check_rate(guild_id, message.author.id)
            if not allowed:
                LOG.info("Rate-limited x actions for user %s in guild %s", message.author.id, guild_id)
                await _safe_send(
                    message.channel,
                    key="rate_limited",
                    fallback="Ehe~ slow down, darling. Let me catch my breath before more links ♥",
                    delete_after=8
                )
                return

            proxy_urls = [to_fixupx(u) for u in urls]
            disguised_links = [f"[x]({p})" for p in proxy_urls]

            # attempt to suppress any built-in embed (if twitter/x embed exists)
            try:
                await message.edit(suppress=True)
            except discord.Forbidden:
                LOG.info("Missing Manage Messages permission to suppress embed in guild %s", guild_id)
                if _should_warn_missing_perm(guild_id, FEATURE_X):
                    await _safe_send(
                        message.channel,
                        key="missing_manage_messages_x",
                        fallback="💕 Oops~ I can't silence that X preview without *Manage Messages*. Help me out, sweet thing?~",
                        delete_after=20
                    )
            except Exception:
                LOG.exception("Failed to suppress x embed")

            if not await _throttle_channel_send(message.channel.id):
                LOG.info("Channel %s throttled; not sending x replacement", message.channel.id)
                return

            try:
                # send clean X links (no extra prefix)
                await message.channel.send("\n".join(disguised_links))
                if SEND_SUCCESS_CONFIRMATION:
                    await _safe_send(
                        message.channel,
                        key="fix_success_x",
                        fallback="Here you go~ a prettier X link, just for you ♥",
                        delete_after=6
                    )
            except Exception:
                LOG.exception("Failed to send x replacement message")
                await _safe_send(
                    message.channel,
                    key="send_failed_x",
                    fallback="Ah~ that X link wriggled free of my charm… try again later, darling. 🌸",
                    delete_after=10
                )
        except Exception:
            LOG.exception("Unhandled exception in _process_x")

    async def handle_message(self, message: discord.Message):
        """
        Main message entrypoint registered as an on_message listener.
        This method is defensive: it will log and swallow exceptions so the main bot won't crash.
        """
        try:
            if message.author.bot or message.guild is None:
                return

            # never trigger on our own proxied domains to avoid loops
            lc = (message.content or "").lower()
            if "kkinstagram.com" in lc or "fixupx.com" in lc:
                return

            # Extract both kinds of links
            instagram_urls = self._extract_instagram_urls(message.content)
            x_urls = self._extract_x_urls(message.content)

            # If both present, handle both but in separate safe coroutines
            tasks = []
            if instagram_urls:
                tasks.append(self._process_instagram(message, instagram_urls))
            if x_urls:
                tasks.append(self._process_x(message, x_urls))

            if tasks:
                # run but don't let exceptions bubble
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception:
            LOG.exception("Unhandled exception in handle_message (top-level)")

# ---- Single-root embedder handler with subcommands ----
async def _embedder_handler(bot, message: discord.Message, tokens: List[str]):
    """
    Usage:
      ahri embedder instaenable
      ahri embedder instadisable
      ahri embedder xenable
      ahri embedder xdisable
      ahri embedder enable
      ahri embedder disable
      ahri embedder help
    """
    try:
        if not tokens:
            await message.channel.send(personality.ahri_say("unknown_trigger", cmd=""))
            return

        sub = tokens.pop(0).lower()

        # help
        if sub == "help":
            # Get current status of features
            insta_status = await _get_guild_feature_state(message.guild.id, FEATURE_INSTA)
            x_status = await _get_guild_feature_state(message.guild.id, FEATURE_X)
            
            lines = ["✨ **Embedder Help** ✨"]
            lines.append("**Current Status:**")
            lines.append(f"- Instagram Embedder: {'✅ Enabled' if insta_status else '❌ Disabled'}")
            lines.append(f"- X Embedder: {'✅ Enabled' if x_status else '❌ Disabled'}")
            lines.append("")
            lines.append("**Subcommands:**")
            lines.append("- `ahri embedder instaenable` / `instadisable`")
            lines.append("- `ahri embedder xenable` / `xdisable`")
            lines.append("- `ahri embedder enable` / `disable` (toggle both)")
            lines.append("- `ahri embedder help`")
            await message.channel.send("\n".join(lines))
            return

        # insta enable/disable
        if sub == "instaenable":
            # admin-only check
            if not await permissions.is_guild_admin(message.author, message.guild.id):
                await message.channel.send(personality.ahri_say("no_permission"))
                return
            await _set_guild_feature_state(message.guild.id, FEATURE_INSTA, True)
            await message.channel.send(personality.ahri_say("feature_toggled", feature="instaembedder", state="enabled"))
            return
        if sub == "instadisable":
            if not await permissions.is_guild_admin(message.author, message.guild.id):
                await message.channel.send(personality.ahri_say("no_permission"))
                return
            await _set_guild_feature_state(message.guild.id, FEATURE_INSTA, False)
            await message.channel.send(personality.ahri_say("feature_toggled", feature="instaembedder", state="disabled"))
            return

        # x enable/disable
        if sub == "xenable":
            if not await permissions.is_guild_admin(message.author, message.guild.id):
                await message.channel.send(personality.ahri_say("no_permission"))
                return
            await _set_guild_feature_state(message.guild.id, FEATURE_X, True)
            await message.channel.send(personality.ahri_say("feature_toggled", feature="xembedder", state="enabled"))
            return
        if sub == "xdisable":
            if not await permissions.is_guild_admin(message.author, message.guild.id):
                await message.channel.send(personality.ahri_say("no_permission"))
                return
            await _set_guild_feature_state(message.guild.id, FEATURE_X, False)
            await message.channel.send(personality.ahri_say("feature_toggled", feature="xembedder", state="disabled"))
            return

        # enable/disable both features simultaneously
        if sub == "enable":
            if not await permissions.is_guild_admin(message.author, message.guild.id):
                await message.channel.send(personality.ahri_say("no_permission"))
                return
            await _set_guild_feature_state(message.guild.id, FEATURE_INSTA, True)
            await _set_guild_feature_state(message.guild.id, FEATURE_X, True)
            await message.channel.send(personality.ahri_say("feature_toggled", feature="embedder", state="enabled"))
            return
        if sub == "disable":
            if not await permissions.is_guild_admin(message.author, message.guild.id):
                await message.channel.send(personality.ahri_say("no_permission"))
                return
            await _set_guild_feature_state(message.guild.id, FEATURE_INSTA, False)
            await _set_guild_feature_state(message.guild.id, FEATURE_X, False)
            await message.channel.send(personality.ahri_say("feature_toggled", feature="embedder", state="disabled"))
            return

        # fallback unknown
        await message.channel.send(personality.ahri_say("unknown_trigger", cmd=sub))
    except Exception:
        LOG.exception("Error in _embedder_handler")
        try:
            await message.channel.send(personality.ahri_say("oops"))
        except Exception:
            pass

# new handler requires admin
_embedder_handler._needs_admin = True

async def setup(bot: discord.Client):
    """
    Setup entrypoint used by your loader system.
    Registers listener and trigger handlers and adds feature info.
    """
    embedder = InstagramXEmbedder(bot)

    # register listener safely. do not crash if registration fails.
    try:
        @bot.listen("on_message")
        async def _on_message(msg: discord.Message):
            # wrap the feature call so exceptions inside don't escape
            try:
                await embedder.handle_message(msg)
            except Exception:
                LOG.exception("embedder listener exception swallowed")
    except Exception:
        LOG.exception("Failed to register on_message listener for InstagramXEmbedder")

    # register trigger handler
    try:
        bot.trigger_handlers["embedder"] = _embedder_handler
    except Exception:
        LOG.exception("Failed to register trigger handler for InstagramXEmbedder")

    # populate feature_info so /help lists it
    try:
        bot.feature_info["InstagramXEmbedder"] = {
            "triggers": ["embedder"]
        }
    except Exception:
        LOG.exception("Failed to set bot.feature_info for InstagramXEmbedder")

    print("InstagramXEmbedder: Feature loaded successfully")
    return embedder

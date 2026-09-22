"""Media/file helpers: size extraction, avatar/icon generation, image and
video downloads with SSRF guards, webxdc packaging of Telegram posts,
and Telegram Instant-View/rich-post extraction.

References to the pervasive bot.py singletons (dc_bot_instance, dc_accid,
userbot_client) and to _is_safe_telegram_url/MAX_ATTACHMENT_SIZE — the
former patched directly on bot.py by the test suite, the latter defined
later in bot.py than this module's own re-export — go through a
function-local `import bot as _bot_module`, same pattern as relay.py
and dc_helpers.py. _resolve_full_res_photos_for_group and
_extract_telethon_rich_message take userbot_client as an explicit
parameter instead and are left untouched — that parameter shadows the
global entirely, so no qualification applies inside them.
"""
import asyncio
import hashlib
import html
import io
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from typing import Optional

import database

from security import retry_async
from rpc_proxy import _get_download_semaphore
from formatting import (
    TelegramRichPost,
    TelegramRichVideo,
    TG_POST_WEBXDC_HTML_TEMPLATE,
    _clean_toml_string,
    _clean_html_for_webxdc,
    _make_teaser,
    _largest_real_photo_size,
    _process_page_blocks,
)

logger = logging.getLogger("tg_dc_bridge")


def _get_media_size(msg) -> int:
    """Helper to reliably extract media file size from a Telethon Message object."""
    if not (msg and msg.media):
        return 0
    if type(msg.media).__name__ in (
        'MessageMediaWebPage', 'MessageMediaUnsupported', 'MessageMediaPoll',
        'MessageMediaContact', 'MessageMediaGeo', 'MessageMediaGeoLive',
        'MessageMediaStory', 'MessageMediaGiveaway', 'MessageMediaGiveawayResults',
        'MessageMediaEmpty'
    ):
        return 0
    
    if type(msg.media).__name__ == 'MessageMediaPaidMedia':
        ext_media = getattr(msg.media, 'extended_media', []) or []
        for item in ext_media:
            sub = getattr(item, 'media', None) or getattr(item, 'photo', None) or getattr(item, 'video', None)
            if sub and hasattr(sub, 'size') and sub.size is not None:
                return sub.size
            if hasattr(item, 'video') and item.video and hasattr(item.video, 'size') and item.video.size is not None:
                return item.video.size

    # Try msg.document (most reliable for documents/videos/audios)
    try:
        if hasattr(msg, 'document') and msg.document and msg.document.size is not None:
            return msg.document.size
    except Exception:
        pass

    # Try msg.file helper (which computes size for photos/documents)
    try:
        if hasattr(msg, 'file') and msg.file and msg.file.size is not None:
            return msg.file.size
    except Exception:
        pass

    # Try direct msg.media.document
    try:
        media = msg.media
        if hasattr(media, 'document') and media.document and media.document.size is not None:
            return media.document.size
    except Exception:
        pass

    # Try photo sizes (rarely > 50MB, but for completeness)
    try:
        media = msg.media
        if hasattr(media, 'photo') and media.photo and hasattr(media.photo, 'sizes') and media.photo.sizes:
            sizes = [s.size for s in media.photo.sizes if hasattr(s, 'size') and s.size is not None]
            if sizes:
                return max(sizes)
    except Exception:
        pass

    return 0


def _get_ptb_media_size(msg) -> int:
    """Extract media file size from a python-telegram-bot Message object."""
    if not msg:
        return 0
    tg_file = None
    if getattr(msg, 'photo', None):
        tg_file = msg.photo[-1]
    elif getattr(msg, 'paid_media', None):
        paid_items = getattr(msg.paid_media, 'paid_media', []) or []
        for item in paid_items:
            if getattr(item, 'photo', None) and item.photo:
                tg_file = item.photo[-1]
                break
            elif getattr(item, 'video', None) and item.video:
                tg_file = item.video
                break
    elif getattr(msg, 'video', None):
        tg_file = msg.video
    elif getattr(msg, 'animation', None):
        tg_file = msg.animation
    elif getattr(msg, 'voice', None):
        tg_file = msg.voice
    elif getattr(msg, 'audio', None):
        tg_file = msg.audio
    elif getattr(msg, 'document', None):
        tg_file = msg.document
    elif getattr(msg, 'sticker', None):
        tg_file = msg.sticker
    elif getattr(msg, 'video_note', None):
        tg_file = msg.video_note

    if tg_file:
        return getattr(tg_file, 'file_size', 0) or 0
    return 0


def _make_square_icon(image_source_path: str | None, max_dim: int = 128, fmt: str = "PNG") -> tuple[bytes | None, str]:
    """Helper to crop and resize an image file into a square icon bytes buffer."""
    if not image_source_path or not os.path.exists(image_source_path):
        return None, ""
    try:
        from PIL import Image
        with Image.open(image_source_path) as img:
            ext = "jpg" if fmt == "JPEG" else "png"
            if fmt == "JPEG":
                img = img.convert("RGB")
            else:
                img = img.convert("RGBA")
            width, height = img.size
            min_dim = min(width, height)
            left = (width - min_dim) // 2
            top = (height - min_dim) // 2
            img = img.crop((left, top, left + min_dim, top + min_dim))
            img = img.resize((max_dim, max_dim), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            if fmt == "JPEG":
                img.save(buf, format="JPEG", quality=85, optimize=True)
            else:
                img.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), f"icon.{ext}"
    except Exception as e:
        logger.warning(f"Failed to generate square icon from {image_source_path}: {e}")
        return None, ""


def _generate_fallback_bridge_icon() -> bytes:
    """Generate a clean 128x128 PNG fallback card icon using PIL (neutral bridge/message design, avoids Telegram copyright)."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (128, 128), color=(36, 129, 204, 255))
        d = ImageDraw.Draw(img)
        # Rounded speech bubble
        d.rounded_rectangle([(24, 28), (104, 86)], radius=16, fill=(255, 255, 255, 255))
        d.polygon([(36, 86), (28, 104), (54, 86)], fill=(255, 255, 255, 255))
        # Inner communication dots
        d.ellipse([(44, 52), (54, 62)], fill=(36, 129, 204, 255))
        d.ellipse([(59, 52), (69, 62)], fill=(36, 129, 204, 255))
        d.ellipse([(74, 52), (84, 62)], fill=(36, 129, 204, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return b""


_generate_fallback_telegram_icon = _generate_fallback_bridge_icon


def _get_bot_self_avatar_path() -> Optional[str]:
    """Retrieve the bot's own profile image path from Delta Chat configuration."""
    import bot as _bot_module
    if not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        return None
    try:
        self_av = _bot_module.dc_bot_instance.rpc.get_config(_bot_module.dc_accid, "selfavatar")
        if self_av and os.path.isfile(self_av):
            return self_av
    except Exception:
        pass
    try:
        cnt = _bot_module.dc_bot_instance.rpc.get_contact(_bot_module.dc_accid, 1)
        prof_img = cnt.get("profile_image") if isinstance(cnt, dict) else getattr(cnt, "profile_image", None)
        if prof_img and os.path.isfile(prof_img):
            return prof_img
    except Exception:
        pass
    return None


def _get_channel_avatar_path(dc_chat_id: Optional[int] = None, username: Optional[str] = None) -> Optional[str]:
    """Retrieve local profile image path for a bridged Delta Chat channel from Delta Chat core."""
    import bot as _bot_module
    if not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        return None
    try:
        target_chat_id = None
        if username:
            clean_user = username.lstrip('@')
            ch = database.get_channel_by_tg_username(clean_user)
            if ch:
                target_chat_id = ch.get('dc_chat_id')
        if not target_chat_id and dc_chat_id:
            # Crucial: only use dc_chat_id if it corresponds to an actual registered bridged channel
            ch = database.get_channel_by_dc_chat_id(dc_chat_id)
            if ch:
                target_chat_id = dc_chat_id
        if not target_chat_id:
            return None

        # 1. Try get_basic_chat_info
        try:
            chat_info = _bot_module.dc_bot_instance.rpc.get_basic_chat_info(_bot_module.dc_accid, target_chat_id)
            prof_img = chat_info.get("profile_image") if chat_info else None
            if prof_img and os.path.exists(prof_img):
                return prof_img
        except Exception:
            pass

        # 2. Fallback to get_full_chat_by_id
        try:
            full_chat = _bot_module.dc_bot_instance.rpc.get_full_chat_by_id(_bot_module.dc_accid, target_chat_id)
            prof_img = full_chat.get("profile_image") if full_chat else None
            if prof_img and os.path.exists(prof_img):
                return prof_img
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"Failed to get channel profile image for chat {dc_chat_id}: {e}")
    return None


async def _download_image_to_file(url: str, output_path: str, max_dim: int = 1280, fmt: str = "WEBP", quality: int = 80) -> bool:
    """Download and optionally optimize an image to a specific path. Supports remote URLs and local file paths."""
    import bot as _bot_module
    if not url:
        return False
    from PIL import Image

    # 1. Handle local file paths directly
    if os.path.isfile(url):
        try:
            with Image.open(url) as img:
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                w, h = img.size
                if max(w, h) > max_dim:
                    scale = max_dim / max(w, h)
                    new_size = (int(w * scale), int(h * scale))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                if fmt.upper() == "WEBP":
                    img.save(output_path, format="WEBP", quality=quality, method=3)
                elif fmt.upper() == "PNG":
                    img.save(output_path, format="PNG", optimize=True)
                else:
                    img.save(output_path, format="JPEG", quality=quality, optimize=True)
                return True
        except Exception as e:
            logger.warning(f"Failed to process local image {url}: {e}")
            try:
                shutil.copy2(url, output_path)
                return True
            except Exception:
                return False

    if url.startswith('//'):
        url = 'https:' + url

    if not _bot_module._is_safe_telegram_url(url):
        logger.warning(f"SSRF guard: Rejected unsafe image URL: {url}")
        if os.path.exists(output_path):
            try:
                os.unlink(output_path)
            except OSError:
                pass
        return False

    try:
        import httpx
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://t.me/'
        }
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200 and resp.content:
                if len(resp.content) > 20 * 1024 * 1024:
                    logger.warning(f"Image {url} exceeds limit (20 MB), skipping download")
                    return False
                try:
                    with Image.open(io.BytesIO(resp.content)) as img:
                        if img.mode in ("RGBA", "P"):
                            img = img.convert("RGB")
                        w, h = img.size
                        if max(w, h) > max_dim:
                            scale = max_dim / max(w, h)
                            new_size = (int(w * scale), int(h * scale))
                            img = img.resize(new_size, Image.Resampling.LANCZOS)
                        if fmt.upper() == "WEBP":
                            img.save(output_path, format="WEBP", quality=quality, method=3)
                        elif fmt.upper() == "PNG":
                            img.save(output_path, format="PNG", optimize=True)
                        else:
                            img.save(output_path, format="JPEG", quality=quality, optimize=True)
                        return True
                except Exception as e:
                    logger.warning(f"Image processing failed for {url} ({e}), falling back to raw bytes")
                    try:
                        with open(output_path, 'wb') as f:
                            f.write(resp.content)
                        return True
                    except Exception:
                        return False
            return False
    except Exception as e:
        logger.warning(f"Failed to download image {url}: {e}")
        return False


async def _download_video_with_limit(url: str, output_path: str, max_bytes: int) -> bool:
    """Stream download a video file up to max_bytes. Abort and return False if exceeded or failed. Supports local files."""
    import bot as _bot_module
    if not url or max_bytes <= 0:
        return False
    if os.path.isfile(url):
        try:
            sz = os.path.getsize(url)
            if sz <= max_bytes:
                shutil.copy2(url, output_path)
                return True
            else:
                logger.info(f"Local video {url} exceeds limit ({sz} > {max_bytes} bytes), skipping")
                return False
        except Exception as e:
            logger.warning(f"Failed to copy local video {url}: {e}")
            return False

    if url.startswith('//'):
        url = 'https:' + url

    if not _bot_module._is_safe_telegram_url(url):
        logger.warning(f"SSRF guard: Rejected unsafe video URL: {url}")
        if os.path.exists(output_path):
            try:
                os.unlink(output_path)
            except OSError:
                pass
        return False

    try:
        import httpx
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://t.me/'
        }
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # 1. Quick HEAD check if Content-Length header is present
            try:
                head_resp = await client.head(url, headers=headers)
                cl = head_resp.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > max_bytes:
                    logger.info(f"Video {url} exceeds limit ({int(cl)} > {max_bytes} bytes), skipping download")
                    return False
            except Exception:
                pass

            # 2. Streaming GET with chunk byte counting
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    return False
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > max_bytes:
                    logger.info(f"Video Content-Length ({int(cl)}) exceeds limit {max_bytes}, skipping download")
                    return False

                downloaded = 0
                exceeded = False
                with open(output_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            logger.info(f"Video download exceeded limit {max_bytes} bytes, aborting")
                            exceeded = True
                            break
                        f.write(chunk)
                if exceeded:
                    if os.path.exists(output_path):
                        try:
                            os.unlink(output_path)
                        except Exception:
                            pass
                    return False
                return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except Exception as e:
        logger.warning(f"Failed to download video {url}: {e}")
        if os.path.exists(output_path):
            try:
                os.unlink(output_path)
            except Exception:
                pass
        return False


async def _download_image_url(url: str) -> Optional[str]:
    """Download an image from a URL or return existing local path for Delta Chat relay."""
    import bot as _bot_module
    if not url:
        return None
    if os.path.isfile(url):
        return url
    if url.startswith('//'):
        url = 'https:' + url
    if not _bot_module._is_safe_telegram_url(url):
        logger.warning(f"SSRF guard: Rejected unsafe image URL in _download_image_url: {url}")
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            if resp.status_code == 200 and resp.content:
                if len(resp.content) > 20 * 1024 * 1024:
                    logger.warning(f"Image {url} exceeds limit (20 MB), skipping download")
                    return None
                ext = '.jpg'
                if '.png' in url.lower():
                    ext = '.png'
                elif '.webp' in url.lower():
                    ext = '.webp'
                elif '.gif' in url.lower():
                    ext = '.gif'
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
                with os.fdopen(tmp_fd, 'wb') as f:
                    f.write(resp.content)
                return tmp_path
    except Exception as e:
        logger.debug(f"Failed to download image from {url}: {e}")
    return None


async def _download_via_userbot(chat_id: int, msg_id: int, suffix: str = "") -> Optional[str]:
    """Fetch message via Userbot and download its media if it exists and is <= 50MB."""
    import bot as _bot_module
    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
        return None
    size = 0
    tmp_path = None
    try:
        # Telethon get_messages can fetch by ID
        msg = await asyncio.wait_for(_bot_module.userbot_client.get_messages(chat_id, ids=msg_id), timeout=15.0)
        if not (msg and msg.media) or type(msg.media).__name__ in ('MessageMediaWebPage', 'MessageMediaUnsupported'):
            return None
        
        # Check size limit
        size = _get_media_size(msg)
        m_type = type(msg.media).__name__
        file_name = getattr(msg.file, 'name', '') if hasattr(msg, 'file') and msg.file else ""
        if not file_name:
            file_name = f"media_{msg_id}{suffix}"

        if size > _bot_module.MAX_ATTACHMENT_SIZE:
            logger.warning(f"Userbot: Media in {chat_id}:{msg_id} ({file_name}) is too large ({size // 1024 // 1024} MB > {_bot_module.MAX_ATTACHMENT_SIZE // 1024 // 1024} MB)")
            return None

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(tmp_fd)

        async def _do_download():
            async with _get_download_semaphore():
                return await asyncio.wait_for(_bot_module.userbot_client.download_media(msg.media, file=tmp_path), timeout=300.0)

        logger.info(f"Userbot downloading {m_type} '{file_name}' ({size // 1024} KB) from chat {chat_id} msg {msg_id}...")
        path = await retry_async(_do_download, max_retries=3, delay=3.0, backoff=2.0)
        return path

    except Exception as e:
        logger.error(f"Userbot download failed for chat {chat_id} msg {msg_id} (size: {size} B) after 3 retries: {e}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return None


async def _package_tg_post_webxdc(post: TelegramRichPost, output_xdc_path: str, dc_chat_id: Optional[int] = None) -> bool:
    """Bundle a rich Telegram post into a standalone offline WebXDC package (.xdc)."""
    import bot as _bot_module
    has_displayable_content = bool(
        (post.text_markdown and post.text_markdown.strip()) or
        (post.text_html and re.sub(r'<[^>]+>', '', post.text_html).strip()) or
        post.image_urls or
        post.videos
    )
    if not has_displayable_content:
        logger.warning(f"Cannot package empty WebXDC for @{post.username}/{post.post_id}: no text, images, or videos found.")
        return False

    try:
        tmp_dir = tempfile.mkdtemp(prefix="tg_webxdc_")
        images_dir = os.path.join(tmp_dir, "images")
        os.makedirs(images_dir, exist_ok=True)
        videos_dir = os.path.join(tmp_dir, "videos")
        os.makedirs(videos_dir, exist_ok=True)

        source_url = f"https://t.me/{post.username}/{post.post_id}" if post.username else "https://t.me"

        # 1. Generate Application Icon (128x128 square PNG)
        icon_bytes = None
        icon_name = "icon.png"

        # 1a. Prioritize existing local Delta Chat channel avatar (only if verified bridged channel)
        local_avatar = _get_channel_avatar_path(dc_chat_id=dc_chat_id, username=post.username)
        if local_avatar:
            icon_bytes, icon_name = _make_square_icon(local_avatar, max_dim=128, fmt="PNG")

        # 1b. Fallback to author avatar from Telegram (local file downloaded by telethon, or remote URL from web embed)
        if not icon_bytes and post.author_avatar_url:
            if os.path.isfile(post.author_avatar_url):
                icon_bytes, icon_name = _make_square_icon(post.author_avatar_url, max_dim=128, fmt="PNG")
            else:
                avatar_tmp = os.path.join(tmp_dir, "avatar_raw")
                if await _bot_module._download_image_to_file(post.author_avatar_url, avatar_tmp, max_dim=256, fmt="PNG"):
                    icon_bytes, icon_name = _make_square_icon(avatar_tmp, max_dim=128, fmt="PNG")

        # 1c. Fallback to bot's own avatar (to avoid Telegram copyright issues)
        if not icon_bytes:
            bot_self_avatar = _get_bot_self_avatar_path()
            if bot_self_avatar:
                icon_bytes, icon_name = _make_square_icon(bot_self_avatar, max_dim=128, fmt="PNG")

        # 1d. Fallback to neutral bridge icon
        if not icon_bytes:
            icon_bytes = _generate_fallback_bridge_icon()
            icon_name = "icon.png"

        # 2. Download Images
        local_images = []
        for idx, img_url in enumerate(post.image_urls):
            img_fname = f"img_{idx}.webp"
            img_dest = os.path.join(images_dir, img_fname)
            if await _bot_module._download_image_to_file(img_url, img_dest, max_dim=1280, fmt="WEBP", quality=80):
                local_images.append(f"images/{img_fname}")

        # 3. Clean content HTML & Build Gallery HTML
        cleaned_content = _clean_html_for_webxdc(post.text_html)

        has_inline_images = bool(re.search(r'<img\s+[^>]*src=["\']images/img_\d+\.webp["\']', cleaned_content))
        gallery_images = []
        if has_inline_images:
            for img_path in local_images:
                if img_path not in cleaned_content:
                    gallery_images.append(img_path)
        else:
            gallery_images = local_images

        gallery_top_html = ""
        gallery_bottom_html = ""
        if len(gallery_images) == 1:
            gallery_top_html = f'<div class="gallery-single"><img src="{gallery_images[0]}" alt="Post media" /></div>'
        elif len(gallery_images) > 1:
            items_html = "\n".join(
                f'<div class="gallery-item"><img src="{img_path}" alt="Photo {i+1}" loading="lazy" /></div>'
                for i, img_path in enumerate(gallery_images)
            )
            gallery_top_html = f'<div class="gallery-grid">\n{items_html}\n</div>'

        # 4. Download & Budget Videos
        # Limits: single video <= 20 MB, cumulative videos package <= 50 MB.
        # Overflow/oversize/unsupported videos rendered as preview poster cards with a Telegram link button.
        MAX_SINGLE_VIDEO_BYTES = 20 * 1024 * 1024   # 20 MB
        MAX_TOTAL_VIDEO_BYTES = 50 * 1024 * 1024    # 50 MB
        total_video_bytes = 0
        embedded_videos: list[dict] = []
        overflow_videos: list[dict] = []

        for idx, vid in enumerate(post.videos):
            poster_fname = f"vid_poster_{idx}.webp"
            poster_dest = os.path.join(images_dir, poster_fname)
            poster_rel = ""
            if vid.poster_url:
                if await _bot_module._download_image_to_file(vid.poster_url, poster_dest, max_dim=1280, fmt="WEBP", quality=80):
                    poster_rel = f"images/{poster_fname}"

            remaining_budget = MAX_TOTAL_VIDEO_BYTES - total_video_bytes
            allowable_bytes = min(MAX_SINGLE_VIDEO_BYTES, remaining_budget)

            is_embedded = False
            if not vid.is_too_big and vid.video_url and allowable_bytes > 0:
                vid_fname = f"vid_{idx}.mp4"
                vid_dest = os.path.join(videos_dir, vid_fname)
                if await _bot_module._download_video_with_limit(vid.video_url, vid_dest, allowable_bytes):
                    v_size = os.path.getsize(vid_dest)
                    total_video_bytes += v_size
                    embedded_videos.append({
                        "video_path": f"videos/{vid_fname}",
                        "poster_path": poster_rel,
                        "duration": vid.duration,
                        "full_path": vid_dest,
                    })
                    is_embedded = True

            if not is_embedded:
                overflow_videos.append({
                    "poster_path": poster_rel,
                    "duration": vid.duration,
                    "is_too_big": vid.is_too_big,
                })

        # 5. Build Video HTML
        video_elements = []
        if len(embedded_videos) == 1:
            v = embedded_videos[0]
            poster_attr = f' poster="{v["poster_path"]}"' if v["poster_path"] else ''
            video_elements.append(
                f'<div class="video-container">\n'
                f'    <video controls playsinline preload="metadata"{poster_attr}>\n'
                f'        <source src="{v["video_path"]}" type="video/mp4">\n'
                f'        Your browser does not support the video tag.\n'
                f'    </video>\n'
                f'</div>'
            )
        elif len(embedded_videos) > 1:
            grid_items = []
            for v in embedded_videos:
                poster_attr = f' poster="{v["poster_path"]}"' if v["poster_path"] else ''
                grid_items.append(
                    f'<div class="video-container">\n'
                    f'    <video controls playsinline preload="metadata"{poster_attr}>\n'
                    f'        <source src="{v["video_path"]}" type="video/mp4">\n'
                    f'        Your browser does not support the video tag.\n'
                    f'    </video>\n'
                    f'</div>'
                )
            video_elements.append(f'<div class="video-grid">\n' + "\n".join(grid_items) + '\n</div>')

        for ov in overflow_videos:
            dur_text = f'<div class="video-duration-badge">{html.escape(ov["duration"])}</div>' if ov["duration"] else ''
            bg_style = f"background-image: url('{ov['poster_path']}');" if ov['poster_path'] else "background-color: #1a1a1a;"
            label_text = f"📹 Video ({html.escape(ov['duration'])})" if ov["duration"] else "📹 Video"
            card_html = (
                f'<div class="video-overflow-card">\n'
                f'    <a href="{source_url}" target="_blank" rel="noopener noreferrer" style="text-decoration:none;display:block;">\n'
                f'        <div class="video-overflow-thumb" style="{bg_style}">\n'
                f'            <div class="video-play-icon">▶</div>\n'
                f'            {dur_text}\n'
                f'        </div>\n'
                f'    </a>\n'
                f'    <div class="video-overflow-footer">\n'
                f'        <div class="video-overflow-title">{label_text}</div>\n'
                f'        <a href="{source_url}" target="_blank" rel="noopener noreferrer" class="tg-video-btn">View all videos in Telegram ↗</a>\n'
                f'    </div>\n'
                f'</div>'
            )
            video_elements.append(card_html)

        video_html = "\n".join(video_elements)
        if bool(re.search(r'videos/vid_\d+\.mp4', cleaned_content)):
            video_html = ""

        # 7. Build Header & Meta
        avatar_html = '<div class="avatar" style="display:flex;align-items:center;justify-content:center;color:white;font-weight:bold;background:var(--accent-color);">TG</div>'
        if icon_bytes:
            avatar_html = '<img src="icon.png" alt="Avatar" class="avatar" />'

        meta_parts = []
        if post.username:
            meta_parts.append(f"@{post.username}")
        if post.published_date and isinstance(post.published_date, str):
            date_clean = post.published_date.replace("T", " ").split("+")[0]
            meta_parts.append(date_clean)
        meta_text = " • ".join(str(p) for p in meta_parts) if meta_parts else "Telegram Post"

        views_text = f"👁️ {post.views} views" if post.views else ""

        # 8. Fill HTML Template
        from datetime import datetime, timezone
        bridged_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M GMT")
        channel_title = post.author_name or (f"@{post.username}" if post.username else "Telegram")
        doc_title = f"{channel_title} #{post.post_id}" if post.post_id else channel_title
        html_doc = TG_POST_WEBXDC_HTML_TEMPLATE.format(
            title=html.escape(doc_title),
            author_name=html.escape(channel_title),
            meta_text=html.escape(meta_text),
            source_url=source_url,
            avatar_html=avatar_html,
            gallery_top_html=gallery_top_html,
            video_html=video_html,
            content_html=cleaned_content,
            gallery_bottom_html=gallery_bottom_html,
            views_text=html.escape(views_text),
            bridged_at=bridged_at
        )

        index_html_path = os.path.join(tmp_dir, "index.html")
        with open(index_html_path, "w", encoding="utf-8") as f:
            f.write(html_doc)

        # 9. Build manifest.toml
        app_name = _clean_toml_string(doc_title[:80])
        manifest_lines = [
            f'name = "{app_name}"',
            f'source_code_url = "{_clean_toml_string(source_url)}"',
        ]
        if icon_bytes:
            manifest_lines.append(f'icon = "{icon_name}"')
        manifest_content = "\n".join(manifest_lines) + "\n"

        # 10. Package into .xdc ZIP
        with zipfile.ZipFile(output_xdc_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(index_html_path, arcname="index.html")
            zf.writestr("manifest.toml", manifest_content)
            if icon_bytes:
                zf.writestr(icon_name, icon_bytes)
            # Write all downloaded images (photos + video posters)
            for root, _, files in os.walk(images_dir):
                for f in files:
                    f_full = os.path.join(root, f)
                    rel = os.path.relpath(f_full, tmp_dir)
                    zf.write(f_full, arcname=rel)
            # Write embedded videos using ZIP_STORED (MP4 is already compressed)
            for v in embedded_videos:
                full_vid = v.get("full_path")
                if full_vid and os.path.exists(full_vid):
                    zf.write(full_vid, arcname=v["video_path"], compress_type=zipfile.ZIP_STORED)

        return os.path.exists(output_xdc_path) and os.path.getsize(output_xdc_path) > 0
    except Exception as e:
        logger.error(f"Failed to package WebXDC post for @{post.username}/{post.post_id}: {e}")
        return False
    finally:
        if 'tmp_dir' in locals():
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _extract_public_tg_post_rich(username: str, post_id: int) -> Optional[TelegramRichPost]:
    """Fetch public Telegram channel post embed and extract full rich post metadata."""
    if not username or not post_id:
        return None
    clean_username = str(username).lstrip('@').strip()
    if not re.match(r'^[a-zA-Z0-9_]{3,32}$', clean_username):
        return None
    if not isinstance(post_id, int) or post_id <= 0:
        return None
    url = f"https://t.me/{clean_username}/{post_id}?embed=1"
    try:
        import httpx
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200 or not resp.text or len(resp.text) > 2 * 1024 * 1024:
                return None
            content = resp.text

            # Author Name
            author_m = re.search(r'class="tgme_widget_message_owner_name"[^>]*>(.*?)</(?:a|div|span)>', content, re.DOTALL)
            author_name = html.unescape(re.sub(r'<[^>]+>', '', author_m.group(1))).strip() if author_m else ""

            # Author Avatar URL
            avatar_m = re.search(r'class="tgme_widget_message_user_photo[^"]*"[^>]*>\s*<img src="([^"]+)"', content)
            if not avatar_m:
                avatar_m = re.search(r'class="tgme_widget_message_user_photo[^"]*"[^>]*style="background-image:url\(\'([^\']+)\'\)"', content)
            avatar_url = avatar_m.group(1) if avatar_m else ""

            # Post Text HTML
            text_m = re.search(r'<div class="tgme_widget_message_text[^"]*js-message_text[^"]*"[^>]*>(.*?)</div>', content, re.DOTALL)
            text_html = text_m.group(1) if text_m else ""

            # Post Text Markdown (for DC message teaser & fallback)
            text_md = ""
            if text_html:
                raw_html = text_html
                raw_html = re.sub(r'<br\s*/?>', '\n', raw_html)
                raw_html = re.sub(r'<(b|strong)[^>]*>(.*?)</\1>', r'**\2**', raw_html)
                raw_html = re.sub(r'<(i|em)[^>]*>(.*?)</\1>', r'*\2*', raw_html)
                raw_html = re.sub(r'<(s|strike|del)[^>]*>(.*?)</\1>', r'~\2~', raw_html)
                raw_html = re.sub(r'<(u|ins)[^>]*>(.*?)</\1>', r'__\2__', raw_html)
                raw_html = re.sub(r'<code[^>]*>(.*?)</code>', r'`\1`', raw_html)
                raw_html = re.sub(r'<pre[^>]*>(.*?)</pre>', r'```\n\1\n```', raw_html)
                raw_html = re.sub(r'<blockquote[^>]*>(.*?)</blockquote>', lambda m: '\n'.join('> ' + l for l in m.group(1).split('\n')), raw_html)
                raw_html = re.sub(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r'[\2](\1)', raw_html)
                raw_html = re.sub(r'<[^>]+>', '', raw_html)
                text_md = html.unescape(raw_html).strip()

            # Videos
            videos: list[TelegramRichVideo] = []
            v_posters: set[str] = set()
            for v_m in re.finditer(r'<a[^>]*tgme_widget_message_video_player[^>]*>(.*?)</a>', content, re.DOTALL):
                v_block = v_m.group(0)

                v_src_m = re.search(r'<video[^>]*src="([^"]+)"', v_block)
                v_src = html.unescape(v_src_m.group(1)).strip() if v_src_m else ""
                if v_src.startswith('//'):
                    v_src = 'https:' + v_src

                v_poster_m = re.search(r'background-image:url\(\'([^\']+)\'\)', v_block)
                v_poster = v_poster_m.group(1).strip() if v_poster_m else ""
                if v_poster.startswith('//'):
                    v_poster = 'https:' + v_poster
                if v_poster:
                    v_posters.add(v_poster)

                v_dur_m = re.search(r'<time[^>]*message_video_duration[^>]*>(.*?)</time>', v_block, re.DOTALL)
                v_dur = html.unescape(v_dur_m.group(1)).strip() if v_dur_m else ""

                is_too_big = "Media is too big" in v_block or not v_src

                videos.append(TelegramRichVideo(
                    video_url=v_src,
                    poster_url=v_poster,
                    duration=v_dur,
                    is_too_big=is_too_big
                ))

            teaser = _make_teaser(text_md)
            if not teaser and videos:
                if len(videos) == 1:
                    teaser = f"📹 Video ({videos[0].duration})" if videos[0].duration else "📹 Video"
                else:
                    teaser = f"📹 {len(videos)} videos"

            # Images (excluding video posters to prevent duplicating thumbnails into the photo gallery)
            image_urls = []
            for u in re.findall(r'background-image:url\(\'([^\']+)\'\)', content):
                if u.startswith('//'):
                    u = 'https:' + u
                if 'telegram.org/img/emoji' not in u and u not in v_posters and u != avatar_url and u not in image_urls:
                    image_urls.append(u)

            # Views
            views_m = re.search(r'class="tgme_widget_message_views"[^>]*>(.*?)</span>', content)
            views = views_m.group(1).strip() if views_m else ""

            # Published Date
            date_m = re.search(r'<time datetime="([^"]+)"', content)
            published_date = date_m.group(1).strip() if date_m else ""

            # Album post IDs (extract from ?single links in grouped messages)
            album_pids = {post_id}
            for u_match, pid_match in re.findall(r't\.me/([^/]+)/(\d+)\?single', content):
                if u_match.lower() == username.lower():
                    try:
                        album_pids.add(int(pid_match))
                    except ValueError:
                        pass
            album_post_ids = sorted(album_pids)

            # Check if post has rich characteristics
            has_tables = '<table' in text_html
            is_grouped = 'tgme_widget_message_grouped' in content and len(image_urls) >= 1
            is_long = len(text_md) > 1500
            has_content = bool(text_md.strip() or image_urls or videos)
            is_rich = has_content and (len(image_urls) > 1 or len(videos) > 0 or has_tables or is_grouped or ((len(image_urls) >= 1 or len(videos) >= 1) and is_long))

            return TelegramRichPost(
                username=username,
                post_id=post_id,
                author_name=author_name,
                author_avatar_url=avatar_url,
                text_html=text_html,
                text_markdown=text_md,
                teaser=teaser,
                image_urls=image_urls,
                video_urls=[v.video_url for v in videos if v.video_url],
                videos=videos,
                album_post_ids=album_post_ids,
                published_date=published_date,
                views=views,
                is_rich=is_rich
            )
    except Exception as e:
        logger.debug(f"Public TG rich post extraction failed for @{username}/{post_id}: {e}")
        return None


async def _extract_public_tg_post(username: str, post_id: int) -> tuple[Optional[str], Optional[str]]:
    """Fetch public Telegram channel post preview and extract formatted text and high-res media URL."""
    import bot as _bot_module
    rich = await _bot_module._extract_public_tg_post_rich(username, post_id)
    if rich:
        first_img = rich.image_urls[0] if rich.image_urls else None
        return rich.text_markdown, first_img
    return None, None


async def _resolve_full_res_photos_for_group(msg, userbot_client) -> dict:
    """RichMessage.photos often only carries low-res preview stubs (PhotoStrippedSize) for
    photos that also exist as full-resolution attachments on sibling messages in the same
    media group. Look those siblings up and return {photo_id: full_res_photo}."""
    grouped_id = getattr(msg, 'grouped_id', None)
    if grouped_id is None or not userbot_client:
        return {}
    try:
        input_chat = await asyncio.wait_for(msg.get_input_chat(), timeout=15.0)
        if not input_chat:
            return {}
        msg_id = getattr(msg, 'id', 0)
        lo = max(1, msg_id - 12)
        ids = list(range(lo, msg_id + 13))
        siblings = await asyncio.wait_for(userbot_client.get_messages(input_chat, ids=ids), timeout=15.0)
        full_res: dict = {}
        for sib in siblings or []:
            if not sib or getattr(sib, 'grouped_id', None) != grouped_id:
                continue
            media = getattr(sib, 'media', None)
            photo = media.photo if type(media).__name__ == 'MessageMediaPhoto' else getattr(sib, 'photo', None)
            photo_id = getattr(photo, 'id', None) if photo else None
            if photo_id:
                full_res[photo_id] = photo
        return full_res
    except Exception as e:
        logger.debug(f"Failed resolving full-res sibling photos for grouped_id {grouped_id}: {e}")
        return {}


async def _extract_telethon_rich_message(msg, userbot_client, entity=None, dc_chat_id: Optional[int] = None) -> Optional[TelegramRichPost]:
    """Extract full rich post metadata from Telethon Message containing a RichMessage object."""
    rich_msg = getattr(msg, 'rich_message', None)
    if not rich_msg:
        return None

    try:
        chat_username = getattr(msg.chat, 'username', '') if getattr(msg, 'chat', None) else ""
        chat_title = getattr(msg.chat, 'title', '') if getattr(msg, 'chat', None) else ""
        author_name = chat_title or (f"@{chat_username}" if chat_username else "Telegram")
        post_id = getattr(msg, 'id', 0)

        # Check if rich message is partitioned and fetch full rich message if needed
        if userbot_client and getattr(rich_msg, 'part', False) and post_id:
            try:
                from telethon.tl.functions.messages import GetRichMessageRequest
                peer = entity or getattr(msg, 'chat', None)
                if peer:
                    full_res = await userbot_client(GetRichMessageRequest(peer=peer, id=post_id))
                    if hasattr(full_res, 'messages') and full_res.messages:
                        full_msg = full_res.messages[0]
                        if getattr(full_msg, 'rich_message', None):
                            rich_msg = full_msg.rich_message
            except Exception as grm_err:
                logger.warning(f"Failed fetching full rich message via GetRichMessageRequest: {grm_err}")

        # Download Telegram channel avatar
        author_avatar_url = None
        if userbot_client:
            chat_to_photo = getattr(msg, 'chat', None) or entity
            if chat_to_photo:
                try:
                    av_tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False).name
                    av_path = await asyncio.wait_for(userbot_client.download_profile_photo(chat_to_photo, file=av_tmp), timeout=30.0)
                    if av_path and os.path.exists(av_path) and os.path.getsize(av_path) > 0:
                        author_avatar_url = av_path
                    elif os.path.exists(av_tmp):
                        try:
                            os.unlink(av_tmp)
                        except Exception:
                            pass
                except Exception as av_err:
                    logger.debug(f"Failed downloading chat avatar: {av_err}")

        published_date = ""
        msg_date = getattr(msg, 'date', None)
        if msg_date and hasattr(msg_date, 'strftime') and type(msg_date).__name__ != 'MagicMock':
            try:
                published_date = str(msg_date.strftime("%b %d at %H:%M"))
            except Exception:
                published_date = ""

        views = ""
        msg_views = getattr(msg, 'views', None)
        if msg_views is not None and type(msg_views).__name__ != 'MagicMock':
            views = str(msg_views)

        photos_map = {getattr(p, 'id', None): p for p in (getattr(rich_msg, 'photos', []) or []) if getattr(p, 'id', None)}
        docs_map = {getattr(d, 'id', None): d for d in (getattr(rich_msg, 'documents', []) or []) if getattr(d, 'id', None)}

        # RichMessage-sourced photos can report a bogus/zero byte size on their real
        # PhotoSize entries, which breaks Telethon's own byte-size-based "largest thumb"
        # heuristic (it ends up picking the tiny PhotoStrippedSize blur placeholder over
        # a genuinely large photo). _largest_real_photo_size() picks by pixel area instead,
        # and download sites pass its result explicitly via thumb=.
        for pid, pobj in photos_map.items():
            best = _largest_real_photo_size(pobj)
            logger.debug(f"RichMessage photo {pid} for post {post_id}: picked {type(best).__name__ if best else None} ({getattr(best, 'w', '?')}x{getattr(best, 'h', '?')})")

        # RichMessage photos are sometimes low-res preview stubs; swap in full-resolution
        # copies from sibling album messages when available.
        full_res_photos = await _resolve_full_res_photos_for_group(msg, userbot_client)
        logger.info(f"Post {post_id}: resolved {len(full_res_photos)} full-res sibling photo(s) out of {len(photos_map)} RichMessage photo(s)")
        for photo_id, full_photo in full_res_photos.items():
            if photo_id in photos_map:
                photos_map[photo_id] = full_photo

        image_urls = []
        videos = []
        downloaded_photo_ids = set()
        downloaded_doc_ids = set()

        blocks = getattr(rich_msg, 'blocks', []) or []
        md_parts, htm_parts = await _process_page_blocks(
            blocks, photos_map, docs_map, userbot_client,
            post_id, image_urls, videos, downloaded_photo_ids, downloaded_doc_ids
        )

        # Also download any photos/videos attached to rich_msg that were not explicitly referenced in blocks
        for pid, pobj in photos_map.items():
            if pid not in downloaded_photo_ids and userbot_client:
                try:
                    p_tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False).name
                    best_size = _largest_real_photo_size(pobj)
                    p_path = await asyncio.wait_for(userbot_client.download_media(pobj, file=p_tmp, thumb=best_size), timeout=60.0)
                    if p_path and os.path.exists(p_path) and os.path.getsize(p_path) > 0:
                        image_urls.append(p_path)
                        downloaded_photo_ids.add(pid)
                    elif os.path.exists(p_tmp):
                        try:
                            os.unlink(p_tmp)
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(f"Failed to download remaining photo {pid} in post {post_id}: {e}")

        text_markdown = "\n\n".join(p for p in md_parts if p).strip()
        text_html = "\n".join(p for p in htm_parts if p).strip()
        teaser = _make_teaser(text_markdown)

        has_content = bool(text_markdown or image_urls or videos)
        if not has_content:
            return None

        has_structured_blocks = any(
            type(b).__name__ in (
                'PageBlockHeader', 'PageBlockHeading1', 'PageBlockHeading2', 'PageBlockHeading3',
                'PageBlockHeading4', 'PageBlockHeading5', 'PageBlockHeading6', 'PageBlockTitle',
                'PageBlockSubheader', 'PageBlockSubtitle', 'PageBlockBlockquote', 'PageBlockPullquote',
                'PageBlockPreformatted', 'PageBlockList', 'PageBlockOrderedList', 'PageBlockTable',
                'PageBlockPhoto', 'PageBlockVideo', 'PageBlockDetails', 'PageBlockCollage', 'PageBlockSlideshow'
            )
            for b in blocks
        )
        is_rich = bool(
            has_structured_blocks or
            len(image_urls) > 1 or
            len(videos) > 0 or
            '<table' in text_html or
            len(text_markdown) > 500 or
            (len(image_urls) >= 1 and len(text_markdown) > 200)
        )

        return TelegramRichPost(
            username=chat_username or "",
            post_id=post_id,
            author_name=author_name,
            author_avatar_url=author_avatar_url or "",
            text_html=text_html,
            text_markdown=text_markdown,
            teaser=teaser,
            image_urls=image_urls,
            videos=videos,
            published_date=published_date,
            views=views,
            is_rich=is_rich,
        )
    except Exception as e:
        logger.warning(f"Failed to extract Telethon RichMessage: {e}", exc_info=True)
        return None


"""Rich-text/HTML/markdown formatting helpers for Telegram <-> Delta Chat posts.

Pure leaf module: no dependencies on the DC/TG command or event handlers.
`get_dc_help_text` reaches into `bot._is_dc_admin` via a qualified,
function-local `import bot` (never a module-level import, and never
`from bot import _is_dc_admin`) for two reasons: that name is patched
directly by the test suite and must stay resolvable through `bot.py`'s
re-export regardless of which module ends up owning its implementation,
and a module-level `import bot` here would deadlock the very re-export
that pulls this module into bot.py in the first place whenever bot.py is
run directly as a script (see the comment on `run_cli()` in bot.py).
"""
import asyncio
import html
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Optional

import database

logger = logging.getLogger("tg_dc_bridge")

TG_WEBXDC_VIDEO_MAX_BYTES = 20 * 1024 * 1024   # 20 MB, matches MAX_SINGLE_VIDEO_BYTES in _package_tg_post_webxdc


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, appending '…' if truncated."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


def _utf16_to_py_indices(text: str) -> list[int]:
    """Map each UTF-16 code unit offset to the corresponding Python character index."""
    utf16_map = []
    for py_idx, ch in enumerate(text):
        utf16_len = len(ch.encode('utf-16-le')) // 2
        for _ in range(utf16_len):
            utf16_map.append(py_idx)
    utf16_map.append(len(text))
    return utf16_map


def _format_telegram_entities(text: str, entities) -> str:
    """Format Telegram text with entities (from PTB or Telethon) into Delta Chat Markdown."""
    if not entities or not text:
        return text or ""

    utf16_map = _utf16_to_py_indices(text)

    def get_py_indices(u_offset, u_len):
        start = utf16_map[u_offset] if u_offset < len(utf16_map) else len(text)
        end_offset = u_offset + u_len
        end = utf16_map[end_offset] if end_offset < len(utf16_map) else len(text)
        return start, end

    parsed = []
    for ent in entities:
        e_type = getattr(ent, 'type', None)
        if e_type is None:
            cls_name = type(ent).__name__
            if cls_name.startswith('MessageEntity'):
                e_type = cls_name[len('MessageEntity'):].lower()
            else:
                e_type = str(cls_name).lower()
        else:
            e_type = str(e_type).lower()

        offset = getattr(ent, 'offset', 0)
        length = getattr(ent, 'length', 0)
        url = getattr(ent, 'url', None)
        lang = getattr(ent, 'language', None) or getattr(ent, 'lang', None)
        user_id = getattr(ent, 'user_id', None) or (getattr(getattr(ent, 'user', None), 'id', None))

        if e_type in ('text_link', 'texturl', 'text_url'):
            e_type = 'text_link'
        elif e_type in ('text_mention', 'mentionname', 'mention_name'):
            e_type = 'text_mention'
        elif e_type in ('bold',):
            e_type = 'bold'
        elif e_type in ('italic',):
            e_type = 'italic'
        elif e_type in ('underline',):
            e_type = 'underline'
        elif e_type in ('strikethrough', 'strike'):
            e_type = 'strikethrough'
        elif e_type in ('spoiler',):
            e_type = 'spoiler'
        elif e_type in ('code',):
            e_type = 'code'
        elif e_type in ('pre',):
            e_type = 'pre'
        elif e_type in ('blockquote',):
            e_type = 'blockquote'
        elif e_type in ('expandable_blockquote', 'expandableblockquote'):
            e_type = 'expandable_blockquote'
        elif e_type in ('header', 'heading'):
            e_type = 'header'
        else:
            continue

        py_start, py_end = get_py_indices(offset, length)
        if py_start < py_end and py_start < len(text):
            if e_type == 'text_link' and url and url.strip() == text[py_start:py_end].strip():
                continue
            parsed.append({
                'type': e_type,
                'start': py_start,
                'end': py_end,
                'url': url,
                'lang': lang,
                'user_id': user_id,
            })

    if not parsed:
        return text

    bq_spans = [e for e in parsed if e['type'] in ('blockquote', 'expandable_blockquote')]
    inline_ents = [e for e in parsed if e['type'] not in ('blockquote', 'expandable_blockquote')]

    open_tags = {i: [] for i in range(len(text) + 1)}
    close_tags = {i: [] for i in range(len(text) + 1)}

    for ent in inline_ents:
        open_tags[ent['start']].append(ent)
        close_tags[ent['end']].append(ent)

    for idx in open_tags:
        open_tags[idx].sort(key=lambda x: -(x['end'] - x['start']))
    for idx in close_tags:
        close_tags[idx].sort(key=lambda x: (x['end'] - x['start']))

    def get_open_str(ent):
        t = ent['type']
        if t == 'bold': return '**'
        if t == 'italic': return '*'
        if t == 'underline': return '__'
        if t == 'strikethrough': return '~'
        if t == 'spoiler': return '||'
        if t == 'code': return '`'
        if t == 'pre':
            l = ent.get('lang') or ''
            return f"```{l}\n"
        if t in ('text_link', 'text_mention'):
            return '['
        if t == 'header':
            return '# '
        return ''

    def get_close_str(ent):
        t = ent['type']
        if t == 'bold': return '**'
        if t == 'italic': return '*'
        if t == 'underline': return '__'
        if t == 'strikethrough': return '~'
        if t == 'spoiler': return '||'
        if t == 'code': return '`'
        if t == 'pre': return '\n```'
        if t == 'text_link':
            url = ent.get('url') or ''
            return f"]({url})"
        if t == 'text_mention':
            uid = ent.get('user_id') or ''
            return f"](tg://user?id={uid})"
        return ''

    res = []
    for i in range(len(text) + 1):
        for ent in close_tags[i]:
            res.append(get_close_str(ent))
        for ent in open_tags[i]:
            res.append(get_open_str(ent))
        if i < len(text):
            res.append(text[i])

    formatted = ''.join(res)

    if bq_spans:
        lines = formatted.split('\n')
        new_lines = []
        for line in lines:
            if line.strip():
                new_lines.append(f"> {line}" if not line.startswith('> ') else line)
            else:
                new_lines.append(">")
        formatted = '\n'.join(new_lines)

    return formatted


def _format_poll_text(obj) -> str:
    """Safely extract and format text from a poll question or answer (supports str and TextWithEntities)."""
    if not obj:
        return ""
    if isinstance(obj, str):
        return obj
    raw = getattr(obj, 'text', None)
    if raw is not None:
        raw_str = str(raw)
        entities = getattr(obj, 'entities', []) or []
        if entities:
            return _format_telegram_entities(raw_str, entities)
        return raw_str
    return str(obj)


@dataclass
class TelegramRichVideo:
    """Represents a video embedded in a Telegram channel post."""
    video_url: str = ""
    poster_url: str = ""
    duration: str = ""
    is_too_big: bool = False


@dataclass
class TelegramRichPost:
    """Structured representation of a Telegram post, channel article, or media album."""
    username: str
    post_id: int
    author_name: str = ""
    author_avatar_url: str = ""
    text_html: str = ""
    text_markdown: str = ""
    teaser: str = ""
    image_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    videos: list[TelegramRichVideo] = field(default_factory=list)
    album_post_ids: list[int] = field(default_factory=list)
    published_date: str = ""
    views: str = ""
    is_rich: bool = False


TG_POST_WEBXDC_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>{title}</title>
    <style>
        :root {{
            --bg-color: #f4f5f7;
            --card-bg: #ffffff;
            --text-color: #0e0e0e;
            --text-secondary: #707579;
            --accent-color: #2481cc;
            --accent-hover: #1c6ba8;
            --border-color: #e4e4e7;
            --quote-bg: #f8f9fa;
            --code-bg: #f1f3f5;
            --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        @media (prefers-color-scheme: dark) {{
            :root {{
                --bg-color: #0f141a;
                --card-bg: #18222d;
                --text-color: #f5f5f5;
                --text-secondary: #8899a6;
                --accent-color: #2ea6ff;
                --accent-hover: #4bb3ff;
                --border-color: #283543;
                --quote-bg: #1c2733;
                --code-bg: #131c26;
            }}
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background-color: var(--bg-color);
            color: var(--text-color);
            font-family: var(--font-family);
            line-height: 1.6;
            display: flex;
            justify-content: center;
            padding: 16px;
        }}
        .container {{
            max-width: 680px;
            width: 100%;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 24px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.06);
        }}
        @media (max-width: 600px) {{
            body {{ padding: 0; }}
            .container {{
                border-radius: 0;
                border: none;
                padding: 16px;
                box-shadow: none;
            }}
        }}
        header.post-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 16px;
            margin-bottom: 20px;
        }}
        .author-box {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .avatar {{
            width: 48px;
            height: 48px;
            border-radius: 50%;
            object-fit: cover;
            background: var(--border-color);
        }}
        .author-info h2 {{
            font-size: 1.1rem;
            font-weight: 700;
            line-height: 1.2;
            color: var(--text-color);
        }}
        .author-meta {{
            font-size: 0.82rem;
            color: var(--text-secondary);
        }}
        .tg-link-btn {{
            background: var(--accent-color);
            color: #ffffff;
            text-decoration: none;
            padding: 7px 14px;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 600;
            white-space: nowrap;
            transition: background 0.2s;
        }}
        .tg-link-btn:hover {{
            background: var(--accent-hover);
        }}
        .post-content {{
            font-size: 1.05rem;
            word-wrap: break-word;
            overflow-wrap: break-word;
        }}
        .post-content p {{
            margin-bottom: 16px;
            line-height: 1.6;
            word-break: break-word;
        }}
        .post-figure {{
            margin: 20px 0;
            text-align: center;
        }}
        .post-figure img {{
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            cursor: zoom-in;
            display: block;
            margin: 0 auto;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            transition: transform 0.2s;
        }}
        .post-figure img:hover {{
            transform: scale(1.01);
        }}
        .post-figure figcaption {{
            margin-top: 8px;
            font-size: 0.88rem;
            color: var(--text-secondary);
            font-style: italic;
            line-height: 1.4;
        }}
        .post-collage {{
            margin: 20px 0;
        }}
        .post-content a {{
            color: var(--accent-color);
            text-decoration: underline;
            text-underline-offset: 3px;
        }}
        .post-content blockquote {{
            border-left: 4px solid var(--accent-color);
            background: var(--quote-bg);
            padding: 10px 16px;
            margin: 14px 0;
            border-radius: 0 8px 8px 0;
            font-style: italic;
        }}
        .post-content code {{
            font-family: "SF Mono", Monaco, Menlo, Consolas, monospace;
            background: var(--code-bg);
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.9em;
        }}
        .post-content pre {{
            background: var(--code-bg);
            padding: 14px;
            border-radius: 8px;
            overflow-x: auto;
            margin: 14px 0;
        }}
        .post-content pre code {{
            background: none;
            padding: 0;
        }}
        .spoiler {{
            filter: blur(6px);
            background: var(--border-color);
            border-radius: 4px;
            padding: 0 4px;
            cursor: pointer;
            transition: filter 0.2s ease, background 0.2s ease;
            user-select: none;
        }}
        .spoiler.revealed {{
            filter: none;
            background: transparent;
            user-select: text;
        }}
        .table-wrap {{
            overflow-x: auto;
            margin: 16px 0;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.95rem;
        }}
        th, td {{
            border: 1px solid var(--border-color);
            padding: 8px 12px;
            text-align: left;
        }}
        th {{
            background: var(--quote-bg);
            font-weight: 600;
        }}
        tr:nth-child(even) td {{
            background: var(--quote-bg);
        }}
        .gallery-single {{
            margin: 18px 0;
            border-radius: 12px;
            overflow: hidden;
            text-align: center;
        }}
        .gallery-single img {{
            width: 100%;
            max-height: 550px;
            object-fit: contain;
            border-radius: 12px;
            cursor: zoom-in;
            display: block;
        }}
        .gallery-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 8px;
            margin: 18px 0;
            border-radius: 12px;
            overflow: hidden;
        }}
        .gallery-item {{
            position: relative;
            aspect-ratio: 1 / 1;
            overflow: hidden;
            background: var(--border-color);
            border-radius: 8px;
        }}
        .gallery-item img {{
            width: 100%;
            height: 100%;
            object-fit: cover;
            cursor: zoom-in;
            transition: transform 0.2s;
        }}
        .gallery-item img:hover {{
            transform: scale(1.02);
        }}
        .video-container {{
            position: relative;
            margin: 18px 0;
            border-radius: 12px;
            overflow: hidden;
            background: #000000;
            text-align: center;
        }}
        .video-container video {{
            width: 100%;
            max-height: 550px;
            display: block;
            border-radius: 12px;
            outline: none;
        }}
        .video-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 12px;
            margin: 18px 0;
        }}
        .video-overflow-card {{
            position: relative;
            margin: 16px 0;
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid var(--border-color);
            background: var(--quote-bg);
        }}
        .video-overflow-thumb {{
            position: relative;
            width: 100%;
            height: 240px;
            background-size: cover;
            background-position: center;
            background-color: #1a1a1a;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .video-play-icon {{
            width: 54px;
            height: 54px;
            background: rgba(0, 0, 0, 0.65);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #ffffff;
            font-size: 24px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            cursor: pointer;
        }}
        .video-duration-badge {{
            position: absolute;
            bottom: 10px;
            right: 10px;
            background: rgba(0, 0, 0, 0.75);
            color: #ffffff;
            padding: 3px 8px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 600;
        }}
        .video-overflow-footer {{
            padding: 12px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
        }}
        .video-overflow-title {{
            font-size: 0.95rem;
            font-weight: 600;
            color: var(--text-color);
        }}
        .tg-video-btn {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: var(--accent-color);
            color: #ffffff !important;
            text-decoration: none !important;
            padding: 7px 14px;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 600;
            transition: background 0.2s;
        }}
        .tg-video-btn:hover {{
            background: var(--accent-hover);
        }}
        footer.post-footer {{
            margin-top: 24px;
            padding-top: 14px;
            border-top: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.85rem;
            color: var(--text-secondary);
        }}
        #lightbox {{
            display: none;
            position: fixed;
            z-index: 9999;
            top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0, 0, 0, 0.94);
            flex-direction: column;
            justify-content: space-between;
            align-items: center;
            user-select: none;
        }}
        .lightbox-header {{
            width: 100%;
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 20px;
            color: #ffffff;
            font-size: 0.95rem;
            box-sizing: border-box;
            z-index: 10001;
        }}
        .lightbox-close {{
            background: none;
            border: none;
            color: #ffffff;
            font-size: 28px;
            cursor: pointer;
            padding: 4px 10px;
            line-height: 1;
            border-radius: 6px;
            transition: background 0.2s;
        }}
        .lightbox-close:hover {{
            background: rgba(255,255,255,0.15);
        }}
        .lightbox-content {{
            position: relative;
            max-width: 90vw;
            max-height: calc(100vh - 170px);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            flex: 1;
        }}
        #lightbox-img {{
            max-width: 90vw;
            max-height: calc(100vh - 190px);
            object-fit: contain;
            border-radius: 6px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.5);
        }}
        #lightbox-caption {{
            margin-top: 10px;
            color: #e0e0e0;
            font-size: 0.9rem;
            font-style: italic;
            text-align: center;
            max-width: 80vw;
        }}
        .lightbox-prev, .lightbox-next {{
            position: absolute;
            top: 50%;
            transform: translateY(-50%);
            background: rgba(0,0,0,0.5);
            border: 1px solid rgba(255,255,255,0.2);
            color: #ffffff;
            font-size: 26px;
            padding: 12px 16px;
            cursor: pointer;
            border-radius: 50%;
            transition: background 0.2s, transform 0.1s;
            z-index: 10001;
        }}
        .lightbox-prev {{ left: 16px; }}
        .lightbox-next {{ right: 16px; }}
        .lightbox-prev:hover, .lightbox-next:hover {{
            background: rgba(255,255,255,0.25);
        }}
        .lightbox-thumbnails {{
            width: 100%;
            overflow-x: auto;
            display: flex;
            justify-content: center;
            gap: 8px;
            padding: 12px 16px;
            background: rgba(0,0,0,0.4);
            box-sizing: border-box;
            z-index: 10001;
        }}
        .lightbox-thumb {{
            width: 48px;
            height: 48px;
            object-fit: cover;
            border-radius: 4px;
            opacity: 0.6;
            cursor: pointer;
            border: 2px solid transparent;
            transition: opacity 0.2s, border-color 0.2s;
            flex-shrink: 0;
        }}
        .lightbox-thumb.active {{
            opacity: 1;
            border-color: var(--accent-color);
        }}
    </style>
</head>
<body>
    <div class="container">
        <header class="post-header">
            <div class="author-box">
                {avatar_html}
                <div class="author-info">
                    <h2>{author_name}</h2>
                    <div class="author-meta">{meta_text}</div>
                </div>
            </div>
            <a href="{source_url}" target="_blank" rel="noopener noreferrer" class="tg-link-btn">Open in TG ↗</a>
        </header>

        {gallery_top_html}

        {video_html}

        <article class="post-content">
            {content_html}
        </article>

        {gallery_bottom_html}

        <footer class="post-footer">
            <div class="views-box">{views_text}</div>
            <a href="{source_url}" target="_blank" rel="noopener noreferrer" style="color: var(--accent-color); text-decoration: none;">View original on Telegram</a>
        </footer>

        <hr style="border: none; border-top: 1px solid var(--border-color); margin-top: 25px; margin-bottom: 15px;">
        <footer style="font-size: 0.85rem; color: var(--text-secondary); text-align: center; padding-bottom: 10px;">
            Post bridged at {bridged_at} by <a href="https://git.gluek.info/gluek/deltachat_telegram_bridge" target="_blank" rel="noopener noreferrer" style="color: var(--accent-color); text-decoration: none;">Delta Chat Telegram Bridge</a>.
        </footer>
    </div>

    <div id="lightbox">
        <div class="lightbox-header">
            <div id="lightbox-counter">1 / 1</div>
            <button class="lightbox-close" onclick="closeLightbox()">&times;</button>
        </div>
        <button class="lightbox-prev" onclick="prevLightboxImage(event)">&#10094;</button>
        <div class="lightbox-content">
            <img id="lightbox-img" src="" alt="Fullscreen view" />
            <div id="lightbox-caption"></div>
        </div>
        <button class="lightbox-next" onclick="nextLightboxImage(event)">&#10095;</button>
        <div id="lightbox-thumbnails" class="lightbox-thumbnails"></div>
    </div>

    <script>
        var galleryImages = [];
        var currentIndex = 0;

        function initGallery() {{
            var imgs = document.querySelectorAll('.post-content img, .gallery-grid img, .gallery-single img');
            galleryImages = [];
            imgs.forEach(function(img) {{
                var fig = img.closest('figure');
                var cap = fig ? (fig.querySelector('figcaption') ? fig.querySelector('figcaption').textContent : '') : (img.getAttribute('alt') || '');
                var item = {{
                    src: img.getAttribute('src'),
                    caption: cap
                }};
                var existing = galleryImages.findIndex(function(g) {{ return g.src === item.src; }});
                var pos = existing >= 0 ? existing : galleryImages.length;
                if (existing < 0) {{
                    galleryImages.push(item);
                }}
                img.style.cursor = 'zoom-in';
                img.addEventListener('click', function(e) {{
                    e.stopPropagation();
                    openLightboxByIndex(pos);
                }});
            }});

            var thumbContainer = document.getElementById('lightbox-thumbnails');
            if (thumbContainer) {{
                thumbContainer.innerHTML = '';
                if (galleryImages.length > 1) {{
                    galleryImages.forEach(function(item, idx) {{
                        var thumb = document.createElement('img');
                        thumb.src = item.src;
                        thumb.className = 'lightbox-thumb';
                        thumb.onclick = function(e) {{
                            e.stopPropagation();
                            openLightboxByIndex(idx);
                        }};
                        thumbContainer.appendChild(thumb);
                    }});
                    thumbContainer.style.display = 'flex';
                }} else {{
                    thumbContainer.style.display = 'none';
                }}
            }}
        }}

        function openLightboxByIndex(idx) {{
            if (idx < 0 || idx >= galleryImages.length) return;
            currentIndex = idx;
            var item = galleryImages[idx];
            document.getElementById('lightbox-img').src = item.src;
            document.getElementById('lightbox-caption').textContent = item.caption || '';
            document.getElementById('lightbox-counter').textContent = (idx + 1) + ' / ' + galleryImages.length;

            var prevBtn = document.querySelector('.lightbox-prev');
            var nextBtn = document.querySelector('.lightbox-next');
            if (prevBtn && nextBtn) {{
                prevBtn.style.display = galleryImages.length > 1 ? 'block' : 'none';
                nextBtn.style.display = galleryImages.length > 1 ? 'block' : 'none';
            }}

            var thumbs = document.querySelectorAll('.lightbox-thumb');
            thumbs.forEach(function(t, i) {{
                if (i === idx) {{
                    t.classList.add('active');
                    t.scrollIntoView({{ behavior: 'smooth', inline: 'center', block: 'nearest' }});
                }} else {{
                    t.classList.remove('active');
                }}
            }});

            var lb = document.getElementById('lightbox');
            lb.style.display = 'flex';
        }}

        function closeLightbox() {{
            document.getElementById('lightbox').style.display = 'none';
        }}

        function prevLightboxImage(e) {{
            if (e) e.stopPropagation();
            if (galleryImages.length <= 1) return;
            var prev = currentIndex > 0 ? currentIndex - 1 : galleryImages.length - 1;
            openLightboxByIndex(prev);
        }}

        function nextLightboxImage(e) {{
            if (e) e.stopPropagation();
            if (galleryImages.length <= 1) return;
            var next = currentIndex < galleryImages.length - 1 ? currentIndex + 1 : 0;
            openLightboxByIndex(next);
        }}

        document.addEventListener('keydown', function(e) {{
            var lb = document.getElementById('lightbox');
            if (lb && lb.style.display === 'flex') {{
                if (e.key === 'ArrowLeft') prevLightboxImage();
                else if (e.key === 'ArrowRight') nextLightboxImage();
                else if (e.key === 'Escape') closeLightbox();
            }}
        }});

        var touchStartX = 0;
        var touchEndX = 0;
        var lbEl = document.getElementById('lightbox');
        if (lbEl) {{
            lbEl.addEventListener('touchstart', function(e) {{
                touchStartX = e.changedTouches[0].screenX;
            }}, {{ passive: true }});
            lbEl.addEventListener('touchend', function(e) {{
                touchEndX = e.changedTouches[0].screenX;
                if (touchStartX - touchEndX > 50) nextLightboxImage();
                else if (touchEndX - touchStartX > 50) prevLightboxImage();
            }}, {{ passive: true }});
            lbEl.addEventListener('click', function(e) {{
                if (e.target === lbEl || e.target.classList.contains('lightbox-content')) {{
                    closeLightbox();
                }}
            }});
        }}

        document.addEventListener('DOMContentLoaded', initGallery);
        initGallery();
    </script>
</body>
</html>
"""


def _clean_toml_string(val: str) -> str:
    """Sanitize string for inclusion in manifest.toml."""
    if not val:
        return ""
    val = val.replace("\r", " ").replace("\n", " ")
    return val.replace("\\", "\\\\").replace('"', '\\"')


def _format_paragraph_html(raw_htm: str) -> str:
    """Format paragraph HTML: split multi-paragraph blocks by double newlines, use <br/> for single line breaks."""
    if not raw_htm or not raw_htm.strip():
        return ""
    raw = raw_htm.replace('\r\n', '\n').replace('\r', '\n')
    chunks = re.split(r'\n{2,}', raw)
    paragraphs = []
    for c in chunks:
        c_clean = c.strip('\n')
        if c_clean.strip():
            p_html = c_clean.replace('\n', '<br/>')
            paragraphs.append(f"<p>{p_html}</p>")
    return "\n".join(paragraphs)


def _make_teaser(text: str, max_len: int = 280) -> str:
    """Extract a clean concise teaser from text for message preview."""
    if not text:
        return ""
    clean = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    clean = re.sub(r'`[^`]+`', '', clean)
    clean = re.sub(r'^\s*#+\s*', '', clean, flags=re.MULTILINE)
    clean = re.sub(r'^\s*>\s*', '', clean, flags=re.MULTILINE)
    clean = ' '.join(clean.split())
    if len(clean) <= max_len:
        return clean
    truncated = clean[:max_len]
    last_space = truncated.rfind(' ')
    if last_space > max_len // 2:
        truncated = truncated[:last_space]
    return truncated.strip() + "…"


def _is_safe_telegram_url(url: str) -> bool:
    """Validate that remote URL uses http(s) and targets legitimate Telegram/CDN hosts without resolving to private/internal networks."""
    if not url or not isinstance(url, str):
        return False
    try:
        from urllib.parse import urlparse
        import ipaddress
        import socket

        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        hostname = (p.hostname or "").lower().strip()
        if not hostname:
            return False
        if hostname in ("localhost", "127.0.0.1", "::1") or hostname.endswith((".local", ".internal", ".lan")):
            return False

        # Reject direct IP addresses (Telegram media and embed endpoints are hosted on named domains)
        try:
            ipaddress.ip_address(hostname)
            return False
        except ValueError:
            pass

        allowed_suffixes = (
            "t.me", "telegram.me", "telegram.org",
            "telesco.pe", "cdn-telegram.org", "telegram-cdn.org", "stel.com"
        )
        if not any(hostname == s or hostname.endswith("." + s) for s in allowed_suffixes):
            return False

        # DNS resolution check: block domains resolving to private, loopback, link-local, multicast, or reserved IPs
        try:
            addr_info = socket.getaddrinfo(hostname, None)
            for _, _, _, _, sockaddr in addr_info:
                ip_str = sockaddr[0]
                ip = ipaddress.ip_address(ip_str)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                    return False
                if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                    mapped_v4 = ip.ipv4_mapped
                    if (
                        mapped_v4.is_private
                        or mapped_v4.is_loopback
                        or mapped_v4.is_link_local
                        or mapped_v4.is_multicast
                        or mapped_v4.is_reserved
                    ):
                        return False
        except (socket.gaierror, OSError):
            # In offline or mock unit test environments, unresolvable allowed test hosts are tolerated
            pass

        return True
    except Exception:
        return False


def _clean_html_for_webxdc(raw_html: str) -> str:
    """Clean and normalize HTML for inclusion inside WebXDC application."""
    if not raw_html:
        return ""
    # Strip script and embed elements
    cleaned = re.sub(r'<script[^>]*>.*?</script>', '', raw_html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<(?:iframe|object|embed|applet)[^>]*>.*?</(?:iframe|object|embed|applet)>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<(?:iframe|object|embed|applet)[^>]*/?>', '', cleaned, flags=re.IGNORECASE)
    # Strip dangerous inline event handlers (onload, onclick, onerror, etc.)
    cleaned = re.sub(r'\s+on[a-zA-Z]+\s*=\s*(?:\"[^\"]*\"|\'[^\']*\'|[^\s>]+)', '', cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r'<tg-emoji[^>]*>(?:<i[^>]*>)?(.*?)(?:</i>)?</tg-emoji>', r'\1', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'<tg-spoiler[^>]*>(.*?)</tg-spoiler>', r"""<span class="spoiler" onclick="this.classList.toggle('revealed')">\1</span>""", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'<span class="tg-spoiler"[^>]*>(.*?)</span>', r"""<span class="spoiler" onclick="this.classList.toggle('revealed')">\1</span>""", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'<blockquote[^>]*\bexpandable\b[^>]*>(.*?)</blockquote>', r'<blockquote class="expandable" onclick="this.classList.toggle(\'expanded\')">\1</blockquote>', cleaned, flags=re.DOTALL)
    def _link_sub(m):
        href = m.group(1).strip()
        text = m.group(2)
        # Block dangerous URI schemes
        clean_scheme = href.lower()
        if clean_scheme.startswith(("javascript:", "data:", "vbscript:", "file:")):
            return text
        return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{text}</a>'
    cleaned = re.sub(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', _link_sub, cleaned, flags=re.DOTALL)
    if '<table' in cleaned and 'table-wrap' not in cleaned:
        cleaned = re.sub(r'(<table[^>]*>.*?</table>)', r'<div class="table-wrap">\1</div>', cleaned, flags=re.DOTALL)
    return cleaned.strip()


def _rich_text_to_markdown(rt) -> str:
    """Convert Telethon TypeRichText AST object to a Markdown formatted string."""
    if rt is None:
        return ""
    name = type(rt).__name__
    if name == 'TextEmpty':
        return ""
    if name == 'TextPlain':
        return getattr(rt, 'text', '') or ""
    if name == 'TextBold':
        return f"**{_rich_text_to_markdown(getattr(rt, 'text', None))}**"
    if name == 'TextItalic':
        return f"*{_rich_text_to_markdown(getattr(rt, 'text', None))}*"
    if name == 'TextUnderline':
        return f"__{_rich_text_to_markdown(getattr(rt, 'text', None))}__"
    if name == 'TextStrike':
        return f"~{_rich_text_to_markdown(getattr(rt, 'text', None))}~"
    if name == 'TextFixed':
        return f"`{_rich_text_to_markdown(getattr(rt, 'text', None))}`"
    if name == 'TextSpoiler':
        return f"||{_rich_text_to_markdown(getattr(rt, 'text', None))}||"
    if name in ('TextUrl', 'TextAutoUrl'):
        url = getattr(rt, 'url', '') or ""
        inner = _rich_text_to_markdown(getattr(rt, 'text', None))
        return f"[{inner}]({url})" if url else inner
    if name == 'TextEmail':
        email = getattr(rt, 'email', '') or ""
        inner = _rich_text_to_markdown(getattr(rt, 'text', None))
        return f"[{inner}](mailto:{email})" if email else inner
    if name == 'TextConcat':
        texts = getattr(rt, 'texts', []) or []
        return "".join(_rich_text_to_markdown(t) for t in texts)
    if hasattr(rt, 'text'):
        return _rich_text_to_markdown(getattr(rt, 'text', None))
    return str(rt)


def _rich_text_to_html(rt) -> str:
    """Convert Telethon TypeRichText AST object to sanitized HTML for WebXDC."""
    if rt is None:
        return ""
    name = type(rt).__name__
    if name == 'TextEmpty':
        return ""
    if name == 'TextPlain':
        return html.escape(getattr(rt, 'text', '') or "")
    if name == 'TextBold':
        return f"<b>{_rich_text_to_html(getattr(rt, 'text', None))}</b>"
    if name == 'TextItalic':
        return f"<i>{_rich_text_to_html(getattr(rt, 'text', None))}</i>"
    if name == 'TextUnderline':
        return f"<u>{_rich_text_to_html(getattr(rt, 'text', None))}</u>"
    if name == 'TextStrike':
        return f"<s>{_rich_text_to_html(getattr(rt, 'text', None))}</s>"
    if name == 'TextFixed':
        return f"<code>{_rich_text_to_html(getattr(rt, 'text', None))}</code>"
    if name == 'TextSpoiler':
        inner = _rich_text_to_html(getattr(rt, 'text', None))
        return f'<span class="spoiler">{inner}</span>'
    if name in ('TextUrl', 'TextAutoUrl'):
        url = getattr(rt, 'url', '') or ""
        inner = _rich_text_to_html(getattr(rt, 'text', None))
        escaped_url = html.escape(url)
        return f'<a href="{escaped_url}" target="_blank" rel="noopener noreferrer">{inner}</a>' if url else inner
    if name == 'TextEmail':
        email = getattr(rt, 'email', '') or ""
        inner = _rich_text_to_html(getattr(rt, 'text', None))
        escaped_email = html.escape(email)
        return f'<a href="mailto:{escaped_email}">{inner}</a>' if email else inner
    if name == 'TextConcat':
        texts = getattr(rt, 'texts', []) or []
        return "".join(_rich_text_to_html(t) for t in texts)
    if hasattr(rt, 'text'):
        return _rich_text_to_html(getattr(rt, 'text', None))
    return html.escape(str(rt))


def _largest_real_photo_size(photo):
    """Pick the largest real (non-stub) size by pixel area for a Telethon Photo.

    Telethon's own download_media()/thumb=None selection sorts sizes by their reported
    byte count (PhotoSize.size), but RichMessage-sourced photos can report a 0/bogus
    byte size on their real PhotoSize entries, which makes the tiny PhotoStrippedSize
    blur placeholder sort as "largest" and get downloaded instead. Selecting by w*h
    instead sidesteps that. Returns None if there's no real (non-stub) size at all,
    in which case the caller should fall back to Telethon's default (thumb=None).
    Callers must pass the size's .type string as thumb=, not the object: Telethon's
    _get_thumb() rejects PhotoSizeProgressive instances and downloads nothing.
    """
    best = None
    best_area = -1
    for s in (getattr(photo, 'sizes', None) or []):
        w = getattr(s, 'w', None)
        h = getattr(s, 'h', None)
        if w is None or h is None:
            continue
        area = w * h
        if area > best_area:
            best_area = area
            best = s
    return best


async def _process_page_blocks(
    blocks,
    photos_map: dict,
    docs_map: dict,
    userbot_client,
    post_id: int,
    image_urls: list,
    videos: list,
    downloaded_photo_ids: set,
    downloaded_doc_ids: set,
) -> tuple[list[str], list[str]]:
    """Recursively process a list of Telethon PageBlock objects into (md_parts, htm_parts)."""
    md_parts = []
    htm_parts = []

    for b in (blocks or []):
        b_type = type(b).__name__

        if b_type == 'PageBlockParagraph':
            md = _rich_text_to_markdown(getattr(b, 'text', None))
            htm = _format_paragraph_html(_rich_text_to_html(getattr(b, 'text', None)))
            if md:
                md_parts.append(md)
            if htm:
                htm_parts.append(htm)

        elif b_type in ('PageBlockHeader', 'PageBlockHeading1', 'PageBlockHeading2', 'PageBlockHeading3', 'PageBlockTitle'):
            md = f"### {_rich_text_to_markdown(getattr(b, 'text', None))}"
            htm = f"<h3>{_rich_text_to_html(getattr(b, 'text', None))}</h3>"
            if md:
                md_parts.append(md)
            if htm:
                htm_parts.append(htm)

        elif b_type in ('PageBlockSubheader', 'PageBlockSubtitle', 'PageBlockHeading4', 'PageBlockHeading5', 'PageBlockHeading6', 'PageBlockKicker'):
            md = f"#### {_rich_text_to_markdown(getattr(b, 'text', None))}"
            htm = f"<h4>{_rich_text_to_html(getattr(b, 'text', None))}</h4>"
            if md:
                md_parts.append(md)
            if htm:
                htm_parts.append(htm)

        elif b_type in ('PageBlockBlockquote', 'PageBlockPullquote'):
            q_md = _rich_text_to_markdown(getattr(b, 'text', None))
            q_htm = _rich_text_to_html(getattr(b, 'text', None))
            if q_md:
                md_parts.append("\n".join(f"> {line}" for line in q_md.split("\n")))
            if q_htm:
                htm_parts.append(f"<blockquote>{q_htm}</blockquote>")

        elif b_type == 'PageBlockBlockquoteBlocks':
            child_blocks = getattr(b, 'blocks', []) or []
            c_md, c_htm = await _process_page_blocks(
                child_blocks, photos_map, docs_map, userbot_client,
                post_id, image_urls, videos, downloaded_photo_ids, downloaded_doc_ids
            )
            if c_md:
                md_parts.append("\n".join(f"> {line}" for line in "\n\n".join(c_md).split("\n")))
            if c_htm:
                c_htm_joined = "\n".join(c_htm)
                htm_parts.append(f"<blockquote>{c_htm_joined}</blockquote>")

        elif b_type == 'PageBlockPreformatted':
            lang = getattr(b, 'language', '') or ''
            c_md = _rich_text_to_markdown(getattr(b, 'text', None))
            c_htm = _rich_text_to_html(getattr(b, 'text', None))
            md_parts.append(f"```{lang}\n{c_md}\n```")
            htm_parts.append(f'<pre><code class="{lang}">{c_htm}</code></pre>')

        elif b_type == 'PageBlockDivider':
            md_parts.append("---")
            htm_parts.append("<hr/>")

        elif b_type == 'PageBlockList':
            items = getattr(b, 'items', []) or []
            l_md = []
            l_htm = []
            for item in items:
                if hasattr(item, 'blocks'):
                    c_md, c_htm = await _process_page_blocks(
                        item.blocks, photos_map, docs_map, userbot_client,
                        post_id, image_urls, videos, downloaded_photo_ids, downloaded_doc_ids
                    )
                    if c_md:
                        l_md.append(f"- {' '.join(c_md)}")
                    if c_htm:
                        l_htm.append(f"<li>{' '.join(c_htm)}</li>")
                elif hasattr(item, 'text'):
                    t_m = _rich_text_to_markdown(item.text)
                    t_h = _rich_text_to_html(item.text)
                    if t_m:
                        l_md.append(f"- {t_m}")
                    if t_h:
                        l_htm.append(f"<li>{t_h}</li>")
            if l_md:
                md_parts.append("\n".join(l_md))
            if l_htm:
                htm_parts.append(f"<ul>{''.join(l_htm)}</ul>")

        elif b_type == 'PageBlockOrderedList':
            items = getattr(b, 'items', []) or []
            start = getattr(b, 'start', 1) or 1
            l_md = []
            l_htm = []
            for idx, item in enumerate(items, start=start):
                if hasattr(item, 'blocks'):
                    c_md, c_htm = await _process_page_blocks(
                        item.blocks, photos_map, docs_map, userbot_client,
                        post_id, image_urls, videos, downloaded_photo_ids, downloaded_doc_ids
                    )
                    if c_md:
                        l_md.append(f"{idx}. {' '.join(c_md)}")
                    if c_htm:
                        l_htm.append(f"<li>{' '.join(c_htm)}</li>")
                elif hasattr(item, 'text'):
                    t_m = _rich_text_to_markdown(item.text)
                    t_h = _rich_text_to_html(item.text)
                    if t_m:
                        l_md.append(f"{idx}. {t_m}")
                    if t_h:
                        l_htm.append(f"<li>{t_h}</li>")
            if l_md:
                md_parts.append("\n".join(l_md))
            if l_htm:
                htm_parts.append(f"<ol>{''.join(l_htm)}</ol>")

        elif b_type == 'PageBlockTable':
            rows = getattr(b, 'rows', []) or []
            t_md = []
            t_htm = ['<div class="table-wrap"><table>']
            for r in rows:
                cells = getattr(r, 'cells', []) or []
                r_md = []
                r_htm = ['<tr>']
                for c in cells:
                    c_m = _rich_text_to_markdown(getattr(c, 'text', None))
                    c_h = _rich_text_to_html(getattr(c, 'text', None))
                    tag = 'th' if getattr(c, 'header', False) else 'td'
                    r_md.append(c_m)
                    r_htm.append(f"<{tag}>{c_h}</{tag}>")
                r_htm.append('</tr>')
                t_md.append("| " + " | ".join(r_md) + " |")
                t_htm.append("".join(r_htm))
            t_htm.append('</table></div>')
            if t_md:
                md_parts.append("\n".join(t_md))
            htm_parts.append("".join(t_htm))

        elif b_type == 'PageBlockCover':
            cover = getattr(b, 'cover', None)
            if cover:
                c_md, c_htm = await _process_page_blocks(
                    [cover], photos_map, docs_map, userbot_client,
                    post_id, image_urls, videos, downloaded_photo_ids, downloaded_doc_ids
                )
                md_parts.extend(c_md)
                htm_parts.extend(c_htm)

        elif b_type == 'PageBlockPhoto':
            photo_id = getattr(b, 'photo_id', None)
            photo_obj = photos_map.get(photo_id)
            p_path = None
            if photo_obj and userbot_client:
                try:
                    p_tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False).name
                    best_size = _largest_real_photo_size(photo_obj)
                    p_path = await asyncio.wait_for(userbot_client.download_media(photo_obj, file=p_tmp, thumb=getattr(best_size, "type", None)), timeout=60.0)
                    if p_path and os.path.exists(p_path) and os.path.getsize(p_path) > 0:
                        downloaded_photo_ids.add(photo_id)
                    else:
                        logger.warning(f"Inline photo {photo_id} in post {post_id} downloaded empty; dropping it")
                        p_path = None
                except Exception as e:
                    logger.warning(f"Failed to download inline photo {photo_id} in post {post_id}: {e}")
            elif not photo_obj:
                logger.warning(f"Inline photo {photo_id} in post {post_id} not present in RichMessage.photos; dropping it")

            cap_obj = getattr(b, 'caption', None)
            cap_text = getattr(cap_obj, 'text', None) if cap_obj else None
            c_m = _rich_text_to_markdown(cap_text) if cap_text else ""
            c_h = _rich_text_to_html(cap_text) if cap_text else ""

            if p_path:
                img_idx = len(image_urls)
                image_urls.append(p_path)
                cap_tag = f"<figcaption><i>{c_h}</i></figcaption>" if c_h else ""
                htm_parts.append(
                    f'<figure class="post-figure">'
                    f'<img src="images/img_{img_idx}.webp" alt="Photo {img_idx+1}" loading="lazy" />'
                    f'{cap_tag}'
                    f'</figure>'
                )
                md_parts.append(f"[📷 Photo {img_idx+1}]" + (f"\n*{c_m}*" if c_m else ""))
            elif c_h:
                htm_parts.append(f'<p class="caption"><i>{c_h}</i></p>')
                if c_m:
                    md_parts.append(f"*{c_m}*")

        elif b_type in ('PageBlockCollage', 'PageBlockSlideshow'):
            items = getattr(b, 'items', []) or []
            c_imgs = []
            for item in items:
                if type(item).__name__ == 'PageBlockPhoto':
                    p_id = getattr(item, 'photo_id', None)
                    p_obj = photos_map.get(p_id)
                    if p_obj and userbot_client:
                        try:
                            p_tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False).name
                            best_size = _largest_real_photo_size(p_obj)
                            dl_p = await asyncio.wait_for(userbot_client.download_media(p_obj, file=p_tmp, thumb=getattr(best_size, "type", None)), timeout=60.0)
                            if dl_p and os.path.exists(dl_p) and os.path.getsize(dl_p) > 0:
                                i_idx = len(image_urls)
                                image_urls.append(dl_p)
                                downloaded_photo_ids.add(p_id)
                                c_imgs.append(f"images/img_{i_idx}.webp")
                            else:
                                logger.warning(f"Collage photo {p_id} in post {post_id} downloaded empty; dropping it")
                        except Exception as e:
                            logger.warning(f"Failed to download collage photo {p_id} in post {post_id}: {e}")
                    elif not p_obj:
                        logger.warning(f"Collage photo {p_id} in post {post_id} not present in RichMessage.photos; dropping it")
            cap_obj = getattr(b, 'caption', None)
            cap_text = getattr(cap_obj, 'text', None) if cap_obj else None
            c_h = _rich_text_to_html(cap_text) if cap_text else ""
            if c_imgs:
                grid_items = "\n".join(
                    f'<div class="gallery-item"><img src="{src}" alt="Photo" loading="lazy" /></div>'
                    for src in c_imgs
                )
                cap_tag = f'<figcaption><i>{c_h}</i></figcaption>' if c_h else ''
                htm_parts.append(f'<figure class="post-collage"><div class="gallery-grid">\n{grid_items}\n</div>{cap_tag}</figure>')
                md_parts.append(f"[Collage of {len(c_imgs)} photos]")

        elif b_type == 'PageBlockVideo':
            video_id = getattr(b, 'video_id', None)
            doc_obj = docs_map.get(video_id)
            v_path = None
            is_playable = False
            if doc_obj and userbot_client:
                doc_size = getattr(doc_obj, 'size', 0) or 0
                if 0 < doc_size <= TG_WEBXDC_VIDEO_MAX_BYTES:
                    try:
                        v_tmp = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False).name
                        v_path = await asyncio.wait_for(userbot_client.download_media(doc_obj, file=v_tmp), timeout=90.0)
                        if v_path and os.path.exists(v_path) and os.path.getsize(v_path) > 0:
                            is_playable = True
                            downloaded_doc_ids.add(video_id)
                        else:
                            v_path = None
                    except Exception as e:
                        logger.warning(f"Failed to download inline video {video_id} in post {post_id}: {e}")

            cap_obj = getattr(b, 'caption', None)
            cap_text = getattr(cap_obj, 'text', None) if cap_obj else None
            c_m = _rich_text_to_markdown(cap_text) if cap_text else ""
            c_h = _rich_text_to_html(cap_text) if cap_text else ""

            if is_playable and v_path:
                v_idx = len(videos)
                videos.append(TelegramRichVideo(
                    video_url=v_path,
                    duration="",
                ))
                cap_tag = f'<p class="caption"><i>{c_h}</i></p>' if c_h else ''
                htm_parts.append(
                    f'<div class="video-container">\n'
                    f'    <video controls playsinline preload="metadata">\n'
                    f'        <source src="videos/vid_{v_idx}.mp4" type="video/mp4">\n'
                    f'    </video>\n'
                    f'    {cap_tag}\n'
                    f'</div>'
                )
                md_parts.append(f"[📹 Video {v_idx+1}]" + (f"\n*{c_m}*" if c_m else ""))
            else:
                videos.append(TelegramRichVideo(
                    video_url="",
                    duration="",
                    is_too_big=True,
                ))
                if c_h:
                    htm_parts.append(f'<p class="caption"><i>{c_h}</i></p>')
                if c_m:
                    md_parts.append(f"*{c_m}*")

        elif b_type == 'PageBlockDetails':
            child_blocks = getattr(b, 'blocks', []) or []
            title_obj = getattr(b, 'title', None)
            title_htm = _rich_text_to_html(title_obj) if title_obj else ""
            title_md = _rich_text_to_markdown(title_obj) if title_obj else ""

            c_md, c_htm = await _process_page_blocks(
                child_blocks, photos_map, docs_map, userbot_client,
                post_id, image_urls, videos, downloaded_photo_ids, downloaded_doc_ids
            )
            # Show content directly inline without folding
            skip_title_words = ("show more", "view more", "развернуть", "показать больше", "читать далее")
            clean_title_check = re.sub(r'<[^>]+>', '', title_htm).strip().lower()
            if clean_title_check and clean_title_check not in skip_title_words:
                htm_parts.append(f"<h4>{title_htm}</h4>")
            if title_md and title_md.strip().lower() not in skip_title_words:
                md_parts.append(f"#### {title_md}")

            md_parts.extend(c_md)
            htm_parts.extend(c_htm)

        elif b_type == 'PageBlockEmbedPost':
            child_blocks = getattr(b, 'blocks', []) or []
            c_md, c_htm = await _process_page_blocks(
                child_blocks, photos_map, docs_map, userbot_client,
                post_id, image_urls, videos, downloaded_photo_ids, downloaded_doc_ids
            )
            md_parts.extend(c_md)
            htm_parts.extend(c_htm)

        elif b_type == 'PageBlockFooter':
            f_md = _rich_text_to_markdown(getattr(b, 'text', None))
            f_htm = _rich_text_to_html(getattr(b, 'text', None))
            if f_md:
                md_parts.append(f"*{f_md}*")
            if f_htm:
                htm_parts.append(f'<p style="font-size:0.85rem;color:var(--text-secondary);">{f_htm}</p>')

    return md_parts, htm_parts


def _inline_links(text: str, entities) -> str:
    """Format entities into Markdown (maintains backwards compatibility for _inline_links)."""
    return _format_telegram_entities(text, entities)


def get_dc_help_text(bot, accid, sender_email, from_id):
    import bot as _bot_module
    admin_dc_fingerprint = database.get_config("admin_dc_fingerprint")
    admin_dc_email = database.get_config("admin_dc_email")
    is_admin = _bot_module._is_dc_admin(bot, accid, from_id)
    
    mode = "Private (bot owner only)" if (admin_dc_fingerprint or admin_dc_email) else "Public (group admins only)"
    
    help_text = (
        f"👋 Hi {sender_email}!\n\n"
        f"I'm the TG Bridge bot. Current mode: {mode}\n\n"
        f"I relay messages between Delta Chat and Telegram groups.\n\n"
        f"Commands:\n"
        f"/channels — List bridged Telegram channels\n"
        f"/channelN — Get invite link for channel #N\n"
        f"/channelNqr — Get QR code invite for channel #N\n"
        f"/stats — Show bridge statistics for current chat\n"
        f"/help — Show this help message\n"
        f"/donate — Support bot development ❤️\n"
    )
    
    if not admin_dc_fingerprint and not admin_dc_email:
        help_text += "\n**Management:**\n"
        help_text += "/initadmin — Claim bot ownership (securely link your identity)\n"
    elif is_admin:
        fp_suffix = f" ({admin_dc_fingerprint[-8:].upper()})" if admin_dc_fingerprint else ""
        help_text += f"\n👑 **Admin:** `{admin_dc_email}`{fp_suffix}\n"
        help_text += (
            f"\n**Channel & Bot Management (Owner only):**\n"
            f"/channeladd @name or ID — Bridge a TG channel, group, or Bot (via Userbot)\n"
            f"/channelremove N — Remove a channel bridge\n"
            f"/channels — List bridged channels\n"
            f"/channelssync — Refresh channel names & avatars from TG\n"
            f"/locupdate — Refresh local channel avatars and info\n"
            f"/botsend @bot or ID <text> — Send a command/message to a bridged TG bot\n"
            f"/catchup [@channel] — Catch up missed posts for channel(s)\n"
            f"/userbotjoin <link> — Join channel via Userbot (no admin needed)\n"
            f"\n**Message Filters (Owner only):**\n"
            f"/filters — List active message filters\n"
            f"/filteradd <phrase> — Add word or phrase filter\n"
            f"/filterdel <id or phrase> — Remove a filter\n"
            f"\n**Bridge Management (Owner only):**\n"
            f"/bridge <tg_group_id> — Link DC group to a Telegram group\n"
            f"/unbridge — Remove the bridge from the group\n"
            f"/cleanup — Clean up stale, duplicate & orphaned bridges\n"
            f"/userbotsync — Force Userbot re-sync\n"
            f"/status — Show detailed bot and userbot status\n"
            f"/transports — Show configured mail relays & stats\n"
            f"/addtransport — Add a backup mail relay\n"
            f"/rmtransport <addr> — Remove a mail relay\n"
            f"/setprimary <addr> — Switch the primary mail relay\n"
            f"/resilient — Toggle resilient sending mode (all relays)\n"
            f"/richmode [webxdc|split|both|off] — Configure Telegram rich posts & album relay mode\n"
        )
    
    help_text += (
        f"\nTo get started, add me to a Delta Chat group and a Telegram group, then use /bridge to connect them.\n\n"
        f"Run your own bot: https://github.com/mrgluek/deltachat_telegram_bridge"
    )
    return help_text


def get_tg_help_text(name: str, user_id: int) -> str:
    admin_tg = database.get_config("admin_tg_id")
    mode = "Private (bot owner only)" if admin_tg else "Public (group admins only)"
    lines = [
        f"👋 Hi {name} (<code>{user_id}</code>)!",
        f"I'm the DC Bridge bot. Current mode: <b>{mode}</b>\n",
        f"I relay messages between Telegram and Delta Chat groups.\n",
        f"Commands:",
        f"/help — Show this help message",
        f"/id — Show group's chat ID",
        f"/bridge — Bridge this TG group to a new DC group",
        f"/unbridge — Remove the bridge from this TG group",
        f"/stats — Show bridge statistics",
        f"/invite — Get Delta Chat bot/group invite link",
        f"/inviteqr — Get Delta Chat bot/group invite QR code",
        f"/donate — Support bot development ❤️",
    ]
    if database.is_owner(user_id):
        lines.append(f"\n<b>⚙️ Channel, Bot & Userbot (Owner, private chat):</b>")
        lines.append(f"/channeladd @name or ID — Bridge a channel/group/bot (bot as admin OR Userbot)")
        lines.append(f"/userbotjoin link — Join channel/group via Userbot (no admin needed; use before /channeladd)")
        lines.append(f"/botsend @bot or ID text — Send a command/message to a bridged TG bot")
        lines.append(f"/channels — List bridged channels")
        lines.append(f"/groups — List Userbot groups to bridge")
        lines.append(f"/channel N — Get channel invite link")
        lines.append(f"/channelqr N — Get channel QR invite")
        lines.append(f"/channelremove N — Remove a channel bridge")
        lines.append(f"/cleanup — Clean up stale, duplicate & orphaned bridges")
        lines.append(f"/catchup [@name] — Catch up missed posts for channel(s)")
        lines.append(f"/userbotsync — Force Userbot re-sync")
        lines.append(f"/status — Show detailed bot and userbot status")
        
        lines.append(f"\n<b>🛡️ Message Filters (Owner):</b>")
        lines.append(f"/filters — List active message filters")
        lines.append(f"/filteradd <i>phrase</i> — Add a word/phrase filter")
        lines.append(f"/filterdel <i>id or phrase</i> — Remove a filter")
        
        lines.append(f"\n<b>👥 Sub-admins (Owner):</b>")
        lines.append(f"/adminadd <i>user_id</i> — Add a sub-admin")
        lines.append(f"/adminremove <i>user_id</i> — Remove a sub-admin")
        lines.append(f"/admins — List sub-admins")
    else:
        lines.append(f"\n<b>📡 Channels (Private chat only):</b>")
        lines.append(f"/channeladd @name or ID — Bridge a channel/group")
        lines.append(f"/userbotjoin link — Join channel/group via Userbot")
        lines.append(f"/channels — List bridged channels")
        lines.append(f"/channel N — Get channel invite link")
        lines.append(f"/channelqr N — Get channel QR invite")
        lines.append(f"/channelremove N — Remove a channel bridge")
    lines.append(f"\nTo get started, add me to a Telegram group and use /bridge to connect it to Delta Chat.")
    lines.append(f"\nℹ️ Make sure Group Privacy is turned off in @BotFather → Bot Settings.")
    lines.append(f"\nRun your own bot: https://github.com/mrgluek/deltachat_telegram_bridge")
    return "\n".join(lines)


def to_dc_markdown(text: str) -> str:
    """Convert limited HTML tags (b, i, code) to Markdown for Delta Chat."""
    if not text:
        return ""
    # Note: Using * for italic as requested by user
    return (text.replace("<b>", "**").replace("</b>", "**")
            .replace("<i>", "*").replace("</i>", "*")
            .replace("<code>", "`").replace("</code>", "`")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


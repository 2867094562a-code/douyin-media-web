# -*- coding: utf-8 -*-
"""抖音媒体纯解析器模块 - 提取链接、解析状态与精准匹配目标作品"""
import json
import re
import urllib.parse
from html import unescape
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

# 受信主机与识别键常量 (严格继承自 original_v2.py)
ALLOWED_HOSTS = ("douyin.com", "iesdouyin.com")
ITEM_ID_KEYS = ("aweme_id", "awemeId", "item_id", "itemId", "note_id", "noteId")
SKIP_KEYS = {"recommend_aweme_list", "related_aweme_list", "comment_list", "user_list", "suggest_list"}

POST_ID_REGEX = re.compile(
    r"/(?:note|video)/(\d+)(?:[/?#]|$)|[?&](?:modal_id|aweme_id|item_id|note_id)=(\d+)(?:[&#]|$)", re.I
)

SCRIPT_REGEX = re.compile(
    r'<script\b[^>]*\bid=["\'](?:RENDER_DATA|__UNIVERSAL_DATA_FOR_REHYDRATION__)["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)


def _safe_int(val: Any, default: int = 0) -> int:
    """安全转换为整数，防御 bit_rate='invalid' 等非数值引发的 ValueError 崩溃。"""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError, OverflowError):
        return default


def is_douyin_url(url: str) -> bool:
    """检查是否为受信域名的合法抖音链接。"""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and any(
            host == h or host.endswith("." + h) for h in ALLOWED_HOSTS
        )
    except ValueError:
        return False


def extract_share_links(text: str) -> List[str]:
    """从输入的文本或分享口令中提取所有合法的抖音链接（去重保序，去除末尾标点）。"""
    if not text:
        return []
    found: List[str] = []
    seen: Set[str] = set()
    for match in re.finditer(r'https?://[^\s<>"\'，,；;。！、（）【】]+', text, re.I):
        url = match.group().rstrip("。！!？?）)】]、,.>")
        if url not in seen and is_douyin_url(url):
            seen.add(url)
            found.append(url)
    return found


def get_post_id(url: str) -> Optional[str]:
    """从作品 URL 中提取数字作品 ID (兼容 note/video 路径与各类 query 传参)。"""
    if not url:
        return None
    match = POST_ID_REGEX.search(urllib.parse.unquote(url))
    return next((x for x in match.groups() if x), None) if match else None


def decode_maybe(value: Any) -> Any:
    """尝试将可能包含 JSON/URL 编码的字符串解析为 Python 字典或列表。"""
    if not isinstance(value, str) or not 2 <= len(value) <= 6_000_000:
        return value
    s = unescape(value.strip())
    for candidate in (s, urllib.parse.unquote(s)):
        if candidate.startswith(("{", "[")):
            try:
                return json.loads(candidate)
            except (ValueError, TypeError):
                pass
    return value


def iter_nodes(root: Any, max_nodes: int = 30_000) -> Generator[Dict[str, Any], None, None]:
    """Bound every visited node and the pending stack, including scalar strings."""
    from itertools import islice
    stack = [(root, 0)]
    seen = 0
    while stack and seen < max_nodes:
        obj, depth = stack.pop()
        seen += 1
        if depth > 20:
            continue
        obj = decode_maybe(obj)
        remaining = max(0, max_nodes - seen - len(stack))
        if isinstance(obj, dict):
            yield obj
            entries = list(islice(obj.items(), remaining))
            for key, val in reversed(entries):
                if key not in SKIP_KEYS and isinstance(val, (dict, list, str)):
                    stack.append((val, depth + 1))
        elif isinstance(obj, list):
            stack.extend((item, depth + 1) for item in reversed(obj[:remaining])
                         if isinstance(item, (dict, list, str)))


def unique_urls(urls: List[Any]) -> List[str]:
    """去重保序，仅保留有效的 HTTPS/HTTP 链接。"""
    out: List[str] = []
    seen: Set[str] = set()
    for u in urls:
        if not isinstance(u, str) or not u.startswith(("https://", "http://")):
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def media_urls(obj: Any, preferred: Tuple[str, ...] = ()) -> List[str]:
    """读取数据节点中给出的 URL，兼容 snake_case 与 camelCase 命名。"""
    if isinstance(obj, str):
        return unique_urls([obj])
    if not isinstance(obj, dict):
        return []
    urls: List[str] = []
    for key in preferred:
        if key in obj:
            urls.extend(media_urls(obj[key]))
    for key in (
        "url_list", "urlList", "download_url_list", "downloadUrlList",
        "urls", "url", "download_url", "downloadUrl"
    ):
        value = obj.get(key)
        if isinstance(value, str):
            urls.append(value)
        elif isinstance(value, list):
            urls.extend(x for x in value if isinstance(x, str))
    return unique_urls(urls)


def image_candidates(obj: Any) -> List[str]:
    """候选图片排序：优先选择明确标为原图的资源，兼容 camelCase。"""
    if isinstance(obj, str):
        return media_urls(obj)
    if not isinstance(obj, dict):
        return []
    urls: List[str] = []
    for key in (
        "origin_image", "originImage", "original_image", "originalImage",
        "origin", "download_image", "downloadImage", "display_image", "displayImage", "image"
    ):
        if key in obj:
            urls.extend(media_urls(obj[key]))
    urls.extend(media_urls(obj))
    return unique_urls(urls)


def extract_gallery(item: Dict[str, Any]) -> List[List[str]]:
    """提取图集作品的全部图片 URL 列表。"""
    candidates: List[Any] = []
    post_info = item.get("image_post_info") or item.get("imagePostInfo")
    if isinstance(post_info, dict):
        candidates.append(post_info.get("images"))
    for key in ("images", "image_list", "imageList"):
        candidates.append(item.get(key))
    for images in candidates:
        if isinstance(images, list) and images:
            gallery = [image_candidates(image) for image in images]
            gallery = [urls for urls in gallery if urls]
            if gallery:
                result: List[List[str]] = []
                seen: Set[str] = set()
                for urls in gallery:
                    if urls[0] not in seen:
                        seen.add(urls[0])
                        result.append(urls)
                return result
    return []


def extract_video(item: Dict[str, Any]) -> List[str]:
    """提取视频播放资源，优先选择最高码率播放流，防御非数值 bit_rate 异常。"""
    video = item.get("video")
    if not isinstance(video, dict):
        return []
    options: List[str] = []
    bitrates = video.get("bit_rate") or video.get("bitRate") or video.get("bitRateList") or []
    if isinstance(bitrates, list):
        sorted_bitrates = sorted(
            (x for x in bitrates[:64] if isinstance(x, dict)),
            key=lambda x: _safe_int(x.get("bit_rate") or x.get("bitRate") or 0),
            reverse=True,
        )
        for entry in sorted_bitrates:
            options.extend(media_urls(entry.get("play_addr") or entry.get("playAddr")))
    for key in (
        "play_addr_h264", "playAddrH264", "play_addr", "playAddr",
        "play_addr_lowbr", "playAddrLowbr", "download_addr", "downloadAddr"
    ):
        options.extend(media_urls(video.get(key)))
    return unique_urls(options)


def extract_cover(item: Dict[str, Any]) -> List[str]:
    """提取作品封面原图，兼容 camelCase。"""
    video = item.get("video")
    if isinstance(video, dict):
        options: List[str] = []
        for key in (
            "origin_cover", "originCover", "cover", "raw_cover", "rawCover",
            "dynamic_cover", "dynamicCover", "big_thumb", "bigThumb"
        ):
            options.extend(media_urls(video.get(key)))
        if options:
            return unique_urls(options)
    for key in ("cover", "origin_cover", "originCover", "image"):
        options = image_candidates(item.get(key))
        if options:
            return options
    return []


def extract_caption(item: Dict[str, Any]) -> Dict[str, str]:
    """提取文案、标题和作者昵称，兼容新版 itemTitle 与 authorInfo 结构。"""
    info = item.get("image_post_info") or item.get("imagePostInfo") or {}
    title = (
        (info.get("title") if isinstance(info, dict) else "")
        or item.get("itemTitle")
        or item.get("title")
        or ""
    )
    description = item.get("desc") or item.get("description") or ""
    author = item.get("authorInfo") or item.get("author") or {}
    nickname = ""
    if isinstance(author, dict):
        nickname = (
            author.get("nickname")
            or author.get("unique_id")
            or author.get("uniqueId")
            or ""
        )
    return {
        "title": str(title).strip(),
        "description": str(description).strip(),
        "author": str(nickname).strip(),
    }


def _has_conflicting_id(item_dict: Dict[str, Any], expected_id: str) -> bool:
    """检查节点字典内部是否存在与目标作品 ID 冲突的其它 ID 标识。
    
    修复 nested target ID 字典内部实际属于另一个作品时被错误采信的风险。
    """
    for key in ITEM_ID_KEYS + (("id",) if "images" in item_dict or "video" in item_dict else ()):
        val = item_dict.get(key)
        if val is not None and str(val).strip():
            if str(val).strip() != expected_id:
                return True
    return False


def extract_post(root: Any, target_id: str) -> Optional[Dict[str, Any]]:
    """必须完全匹配目标作品 ID，彻底过滤推荐列表与嵌套冲突节点。
    
    返回原版格式字典：{gallery: [[url]], video: [url], cover: [url], title: str, description: str, author: str} 或 None。
    """
    target_id_str = str(target_id).strip()
    if not target_id_str:
        return None

    best: Optional[Dict[str, Any]] = None
    best_score = -1

    for node in iter_nodes(root):
        candidates: List[Dict[str, Any]] = []

        # 1. 直接属性匹配
        if any(str(node.get(key, "")) == target_id_str for key in ITEM_ID_KEYS):
            if not _has_conflicting_id(node, target_id_str):
                candidates.append(node)

        if str(node.get("id", "")) == target_id_str and ("video" in node or "images" in node):
            if not _has_conflicting_id(node, target_id_str):
                candidates.append(node)

        # 2. 嵌套以 target_id 为 key 的字典
        nested = node.get(target_id_str)
        if isinstance(nested, dict):
            # 严格核验：如果嵌套对象内部声明了与 target_id 冲突的其它 ID，坚决不予采信！
            if not _has_conflicting_id(nested, target_id_str):
                candidates.append(nested)

        for item in candidates:
            gallery = extract_gallery(item)
            video = extract_video(item)
            cover = extract_cover(item)
            caption = extract_caption(item)

            score = len(gallery) * 20 + int(bool(video)) * 30 + int(bool(cover)) * 5
            score += int(bool(caption["description"])) * 6 + int(bool(caption["author"])) * 3

            if score > best_score and (gallery or video or cover or caption["description"]):
                best = {
                    "gallery": gallery,
                    "video": video,
                    "cover": cover,
                    **caption,
                }
                best_score = score

    return best


def parse_html_states(html: str) -> List[Any]:
    """Read bounded embedded JSON, never execute scripts or evaluate JavaScript."""
    if not isinstance(html, str) or not html or len(html) > 6_000_000:
        return []
    states: List[Any] = []
    for attempt, match in enumerate(SCRIPT_REGEX.finditer(html)):
        if attempt >= 80:
            break
        data = decode_maybe(match.group(1))
        if isinstance(data, (dict, list)):
            states.append(data)
    decoder = json.JSONDecoder()
    for attempt, match in enumerate(re.finditer(r"self\.__pace_f\.push\(", html)):
        if attempt >= 80 or len(states) >= 80:
            break
        try:
            arr, end = decoder.raw_decode(html, match.end())
            while end < len(html) and html[end].isspace():
                end += 1
            if end >= len(html) or html[end] != ')' or not isinstance(arr, list):
                continue
            for item in arr:
                if not isinstance(item, str):
                    continue
                decoded = decode_maybe(item)
                if isinstance(decoded, (dict, list)):
                    states.append(decoded)
                for line in item.splitlines():
                    if len(states) >= 80:
                        return states
                    value = line.split(':', 1)[-1].strip()
                    if value.startswith(('{', '[')):
                        try:
                            data = json.loads(value)
                            if isinstance(data, (dict, list)):
                                states.append(data)
                        except (ValueError, TypeError):
                            pass
        except (ValueError, TypeError):
            continue
    return states[:80]

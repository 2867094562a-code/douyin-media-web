# -*- coding: utf-8 -*-
"""纯解析器单元与回归测试 - 覆盖链接提取、ID识别、页面状态解码、精确匹配及安全边界"""
import json
import sys
from pathlib import Path
import pytest

# 将 backend 路径加入 sys.path 以支持跨目录与直接运行 pytest
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.extraction import (
    extract_caption,
    extract_cover,
    extract_gallery,
    extract_post,
    extract_share_links,
    extract_video,
    get_post_id,
    image_candidates,
    is_douyin_url,
    iter_nodes,
    parse_html_states,
)


def test_is_douyin_url():
    assert is_douyin_url("https://v.douyin.com/TEST-video/")
    assert is_douyin_url("https://www.douyin.com/video/1111111111111111111")
    assert is_douyin_url("https://www.iesdouyin.com/share/video/1111111111111111111/")
    assert not is_douyin_url("https://evil.com/video/123")
    assert not is_douyin_url("https://douyin.com.evil.com/video/123")
    assert not is_douyin_url("javascript:alert(1)")


def test_extract_share_links():
    text = (
        "快来看这个视频：7.12 复制打开抖音，看看【测试作者的作品】"
        "https://v.douyin.com/TEST-video/ 精彩内容！以及另一个链接"
        "https://www.douyin.com/note/2222222222222222222?modal_id=2222222222222222222。"
        "包含非抖音链接 https://google.com/test 不应提取。"
    )
    links = extract_share_links(text)
    assert len(links) == 2
    assert links[0] == "https://v.douyin.com/TEST-video/"
    assert links[1] == "https://www.douyin.com/note/2222222222222222222?modal_id=2222222222222222222"

    assert extract_share_links("") == []
    assert extract_share_links(None) == []


def test_get_post_id():
    # 路径形式
    assert get_post_id("https://www.douyin.com/video/1111111111111111111") == "1111111111111111111"
    assert get_post_id("https://www.iesdouyin.com/share/video/1111111111111111111/") == "1111111111111111111"
    assert get_post_id("https://www.douyin.com/note/2222222222222222222") == "2222222222222222222"
    # 查询参数形式
    assert get_post_id("https://www.douyin.com/user/123?modal_id=1111111111111111111") == "1111111111111111111"
    assert get_post_id("https://www.douyin.com/?aweme_id=2222222222222222222&from=share") == "2222222222222222222"
    # 空或无效
    assert get_post_id("https://www.douyin.com/") is None
    assert get_post_id("") is None
    assert get_post_id(None) is None


def test_parse_html_states_render_data():
    html = """
    <!DOCTYPE html>
    <html>
      <head>
        <script id="RENDER_DATA">
          {"app": {"videoDetail": {"aweme_id": "999888", "desc": "传统RenderData作品"}}}
        </script>
      </head>
      <body></body>
    </html>
    """
    states = parse_html_states(html)
    assert len(states) >= 1
    post = extract_post(states, "999888")
    assert post is not None
    assert post["description"] == "传统RenderData作品"


def test_parse_html_states_synthetic_pace_hydration():
    # 测试 Next.js / React 流式 self.__pace_f.push 解码
    item = {"awemeId": "1234567890123456789", "desc": "测试图文文案", "itemTitle": "测试标题",
            "authorInfo": {"nickname": "测试作者"},
            "images": [{"urlList": ["https://p3.douyinpic.com/original-1.webp"]}],
            "video": {"originCover": {"urlList": ["https://p3.douyinpic.com/cover.webp"]}}}
    record = ["$", "$L9", None, {"awemeId": item["awemeId"], "aweme": {"detail": item}}]
    chunk = json.dumps([1, "7:" + json.dumps(record, ensure_ascii=False)], ensure_ascii=False)
    html = "<script>self.__pace_f.push(" + chunk + ")</script>"

    states = parse_html_states(html)
    assert len(states) > 0

    post = extract_post(states, "1234567890123456789")
    assert post is not None
    assert post["title"] == "测试标题"
    assert post["description"] == "测试图文文案"
    assert post["author"] == "测试作者"
    assert len(post["gallery"]) == 1
    assert post["gallery"][0][0] == "https://p3.douyinpic.com/original-1.webp"
    assert post["cover"][0] == "https://p3.douyinpic.com/cover.webp"


def test_synthetic_13_images_and_camel_case():
    # 模拟 13 张高清图文，验证准确提取数量且原图优先 (urlList > downloadUrlList)
    images_data = []
    for i in range(1, 14):
        images_data.append({
            "originImage": {
                "urlList": [
                    f"https://p3.douyincdn.com/obj/original-image-{i}.webp",
                    f"https://p9.douyincdn.com/obj/original-image-{i}.webp",
                ]
            },
            "downloadUrlList": [
                f"https://p3.douyincdn.com/obj/watermarked-image-{i}.jpeg"
            ],
            "width": 2160,
            "height": 3840,
        })

    mock_node = {
        "awemeId": "2222222222222222222",
        "itemTitle": "13张摄影图集",
        "desc": "秋天风景摄影精选",
        "authorInfo": {
            "nickname": "摄影师小明",
            "uniqueId": "photographer_xm",
        },
        "images": images_data,
        "video": {
            "originCover": {
                "urlList": ["https://p3.douyincdn.com/obj/cover.jpeg"]
            }
        },
    }

    post = extract_post(mock_node, "2222222222222222222")
    assert post is not None
    assert len(post["gallery"]) == 13
    assert post["title"] == "13张摄影图集"
    assert post["description"] == "秋天风景摄影精选"
    assert post["author"] == "摄影师小明"

    # 原图优先校验：第一候选必须是 origin-image webp，而不是带有水印的 downloadUrlList
    for i in range(13):
        first_url = post["gallery"][i][0]
        assert f"original-image-{i+1}.webp" in first_url


def test_exact_id_matching_rejects_recommendations():
    # 构造包含目标作品与推荐作品列表的结构
    target_id = "111222333"
    other_id = "999888777"
    data = {
        "aweme_id": target_id,
        "desc": "目标作品",
        "recommend_aweme_list": [
            {"aweme_id": other_id, "desc": "推荐作品1"},
            {"aweme_id": "555666777", "desc": "推荐作品2"},
        ],
    }

    # 精确匹配目标 ID
    post = extract_post(data, target_id)
    assert post is not None
    assert post["description"] == "目标作品"

    # 推荐列表被 SKIP_KEYS 拦截，不能匹配推荐列表里的 ID
    rec_post = extract_post(data, other_id)
    assert rec_post is None


def test_conflicting_nested_target_id_rejected():
    # 安全回归：nested target ID 字典内部若存在冲突 ID，坚决不予采信
    target_id = "666888"
    conflicting_data = {
        # 键是 target_id，但内部包含 conflicting aweme_id
        target_id: {
            "aweme_id": "777999",  # 冲突 ID!
            "desc": "恶意伪造嵌套作品",
            "author": {"nickname": "伪造者"},
        }
    }
    result = extract_post(conflicting_data, target_id)
    assert result is None


def test_extract_video_with_invalid_bitrate_no_crash():
    # 回归测试：bit_rate 出现 'invalid' 或非数字时不得抛出 ValueError
    mock_item = {
        "video": {
            "bitRateList": [
                {"bit_rate": "invalid", "play_addr": {"url_list": ["https://cdn.com/bad.mp4"]}},
                {"bit_rate": 800000, "play_addr": {"url_list": ["https://cdn.com/low.mp4"]}},
                {"bitRate": 2500000, "playAddr": {"urlList": ["https://cdn.com/high.mp4"]}},
                {"bit_rate": None, "play_addr": {"url_list": ["https://cdn.com/none.mp4"]}},
            ]
        }
    }
    video_urls = extract_video(mock_item)
    assert len(video_urls) >= 2
    # 验证排序优先选择最高码率 2500000 的流
    assert video_urls[0] == "https://cdn.com/high.mp4"
    assert "https://cdn.com/low.mp4" in video_urls


def test_iter_nodes_safe_depth_and_cycle_prevention():
    # 防御深度与节点爆炸
    deep_dict = {}
    curr = deep_dict
    for i in range(50):
        curr["child"] = {}
        curr = curr["child"]

    nodes = list(iter_nodes(deep_dict, max_nodes=100))
    # 深度超过 20 层应当自动截断
    assert len(nodes) <= 25


def test_extract_post_output_keys_contract():
    # 严格检验返回格式字典 key
    mock = {
        "aweme_id": "54321",
        "desc": "文案",
        "title": "标题",
        "author": {"nickname": "作者"},
        "video": {"play_addr": {"url_list": ["https://cdn.com/v.mp4"]}},
        "cover": {"url_list": ["https://cdn.com/c.jpg"]},
    }
    res = extract_post(mock, "54321")
    assert res is not None
    expected_keys = {"gallery", "video", "cover", "title", "description", "author"}
    assert set(res.keys()) == expected_keys
    assert isinstance(res["gallery"], list)
    assert isinstance(res["video"], list)
    assert isinstance(res["cover"], list)
    assert isinstance(res["title"], str)
    assert isinstance(res["description"], str)
    assert isinstance(res["author"], str)


def test_all_visited_nodes_are_bounded():
    assert list(iter_nodes(["x"] * 100 + [{"aweme_id": "123"}], max_nodes=10)) == []


def test_generic_nested_id_conflict_rejected():
    assert extract_post({"123": {"id": "999", "images": [{"url_list": ["https://p3.douyinpic.com/a.webp"]}]}}, "123") is None


def test_malformed_hydration_attempts_bounded():
    assert parse_html_states('self.__pace_f.push(not_json);' * 1000) == []
    assert parse_html_states('x' * 6_000_001) == []

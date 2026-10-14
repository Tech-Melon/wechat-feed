"""微信表情：自定义贴纸走腾讯 CDN，系统表情走官方 GIF / 新版 PNG。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .wximage import sniff

_MD5 = re.compile(r"^[0-9a-f]{32}$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_STICKER_HOSTS = (".tc.qq.com", ".qpic.cn", ".weixin.qq.com", ".gtimg.cn")
STICKER_MAX = 4_000_000
_EMOTE_HOSTS = (
    "cdn.jsdelivr.net",
    "fastly.jsdelivr.net",
    "gcore.jsdelivr.net",
    "res.wx.qq.com",
)
_PNG = "https://cdn.jsdelivr.net/gh/YiJio/emoji-chinese@1.1.0/wx/wx-new/default/{slug}.png"
_GIF = "https://res.wx.qq.com/mpres/htmledition/images/icon/emotion/{slug}.gif"

EMOTE_SLUGS = {
    "微笑": "wx",
    "撇嘴": "pz",
    "色": "se",
    "发呆": "fd",
    "得意": "dy",
    "流泪": "ll",
    "害羞": "hx",
    "闭嘴": "bz",
    "睡": "shui",
    "大哭": "dk",
    "尴尬": "gg",
    "发怒": "fn",
    "调皮": "tp",
    "呲牙": "cy",
    "惊讶": "jy",
    "难过": "ng",
    "囧": "jiong",
    "抓狂": "zk",
    "吐": "tu",
    "偷笑": "tx",
    "愉快": "yk",
    "白眼": "by",
    "傲慢": "am",
    "困": "kun",
    "惊恐": "jk",
    "憨笑": "hx2",
    "大笑": "hx2",
    "悠闲": "yx",
    "咒骂": "zm",
    "疑问": "yw",
    "嘘": "xu",
    "晕": "yun",
    "衰": "shuai",
    "骷髅": "kl",
    "敲打": "qd",
    "再见": "zj",
    "擦汗": "ch",
    "抠鼻": "kb",
    "鼓掌": "gz",
    "坏笑": "hx3",
    "左哼哼": "zhh",
    "右哼哼": "yhh",
    "鄙视": "bs",
    "委屈": "wq",
    "快哭了": "kkl",
    "阴险": "yx2",
    "亲亲": "qq",
    "可怜": "kl2",
    "生病": "sb",
    "脸红": "lh",
    "破涕为笑": "ptwx",
    "恐惧": "kj",
    "失望": "sw",
    "无语": "wy",
    "嘿哈": "hh",
    "捂脸": "wl",
    "奸笑": "jx",
    "机智": "jz",
    "皱眉": "zm2",
    "耶": "ye",
    "吃瓜": "cg",
    "加油": "jy2",
    "汗": "han",
    "天啊": "ta",
    "Emm": "emm",
    "社会社会": "shsh",
    "旺柴": "doge",
    "好的": "hd",
    "打脸": "dl",
    "哇": "wa",
    "翻白眼": "fby",
    "666": "666",
    "让我看看": "rwkk",
    "叹气": "tq",
    "苦涩": "ks",
    "裂开": "lk",
    "嘴唇": "zc",
    "爱心": "ax",
    "心碎": "xs",
    "拥抱": "yb",
    "强": "qiang",
    "弱": "ruo",
    "握手": "ws",
    "胜利": "sl",
    "抱拳": "bq",
    "勾引": "gy",
    "拳头": "qt",
    "OK": "ok",
    "合十": "hs",
    "啤酒": "pj",
    "咖啡": "kf",
    "蛋糕": "dg",
    "玫瑰": "mg",
    "凋谢": "dx",
    "菜刀": "cd",
    "炸弹": "zd",
    "便便": "bb",
    "月亮": "yl",
    "太阳": "ty",
    "庆祝": "qz",
    "礼物": "lw",
    "红包": "hb",
    "發": "fa",
    "福": "fu",
    "烟花": "yh",
    "爆竹": "bz2",
    "猪头": "zt",
    "跳跳": "tt",
    "发抖": "fd2",
    "转圈": "zq",
    "加油加油": "jyjy",
    "酷": "16",
    "饥饿": "24",
    "流汗": "27",
    "奋斗": "30",
    "折磨": "35",
    "糗大了": "43",
    "哈欠": "47",
    "吓": "53",
    "西瓜": "56",
    "篮球": "58",
    "乒乓": "59",
    "饭": "61",
    "闪电": "69",
    "刀": "71",
    "足球": "72",
    "瓢虫": "73",
    "差劲": "86",
    "爱你": "87",
    "NO": "88",
    "爱情": "90",
    "飞吻": "91",
    "怄火": "94",
    "磕头": "98",
    "回头": "97",
    "投降": "99",
    "激动": "100",
    "乱舞": "101",
    "献吻": "102",
    "左太极": "103",
    "右太极": "104",
}

EMOTE_SLUGS["可爱"] = "yk"
EMOTE_SLUGS["冷汗"] = "17"


def allowed_sticker_url(url: str) -> bool:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return any(host == item[1:] or host.endswith(item) for item in _STICKER_HOSTS)


def allowed_emote_url(url: str) -> bool:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host in _EMOTE_HOSTS


def sticker_cache(folder: Path, md5: str) -> Path:
    return folder / f"{md5}.bin"


def emote_source(name: str) -> tuple[str, str] | None:
    slug = EMOTE_SLUGS.get(str(name or ""))
    if not slug:
        return None
    if slug.isdigit():
        if not 0 <= int(slug) <= 104:
            return None
        return _GIF.format(slug=slug), f"{slug}.gif"
    if not _SLUG.fullmatch(slug):
        return None
    return _PNG.format(slug=slug), f"{slug}.png"


def valid_md5(value: str) -> bool:
    return bool(_MD5.fullmatch(str(value or "")))


def decode_query_url(raw: str) -> str:
    return unquote(str(raw or "")).strip()


def pick_mime(blob: bytes) -> str | None:
    return sniff(blob)


def within_limit(blob: bytes, ceiling: int) -> bool:
    return 32 <= len(blob) <= ceiling

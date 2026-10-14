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

# 0-104 走微信官方 GIF，避免第三方 PNG 画反；其余新版表情仍用命名 PNG。
EMOTE_SLUGS = {
    "微笑": "0",
    "撇嘴": "1",
    "色": "2",
    "发呆": "3",
    "得意": "4",
    "流泪": "5",
    "害羞": "6",
    "闭嘴": "7",
    "睡": "8",
    "大哭": "9",
    "尴尬": "10",
    "发怒": "11",
    "调皮": "12",
    "呲牙": "13",
    "惊讶": "14",
    "难过": "15",
    "酷": "16",
    "冷汗": "17",
    "抓狂": "18",
    "吐": "19",
    "偷笑": "20",
    "愉快": "21",
    "可爱": "21",
    "白眼": "22",
    "傲慢": "23",
    "饥饿": "24",
    "困": "25",
    "惊恐": "26",
    "流汗": "27",
    "憨笑": "28",
    "大笑": "28",
    "悠闲": "29",
    "奋斗": "30",
    "咒骂": "31",
    "疑问": "32",
    "嘘": "33",
    "晕": "34",
    "折磨": "35",
    "衰": "36",
    "骷髅": "37",
    "敲打": "38",
    "再见": "39",
    "擦汗": "40",
    "抠鼻": "41",
    "鼓掌": "42",
    "糗大了": "43",
    "坏笑": "44",
    "左哼哼": "45",
    "右哼哼": "46",
    "哈欠": "47",
    "鄙视": "48",
    "委屈": "49",
    "快哭了": "50",
    "阴险": "51",
    "亲亲": "52",
    "吓": "53",
    "可怜": "54",
    "菜刀": "55",
    "西瓜": "56",
    "啤酒": "57",
    "篮球": "58",
    "乒乓": "59",
    "咖啡": "60",
    "饭": "61",
    "猪头": "62",
    "玫瑰": "63",
    "凋谢": "64",
    "嘴唇": "65",
    "爱心": "66",
    "心碎": "67",
    "蛋糕": "68",
    "闪电": "69",
    "炸弹": "70",
    "刀": "71",
    "足球": "72",
    "瓢虫": "73",
    "便便": "74",
    "月亮": "75",
    "太阳": "76",
    "礼物": "77",
    "拥抱": "78",
    "强": "79",
    "弱": "80",
    "握手": "81",
    "胜利": "82",
    "抱拳": "83",
    "勾引": "84",
    "拳头": "85",
    "差劲": "86",
    "爱你": "87",
    "NO": "88",
    "OK": "89",
    "爱情": "90",
    "飞吻": "91",
    "跳跳": "92",
    "发抖": "93",
    "怄火": "94",
    "转圈": "95",
    "磕头": "96",
    "回头": "97",
    "跳绳": "98",
    "投降": "99",
    "激动": "100",
    "乱舞": "101",
    "献吻": "102",
    "左太极": "103",
    "右太极": "104",
    "囧": "jiong",
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
    "合十": "hs",
    "庆祝": "qz",
    "红包": "hb",
    "發": "fa",
    "福": "fu",
    "烟花": "yh",
    "爆竹": "bz2",
    "加油加油": "jyjy",
}


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

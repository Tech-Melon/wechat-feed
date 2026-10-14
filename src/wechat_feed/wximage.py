"""解开微信 4.x 本地图片 .dat。

V4 格式：6 字节签名 + 4 字节 AES 段长 + 4 字节 XOR 段长 + 1 字节保留；
随后依次是 AES-128-ECB（PKCS7）段、明文段、按单字节异或的尾段。
V1 签名用固定密钥，V2 用从微信进程里取出的图片密钥（WDA 会存一份）。
没有签名的老格式整份按单字节异或。
"""

from __future__ import annotations

import re
from pathlib import Path

_SIG_V1 = b"\x07\x08V1\x08\x07"
_SIG_V2 = b"\x07\x08V2\x08\x07"
_V1_KEY = b"cfcd208495d565ef"
_REF = re.compile(r"^([0-9a-f]{32})/(\d{4}-\d{2})/([0-9a-f]{32})$")

_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG", "image/png"),
    (b"GIF8", "image/gif"),
    (b"BM", "image/bmp"),
)


def sniff(blob: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if blob.startswith(magic):
            return mime
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return None


def _xor(blob: bytes, key: int) -> bytes:
    return blob.translate(bytes(value ^ key for value in range(256)))


def _aes_ecb(key: bytes, blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(blob) + decryptor.finalize()


def _legacy(data: bytes) -> bytes | None:
    for magic, _ in _MAGIC:
        key = data[0] ^ magic[0]
        if all(data[i] ^ key == magic[i] for i in range(1, len(magic))):
            return _xor(data, key)
    return None


def decode_dat(data: bytes, aes_key: bytes | None, xor_key: int | None) -> bytes | None:
    signature = data[:6]
    if signature not in (_SIG_V1, _SIG_V2):
        return _legacy(data) if len(data) > 4 else None
    key = _V1_KEY if signature == _SIG_V1 else aes_key
    if not key or xor_key is None or len(data) < 15:
        return None
    aes_size = int.from_bytes(data[6:10], "little")
    xor_size = int.from_bytes(data[10:14], "little")
    body = data[15:]
    enc_len = (aes_size // 16 + 1) * 16
    if enc_len + xor_size > len(body):
        return None
    head = _aes_ecb(key, body[:enc_len])
    pad = head[-1]
    if not 1 <= pad <= 16:
        return None
    head = head[:-pad]
    tail_start = len(body) - xor_size
    return head + body[enc_len:tail_start] + _xor(body[tail_start:], xor_key)


def parse_ref(ref: str) -> tuple[str, str, str] | None:
    match = _REF.match(str(ref or ""))
    return match.groups() if match else None


def candidates(wx_dir: Path, ref: str, *, hd: bool) -> list[Path]:
    parts = parse_ref(ref)
    if parts is None:
        return []
    room, month, name = parts
    folder = wx_dir / "msg" / "attach" / room / month / "Img"
    order = ("_h", "", "_t") if hd else ("_t", "_h", "")
    return [folder / f"{name}{suffix}.dat" for suffix in order]


def ref_for(wx_dir: Path, room: str, name: str) -> str:
    """不知道月份时（WDA 时代的卡片）按月份目录从新到旧找文件。"""
    import hashlib

    if not re.fullmatch(r"[0-9a-f]{32}", name or ""):
        return ""
    room_hash = hashlib.md5(room.encode("utf-8")).hexdigest()
    base = wx_dir / "msg" / "attach" / room_hash
    try:
        months = sorted((p.name for p in base.iterdir() if p.is_dir()), reverse=True)
    except OSError:
        return ""
    for month in months:
        if re.fullmatch(r"\d{4}-\d{2}", month) and any(
            (base / month / "Img" / f"{name}{suffix}.dat").exists() for suffix in ("_t", "_h", "")
        ):
            return f"{room_hash}/{month}/{name}"
    return ""


def _kind(path: Path) -> str:
    name = path.name
    if name.endswith("_t.dat"):
        return "t"
    if name.endswith("_h.dat"):
        return "h"
    return "orig"


def load_image(
    wx_dir: Path, ref: str, aes_key: bytes | None, xor_key: int | None, *, hd: bool = False
) -> tuple[bytes, str, str] | None:
    """按缩略图 / 高清优先级找第一张浏览器能显示的图。原图常是 wxgf（HEVC），跳过。"""
    for path in candidates(wx_dir, ref, hd=hd):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        blob = decode_dat(data, aes_key, xor_key)
        mime = sniff(blob) if blob else None
        if mime:
            return blob, mime, _kind(path)
    return None

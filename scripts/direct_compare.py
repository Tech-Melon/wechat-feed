"""把直读生成的卡片和 feed.db 里已有的卡片按 id 对比：uv run python scripts/direct_compare.py"""

import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from wechat_feed.normalize import normalize_message_event
from wechat_feed.wechatdb import WeChatDB

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "compare_out.txt"
FIELDS = ("renderType", "senderName", "text", "quoteText", "createTime")


def main() -> None:
    stored = json.loads((ROOT / "data" / "direct.json").read_text(encoding="utf-8"))
    db = WeChatDB(
        Path(stored["db_storage_dir"]), bytes.fromhex(stored["db_key"]), stored["account"]
    )
    feed = sqlite3.connect(f"file:{ROOT / 'data' / 'feed.db'}?mode=ro", uri=True)
    rows = feed.execute("SELECT payload FROM cards ORDER BY create_time DESC LIMIT 3000").fetchall()
    old = {}
    for (payload,) in rows:
        card = json.loads(payload)
        old[card["id"]] = card
    rooms = Counter(card["conversationId"] for card in old.values())
    since = int(time.time()) - 86400
    diffs: Counter = Counter()
    samples: list[str] = []
    matched = missing = 0
    for room, _ in rooms.most_common(6):
        name = db.group_name(room)
        for message in db.history(room, since):
            card = normalize_message_event(
                {"type": "message", "username": room, "groupName": name, "message": message}
            )
            if card is None or card["id"] not in old:
                missing += card is not None
                continue
            matched += 1
            stored = old[card["id"]]
            for field in FIELDS:
                if card.get(field) != stored.get(field):
                    diffs[field] += 1
                    if len(samples) < 15:
                        samples.append(
                            f"{field}: direct={card.get(field)!r:.60} old={stored.get(field)!r:.60}"
                        )
    lines = [f"matched={matched} direct-only={missing}", f"diffs={dict(diffs)}", *samples]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"结果写在 {OUT}")


if __name__ == "__main__":
    main()

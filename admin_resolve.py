# admin_resolve.py — ربط callback_data بجلسة DB
from keyboards import CB
import database


def parse_session_id(data: str, kind: str) -> int | None:
    prefix = CB.get(kind)
    if not prefix or not data.startswith(prefix):
        return None
    rest = data[len(prefix):]
    if not rest.isdigit():
        return None
    return int(rest)


async def get_session_from_callback(data: str, kind: str):
    sid = parse_session_id(data, kind)
    if sid is None:
        return None
    return await database.get_session_by_id(sid)

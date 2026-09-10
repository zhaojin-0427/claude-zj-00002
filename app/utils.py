from datetime import datetime, timezone


def utcnow() -> datetime:
    """统一使用 naive UTC 时间，便于 SQLite 存储与比较。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

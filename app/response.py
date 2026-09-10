"""统一响应格式：{code, message, data}。code=0 表示成功。"""


def ok(data=None, message: str = "ok") -> dict:
    return {"code": 0, "message": message, "data": data}


def error(code: int, message: str) -> dict:
    return {"code": code, "message": message, "data": None}

class ApiException(Exception):
    """业务异常，由全局处理器转换为统一响应。"""

    def __init__(self, code: int, message: str, http_status: int = 400):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)

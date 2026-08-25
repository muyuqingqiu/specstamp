class SdlcError(Exception):
    """统一收口业务错误，避免直接把英文堆栈甩给用户。"""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code

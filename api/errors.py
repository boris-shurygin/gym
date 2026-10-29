"""Безопасные прикладные коды, без учётных данных в исключениях."""


class ServiceError(Exception):
    def __init__(self, code, *, reason=None):
        self.code = code
        self.reason = reason
        super().__init__(code)

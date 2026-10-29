"""Безопасные прикладные коды, без учётных данных в исключениях."""


class ServiceError(Exception):
    def __init__(self, code, *, reason=None, receipt=None):
        self.code = code
        self.reason = reason
        self.receipt = receipt
        super().__init__(code)

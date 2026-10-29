"""Безопасные прикладные коды, без учётных данных в исключениях."""


class ServiceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

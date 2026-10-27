import unittest

from api.database import DatabaseSettings, create_engine, transaction


class SettingsTests(unittest.TestCase):
    def test_insecure_or_ambiguous_endpoints_are_rejected(self):
        for endpoint, database, auth in [
            ('grpc://remote:2136', '/local', 'anonymous'),
            ('grpcs://remote:2135', '/local', 'anonymous'),
            ('grpc://remote:2136', '/local', 'token'),
            ('grpcs://token@remote:2135', '/db', 'token'),
            ('grpcs://remote:2135/?token=x', '/db', 'token'),
            ('grpcs://remote:2135', 'relative', 'metadata'),
            ('grpcs://remote:2135', '/db/../other', 'metadata'),
            ('grpcs://remote:2135', '/db', 'unknown'),
        ]:
            with self.subTest(endpoint=endpoint, database=database, auth=auth):
                with self.assertRaises(ValueError):
                    DatabaseSettings(endpoint, database, auth)

    def test_explicit_token_is_not_stored_in_url(self):
        settings = DatabaseSettings('grpcs://remote:2135', '/db', 'token')
        with self.assertRaises(ValueError):
            create_engine(settings)
        engine = create_engine(settings, token='synthetic-secret')
        try:
            self.assertNotIn('synthetic-secret', str(engine.url))
            self.assertTrue(engine.hide_parameters)
        finally:
            engine.dispose()

    def test_retry_bound_is_checked_before_connection(self):
        for attempts in [0, 11]:
            with self.assertRaises(ValueError):
                transaction(None, lambda _: None, max_attempts=attempts)

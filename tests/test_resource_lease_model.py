import unittest

from resourcelease.model import Config, LeaseError


def configuration(**changes):
    return {"schema": 1, "slots": [
        {"id": "one", "backend": "fake", "capabilities": ["test"], "options": {}}
    ], **changes}


class ModelTests(unittest.TestCase):
    def test_round_trip(self):
        value = Config.parse(configuration())
        self.assertEqual(value, Config.parse(value.document()))

    def test_closed_finite_configuration(self):
        for value in (None, {}, configuration(unexpected=True),
                      configuration(slots=[]), configuration(lease_seconds=float("nan")),
                      configuration(max_requests=True), configuration(history_seconds=1)):
            with self.subTest(value=value), self.assertRaises(LeaseError):
                Config.parse(value)

    def test_duplicate_slot_and_capability(self):
        value = configuration()
        value["slots"] *= 2
        with self.assertRaises(LeaseError):
            Config.parse(value)
        value = configuration()
        value["slots"][0]["capabilities"] *= 2
        with self.assertRaises(LeaseError):
            Config.parse(value)

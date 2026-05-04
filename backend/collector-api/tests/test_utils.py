from __future__ import annotations

import unittest

from collector_api.utils import hash_ip, subject_for_app


class UtilsTests(unittest.TestCase):
    def test_hash_ip_is_stable(self) -> None:
        left = hash_ip("1.2.3.4", "salt")
        right = hash_ip("1.2.3.4", "salt")
        self.assertEqual(left, right)

    def test_subject_for_app(self) -> None:
        self.assertEqual(subject_for_app("events", "com.example.app"), "events.com.example.app")


if __name__ == "__main__":
    unittest.main()

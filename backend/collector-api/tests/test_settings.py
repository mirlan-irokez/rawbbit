from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from collector_api.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_geoip_settings_parse_from_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GEOIP_ENABLED": "1",
                "GEOIP_MMDB_PATH": "/var/lib/geoip/dbip-country-lite.mmdb",
            },
            clear=True,
        ):
            settings = Settings()

        self.assertTrue(settings.geoip_enabled)
        self.assertEqual(settings.geoip_mmdb_path, "/var/lib/geoip/dbip-country-lite.mmdb")


if __name__ == "__main__":
    unittest.main()

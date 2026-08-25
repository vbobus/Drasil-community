from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.core.env import cors_origins


class CorsOriginsTests(unittest.TestCase):
    def test_defaults_to_local_frontend(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cors_origins(), ["http://localhost:5173"])

    def test_combines_deduplicates_and_normalizes_origins(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DRASIL_CORS_ORIGINS": (
                    "https://app.example.com/, http://localhost:5173"
                ),
                "DRASIL_FRONTEND_ORIGIN": "https://app.example.com",
            },
            clear=True,
        ):
            self.assertEqual(
                cors_origins(),
                ["https://app.example.com", "http://localhost:5173"],
            )

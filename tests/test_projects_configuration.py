from __future__ import annotations

import os
import unittest
from dataclasses import fields
from unittest.mock import patch

from bot.config import Config, get_config

BASE_ENV = {"BOT_TOKEN": "123:test", "BOT_ID": "123", "ALLOWED_USER_ID": "42"}


class ProjectsConfigurationTests(unittest.TestCase):
    def test_project_url_is_optional_and_preserves_raw_value_for_validation(self):
        with patch.dict(os.environ, BASE_ENV, clear=True):
            self.assertEqual("", get_config().project_813spam_url)
        for url in ("https://spam.example.com/panel", " https://spam.example.com/panel"):
            with self.subTest(url=url):
                with patch.dict(
                    os.environ, {**BASE_ENV, "PROJECT_813SPAM_URL": url}, clear=True,
                ):
                    self.assertEqual(url, get_config().project_813spam_url)

    def test_existing_config_constructors_keep_an_empty_project_url_by_default(self):
        project_url_field = next(
            field for field in fields(Config) if field.name == "project_813spam_url"
        )
        self.assertEqual("", project_url_field.default)

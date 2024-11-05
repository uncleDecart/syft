import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from syftbox.client.config import get_user_input, prompt_email, prompt_sync_dir
from syftbox.lib.lib import DEFAULT_SYNC_FOLDER, is_valid_dir, is_valid_email


def test_get_user_input():
    with patch("builtins.input", return_value="test"):
        assert get_user_input("prompt") == "test"

    with patch("builtins.input", return_value=""):
        assert get_user_input("prompt", default="default") == "default"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/tmp", True),
        ("./test", True),
        (".", True),
        ("..", True),
        ("~", True),
        ("", False),  # Empty path = invalid
        ("/x", False),  # unwriteable path
    ],
)
def test_is_valid_dir(path, expected):
    """Test various email formats"""
    valid, reason = is_valid_dir(path, check_empty=False, check_writable=True)
    assert valid == expected, reason


def test_empty_dir():
    # Test with temp directory
    with tempfile.TemporaryDirectory() as temp_dir:
        # Valid empty directory
        valid, reason = is_valid_dir(temp_dir)
        assert valid
        assert reason == ""

        # Non-empty directory
        with open(os.path.join(temp_dir, "test.txt"), "w") as f:
            f.write("test")

        valid, reason = is_valid_dir(temp_dir)
        assert not valid
        assert "not empty" in reason.lower()


@pytest.mark.parametrize(
    "email,expected",
    [
        ("test@example.com", True),
        ("test.name@example.com", True),
        ("test+label@example.com", True),
        ("test@sub.example.com", True),
        ("a@b.c", True),
        ("", False),  # Empty email
        ("test@", False),  # Missing domain
        ("@example.com", False),  # Missing username
        ("test@example", False),  # Mising TLD
        ("test.example.com", False),  # Missing @
        ("test@@example.com", False),  # Double @
        ("test@exam ple.com", False),  # Space
        ("test@example..com", False),  # Double dots
    ],
)
def test_email_validation(email, expected):
    """Test various email formats"""
    assert is_valid_email(email) == expected


@pytest.mark.parametrize(
    "user_input,expected",
    [
        ("", Path(DEFAULT_SYNC_FOLDER)),
        ("./valid/path", Path("./valid/path")),
    ],
)
def test_prompt_sync_dir(user_input, expected, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda x: user_input)
    monkeypatch.setattr("syftbox.client.config.is_valid_dir", lambda x: (True, ""))

    result = prompt_sync_dir()
    assert result.absolute() == expected.absolute()


@pytest.mark.timeout(1)
def test_prompt_email(monkeypatch):
    valid_email = "test@example.com"

    monkeypatch.setattr("builtins.input", lambda x: valid_email)
    monkeypatch.setattr("syftbox.client.config.is_valid_dir", lambda x: (True, ""))

    with patch("builtins.input", return_value=valid_email):
        assert prompt_email() == valid_email

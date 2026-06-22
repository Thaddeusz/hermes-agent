"""Tests for the ``kanban.notifications`` config block (task t_863ea8ff).

This is the data-layer foundation for the kanban-status notifier: the
schema lives in ``DEFAULT_CONFIG`` (so a missing block falls back to v1
defaults), the loader validates the shape on every read (warn-only,
never fatal — forward-compat for v2 adapters), and a user override on
disk deep-merges cleanly with the defaults.

These tests cover the four acceptance bullets from the task spec:

  1. The v1 default block is present in ``DEFAULT_CONFIG`` with the
     exact documented shape (``on_status`` defaults, WhatsApp
     destination defaults, other destinations ``enabled: false``).
  2. Missing top-level / nested blocks fall back to those defaults
     rather than raising — legacy configs that pre-date this block
     keep working.
  3. A user-supplied ``kanban.notifications`` block deep-merges with
     the defaults (override one key, keep the rest).
  4. Unknown destination / on_status keys log a warning and otherwise
     load fine — forward-compat for v2 adapters.

Auxiliary edges:

  - The loader never raises on a malformed block; it warns and keeps
    booting.
  - The validator is a no-op when the block is missing / wrong-type
    at the top level (so a legacy config without ``kanban:`` at all
    produces zero warnings).
  - An explicit ``kanban: null`` or ``kanban.notifications: null`` in
    user YAML is preserved by the deep-merge semantics (the user is
    asserting "no block") — only the legacy "key absent" path falls
    back to defaults.
  - Non-dict / non-string fields in ``destinations.whatsapp`` warn
    but don't block boot.
"""

from __future__ import annotations

import logging
import textwrap

import pytest

from hermes_cli.config import (
    DEFAULT_CONFIG,
    load_config,
    validate_kanban_notifications_config,
)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Redirect ``HERMES_HOME`` to a clean tmp dir for each test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


class TestDefaultsPresent:
    """AC1: the v1 default block ships with the exact documented shape."""

    def test_kanban_block_has_notifications_subblock(self):
        assert "kanban" in DEFAULT_CONFIG, "DEFAULT_CONFIG missing 'kanban' block"
        assert "notifications" in DEFAULT_CONFIG["kanban"], (
            "DEFAULT_CONFIG['kanban'] missing 'notifications' subblock — "
            "t_863ea8ff not landed"
        )

    def test_master_enabled_default_true(self):
        notif = DEFAULT_CONFIG["kanban"]["notifications"]
        assert notif["enabled"] is True

    def test_on_status_defaults_match_spec(self):
        on_status = DEFAULT_CONFIG["kanban"]["notifications"]["on_status"]
        assert on_status == {
            "blocked": True,
            "awaiting_clarification": True,
            "review": True,
            "scheduled": False,
        }

    def test_destinations_contain_all_four_keys(self):
        dests = DEFAULT_CONFIG["kanban"]["notifications"]["destinations"]
        assert set(dests.keys()) == {"whatsapp", "desktop_toast", "email", "cli"}

    def test_whatsapp_defaults_match_spec(self):
        wa = DEFAULT_CONFIG["kanban"]["notifications"]["destinations"]["whatsapp"]
        assert wa["chat_id"] == "353899843924"
        assert wa["thread_id"] is None
        assert wa["profile"] == "main_profile"
        assert wa["template"].startswith("🔔 Kanban:")
        # The documented placeholders must all be in the default template
        # so a custom override that drops one is a deliberate user choice.
        for placeholder in ("{task_id}", "{title}", "{new_status}",
                            "{block_reason}", "{workspace_path}"):
            assert placeholder in wa["template"], (
                f"default WhatsApp template missing placeholder {placeholder}"
            )

    def test_reserved_v2_destinations_default_disabled(self):
        dests = DEFAULT_CONFIG["kanban"]["notifications"]["destinations"]
        assert dests["desktop_toast"] == {"enabled": False}
        assert dests["email"] == {"enabled": False}
        assert dests["cli"] == {"enabled": False}


class TestLegacyConfigStillLoads:
    """AC2: missing blocks fall back to defaults rather than raising."""

    def test_no_user_config_means_defaults(self, hermes_home):
        # No config.yaml written → defaults only
        config = load_config()
        notif = config["kanban"]["notifications"]
        assert notif["enabled"] is True
        assert notif["on_status"]["blocked"] is True
        assert notif["destinations"]["whatsapp"]["chat_id"] == "353899843924"

    def test_user_config_without_kanban_block_uses_defaults(self, hermes_home):
        # Legacy user config: only model settings, no kanban block.
        # Must NOT raise; kanban.notifications falls back to defaults.
        (hermes_home / "config.yaml").write_text(
            textwrap.dedent("""\
                model:
                  default: some-model
            """),
            encoding="utf-8",
        )
        config = load_config()
        notif = config["kanban"]["notifications"]
        assert notif["enabled"] is True
        assert notif["destinations"]["whatsapp"]["chat_id"] == "353899843924"

    def test_explicit_kanban_null_is_preserved(self, hermes_home):
        # User explicitly wrote ``kanban: null``: deep-merge semantics
        # preserve that explicit null (the user is asserting "no kanban
        # block"). The validator is silent on this case — no warning
        # because the user didn't ask for notifier behaviour.
        (hermes_home / "config.yaml").write_text(
            "kanban: null\n",
            encoding="utf-8",
        )
        config = load_config()
        assert config["kanban"] is None

    def test_explicit_notifications_null_is_preserved(self, hermes_home):
        # Same semantic: user wrote ``kanban.notifications: null`` to
        # opt out of notifications. Deep-merge preserves the null;
        # validator is silent because the block is absent.
        (hermes_home / "config.yaml").write_text(
            "kanban:\n  notifications: null\n",
            encoding="utf-8",
        )
        config = load_config()
        assert config["kanban"]["notifications"] is None


class TestOverrideMerge:
    """AC3: user override deep-merges with defaults (preserves siblings)."""

    def test_override_only_blocks_status_keeps_whatsapp_defaults(
        self, hermes_home
    ):
        (hermes_home / "config.yaml").write_text(
            textwrap.dedent("""\
                kanban:
                  notifications:
                    on_status:
                      blocked: false
            """),
            encoding="utf-8",
        )
        config = load_config()
        notif = config["kanban"]["notifications"]
        # Overridden
        assert notif["on_status"]["blocked"] is False
        # Preserved from defaults
        assert notif["on_status"]["awaiting_clarification"] is True
        assert notif["on_status"]["review"] is True
        assert notif["on_status"]["scheduled"] is False
        assert notif["destinations"]["whatsapp"]["chat_id"] == "353899843924"
        assert notif["destinations"]["whatsapp"]["template"].startswith("🔔 Kanban:")

    def test_override_whatsapp_chat_id_keeps_template(self, hermes_home):
        (hermes_home / "config.yaml").write_text(
            textwrap.dedent("""\
                kanban:
                  notifications:
                    destinations:
                      whatsapp:
                        chat_id: "15551234567"
            """),
            encoding="utf-8",
        )
        config = load_config()
        wa = config["kanban"]["notifications"]["destinations"]["whatsapp"]
        assert wa["chat_id"] == "15551234567"
        # Template + other fields kept from defaults.
        assert wa["template"].startswith("🔔 Kanban:")
        assert wa["profile"] == "main_profile"
        assert wa["thread_id"] is None

    def test_disable_v2_destination_keeps_others(self, hermes_home):
        (hermes_home / "config.yaml").write_text(
            textwrap.dedent("""\
                kanban:
                  notifications:
                    destinations:
                      email:
                        enabled: true
            """),
            encoding="utf-8",
        )
        config = load_config()
        dests = config["kanban"]["notifications"]["destinations"]
        assert dests["email"]["enabled"] is True
        # Sibling destinations preserved.
        assert dests["desktop_toast"]["enabled"] is False
        assert dests["cli"]["enabled"] is False
        # WhatsApp block preserved.
        assert dests["whatsapp"]["chat_id"] == "353899843924"

    def test_disable_master_toggle(self, hermes_home):
        (hermes_home / "config.yaml").write_text(
            textwrap.dedent("""\
                kanban:
                  notifications:
                    enabled: false
            """),
            encoding="utf-8",
        )
        config = load_config()
        assert config["kanban"]["notifications"]["enabled"] is False
        # Per-destination flags preserved (not silently flipped).
        wa = config["kanban"]["notifications"]["destinations"]["whatsapp"]
        assert wa["chat_id"] == "353899843924"


class TestUnknownDestinationWarns:
    """AC4: unknown destination / on_status keys log a warning, no raise."""

    def test_unknown_destination_logs_warning_and_does_not_raise(self, caplog):
        cfg = {
            "kanban": {
                "notifications": {
                    "enabled": True,
                    "destinations": {
                        # Typo'd destination — should warn, not raise.
                        "whatsap": {"chat_id": "123"},
                        # v2 adapter placeholder — also warn.
                        "push": {"enabled": True},
                    },
                },
            },
        }
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            # Must not raise.
            validate_kanban_notifications_config(cfg)
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("whatsap" in m for m in warnings), (
            f"expected warning about 'whatsap' destination, got {warnings}"
        )
        assert any("push" in m for m in warnings), (
            f"expected warning about 'push' destination, got {warnings}"
        )
        # Known destinations produce no warning.
        assert not any("whatsapp.chat_id" in m and "should be" in m for m in warnings)

    def test_unknown_status_logs_warning(self, caplog):
        cfg = {
            "kanban": {
                "notifications": {
                    "on_status": {
                        "blocked": True,
                        # Forward-compat: a future status; warns in v1.
                        "superseded": True,
                    },
                },
            },
        }
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            validate_kanban_notifications_config(cfg)
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("superseded" in m for m in warnings), (
            f"expected warning about 'superseded' status, got {warnings}"
        )

    def test_known_keys_produce_no_warnings(self, caplog):
        cfg = {
            "kanban": {
                "notifications": {
                    "enabled": True,
                    "on_status": {
                        "blocked": True,
                        "awaiting_clarification": True,
                        "review": True,
                        "scheduled": False,
                    },
                    "destinations": {
                        "whatsapp": {"chat_id": "353899843924"},
                        "desktop_toast": {"enabled": False},
                        "email": {"enabled": False},
                        "cli": {"enabled": False},
                    },
                },
            },
        }
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            validate_kanban_notifications_config(cfg)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], (
            f"known-shape config should produce no warnings, got: "
            f"{[r.getMessage() for r in warnings]}"
        )

    def test_missing_block_is_silent(self, caplog):
        # No ``kanban`` block at all — validator must be a no-op.
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            validate_kanban_notifications_config({})
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            validate_kanban_notifications_config({"kanban": {}})
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], (
            f"missing-block case should be silent, got: "
            f"{[r.getMessage() for r in warnings]}"
        )

    def test_load_config_with_unknown_destination_boots_clean(self, hermes_home, caplog):
        # End-to-end: user has a typo'd destination in their YAML. The
        # loader must NOT raise; the warning is the only side effect.
        (hermes_home / "config.yaml").write_text(
            textwrap.dedent("""\
                kanban:
                  notifications:
                    destinations:
                      slack_typo:
                        channel: "#ops"
            """),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            config = load_config()  # must not raise
        notif = config["kanban"]["notifications"]
        # The typo'd destination survived the merge (defaults don't
        # silently drop user keys — that's the merge semantics).
        assert "slack_typo" in notif["destinations"]
        # But a warning was logged so the user knows it's a no-op.
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("slack_typo" in m for m in warnings)


class TestMalformedBlocksWarn:
    """Auxiliary edges: malformed shapes warn but never raise."""

    def test_non_dict_destinations_block_warns(self, caplog):
        cfg = {
            "kanban": {
                "notifications": {
                    "destinations": "should-be-a-dict",
                },
            },
        }
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            validate_kanban_notifications_config(cfg)
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("destinations should be a mapping" in m for m in warnings)

    def test_non_dict_on_status_warns(self, caplog):
        cfg = {
            "kanban": {
                "notifications": {
                    "on_status": ["blocked", "review"],
                },
            },
        }
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            validate_kanban_notifications_config(cfg)
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("on_status should be a mapping" in m for m in warnings)

    def test_whatsapp_chat_id_wrong_type_warns(self, caplog):
        cfg = {
            "kanban": {
                "notifications": {
                    "destinations": {
                        "whatsapp": {"chat_id": 353899843924},  # int, not str
                    },
                },
            },
        }
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            validate_kanban_notifications_config(cfg)
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("chat_id" in m and "should be a string" in m for m in warnings)

    def test_destination_value_not_a_dict_warns(self, caplog):
        cfg = {
            "kanban": {
                "notifications": {
                    "destinations": {
                        "whatsapp": "should-be-a-dict",
                    },
                },
            },
        }
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            validate_kanban_notifications_config(cfg)
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("whatsapp should be a mapping" in m for m in warnings)

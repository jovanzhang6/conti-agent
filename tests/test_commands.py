from __future__ import annotations

import asyncio
import unittest

from conti_agent.commands import CommandContext, create_default_registry


class FakeRuntime:
    def __init__(self) -> None:
        self.busy = False
        self.commands = create_default_registry()
        self.provider_names = ["one", "two"]
        self.active_name = "one"

    def describe(self):
        return {"provider": self.active_name, "permission_mode": "workspace"}

    def list_providers(self):
        return [
            {"name": name, "model": f"model-{name}", "protocol": "fake",
             "active": name == self.active_name, "api_key_ready": True}
            for name in self.provider_names
        ]

    def get_provider_info(self, name):
        if name not in self.provider_names:
            raise ValueError("unknown")
        return {"name": name, "model": f"model-{name}", "active": name == self.active_name}

    def active_provider_name(self):
        return self.active_name

    def set_active_provider(self, name):
        if self.busy:
            raise RuntimeError("busy")
        if name not in self.provider_names:
            raise ValueError("unknown")
        self.active_name = name

    class sessions:
        @staticmethod
        def list():
            return [{"session_id": "abc", "title": "old"}]


class CommandTestCase(unittest.TestCase):
    def test_suggestions_include_commands_and_models(self) -> None:
        registry = create_default_registry()
        context = CommandContext(FakeRuntime())
        commands = [item.value for item in registry.suggest("/", context)]
        self.assertIn("/models", commands)
        self.assertIn("/model", commands)
        models = [item.value for item in registry.suggest("/model ", context)]
        self.assertEqual(models, ["one", "two"])

    def test_models_and_model_switch(self) -> None:
        registry = create_default_registry()
        runtime = FakeRuntime()
        context = CommandContext(runtime)
        result = asyncio.run(registry.execute("/models", context))
        self.assertTrue(result.ok)
        self.assertIn("model-one", "\n".join(result.output))
        result = asyncio.run(registry.execute("/model two", context))
        self.assertTrue(result.ok)
        self.assertEqual(runtime.active_name, "two")

    def test_model_switch_is_rejected_when_busy(self) -> None:
        runtime = FakeRuntime()
        runtime.busy = True
        registry = create_default_registry()
        result = asyncio.run(registry.execute("/model two", CommandContext(runtime)))
        self.assertFalse(result.ok)
        self.assertIn("运行中", result.output[0])

    def test_unknown_command_and_missing_argument(self) -> None:
        registry = create_default_registry()
        context = CommandContext(FakeRuntime())
        result = asyncio.run(registry.execute("/missing", context))
        self.assertFalse(result.ok)
        result = asyncio.run(registry.execute("/model", context))
        self.assertFalse(result.ok)
        self.assertIn("缺少", result.output[0])

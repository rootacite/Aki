from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_installer():
    spec = importlib.util.spec_from_file_location("aki_widget_installer", ROOT / "install-widget.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WidgetInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.installer = load_installer()

    def test_service_unit_has_bridge_settings(self) -> None:
        unit = self.installer.service_unit(
            python_bin="/usr/bin/python3",
            repo=ROOT,
            socket_path="/run/user/1000/aki/akid.sock",
            bridge_port=8765,
            token_file="/home/user/.local/share/aki/gui-token",
            endpoint_file="/home/user/.local/share/aki/gui-bridge.json",
        )
        for fragment in (
            "ExecStart=/usr/bin/python3",
            "engine/gui_bridge.py",
            "AKI_GUI_BRIDGE_PORT=8765",
            "AKI_GUI_TOKEN_FILE=",
            "Restart=on-failure",
            "UMask=0077",
        ):
            self.assertIn(fragment, unit)

    def test_generated_widget_config_contains_token(self) -> None:
        config = self.installer.bridge_config_js(port=8765, token="secret-token")
        self.assertIn('var port = 8765;', config)
        self.assertIn('var token = "secret-token";', config)
        self.assertIn("encodeURIComponent(token)", config)

    def test_dry_run_does_not_write_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            token_file = Path(tempdir) / "gui-token"
            endpoint_file = Path(tempdir) / "gui-bridge.json"
            output = io.StringIO()
            with redirect_stdout(output):
                result = self.installer.main(
                    [
                        "--dry-run",
                        "--bridge-port",
                        "8765",
                        "--token-file",
                        str(token_file),
                        "--endpoint-file",
                        str(endpoint_file),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertFalse(token_file.exists())
            self.assertFalse(endpoint_file.exists())
            self.assertIn("engine/gui_bridge.py", output.getvalue())


if __name__ == "__main__":
    unittest.main()

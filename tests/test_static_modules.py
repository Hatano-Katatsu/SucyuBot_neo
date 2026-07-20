from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = PROJECT_ROOT / "telegram_comfyui_selfie" / "static"


class StaticModuleBoundaryTestCase(unittest.TestCase):
    """前端拆分的静态契约，防止功能又被并回单体入口。"""

    def test_admin_logs_module_loads_before_app_entrypoint(self):
        index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")

        admin_pos = index.index('<script src="/static/admin_logs.js"></script>')
        app_pos = index.index('<script src="/static/app.js"></script>')

        self.assertLess(admin_pos, app_pos)

    def test_admin_logs_domain_is_kept_out_of_app_entrypoint(self):
        admin_logs = (STATIC_ROOT / "admin_logs.js").read_text(encoding="utf-8")
        app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        domain_functions = (
            "loadLogs",
            "loadUsage",
            "renderLogList",
            "renderUsage",
            "selectLog",
            "selectSystemLog",
            "formatSystemErrorEntry",
        )

        for name in domain_functions:
            declaration = f"function {name}("
            self.assertIn(declaration, admin_logs)
            self.assertNotIn(declaration, app)


if __name__ == "__main__":
    unittest.main()

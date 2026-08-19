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

    def test_frontend_core_loads_before_and_is_used_by_app_entrypoint(self):
        index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

        core_pos = index.index('<script src="/static/frontend_core.js"></script>')
        app_pos = index.index('<script src="/static/app.js"></script>')
        self.assertLess(core_pos, app_pos)
        for helper in (
            "buildRequestOptions",
            "parseApiResponse",
            "firstInvalidNumberField",
            "resolveCommands",
            "resolveSelectedSession",
        ):
            self.assertIn(f"frontendCore.{helper}", app)

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

    def test_world_ui_module_loads_before_app_entrypoint(self):
        index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")

        world_pos = index.index('<script src="/static/world_ui.js"></script>')
        app_pos = index.index('<script src="/static/app.js"></script>')

        self.assertLess(world_pos, app_pos)

    def test_world_domain_is_kept_out_of_app_entrypoint(self):
        world_ui = (STATIC_ROOT / "world_ui.js").read_text(encoding="utf-8")
        app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        domain_functions = (
            "loadWorldSessions",
            "loadWorldRoute",
            "renderWorldSessionList",
            "renderWorldRoute",
            "renderLifePlan",
            "handleLifePlanAction",
            "renderCharacterInteraction",
            "handleCharacterInteractionAction",
        )

        for name in domain_functions:
            declaration = f"function {name}("
            self.assertIn(declaration, world_ui)
            self.assertNotIn(declaration, app)

    def test_character_ui_module_loads_before_app_entrypoint(self):
        index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")

        character_pos = index.index('<script src="/static/character_ui.js"></script>')
        app_pos = index.index('<script src="/static/app.js"></script>')

        self.assertLess(character_pos, app_pos)

    def test_character_domain_is_kept_out_of_app_entrypoint(self):
        character_ui = (STATIC_ROOT / "character_ui.js").read_text(encoding="utf-8")
        app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        domain_functions = (
            "renderWardrobePanel",
            "loadCharacterPage",
            "renderCharacterPool",
            "renderCharacterForm",
            "loadMemories",
            "loadDiaries",
            "activateSelectedCharacter",
            "handleCharacterImportFile",
        )

        for name in domain_functions:
            declaration = f"function {name}("
            self.assertIn(declaration, character_ui)
            self.assertNotIn(declaration, app)

    def test_world_session_actions_are_separate_native_buttons(self):
        world_ui = (STATIC_ROOT / "world_ui.js").read_text(encoding="utf-8")

        self.assertIn('document.createElement("div")', world_ui)
        self.assertIn('select.className = "world-session-select"', world_ui)
        self.assertIn('toggle.className = "session-freeze-toggle"', world_ui)
        self.assertNotIn('role="button"', world_ui)
        self.assertIn('toast(err.message, "error")', world_ui)

    def test_world_ui_exposes_local_character_interaction_settings(self):
        world_ui = (STATIC_ROOT / "world_ui.js").read_text(encoding="utf-8")
        app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

        self.assertIn("/character-interaction", world_ui)
        self.assertIn("每日互动上限", world_ui)
        self.assertIn("当前活动角色也必须在所选列表中", world_ui)
        self.assertIn('document.addEventListener("click", handleCharacterInteractionAction)', app)

    def test_cross_world_encounter_settings_fold_and_use_llm_strength(self):
        app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

        self.assertIn('"cross_world_encounter_trigger_strength"', app)
        self.assertNotIn('"cross_world_encounter_chance"', app)
        self.assertIn('"encounter_strength", "cross-world-detail"', app)
        self.assertIn('form.querySelectorAll(".field-cross-world-detail")', app)
        self.assertIn("wrap.hidden = !enabled", app)
        self.assertIn("control.disabled = !enabled", app)
        self.assertIn("仅作为模型判断的软倾向，不对应固定概率", app)
        self.assertIn('<option value="low">低</option>', app)
        self.assertIn('<option value="medium">中</option>', app)
        self.assertIn('<option value="high">高</option>', app)

    def test_admin_animaflow_ui_discovers_workflows_and_surfaces_legacy_fallback(self):
        app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

        self.assertIn('["animaflow_enabled", "启用 AnimaFlow", "bool"]', app)
        self.assertIn('"/api/admin/animaflow/discover"', app)
        self.assertIn("data.legacy_fallback", app)
        self.assertIn("已回退 turbo_v1", app)
        self.assertNotIn('<option value="turbo_v1"', app)

    def test_animaflow_toggle_folds_only_native_image_backend_fields(self):
        app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

        for key in ("width", "height", "sampler", "scheduler", "turbo_mode", "turbo_strength"):
            self.assertRegex(app, rf'\["{key}",[^\n]+"native-image-detail"\]')
        for key in ("current_style",):
            field_line = next(line for line in app.splitlines() if line.strip().startswith(f'["{key}",'))
            self.assertNotIn("native-image-detail", field_line)
        self.assertIn('form.querySelectorAll(".field-native-image-detail")', app)
        self.assertIn("wrap.hidden = enabled", app)
        self.assertIn("control.disabled = enabled", app)

    def test_llm_debug_ui_uses_cursor_pagination(self):
        index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        admin_logs = (STATIC_ROOT / "admin_logs.js").read_text(encoding="utf-8")

        self.assertIn('id="log-page-newer"', index)
        self.assertIn('id="log-page-older"', index)
        self.assertIn('params.set("before", String(before))', admin_logs)
        self.assertIn("data.next_before", admin_logs)

    def test_log_ui_switches_info_debug_and_renders_full_debug_payload(self):
        index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        admin_logs = (STATIC_ROOT / "admin_logs.js").read_text(encoding="utf-8")
        app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="log-level"', index)
        self.assertIn('<option value="info" selected>INFO · 交互与行为</option>', index)
        self.assertIn('<option value="debug">DEBUG · 完整 LLM 请求</option>', index)
        self.assertIn('logLevel: "info"', app)
        self.assertIn('state.logLevel === "debug"', admin_logs)
        self.assertIn('完整请求:\\n${prettyJson(entry.request)}', admin_logs)
        self.assertIn('完整返回:\\n${prettyJson(entry.response)', admin_logs)
        self.assertIn("box.scrollTop = 0", admin_logs)

if __name__ == "__main__":
    unittest.main()

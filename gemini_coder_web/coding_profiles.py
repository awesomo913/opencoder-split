"""Presets for improvement focuses + build target, per coding context."""

from __future__ import annotations

import json
import copy
from pathlib import Path
from typing import Any, Optional

# Keys must exist in broadcast.IMPROVEMENT_FOCUSES
from .broadcast import FOCUS_ORDER

PROFILE_PATH_HUMAN = "coding_profile_settings.json"


def _path() -> Path:
    p = Path.home() / ".autocoder" / PROFILE_PATH_HUMAN
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def default_settings() -> dict[str, Any]:
    return {
        "active_preset": "balanced",
        "show_task_panel": True,
        "show_startup_summary": True,
        "autostart_windows_login": False,
        "autoresume_endless_on_login": False,
        # When True, Autocoder attaches docs/openclaw_reference_pack/*.md (except
        # the master one-shot, which seeds the task box) and applies sovereign toggles.
        "openclaw_reference_bootstrap": True,
        # When True (and reference bootstrap runs), also enable Apex Frontier overdrive in the UI.
        "openclaw_frontier_on_bootstrap": True,
        "overrides": {},  # preset_id -> {build_target, focuses, expand_on_stagnation, perfection_loop}
    }


def load_profile_settings() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return default_settings()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_settings()
        base = default_settings()
        base.update({k: v for k, v in data.items() if k in base})
        if "overrides" in data and isinstance(data["overrides"], dict):
            base["overrides"] = data["overrides"]
        return base
    except Exception:
        return default_settings()


def save_profile_settings(data: dict[str, Any]) -> None:
    try:
        _path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


# Preset catalog: id -> display metadata + default bundle (before user overrides)
# focuses: list of focus keys; None for custom = do not auto-apply
PRESETS: dict[str, dict[str, Any]] = {
    "balanced": {
        "label": "Balanced (general)",
        "description": "A sensible mix of structure, quality, and features for most projects.",
        "build_target": "PC Desktop App",
        "focuses": [
            "deep_dive", "solid_functional", "extra_features", "beautiful_gui",
            "review_grade", "pressure_test", "documentation", "packaging",
        ],
        "expand_on_stagnation": False,
        "perfection_loop": False,
    },
    "web": {
        "label": "Web / full-stack",
        "description": "APIs, front-end, security, and deployment-minded passes.",
        "build_target": "Website / Web App",
        "focuses": [
            "data_layer", "api_design", "state_management", "security",
            "beautiful_gui", "accessibility", "performance", "network_resilience",
            "ci_cd", "documentation",
        ],
        "expand_on_stagnation": True,
        "perfection_loop": False,
    },
    "backend": {
        "label": "Backend & services",
        "description": "Data layer, jobs, auth, logging, and resilience — minimal UI focus.",
        "build_target": "PC Desktop App",
        "focuses": [
            "data_layer", "api_design", "background_jobs", "error_recovery",
            "authentication", "authorization_permissions", "logging_observability",
            "concurrency", "network_resilience", "pressure_test", "ci_cd",
        ],
        "expand_on_stagnation": True,
        "perfection_loop": False,
    },
    "desktop_gui": {
        "label": "Desktop / GUI",
        "description": "Polished UI, accessibility, config, and packaging for desktop apps.",
        "build_target": "PC Desktop App",
        "focuses": [
            "solid_functional", "beautiful_gui", "accessibility", "configuration",
            "shortcuts_hotkeys", "import_export", "notifications_feedback", "packaging", "review_grade",
        ],
        "expand_on_stagnation": False,
        "perfection_loop": False,
    },
    "data_ml": {
        "label": "Data / ML / analytics",
        "description": "Performance, data pipelines, and observability over pixels.",
        "build_target": "PC Desktop App",
        "focuses": [
            "data_layer", "memory_optimization", "performance", "logging_observability",
            "monitoring_alerting", "pressure_test", "test_suite", "documentation",
        ],
        "expand_on_stagnation": True,
        "perfection_loop": False,
    },
    "game": {
        "label": "Game / interactive",
        "description": "Game loop, AI behavior, save systems, and real-time feel.",
        "build_target": "Game",
        "focuses": [
            "game_loop", "ai_behavior", "save_system", "performance", "memory_optimization",
            "beautiful_gui", "animations_transitions", "error_recovery", "packaging",
        ],
        "expand_on_stagnation": True,
        "perfection_loop": False,
    },
    "cli_script": {
        "label": "Script / CLI / automation",
        "description": "CLIs, packaging, and robust one-off tools.",
        "build_target": "PC Desktop App",
        "focuses": [
            "cli_ux", "solid_functional", "error_recovery", "configuration", "packaging",
            "documentation", "type_safety", "review_grade",
        ],
        "expand_on_stagnation": False,
        "perfection_loop": False,
    },
    "devops": {
        "label": "DevOps / platform",
        "description": "CI/CD, config, security, and ops hygiene.",
        "build_target": "PC Desktop App",
        "focuses": [
            "ci_cd", "packaging", "security", "logging_observability", "monitoring_alerting",
            "configuration", "network_resilience", "documentation", "review_grade",
        ],
        "expand_on_stagnation": False,
        "perfection_loop": False,
    },
    "custom": {
        "label": "Custom (manual)",
        "description": "No automatic changes. Use checkboxes and Edit to tune your own plan.",
        "build_target": None,  # sentinel: leave combo as-is
        "focuses": None,  # do not auto-check
        "expand_on_stagnation": None,
        "perfection_loop": None,
    },
    "openclaw_sovereign": {
        "label": "OpenClaw / sovereign stack",
        "description": (
            "OpenClaw plugins + workstation automation. Enables perfection loop, "
            "expand on stagnation, and broad passes including Explore & Expand."
        ),
        "build_target": "PC Desktop App",
        "focuses": [
            "explore_expand",
            "deep_dive",
            "solid_functional",
            "extra_features",
            "integration_layer",
            "security",
            "error_recovery",
            "configuration",
            "documentation",
            "review_grade",
            "test_suite",
            "cli_ux",
        ],
        "expand_on_stagnation": True,
        "perfection_loop": True,
        # OpenClaw artifacts are usually pasted TS/JSON, not an npm repo — do not
        # run npm test by default (was causing false rejections in broadcast logs).
        "acceptance_commands": [],
        "strict_acceptance": False,
        "promote_only_passing": True,
    },
}


def _valid_focuses(keys: list[str] | None) -> list[str]:
    if not keys:
        return []
    from .broadcast import IMPROVEMENT_FOCUSES
    return [k for k in keys if k in IMPROVEMENT_FOCUSES]


def effective_bundle(preset_id: str) -> dict[str, Any]:
    """Merge PRESETS[preset_id] with saved overrides."""
    if preset_id not in PRESETS:
        preset_id = "balanced"
    base = copy.deepcopy(PRESETS[preset_id])
    s = load_profile_settings()
    ov = s.get("overrides", {}).get(preset_id) or {}
    for k in (
        "build_target",
        "expand_on_stagnation",
        "perfection_loop",
        "acceptance_commands",
        "strict_acceptance",
        "promote_only_passing",
    ):
        if k in ov and ov[k] is not None:
            base[k] = ov[k]
    if "focuses" in ov and ov["focuses"] is not None:
        base["focuses"] = _valid_focuses(list(ov["focuses"]))
    if base.get("focuses") is not None:
        base["focuses"] = _valid_focuses(base["focuses"])
    return base


def list_preset_ids() -> list[str]:
    return list(PRESETS.keys())


def preset_choices() -> list[str]:
    return [PRESETS[k]["label"] for k in PRESETS]


def id_from_label(label: str) -> str:
    for k, v in PRESETS.items():
        if v["label"] == label:
            return k
    return "balanced"

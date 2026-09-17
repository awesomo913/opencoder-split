"""Expansion engine - infinitely explore and branch ideas with Gemini."""

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExpansionOption:
    """A single option/branch that the user can explore."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    description: str = ""
    difficulty: str = "beginner"
    estimated_time: str = ""
    code_preview: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class ExpansionNode:
    """A node in the expansion tree."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    prompt: str = ""
    response_summary: str = ""
    full_response: str = ""
    options: list[ExpansionOption] = field(default_factory=list)
    selected_option: Optional[str] = None
    children: list["ExpansionNode"] = field(default_factory=list)
    parent_id: Optional[str] = None
    depth: int = 0
    created_at: float = field(default_factory=time.time)


class ExpansionEngine:
    """Manages the expansion tree and generates options via Gemini."""

    SYSTEM_PROMPT = (
        "You are a patient coding mentor explaining things to a complete beginner. "
        "Your job is to take an idea and break it into clear options.\n\n"
        "RULES:\n"
        "- Explain everything like the reader has NEVER written code before\n"
        "- Use simple analogies (like comparing variables to labeled boxes)\n"
        "- Every technical term gets a plain English explanation in parentheses\n"
        "- Show small code snippets only as examples, with line-by-line explanations\n"
        "- Be encouraging and supportive\n"
        "- Present exactly 3-5 options for how to proceed\n\n"
        "FORMAT YOUR RESPONSE EXACTLY LIKE THIS:\n"
        "## Summary\n"
        "[2-3 sentence overview of what we're building and why]\n\n"
        "## Options\n\n"
        "### Option 1: [Title]\n"
        "**Difficulty:** [Beginner/Intermediate/Advanced]\n"
        "**Time:** [estimated time]\n"
        "**What it does:** [plain English explanation]\n"
        "**Why choose this:** [when this is the right choice]\n"
        "```[language]\n"
        "[small code preview, 5-15 lines max]\n"
        "```\n\n"
        "[Repeat for each option]\n\n"
        "## What I Recommend\n"
        "[Your recommendation for a beginner and why]"
    )

    def __init__(self, gemini_client, depth_limit: int = 10) -> None:
        self._client = gemini_client
        self._depth_limit = depth_limit
        self._root: Optional[ExpansionNode] = None
        self._current_node: Optional[ExpansionNode] = None
        self._all_nodes: dict[str, ExpansionNode] = {}
        self._lock = threading.Lock()
        self._on_expand: Optional[Callable[[ExpansionNode], None]] = None
        self._on_progress: Optional[Callable[[str], None]] = None

    @property
    def root(self) -> Optional[ExpansionNode]:
        return self._root

    @property
    def current_node(self) -> Optional[ExpansionNode]:
        return self._current_node

    @property
    def current_depth(self) -> int:
        return self._current_node.depth if self._current_node else 0

    @property
    def can_go_deeper(self) -> bool:
        return self.current_depth < self._depth_limit

    def set_callbacks(
        self,
        on_expand: Optional[Callable[[ExpansionNode], None]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_expand = on_expand
        self._on_progress = on_progress

    def start_new(self, idea: str) -> ExpansionNode:
        """Start a new expansion tree from an idea."""
        with self._lock:
            self._root = ExpansionNode(prompt=idea, depth=0)
            self._all_nodes = {self._root.id: self._root}
            self._current_node = self._root
        self._expand_node(self._root)
        return self._root

    def expand_option(self, option_id: str) -> Optional[ExpansionNode]:
        """Select an option and expand it into a new node."""
        if not self._current_node:
            return None
        if not self.can_go_deeper:
            logger.warning("Depth limit %d reached", self._depth_limit)
            return None

        option = None
        for opt in self._current_node.options:
            if opt.id == option_id:
                option = opt
                break
        if not option:
            return None

        self._current_node.selected_option = option_id

        child = ExpansionNode(
            prompt=f"I chose: {option.title}\n\n{option.description}\n\nExpand on this choice. What are the next steps and options?",
            parent_id=self._current_node.id,
            depth=self._current_node.depth + 1,
        )

        with self._lock:
            self._current_node.children.append(child)
            self._all_nodes[child.id] = child
            self._current_node = child

        self._expand_node(child)
        return child

    def go_back(self) -> Optional[ExpansionNode]:
        """Navigate back to the parent node."""
        if not self._current_node or not self._current_node.parent_id:
            return None
        parent = self._all_nodes.get(self._current_node.parent_id)
        if parent:
            self._current_node = parent
            return parent
        return None

    def go_to_node(self, node_id: str) -> Optional[ExpansionNode]:
        """Navigate to a specific node by ID."""
        node = self._all_nodes.get(node_id)
        if node:
            self._current_node = node
            return node
        return None

    def generate_code_for_current(self) -> str:
        """Generate full implementation code for the current path."""
        if not self._current_node:
            return ""

        path_descriptions = self._get_path_to_current()
        prompt = (
            "Based on all the choices made so far, write the COMPLETE, "
            "RUNNABLE code implementation.\n\n"
            "Choices made:\n"
        )
        for i, desc in enumerate(path_descriptions, 1):
            prompt += f"{i}. {desc}\n"
        prompt += (
            "\nProvide the full implementation with:\n"
            "- All imports\n"
            "- All functions and classes\n"
            "- Error handling\n"
            "- A main section that demonstrates usage\n"
            "- Comments explaining each section for a beginner"
        )

        return self._client.generate(
            prompt=prompt,
            system_instruction=(
                "You are an expert coder. Write complete, production-ready code "
                "based on the user's selections. Include beginner-friendly comments."
            ),
            on_progress=self._on_progress,
        )

    def _expand_node(self, node: ExpansionNode) -> None:
        """Send the node's prompt to Gemini and parse options."""
        try:
            context = ""
            if node.parent_id:
                path = self._get_path_to_node(node.id)
                if path:
                    context = "Previous choices:\n"
                    for desc in path[:-1]:
                        context += f"- {desc}\n"
                    context += "\n"

            full_prompt = context + node.prompt

            response = self._client.generate(
                prompt=full_prompt,
                system_instruction=self.SYSTEM_PROMPT,
                on_progress=self._on_progress,
            )

            node.full_response = response
            node.response_summary = self._extract_summary(response)
            node.options = self._parse_options(response)

            if self._on_expand:
                self._on_expand(node)

        except Exception as e:
            logger.error("Expansion failed: %s", e)
            node.response_summary = f"Error: {e}"
            if self._on_expand:
                self._on_expand(node)

    def _parse_options(self, response: str) -> list[ExpansionOption]:
        """Parse options from Gemini's response."""
        options = []
        option_pattern = re.compile(
            r"###\s*Option\s*\d+\s*:\s*(.+?)(?:\n|$)"
            r"(.*?)(?=###\s*Option|##\s*What|$)",
            re.DOTALL | re.IGNORECASE,
        )

        for match in option_pattern.finditer(response):
            title = match.group(1).strip()
            body = match.group(2).strip()

            difficulty = "beginner"
            diff_match = re.search(
                r"\*\*Difficulty:\*\*\s*(\w+)", body, re.IGNORECASE
            )
            if diff_match:
                difficulty = diff_match.group(1).lower()

            time_est = ""
            time_match = re.search(
                r"\*\*Time:\*\*\s*(.+?)(?:\n|$)", body, re.IGNORECASE
            )
            if time_match:
                time_est = time_match.group(1).strip()

            code_preview = ""
            code_match = re.search(r"```[\w]*\n(.*?)```", body, re.DOTALL)
            if code_match:
                code_preview = code_match.group(1).strip()

            description = body
            desc_match = re.search(
                r"\*\*What it does:\*\*\s*(.+?)(?:\n\*\*|$)", body,
                re.DOTALL | re.IGNORECASE,
            )
            if desc_match:
                description = desc_match.group(1).strip()

            options.append(ExpansionOption(
                title=title,
                description=description,
                difficulty=difficulty,
                estimated_time=time_est,
                code_preview=code_preview,
            ))

        if not options:
            options.append(ExpansionOption(
                title="Continue with this approach",
                description="Let Gemini elaborate further on this idea.",
                difficulty="beginner",
            ))

        return options

    def _extract_summary(self, response: str) -> str:
        """Extract the summary section from the response."""
        summary_match = re.search(
            r"##\s*Summary\s*\n(.*?)(?=##|\Z)", response,
            re.DOTALL | re.IGNORECASE,
        )
        if summary_match:
            return summary_match.group(1).strip()
        lines = response.split("\n")
        return " ".join(lines[:3]).strip()

    def _get_path_to_current(self) -> list[str]:
        """Get descriptions of all choices leading to current node."""
        return self._get_path_to_node(
            self._current_node.id if self._current_node else ""
        )

    def _get_path_to_node(self, node_id: str) -> list[str]:
        """Get descriptions of all choices leading to a node."""
        path = []
        current_id = node_id
        while current_id:
            node = self._all_nodes.get(current_id)
            if not node:
                break
            if node.selected_option:
                for opt in node.options:
                    if opt.id == node.selected_option:
                        path.insert(0, f"{opt.title}: {opt.description[:100]}")
                        break
            elif node.prompt:
                path.insert(0, node.prompt[:100])
            current_id = node.parent_id
        return path

    def get_breadcrumbs(self) -> list[tuple[str, str]]:
        """Get (id, title) breadcrumbs from root to current node."""
        crumbs = []
        current_id = self._current_node.id if self._current_node else None
        while current_id:
            node = self._all_nodes.get(current_id)
            if not node:
                break
            title = node.prompt[:40] + "..." if len(node.prompt) > 40 else node.prompt
            crumbs.insert(0, (node.id, title))
            current_id = node.parent_id
        return crumbs

    def export_tree(self) -> dict:
        """Export the expansion tree as a dict."""
        if not self._root:
            return {}
        return self._node_to_dict(self._root)

    def _node_to_dict(self, node: ExpansionNode) -> dict:
        return {
            "id": node.id,
            "prompt": node.prompt,
            "summary": node.response_summary,
            "depth": node.depth,
            "options": [
                {
                    "id": o.id, "title": o.title,
                    "description": o.description, "difficulty": o.difficulty,
                    "selected": o.id == node.selected_option,
                }
                for o in node.options
            ],
            "children": [self._node_to_dict(c) for c in node.children],
        }

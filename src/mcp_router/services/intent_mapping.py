"""Intent-based keyword fallback for tool search."""

# Maps intent -> server -> tool names (subset for MVP fallback)
INTENT_TOOL_MAPPING: dict[str, dict[str, list[str]]] = {
    "task_management": {
        "code-tasks": ["create_task", "get_task", "list_tasks", "update_task", "set_current_task"],
    },
    "code_search": {
        "code-analyze": ["find_code", "search_code", "hybrid_search", "search_docs"],
    },
    "research": {
        "context7": ["resolve-library-id", "query-docs"],
        "searxng": ["searxng_web_search", "web_url_read"],
    },
    "vcs": {
        "github": ["search_repositories", "create_pull_request", "list_issues"],
    },
}

INTENT_KEYWORDS: dict[str, set[str]] = {
    "task_management": {
        "задача",
        "задачи",
        "создай",
        "создать",
        "создать задачу",
        "task",
        "create task",
        "todo",
    },
    "code_search": {
        "найди",
        "поиск",
        "код",
        "функция",
        "find",
        "search code",
        "analyze",
    },
    "research": {
        "документация",
        "docs",
        "library",
        "search web",
        "поиск информации",
    },
    "vcs": {
        "github",
        "git",
        "pull request",
        "pr",
        "commit",
        "repository",
    },
}

SERVER_KEYWORDS: dict[str, set[str]] = {
    "github": {"github", "git", "repo", "pull request", "pr"},
    "context7": {"context7", "library", "docs", "documentation"},
    "code-tasks": {"задача", "task", "gr-", "todo"},
    "code-analyze": {"code", "код", "analyze", "find", "search"},
    "searxng": {"search", "web", "поиск", "url"},
}


def detect_intent_from_query(query: str) -> list[str]:
    q = query.lower()
    scores: dict[str, int] = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in q)
        if count:
            scores[intent] = count
    return [intent for intent, _ in sorted(scores.items(), key=lambda x: -x[1])]


def detect_servers_from_query(query: str) -> list[str]:
    q = query.lower()
    found = []
    for server, keywords in SERVER_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            found.append(server)
    return found


def get_tools_for_intents(intents: list[str]) -> list[tuple[str, str]]:
    tools: list[tuple[str, str]] = []
    for intent in intents:
        for server, names in INTENT_TOOL_MAPPING.get(intent, {}).items():
            for name in names:
                tools.append((server, name))
    return tools

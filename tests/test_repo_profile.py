import json
import pytest
from founderscrew.state.store import StateStore
from founderscrew.tools.repo_profile import (
    build_repo_profile,
    get_repo_memory,
    add_repo_lesson,
    format_repo_memory,
    MAX_LESSONS
)

@pytest.fixture
def memory_store(tmp_path):
    store = StateStore()
    store.backend = "sqlite"
    store.sqlite_db_path = tmp_path / "memory.db"
    store._init_sqlite()
    return store

@pytest.fixture
def js_repo(tmp_path):
    """A minimal vite/playwright JS repo layout."""
    root = tmp_path / "repo"
    (root / "tests" / "integration").mkdir(parents=True)
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "package.json").write_text(json.dumps({
        "name": "demo",
        "scripts": {"test": "playwright test", "dev": "vite", "build": "vite build"},
        "dependencies": {"react": "^19.0.0", "vite": "^5.0.0"},
        "devDependencies": {"@playwright/test": "^1.40.0"}
    }), encoding="utf-8")
    (root / "tests" / "integration" / "01-journey.spec.js").write_text("// test", encoding="utf-8")
    (root / "tests" / "unit" / "calc.test.mjs").write_text("// test", encoding="utf-8")
    return root

def test_build_repo_profile_js(js_repo):
    profile = build_repo_profile("owner/demo", str(js_repo))
    assert "javascript/typescript" in profile["languages"]
    assert profile["test_framework"] == "playwright"
    assert profile["test_command"] == "npm test"
    assert "vite" in profile["dev_server_command"]
    assert "tests/integration" in profile["test_dirs"]
    assert "tests/unit" in profile["test_dirs"]
    assert "*.spec.js" in profile["test_naming"]
    assert "*.test.mjs" in profile["test_naming"]
    assert "react" in profile["frameworks"]

def test_repo_memory_roundtrip_and_lessons_survive(memory_store, js_repo):
    # First call with a workdir builds and persists the profile
    memory = get_repo_memory(memory_store, "owner/demo", str(js_repo))
    assert memory["profile"]["test_command"] == "npm test"

    # Lessons are appended and survive subsequent profile lookups
    add_repo_lesson(memory_store, "owner/demo", {"issue": 1, "summary": "tests need PLAYWRIGHT_BASE_URL"})
    memory = get_repo_memory(memory_store, "owner/demo", str(js_repo))
    assert memory["profile"] is not None
    assert len(memory["lessons"]) == 1
    assert memory["lessons"][0]["summary"] == "tests need PLAYWRIGHT_BASE_URL"

    # Formatter renders both sections
    text = format_repo_memory(memory)
    assert "REPO PROFILE" in text
    assert "npm test" in text
    assert "LESSONS FROM PREVIOUS WORK" in text
    assert "PLAYWRIGHT_BASE_URL" in text

def test_lessons_capped(memory_store):
    for i in range(MAX_LESSONS + 5):
        add_repo_lesson(memory_store, "owner/demo", {"issue": i, "summary": f"lesson {i}"})
    record = memory_store.load_repo_memory("owner/demo")
    assert len(record["lessons"]) == MAX_LESSONS
    # Oldest entries were dropped, newest kept
    assert record["lessons"][-1]["summary"] == f"lesson {MAX_LESSONS + 4}"

def test_format_repo_memory_empty():
    assert format_repo_memory({"profile": None, "lessons": []}) == ""

from pathlib import Path
from unittest.mock import patch
from PIL import Image
from founderscrew.tools import screenshot_tools
from founderscrew.tools.screenshot_tools import (
    analyze_screenshot,
    capture_interactive_screenshot,
    capture_screenshot,
    diagnose_page_render,
    generate_mock_browser_screenshot,
    compare_screenshots,
)

def test_generate_mock_screenshot(tmp_path):
    output_path = tmp_path / "mock_browser.png"
    success = generate_mock_browser_screenshot("https://google.com", str(output_path))
    assert success is True
    assert output_path.exists()
    assert output_path.stat().st_size > 0

def test_compare_screenshots_identical(tmp_path):
    img_a = tmp_path / "img_a.png"
    img_b = tmp_path / "img_b.png"
    
    # Generate identical mockup screenshots
    generate_mock_browser_screenshot("https://test.local", str(img_a))
    generate_mock_browser_screenshot("https://test.local", str(img_b))
    
    similarity = compare_screenshots(str(img_a), str(img_b))
    assert similarity == 100.0

def test_compare_screenshots_different(tmp_path):
    img_a = tmp_path / "img_a.png"
    img_b = tmp_path / "img_b.png"
    
    # Generate different mockup screenshots (different URLs)
    generate_mock_browser_screenshot("https://url-one.local", str(img_a))
    generate_mock_browser_screenshot("https://url-two.local", str(img_b))
    
    similarity = compare_screenshots(str(img_a), str(img_b))
    # It should not be exactly 100.0 because the URL texts in url bars differ
    assert similarity < 100.0

def test_capture_screenshot_uses_workspace_node_playwright_when_python_missing(tmp_path, monkeypatch):
    workdir = tmp_path / "app"
    workdir.mkdir()
    (workdir / "node_modules").mkdir()
    (workdir / "package.json").write_text('{"devDependencies":{"@playwright/test":"^1.40.0"}}')
    (workdir / ".env").write_text(
        "PLAYWRIGHT_TEST_EMAIL=founder@example.com\nPLAYWRIGHT_TEST_PASSWORD=secret-password\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "real.png"

    def fail_if_python_is_used(url, output_file, errors):
        raise AssertionError("workspace screenshots must not use host Python Playwright")

    def fake_run(cmd, cwd, capture_output, text, timeout, env):
        assert cmd[0:2] == ["node", "-e"]
        assert cwd == str(workdir)
        assert cmd[-1] == str(workdir)
        assert "secret-password" not in cmd
        assert "bootstrapLogin" in cmd[2]
        assert "dismissConsentPopups" in cmd[2]
        assert "waitForAuthToClear" in cmd[2]
        assert "Target route" in cmd[2]
        assert 'button:has-text("Accept All")' in cmd[2]
        assert env["PLAYWRIGHT_BROWSERS_PATH"] == "0"
        assert env["PLAYWRIGHT_TEST_EMAIL"] == "founder@example.com"
        assert env["PLAYWRIGHT_TEST_PASSWORD"] == "secret-password"
        assert "PLAYWRIGHT_TEST_EMAIL" in env["FOUNDERSCREW_QA_EMAIL_KEYS"]
        Path(cmd[-2]).write_bytes(b"png")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(screenshot_tools, "_capture_with_python_playwright", fail_if_python_is_used)
    monkeypatch.setattr(screenshot_tools.subprocess, "run", fake_run)

    assert capture_screenshot("http://localhost:3001", str(output_path), allow_mock=False, workdir=str(workdir)) is True
    assert output_path.read_bytes() == b"png"

def test_capture_screenshot_refuses_mock_when_real_capture_fails(tmp_path, monkeypatch):
    output_path = tmp_path / "no_mock.png"

    def fail_python(url, output_file, errors):
        errors.append("Python Playwright: missing")
        return False

    def fail_node(url, output_file, workdir, errors):
        errors.append("Node Playwright: missing")
        return False

    monkeypatch.setattr(screenshot_tools, "_capture_with_python_playwright", fail_python)
    monkeypatch.setattr(screenshot_tools, "_capture_with_node_playwright", fail_node)

    assert capture_screenshot("http://localhost:3001", str(output_path), allow_mock=False) is False
    assert not output_path.exists()

def test_analyze_screenshot_flags_blank_image(tmp_path):
    output_path = tmp_path / "blank.png"
    Image.new("RGB", (1280, 800), "#0f172a").save(output_path)

    analysis = analyze_screenshot(str(output_path))

    assert analysis["ok"] is True
    assert analysis["is_blank"] is True
    assert "same color" in analysis["reason"]

def test_analyze_screenshot_allows_visual_mock(tmp_path):
    output_path = tmp_path / "mock.png"
    generate_mock_browser_screenshot("http://localhost:3001", str(output_path))

    analysis = analyze_screenshot(str(output_path))

    assert analysis["ok"] is True
    assert analysis["is_blank"] is False
    assert analysis["unique_color_count"] > 3

def test_diagnose_page_render_uses_workspace_node_playwright(tmp_path, monkeypatch):
    workdir = tmp_path / "app"
    workdir.mkdir()
    (workdir / "node_modules").mkdir()

    def fake_run(cmd, cwd, capture_output, text, timeout, env):
        assert cmd[0:2] == ["node", "-e"]
        assert cwd == str(workdir)
        assert cmd[-1] == str(workdir)
        assert env["PLAYWRIGHT_BROWSERS_PATH"] == "0"

        class Result:
            returncode = 0
            stdout = '{"ok": true, "status": 200, "bodyTextLength": 12}'
            stderr = ""

        return Result()

    monkeypatch.setattr(screenshot_tools.subprocess, "run", fake_run)

    diagnostics = diagnose_page_render("http://localhost:3001", str(workdir))

    assert diagnostics["ok"] is True
    assert diagnostics["status"] == 200

def test_capture_interactive_screenshot_loads_workspace_auth_env(tmp_path, monkeypatch):
    workdir = tmp_path / "app"
    workdir.mkdir()
    (workdir / "node_modules").mkdir()
    (workdir / ".env").write_text(
        "PLAYWRIGHT_TEST_EMAIL=founder@example.com\nPLAYWRIGHT_TEST_PASSWORD=secret-password\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "shots"

    def fake_run(cmd, cwd, capture_output, text, timeout, env):
        assert cmd[0:2] == ["node", "-e"]
        assert cwd == str(workdir)
        assert "bootstrapLogin" in cmd[2]
        assert "dismissConsentPopups" in cmd[2]
        assert "waitForAuthToClear" in cmd[2]
        assert "Target route" in cmd[2]
        assert "secret-password" not in cmd
        assert env["PLAYWRIGHT_TEST_EMAIL"] == "founder@example.com"
        assert env["PLAYWRIGHT_TEST_PASSWORD"] == "secret-password"

        class Result:
            returncode = 0
            stdout = '{"ok": true, "screenshots": [], "errors": [], "observations": ["Authenticated browser session using configured QA credentials."]}'
            stderr = ""

        return Result()

    monkeypatch.setattr(screenshot_tools.subprocess, "run", fake_run)

    result = capture_interactive_screenshot(
        '[{"action":"navigate","url":"/draft"},{"action":"screenshot","name":"draft"}]',
        "http://localhost:3001",
        str(output_dir),
        workdir=str(workdir),
    )

    assert "Authenticated browser session" in result

def test_capture_interactive_screenshot_rejects_off_candidate_route(tmp_path, monkeypatch):
    workdir = tmp_path / "app"
    workdir.mkdir()
    (workdir / "node_modules").mkdir()

    with patch.dict("os.environ", {"FOUNDERSCREW_QA_ALLOWED_PATHS": '["/draft", "/draftplan"]'}):
        result = capture_interactive_screenshot(
            '[{"action":"navigate","url":"/addteam"},{"action":"screenshot","name":"wrong"}]',
            "http://localhost:3001",
            str(tmp_path / "shots"),
            workdir=str(workdir),
        )

    assert "Unsupported QA navigation route: /addteam" in result

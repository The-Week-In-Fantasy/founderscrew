import os
import pytest
from pathlib import Path
from founderscrew.tools.screenshot_tools import generate_mock_browser_screenshot, compare_screenshots

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

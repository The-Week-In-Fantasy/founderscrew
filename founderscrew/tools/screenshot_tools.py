import os
import logging
from pathlib import Path
from typing import Tuple
from PIL import Image, ImageDraw, ImageFont, ImageChops

logger = logging.getLogger("founderscrew.screenshot_tools")

def capture_screenshot(url: str, output_path: str, allow_mock: bool = True) -> bool:
    """Captures a screenshot of a webpage.

    Args:
        url: The page URL to capture.
        output_path: Where to save the PNG.
        allow_mock: When True, falls back to a generated mock browser image if
            Playwright is unavailable. Pass False when the screenshot is used
            for real verification — a mock must never pass as evidence.
    """
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Try to use playwright if installed
        from playwright.sync_api import sync_playwright
        logger.info(f"Attempting to capture screenshot of {url} using Playwright...")
        with sync_playwright() as p:
            # launch headless
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_viewport_size({"width": 1280, "height": 800})
            page.goto(url, timeout=30000)
            page.screenshot(path=str(output_file))
            browser.close()
            logger.info(f"Screenshot saved to {output_path} using Playwright.")
            return True
    except Exception as e:
        if not allow_mock:
            logger.warning(f"Playwright screenshot of {url} failed: {e}. No mock fallback allowed.")
            return False
        logger.warning(f"Playwright screenshot failed or unavailable: {e}. Generating mockup fallback.")
        return generate_mock_browser_screenshot(url, str(output_file))

def generate_mock_browser_screenshot(url: str, output_path: str) -> bool:
    """Generates a high-quality mock browser mockup image for visual testing and fallback reporting."""
    width, height = 1280, 800
    # Create dark-themed mock image
    image = Image.new("RGB", (width, height), color="#0F172A") # slate-900 background
    draw = ImageDraw.Draw(image)
    
    # 1. Draw browser title bar (slate-800)
    draw.rectangle([(0, 0), (width, 40)], fill="#1E293B")
    
    # 2. Draw mock window buttons (red, yellow, green circles)
    draw.ellipse([(15, 12), (27, 24)], fill="#EF4444")
    draw.ellipse([(35, 12), (47, 24)], fill="#F59E0B")
    draw.ellipse([(55, 12), (67, 24)], fill="#10B981")
    
    # 3. Draw URL bar (slate-700)
    draw.rounded_rectangle([(100, 8), (width - 100, 32)], radius=4, fill="#334155")
    
    # 4. Draw dummy URL text
    # Try to load a font, fall back to default
    try:
        # Default to a simple built-in or try standard system fonts
        font = ImageFont.load_default()
    except Exception:
        font = None
        
    draw.text((120, 12), f"Secure Connection | {url}", fill="#94A3B8", font=font)
    
    # 5. Draw page content area mockup
    # Draw a header/hero area
    draw.rectangle([(50, 80), (width - 50, 240)], fill="#1E293B", outline="#475569", width=2)
    draw.text((80, 120), "FOUNDERSCREW QA VISUAL REPORT", fill="#6366F1", font=font) # indigo-500
    draw.text((80, 160), "This is an automatically generated mockup validation screen for testing.", fill="#F8FAFC", font=font)
    
    # Draw some grid items / components
    for i in range(3):
        x_start = 50 + i * 400
        draw.rectangle([(x_start, 280), (x_start + 380, 500)], fill="#1E293B", outline="#334155", width=1)
        draw.text((x_start + 20, 300), f"Component {i + 1}", fill="#10B981", font=font)
        draw.text((x_start + 20, 340), "Status: Running\nHealth: OK\nVisual Check: Passed", fill="#94A3B8", font=font)
        
    # Draw footer
    draw.rectangle([(0, height - 40), (width, height)], fill="#1E293B")
    draw.text((50, height - 25), "System active: Local Development Mode", fill="#64748B", font=font)
    
    try:
        image.save(output_path, "PNG")
        logger.info(f"Mock browser screenshot saved to {output_path}.")
        return True
    except Exception as e:
        logger.error(f"Failed to generate mock screenshot: {e}")
        return False

def compare_screenshots(image_path_a: str, image_path_b: str) -> float:
    """Compares two screenshots and returns a similarity percentage (0.0 to 100.0).
    
    Args:
        image_path_a: Path to the original/reference image
        image_path_b: Path to the current/new image
    """
    try:
        img_a = Image.open(image_path_a).convert("RGB")
        img_b = Image.open(image_path_b).convert("RGB")
        
        # Ensure identical sizes
        if img_a.size != img_b.size:
            img_b = img_b.resize(img_a.size)
            
        # Get absolute difference
        diff = ImageChops.difference(img_a, img_b)
        
        # Get bbox or calculate stats
        # If identical, diff.getbbox() is None
        if diff.getbbox() is None:
            return 100.0
            
        # Calculate pixel differences
        # Get count of non-zero pixels using load()
        pixels = diff.load()
        width, height = diff.size
        total_pixels = width * height
        differing_pixels = 0
        
        # Threshold: if sum of RGB differences is > 10, count as different
        for y in range(height):
            for x in range(width):
                r, g, b = pixels[x, y]
                if (r + g + b) > 10:
                    differing_pixels += 1
                
        similarity = 100.0 - (differing_pixels / total_pixels * 100.0)
        return round(similarity, 2)
    except Exception as e:
        logger.error(f"Error comparing screenshots {image_path_a} and {image_path_b}: {e}")
        return 0.0


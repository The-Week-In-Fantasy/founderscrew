import logging
import os
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from PIL import Image, ImageDraw, ImageFont, ImageChops, ImageStat

logger = logging.getLogger("founderscrew.screenshot_tools")

QA_EMAIL_ENV_KEYS = [
    "PLAYWRIGHT_TEST_EMAIL",
    "QA_LOGIN_EMAIL",
    "QA_EMAIL",
    "QA_USER_EMAIL",
    "TWIF_QA_EMAIL",
    "TWIF_TEST_EMAIL",
    "TWIF_LOGIN_EMAIL",
    "FOUNDER_EMAIL",
    "FOUNDERCREW_QA_EMAIL",
    "E2E_EMAIL",
    "PLAYWRIGHT_EMAIL",
    "TEST_USER_EMAIL",
]
QA_PASSWORD_ENV_KEYS = [
    "PLAYWRIGHT_TEST_PASSWORD",
    "QA_LOGIN_PASSWORD",
    "QA_PASSWORD",
    "QA_USER_PASSWORD",
    "TWIF_QA_PASSWORD",
    "TWIF_TEST_PASSWORD",
    "TWIF_LOGIN_PASSWORD",
    "FOUNDER_PASSWORD",
    "FOUNDERCREW_QA_PASSWORD",
    "E2E_PASSWORD",
    "PLAYWRIGHT_PASSWORD",
    "TEST_USER_PASSWORD",
]
QA_LOGIN_PATH_ENV_KEYS = ["QA_LOGIN_PATH", "TWIF_QA_LOGIN_PATH", "E2E_LOGIN_PATH"]
QA_ALLOWED_PATHS_ENV = "FOUNDERSCREW_QA_ALLOWED_PATHS"

def capture_screenshot(
    url: str,
    output_path: str,
    allow_mock: bool = False,
    workdir: Optional[str] = None,
) -> bool:
    """Captures a screenshot of a webpage.

    Args:
        url: The page URL to capture.
        output_path: Where to save the PNG.
        allow_mock: When True, falls back to a generated mock browser image if
            Playwright is unavailable. Pass False when the screenshot is used
            for real verification — a mock must never pass as evidence.
        workdir: Optional project workspace. When Python Playwright is not
            available, this is used to resolve a project-local Node Playwright
            installation such as node_modules/@playwright/test.
    """
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    errors = []
    if workdir:
        if _capture_with_node_playwright(url, output_file, Path(workdir), errors):
            return True
    else:
        if _capture_with_python_playwright(url, output_file, errors):
            return True
        if _capture_with_node_playwright(url, output_file, Path.cwd(), errors):
            return True

    error_text = "; ".join(errors) or "unknown error"
    if not allow_mock:
        logger.warning(f"Playwright screenshot of {url} failed: {error_text}. No mock fallback allowed.")
        return False
    logger.warning(f"Playwright screenshot failed or unavailable: {error_text}. Generating mockup fallback.")
    return generate_mock_browser_screenshot(url, str(output_file))


def analyze_screenshot(image_path: str) -> Dict[str, Any]:
    """Returns basic visual signal metrics for a screenshot.

    A successful browser screenshot can still be useless if the frontend
    rendered only a blank root/background. This analysis intentionally favors
    simple, deterministic image metrics so QA can reject low-information
    captures before asking a founder to approve them.
    """
    image_file = Path(image_path)
    try:
        with Image.open(image_file) as raw_image:
            image = raw_image.convert("RGB")
            width, height = image.size
            sample = image.copy()
            sample.thumbnail((320, 320))
            total_pixels = sample.width * sample.height
            colors = sample.getcolors(maxcolors=total_pixels) or []
            unique_color_count = len(colors)
            dominant_count = max((count for count, _color in colors), default=0)
            dominant_ratio = dominant_count / total_pixels if total_pixels else 1.0
            stat = ImageStat.Stat(sample)
            variance = sum(stat.var) / len(stat.var) if stat.var else 0.0

        reasons = []
        if dominant_ratio >= 0.995:
            reasons.append(f"{dominant_ratio:.1%} of sampled pixels are the same color")
        if unique_color_count <= 3:
            reasons.append(f"only {unique_color_count} unique sampled color(s)")
        if variance < 1.0:
            reasons.append(f"very low color variance ({variance:.2f})")

        return {
            "ok": True,
            "path": str(image_file),
            "width": width,
            "height": height,
            "unique_color_count": unique_color_count,
            "dominant_color_ratio": round(dominant_ratio, 4),
            "color_variance": round(variance, 2),
            "is_blank": bool(reasons),
            "reason": "; ".join(reasons),
        }
    except Exception as e:
        logger.warning(f"Could not analyze screenshot {image_path}: {e}")
        return {
            "ok": False,
            "path": str(image_file),
            "is_blank": True,
            "reason": f"screenshot analysis failed: {e}",
        }


def _playwright_env(workdir: Optional[Path] = None) -> Dict[str, str]:
    env = os.environ.copy()
    if workdir:
        env.update(_read_workspace_dotenv(workdir))
    env["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    env["FOUNDERSCREW_QA_EMAIL_KEYS"] = json.dumps(QA_EMAIL_ENV_KEYS)
    env["FOUNDERSCREW_QA_PASSWORD_KEYS"] = json.dumps(QA_PASSWORD_ENV_KEYS)
    env["FOUNDERSCREW_QA_LOGIN_PATH_KEYS"] = json.dumps(QA_LOGIN_PATH_ENV_KEYS)
    if _first_env_value(env, QA_EMAIL_ENV_KEYS) and _first_env_value(env, QA_PASSWORD_ENV_KEYS):
        logger.info("QA browser authentication credentials detected in workspace environment.")
    else:
        logger.info("QA browser authentication credentials not configured; proceeding unauthenticated.")
    return env


def _read_workspace_dotenv(workdir: Path) -> Dict[str, str]:
    env_file = workdir / ".env"
    if not env_file.exists():
        return {}
    values: Dict[str, str] = {}
    try:
        for raw_line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key] = value
    except Exception as e:
        logger.warning(f"Could not read workspace .env for QA browser environment: {e}")
    return values


def _first_env_value(env: Dict[str, str], keys: list[str]) -> str:
    for key in keys:
        value = env.get(key)
        if value:
            return value
    return ""


def _unsupported_interactive_route(actions: list[dict[str, Any]], base_url: str) -> str:
    raw_allowed = os.environ.get(QA_ALLOWED_PATHS_ENV, "")
    if not raw_allowed:
        return ""
    try:
        allowed = {str(path).strip() for path in json.loads(raw_allowed) if str(path).strip()}
    except Exception:
        return ""
    if not allowed:
        return ""
    allowed_normalized = {_normalize_route_path(path) for path in allowed}
    base_origin = _url_origin(base_url)
    for step in actions:
        if not isinstance(step, dict) or step.get("action") != "navigate":
            continue
        target = str(step.get("url") or "").strip()
        if not target:
            continue
        parsed = urlparse(target)
        if parsed.scheme and _url_origin(target) != base_origin:
            return target
        target_path = _normalize_route_path(parsed.path if parsed.scheme else target)
        if target_path not in allowed_normalized:
            return target_path
    return ""


def _normalize_route_path(path: str) -> str:
    normalized = (path or "/").split("?", 1)[0].split("#", 1)[0].strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized.rstrip("/") or "/"


def _url_origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


def diagnose_page_render(url: str, workdir: Optional[str] = None) -> Dict[str, Any]:
    """Collects browser diagnostics for a rendered page using local Playwright."""
    workspace = Path(workdir) if workdir else Path.cwd()
    local_node_modules = workspace / "node_modules"
    if not local_node_modules.exists():
        return {
            "ok": False,
            "url": url,
            "error": f"no local node_modules found at {workspace}",
        }

    script = r"""
const path = require('path');
const [url, workspacePath] = process.argv.slice(1);
const localNodeModules = path.resolve(workspacePath, 'node_modules') + path.sep;

function localRequire(packageName) {
  const resolved = require.resolve(packageName, { paths: [workspacePath] });
  const normalized = path.resolve(resolved);
  if (!normalized.startsWith(localNodeModules)) {
    throw new Error(`${packageName} resolved outside workspace: ${normalized}`);
  }
  return require(resolved);
}

let chromium;
try {
  chromium = localRequire('playwright').chromium;
} catch (firstError) {
  try {
    chromium = localRequire('@playwright/test').chromium;
  } catch (secondError) {
    throw new Error(`Unable to load workspace-local Playwright package: ${firstError.message}; ${secondError.message}`);
  }
}

const emailKeys = JSON.parse(process.env.FOUNDERSCREW_QA_EMAIL_KEYS || '[]');
const passwordKeys = JSON.parse(process.env.FOUNDERSCREW_QA_PASSWORD_KEYS || '[]');
const loginPathKeys = JSON.parse(process.env.FOUNDERSCREW_QA_LOGIN_PATH_KEYS || '[]');

function firstEnv(keys) {
  for (const key of keys) {
    if (process.env[key]) return process.env[key];
  }
  return '';
}

function withBase(baseUrl, pathOrUrl) {
  if (/^https?:\/\//i.test(pathOrUrl)) return pathOrUrl;
  const origin = new URL(baseUrl).origin;
  return origin + '/' + String(pathOrUrl || '/').replace(/^\//, '');
}

function pathLooksAuth(url) {
  try {
    const pathname = new URL(url).pathname.toLowerCase();
    return /(^|\/)(auth|login|signin|sign-in|sign_in)(\/|$)/.test(pathname);
  } catch {
    return false;
  }
}

async function hasVisibleAuthForm(page) {
  const emailVisible = await page.locator('input[type="email"], input[name="email"], input[autocomplete="email"]').first().isVisible({ timeout: 500 }).catch(() => false);
  const passwordVisible = await page.locator('input[type="password"], input[name="password"], input[autocomplete="current-password"]').first().isVisible({ timeout: 500 }).catch(() => false);
  return emailVisible && passwordVisible;
}

async function isLikelyAuthScreen(page) {
  if (await hasVisibleAuthForm(page)) return true;
  return pathLooksAuth(page.url());
}

async function waitForAuthToClear(page) {
  for (let i = 0; i < 12; i++) {
    if (!(await isLikelyAuthScreen(page))) return true;
    await page.waitForTimeout(500);
  }
  return false;
}

async function dismissConsentPopups(page, result) {
  const selectors = [
    'button:has-text("Accept All")',
    'button:has-text("Accept all")',
    'button:has-text("Accept Cookies")',
    'button:has-text("Accept cookies")',
    'button:has-text("I Accept")',
    'button:has-text("I agree")',
    'button:has-text("Agree")',
    '[data-testid="accept-cookies"]',
    '[data-testid="cookie-accept"]',
    '[aria-label="Accept cookies"]',
    '[aria-label="Accept All"]'
  ];
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    if (await locator.isVisible({ timeout: 1000 }).catch(() => false)) {
      await locator.click({ timeout: 3000 }).catch(() => {});
      await page.waitForTimeout(500);
      if (result) result.consent = { dismissed: true, selector };
      return true;
    }
  }
  if (result && !result.consent) result.consent = { dismissed: false };
  return false;
}

async function clickLoginControlNearPassword(page, passwordInput, result) {
  const match = await passwordInput.evaluate((input) => {
    const inputRect = input.getBoundingClientRect();
    const labelPattern = /^(login|log in|sign in)$/i;
    const visible = (el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const candidates = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], [role="button"]'))
      .filter(visible)
      .map((el) => {
        const rect = el.getBoundingClientRect();
        const text = String(el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
        const belowPassword = rect.top >= inputRect.bottom - 8;
        const verticalDistance = Math.abs(rect.top - inputRect.bottom);
        const horizontalDistance = Math.abs((rect.left + rect.width / 2) - (inputRect.left + inputRect.width / 2));
        return { el, rect, text, belowPassword, verticalDistance, horizontalDistance };
      })
      .filter((candidate) => labelPattern.test(candidate.text) && candidate.belowPassword && candidate.verticalDistance < 260);
    candidates.sort((a, b) => (a.verticalDistance + a.horizontalDistance / 10) - (b.verticalDistance + b.horizontalDistance / 10));
    const best = candidates[0];
    if (!best) return null;
    return {
      text: best.text,
      x: best.rect.left + best.rect.width / 2,
      y: best.rect.top + best.rect.height / 2,
    };
  });
  if (!match) return false;
  await page.mouse.click(match.x, match.y);
  if (result && result.auth) result.auth.submitMethod = `near-password ${match.text} button`;
  return true;
}

async function submitLoginCredentials(page, email, password, result) {
  const emailInput = page.locator('input[type="email"], input[name="email"], input[autocomplete="email"]').first();
  const passwordInput = page.locator('input[type="password"], input[name="password"], input[autocomplete="current-password"]').first();
  await emailInput.fill(email, { timeout: 10000 });
  await passwordInput.fill(password, { timeout: 10000 });

  // A consent modal can render after hydration and overlay the form, intercepting
  // pointer clicks on the submit button. Dismiss it again right before submitting.
  await dismissConsentPopups(page, result);

  // Primary: submit the form programmatically. requestSubmit() fires the same
  // submit handler as clicking the button, but is immune to any overlay still
  // intercepting pointer events (the root cause of QA login flakiness).
  const submittedViaForm = await passwordInput.evaluate((input) => {
    const form = input.form || input.closest('form');
    if (!form || typeof form.requestSubmit !== 'function') return false;
    form.requestSubmit();
    return true;
  }).catch(() => false);
  if (submittedViaForm) {
    if (result && result.auth) result.auth.submitMethod = 'form.requestSubmit()';
    return;
  }

  const credentialForm = page.locator('form').filter({ has: passwordInput }).first();
  const formSubmit = credentialForm.locator('button[type="submit"], input[type="submit"]').first();
  if (await formSubmit.isVisible({ timeout: 1000 }).catch(() => false)) {
    await formSubmit.click({ timeout: 10000 });
    if (result && result.auth) result.auth.submitMethod = 'credential form submit button';
    return;
  }

  const namedFormSubmit = credentialForm.locator('button').filter({ hasText: /^(Login|Log In|Sign In)$/i }).last();
  if (await namedFormSubmit.isVisible({ timeout: 1000 }).catch(() => false)) {
    await namedFormSubmit.click({ timeout: 10000 });
    if (result && result.auth) result.auth.submitMethod = 'credential form named button';
    return;
  }

  const pageSubmit = page.locator('button[type="submit"], input[type="submit"]').last();
  if (await pageSubmit.isVisible({ timeout: 1000 }).catch(() => false)) {
    await pageSubmit.click({ timeout: 10000 });
    if (result && result.auth) result.auth.submitMethod = 'page submit button';
    return;
  }

  if (await clickLoginControlNearPassword(page, passwordInput, result)) {
    return;
  }

  await passwordInput.press('Enter', { timeout: 5000 });
  if (result && result.auth) result.auth.submitMethod = 'password enter key';
}

async function bootstrapLogin(page, targetUrl, result) {
  const email = firstEnv(emailKeys);
  const password = firstEnv(passwordKeys);
  if (!email || !password) {
    result.auth = { attempted: false, reason: 'QA login credentials not configured' };
    return;
  }
  const loginUrl = withBase(targetUrl, firstEnv(loginPathKeys) || '/auth');
  result.auth = { attempted: true, loginUrl };
  try {
    await page.goto(loginUrl, { timeout: 30000, waitUntil: 'load' });
    // Wait for the auth form to hydrate (the page gates rendering on a beta-status
    // check) instead of a fixed delay, so the form and any consent modal are present.
    await page.locator('input[type="password"], input[name="password"], input[autocomplete="current-password"]').first().waitFor({ state: 'visible', timeout: 20000 }).catch(() => {});
    await dismissConsentPopups(page, result);
    await submitLoginCredentials(page, email, password, result);
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(1500);
    result.auth.ok = await waitForAuthToClear(page);
    result.auth.finalUrl = page.url();
    if (!result.auth.ok) {
      result.auth.error = 'Login form or auth route remained visible after submitting QA credentials.';
    }
  } catch (error) {
    result.auth.ok = false;
    result.auth.error = error && error.message ? error.message : String(error);
  }
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const result = { ok: true, url, consoleErrors: [], pageErrors: [], failedRequests: [] };
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    page.on('console', (msg) => {
      if (['error', 'warning'].includes(msg.type())) {
        result.consoleErrors.push(`${msg.type()}: ${msg.text()}`);
      }
    });
    page.on('pageerror', (error) => result.pageErrors.push(error.stack || error.message || String(error)));
    page.on('requestfailed', (request) => {
      const failure = request.failure();
      result.failedRequests.push(`${request.method()} ${request.url()} ${failure ? failure.errorText : ''}`.trim());
    });
    await bootstrapLogin(page, url, result);
    const response = await page.goto(url, { timeout: 30000, waitUntil: 'load' });
    await page.waitForTimeout(1000);
    await dismissConsentPopups(page, result);
    if (result.auth && result.auth.attempted && await isLikelyAuthScreen(page)) {
      result.ok = false;
      result.auth.ok = false;
      result.auth.finalUrl = page.url();
      result.auth.error = `Target route ${url} rendered an auth/login screen after QA login.`;
    }
    result.status = response ? response.status() : null;
    result.finalUrl = page.url();
    result.title = await page.title();
    result.bodyText = await page.locator('body').innerText({ timeout: 3000 }).catch(() => '');
    result.bodyTextLength = result.bodyText.length;
    result.bodyTextSample = result.bodyText.slice(0, 1000);
    result.htmlSample = (await page.content()).slice(0, 2000);
    result.consoleErrors = result.consoleErrors.slice(0, 20);
    result.pageErrors = result.pageErrors.slice(0, 10);
    result.failedRequests = result.failedRequests.slice(0, 20);
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(result));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""
    try:
        env = _playwright_env(workspace)
        result = subprocess.run(
            ["node", "-e", script, url, str(workspace)],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            env=env,
        )
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)}

    if result.returncode != 0:
        return {
            "ok": False,
            "url": url,
            "error": (result.stderr or result.stdout or "unknown browser diagnostic failure").strip()[:4000],
        }

    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as e:
        return {
            "ok": False,
            "url": url,
            "error": f"browser diagnostic output was not JSON: {e}",
            "raw_output": (result.stdout or "")[:2000],
        }


def _capture_with_python_playwright(url: str, output_file: Path, errors: list[str]) -> bool:
    try:
        # Try to use playwright if installed
        from playwright.sync_api import sync_playwright
        logger.info(f"Attempting to capture screenshot of {url} using Playwright...")
        with sync_playwright() as p:
            # launch headless
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_viewport_size({"width": 1280, "height": 800})
            # Use networkidle to wait for client-side hydration/rendering
            page.goto(url, timeout=30000, wait_until="networkidle")
            # Extra delay for React/Next.js apps that render after network settles
            page.wait_for_timeout(3000)
            page.screenshot(path=str(output_file))
            browser.close()
            logger.info(f"Screenshot saved to {output_file} using Python Playwright.")
            return True
    except Exception as e:
        errors.append(f"Python Playwright: {e}")
        return False


def _capture_with_node_playwright(
    url: str,
    output_file: Path,
    workdir: Path,
    errors: list[str],
) -> bool:
    """Capture using the target repo's Node Playwright package."""
    local_node_modules = workdir / "node_modules"
    if not local_node_modules.exists():
        errors.append(f"Node Playwright: no local node_modules found at {workdir}")
        return False

    script = r"""
const path = require('path');
const [url, outputPath, workspacePath] = process.argv.slice(1);
const localNodeModules = path.resolve(workspacePath, 'node_modules') + path.sep;

function localRequire(packageName) {
  const resolved = require.resolve(packageName, { paths: [workspacePath] });
  const normalized = path.resolve(resolved);
  if (!normalized.startsWith(localNodeModules)) {
    throw new Error(`${packageName} resolved outside workspace: ${normalized}`);
  }
  return require(resolved);
}

let chromium;
try {
  chromium = localRequire('playwright').chromium;
} catch (firstError) {
  try {
    chromium = localRequire('@playwright/test').chromium;
  } catch (secondError) {
    throw new Error(`Unable to load workspace-local Playwright package: ${firstError.message}; ${secondError.message}`);
  }
}

const emailKeys = JSON.parse(process.env.FOUNDERSCREW_QA_EMAIL_KEYS || '[]');
const passwordKeys = JSON.parse(process.env.FOUNDERSCREW_QA_PASSWORD_KEYS || '[]');
const loginPathKeys = JSON.parse(process.env.FOUNDERSCREW_QA_LOGIN_PATH_KEYS || '[]');

function firstEnv(keys) {
  for (const key of keys) {
    if (process.env[key]) return process.env[key];
  }
  return '';
}

function withBase(baseUrl, pathOrUrl) {
  if (/^https?:\/\//i.test(pathOrUrl)) return pathOrUrl;
  const origin = new URL(baseUrl).origin;
  return origin + '/' + String(pathOrUrl || '/').replace(/^\//, '');
}

function pathLooksAuth(url) {
  try {
    const pathname = new URL(url).pathname.toLowerCase();
    return /(^|\/)(auth|login|signin|sign-in|sign_in)(\/|$)/.test(pathname);
  } catch {
    return false;
  }
}

async function hasVisibleAuthForm(page) {
  const emailVisible = await page.locator('input[type="email"], input[name="email"], input[autocomplete="email"]').first().isVisible({ timeout: 500 }).catch(() => false);
  const passwordVisible = await page.locator('input[type="password"], input[name="password"], input[autocomplete="current-password"]').first().isVisible({ timeout: 500 }).catch(() => false);
  return emailVisible && passwordVisible;
}

async function isLikelyAuthScreen(page) {
  if (await hasVisibleAuthForm(page)) return true;
  return pathLooksAuth(page.url());
}

async function waitForAuthToClear(page) {
  for (let i = 0; i < 12; i++) {
    if (!(await isLikelyAuthScreen(page))) return true;
    await page.waitForTimeout(500);
  }
  return false;
}

async function dismissConsentPopups(page) {
  const selectors = [
    'button:has-text("Accept All")',
    'button:has-text("Accept all")',
    'button:has-text("Accept Cookies")',
    'button:has-text("Accept cookies")',
    'button:has-text("I Accept")',
    'button:has-text("I agree")',
    'button:has-text("Agree")',
    '[data-testid="accept-cookies"]',
    '[data-testid="cookie-accept"]',
    '[aria-label="Accept cookies"]',
    '[aria-label="Accept All"]'
  ];
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    if (await locator.isVisible({ timeout: 1000 }).catch(() => false)) {
      await locator.click({ timeout: 3000 }).catch(() => {});
      await page.waitForTimeout(500);
      return true;
    }
  }
  return false;
}

async function clickLoginControlNearPassword(page, passwordInput) {
  const match = await passwordInput.evaluate((input) => {
    const inputRect = input.getBoundingClientRect();
    const labelPattern = /^(login|log in|sign in)$/i;
    const visible = (el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const candidates = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], [role="button"]'))
      .filter(visible)
      .map((el) => {
        const rect = el.getBoundingClientRect();
        const text = String(el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
        const belowPassword = rect.top >= inputRect.bottom - 8;
        const verticalDistance = Math.abs(rect.top - inputRect.bottom);
        const horizontalDistance = Math.abs((rect.left + rect.width / 2) - (inputRect.left + inputRect.width / 2));
        return { el, rect, text, belowPassword, verticalDistance, horizontalDistance };
      })
      .filter((candidate) => labelPattern.test(candidate.text) && candidate.belowPassword && candidate.verticalDistance < 260);
    candidates.sort((a, b) => (a.verticalDistance + a.horizontalDistance / 10) - (b.verticalDistance + b.horizontalDistance / 10));
    const best = candidates[0];
    if (!best) return null;
    return {
      x: best.rect.left + best.rect.width / 2,
      y: best.rect.top + best.rect.height / 2,
    };
  });
  if (!match) return false;
  await page.mouse.click(match.x, match.y);
  return true;
}

async function submitLoginCredentials(page, email, password) {
  const emailInput = page.locator('input[type="email"], input[name="email"], input[autocomplete="email"]').first();
  const passwordInput = page.locator('input[type="password"], input[name="password"], input[autocomplete="current-password"]').first();
  await emailInput.fill(email, { timeout: 10000 });
  await passwordInput.fill(password, { timeout: 10000 });

  // A consent modal can render after hydration and overlay the form, intercepting
  // pointer clicks on the submit button. Dismiss it again right before submitting.
  await dismissConsentPopups(page);

  // Primary: submit the form programmatically. requestSubmit() fires the same
  // submit handler as clicking the button, but is immune to any overlay still
  // intercepting pointer events (the root cause of QA login flakiness).
  const submittedViaForm = await passwordInput.evaluate((input) => {
    const form = input.form || input.closest('form');
    if (!form || typeof form.requestSubmit !== 'function') return false;
    form.requestSubmit();
    return true;
  }).catch(() => false);
  if (submittedViaForm) {
    return;
  }

  const credentialForm = page.locator('form').filter({ has: passwordInput }).first();
  const formSubmit = credentialForm.locator('button[type="submit"], input[type="submit"]').first();
  if (await formSubmit.isVisible({ timeout: 1000 }).catch(() => false)) {
    await formSubmit.click({ timeout: 10000 });
    return;
  }

  const namedFormSubmit = credentialForm.locator('button').filter({ hasText: /^(Login|Log In|Sign In)$/i }).last();
  if (await namedFormSubmit.isVisible({ timeout: 1000 }).catch(() => false)) {
    await namedFormSubmit.click({ timeout: 10000 });
    return;
  }

  const pageSubmit = page.locator('button[type="submit"], input[type="submit"]').last();
  if (await pageSubmit.isVisible({ timeout: 1000 }).catch(() => false)) {
    await pageSubmit.click({ timeout: 10000 });
    return;
  }

  if (await clickLoginControlNearPassword(page, passwordInput)) {
    return;
  }

  await passwordInput.press('Enter', { timeout: 5000 });
}

async function bootstrapLogin(page, targetUrl) {
  const email = firstEnv(emailKeys);
  const password = firstEnv(passwordKeys);
  if (!email || !password) return;
  await page.goto(withBase(targetUrl, firstEnv(loginPathKeys) || '/auth'), { timeout: 30000, waitUntil: 'load' });
  // Wait for the auth form to hydrate (the page gates rendering on a beta-status
  // check) instead of a fixed delay, so the form and any consent modal are present.
  await page.locator('input[type="password"], input[name="password"], input[autocomplete="current-password"]').first().waitFor({ state: 'visible', timeout: 20000 }).catch(() => {});
  await dismissConsentPopups(page);
  await submitLoginCredentials(page, email, password);
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(1500);
  if (!(await waitForAuthToClear(page))) {
    throw new Error('Login form or auth route remained visible after submitting QA credentials.');
  }
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    await bootstrapLogin(page, url);
    // Use networkidle to wait for client-side hydration (React, Next.js, etc.)
    await page.goto(url, { timeout: 30000, waitUntil: 'networkidle' });
    // Extra delay for SPA frameworks that render after network settles
    await page.waitForTimeout(3000);
    await dismissConsentPopups(page);
    if ((firstEnv(emailKeys) || firstEnv(passwordKeys)) && await isLikelyAuthScreen(page)) {
      throw new Error(`Target route ${url} rendered an auth/login screen after QA login.`);
    }
    await page.screenshot({ path: outputPath });
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
"""
    try:
        logger.info(f"Attempting to capture screenshot of {url} using Node Playwright in {workdir}...")
        env = _playwright_env(workdir)
        result = subprocess.run(
            ["node", "-e", script, url, str(output_file), str(workdir)],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            env=env,
        )
    except Exception as e:
        errors.append(f"Node Playwright: {e}")
        return False

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "unknown error").strip()
        errors.append(f"Node Playwright: {stderr}")
        return False

    if output_file.exists() and output_file.stat().st_size > 0:
        logger.info(f"Screenshot saved to {output_file} using Node Playwright.")
        return True

    errors.append("Node Playwright: command completed without creating a screenshot")
    return False

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

def capture_interactive_screenshot(
    actions: str,
    base_url: str,
    output_dir: str,
    workdir: Optional[str] = None,
) -> str:
    """Executes a sequence of browser actions and captures screenshots at each
    screenshot step. This enables targeted QA verification by navigating to
    specific pages, clicking elements, hovering to trigger tooltips, etc.

    Args:
        actions: A JSON string containing a list of action objects. Each action
            has an "action" key and action-specific parameters. Supported actions:
            - {"action": "navigate", "url": "/some/path"} — navigate to a URL (relative to base_url or absolute)
            - {"action": "click", "selector": "css selector"} — click an element
            - {"action": "hover", "selector": "css selector"} — hover over an element
            - {"action": "type", "selector": "css selector", "text": "value"} — type into an input
            - {"action": "wait", "ms": 2000} — wait for a number of milliseconds
            - {"action": "wait_for", "selector": "css selector"} — wait for an element to appear
            - {"action": "scroll_to", "selector": "css selector"} — scroll an element into view
            - {"action": "screenshot", "name": "descriptive_name"} — capture a screenshot at this point
        base_url: The base URL of the running dev server (e.g. "http://localhost:3000").
        output_dir: Directory where screenshot images will be saved.
        workdir: Optional project workspace path for resolving Node Playwright.

    Returns:
        A JSON string with results: {"ok": true/false, "screenshots": [...], "errors": [...], "observations": [...]}
    """
    workspace = Path(workdir) if workdir else Path.cwd()
    local_node_modules = workspace / "node_modules"
    if not local_node_modules.exists():
        return json.dumps({
            "ok": False,
            "screenshots": [],
            "errors": [f"No local node_modules found at {workspace}"],
            "observations": [],
        })

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Parse and validate the actions JSON
    try:
        action_list = json.loads(actions) if isinstance(actions, str) else actions
        if not isinstance(action_list, list):
            return json.dumps({
                "ok": False,
                "screenshots": [],
                "errors": ["actions must be a JSON array of action objects"],
                "observations": [],
            })
    except json.JSONDecodeError as e:
        return json.dumps({
            "ok": False,
            "screenshots": [],
            "errors": [f"Invalid JSON in actions: {e}"],
            "observations": [],
        })

    unsupported_route = _unsupported_interactive_route(action_list, base_url)
    if unsupported_route:
        allowed = os.environ.get(QA_ALLOWED_PATHS_ENV, "")
        logger.warning(
            "Rejected QA browser navigation outside inferred route candidates: %s (allowed: %s)",
            unsupported_route,
            allowed,
        )
        return json.dumps({
            "ok": False,
            "screenshots": [],
            "errors": [
                f"Unsupported QA navigation route: {unsupported_route}. "
                f"Use one of the inferred target routes: {allowed}"
            ],
            "observations": [
                "The interactive browser tool refused to navigate outside the orchestrator-inferred QA route candidates."
            ],
        })

    # Serialize validated actions for the Node script
    actions_json = json.dumps(action_list)

    script = r"""
const path = require('path');
const fs = require('fs');
const [baseUrl, outputDir, workspacePath, actionsJson] = process.argv.slice(1);
const localNodeModules = path.resolve(workspacePath, 'node_modules') + path.sep;

function localRequire(packageName) {
  const resolved = require.resolve(packageName, { paths: [workspacePath] });
  const normalized = path.resolve(resolved);
  if (!normalized.startsWith(localNodeModules)) {
    throw new Error(`${packageName} resolved outside workspace: ${normalized}`);
  }
  return require(resolved);
}

let chromium;
try {
  chromium = localRequire('playwright').chromium;
} catch (firstError) {
  try {
    chromium = localRequire('@playwright/test').chromium;
  } catch (secondError) {
    throw new Error(`Unable to load workspace-local Playwright: ${firstError.message}; ${secondError.message}`);
  }
}

const emailKeys = JSON.parse(process.env.FOUNDERSCREW_QA_EMAIL_KEYS || '[]');
const passwordKeys = JSON.parse(process.env.FOUNDERSCREW_QA_PASSWORD_KEYS || '[]');
const loginPathKeys = JSON.parse(process.env.FOUNDERSCREW_QA_LOGIN_PATH_KEYS || '[]');

function firstEnv(keys) {
  for (const key of keys) {
    if (process.env[key]) return process.env[key];
  }
  return '';
}

function withBase(baseUrl, pathOrUrl) {
  if (/^https?:\/\//i.test(pathOrUrl)) return pathOrUrl;
  const origin = new URL(baseUrl).origin;
  return origin + '/' + String(pathOrUrl || '/').replace(/^\//, '');
}

function pathLooksAuth(url) {
  try {
    const pathname = new URL(url).pathname.toLowerCase();
    return /(^|\/)(auth|login|signin|sign-in|sign_in)(\/|$)/.test(pathname);
  } catch {
    return false;
  }
}

async function hasVisibleAuthForm(page) {
  const emailVisible = await page.locator('input[type="email"], input[name="email"], input[autocomplete="email"]').first().isVisible({ timeout: 500 }).catch(() => false);
  const passwordVisible = await page.locator('input[type="password"], input[name="password"], input[autocomplete="current-password"]').first().isVisible({ timeout: 500 }).catch(() => false);
  return emailVisible && passwordVisible;
}

async function isLikelyAuthScreen(page) {
  if (await hasVisibleAuthForm(page)) return true;
  return pathLooksAuth(page.url());
}

async function waitForAuthToClear(page) {
  for (let i = 0; i < 12; i++) {
    if (!(await isLikelyAuthScreen(page))) return true;
    await page.waitForTimeout(500);
  }
  return false;
}

async function dismissConsentPopups(page, result) {
  const selectors = [
    'button:has-text("Accept All")',
    'button:has-text("Accept all")',
    'button:has-text("Accept Cookies")',
    'button:has-text("Accept cookies")',
    'button:has-text("I Accept")',
    'button:has-text("I agree")',
    'button:has-text("Agree")',
    '[data-testid="accept-cookies"]',
    '[data-testid="cookie-accept"]',
    '[aria-label="Accept cookies"]',
    '[aria-label="Accept All"]'
  ];
  for (const selector of selectors) {
    const locator = page.locator(selector).first();
    if (await locator.isVisible({ timeout: 1000 }).catch(() => false)) {
      await locator.click({ timeout: 3000 }).catch(() => {});
      await page.waitForTimeout(500);
      result.consent = { dismissed: true, selector };
      result.observations.push(`Dismissed consent popup using ${selector}`);
      return true;
    }
  }
  if (!result.consent) result.consent = { dismissed: false };
  return false;
}

async function clickLoginControlNearPassword(page, passwordInput, result) {
  const match = await passwordInput.evaluate((input) => {
    const inputRect = input.getBoundingClientRect();
    const labelPattern = /^(login|log in|sign in)$/i;
    const visible = (el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const candidates = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], [role="button"]'))
      .filter(visible)
      .map((el) => {
        const rect = el.getBoundingClientRect();
        const text = String(el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
        const belowPassword = rect.top >= inputRect.bottom - 8;
        const verticalDistance = Math.abs(rect.top - inputRect.bottom);
        const horizontalDistance = Math.abs((rect.left + rect.width / 2) - (inputRect.left + inputRect.width / 2));
        return { el, rect, text, belowPassword, verticalDistance, horizontalDistance };
      })
      .filter((candidate) => labelPattern.test(candidate.text) && candidate.belowPassword && candidate.verticalDistance < 260);
    candidates.sort((a, b) => (a.verticalDistance + a.horizontalDistance / 10) - (b.verticalDistance + b.horizontalDistance / 10));
    const best = candidates[0];
    if (!best) return null;
    return {
      text: best.text,
      x: best.rect.left + best.rect.width / 2,
      y: best.rect.top + best.rect.height / 2,
    };
  });
  if (!match) return false;
  await page.mouse.click(match.x, match.y);
  if (result.auth) result.auth.submitMethod = `near-password ${match.text} button`;
  return true;
}

async function submitLoginCredentials(page, email, password, result) {
  const emailInput = page.locator('input[type="email"], input[name="email"], input[autocomplete="email"]').first();
  const passwordInput = page.locator('input[type="password"], input[name="password"], input[autocomplete="current-password"]').first();
  await emailInput.fill(email, { timeout: 10000 });
  await passwordInput.fill(password, { timeout: 10000 });

  // A consent modal can render after hydration and overlay the form, intercepting
  // pointer clicks on the submit button. Dismiss it again right before submitting.
  await dismissConsentPopups(page, result);

  // Primary: submit the form programmatically. requestSubmit() fires the same
  // submit handler as clicking the button, but is immune to any overlay still
  // intercepting pointer events (the root cause of QA login flakiness).
  const submittedViaForm = await passwordInput.evaluate((input) => {
    const form = input.form || input.closest('form');
    if (!form || typeof form.requestSubmit !== 'function') return false;
    form.requestSubmit();
    return true;
  }).catch(() => false);
  if (submittedViaForm) {
    if (result.auth) result.auth.submitMethod = 'form.requestSubmit()';
    return;
  }

  const credentialForm = page.locator('form').filter({ has: passwordInput }).first();
  const formSubmit = credentialForm.locator('button[type="submit"], input[type="submit"]').first();
  if (await formSubmit.isVisible({ timeout: 1000 }).catch(() => false)) {
    await formSubmit.click({ timeout: 10000 });
    if (result.auth) result.auth.submitMethod = 'credential form submit button';
    return;
  }

  const namedFormSubmit = credentialForm.locator('button').filter({ hasText: /^(Login|Log In|Sign In)$/i }).last();
  if (await namedFormSubmit.isVisible({ timeout: 1000 }).catch(() => false)) {
    await namedFormSubmit.click({ timeout: 10000 });
    if (result.auth) result.auth.submitMethod = 'credential form named button';
    return;
  }

  const pageSubmit = page.locator('button[type="submit"], input[type="submit"]').last();
  if (await pageSubmit.isVisible({ timeout: 1000 }).catch(() => false)) {
    await pageSubmit.click({ timeout: 10000 });
    if (result.auth) result.auth.submitMethod = 'page submit button';
    return;
  }

  if (await clickLoginControlNearPassword(page, passwordInput, result)) {
    return;
  }

  await passwordInput.press('Enter', { timeout: 5000 });
  if (result.auth) result.auth.submitMethod = 'password enter key';
}

async function bootstrapLogin(page, baseUrl, result) {
  const email = firstEnv(emailKeys);
  const password = firstEnv(passwordKeys);
  if (!email || !password) {
    result.auth = { attempted: false, reason: 'QA login credentials not configured' };
    return;
  }
  result.auth = { attempted: true, loginUrl: withBase(baseUrl, firstEnv(loginPathKeys) || '/auth') };
  try {
    await page.goto(result.auth.loginUrl, { timeout: 30000, waitUntil: 'load' });
    // Wait for the auth form to hydrate (the page gates rendering on a beta-status
    // check) instead of a fixed delay, so the form and any consent modal are present.
    await page.locator('input[type="password"], input[name="password"], input[autocomplete="current-password"]').first().waitFor({ state: 'visible', timeout: 20000 }).catch(() => {});
    await dismissConsentPopups(page, result);
    await submitLoginCredentials(page, email, password, result);
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(1500);
    result.auth.ok = await waitForAuthToClear(page);
    result.auth.finalUrl = page.url();
    if (result.auth.ok) {
      result.observations.push('Authenticated browser session using configured QA credentials.');
    } else {
      result.auth.error = 'Login form or auth route remained visible after submitting QA credentials.';
      result.errors.push(`QA login did not establish an authenticated session: ${result.auth.error}`);
    }
  } catch (error) {
    result.auth.ok = false;
    result.auth.error = error && error.message ? error.message : String(error);
    result.errors.push(`QA login failed: ${result.auth.error}`);
  }
}

(async () => {
  const actions = JSON.parse(actionsJson);
  const result = { ok: true, screenshots: [], errors: [], observations: [] };
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });

    await bootstrapLogin(page, baseUrl, result);

    for (let i = 0; i < actions.length; i++) {
      const step = actions[i];
      try {
        switch (step.action) {
          case 'navigate': {
            const target = withBase(baseUrl, step.url);
            await page.goto(target, { timeout: 30000, waitUntil: 'load' });
            await dismissConsentPopups(page, result);
            await page.waitForTimeout(1500);
            if (result.auth && result.auth.attempted && await isLikelyAuthScreen(page)) {
              result.ok = false;
              result.auth.ok = false;
              result.auth.finalUrl = page.url();
              const message = `Target route ${target} rendered an auth/login screen after QA login.`;
              result.auth.error = message;
              result.errors.push(message);
            }
            result.observations.push(`Navigated to ${target}`);
            break;
          }
          case 'click': {
            await page.locator(step.selector).first().click({ timeout: 10000 });
            await page.waitForTimeout(500);
            result.observations.push(`Clicked: ${step.selector}`);
            break;
          }
          case 'hover': {
            await page.locator(step.selector).first().hover({ timeout: 10000 });
            await page.waitForTimeout(800);
            result.observations.push(`Hovered: ${step.selector}`);
            break;
          }
          case 'type': {
            await page.locator(step.selector).first().fill(step.text || '', { timeout: 10000 });
            result.observations.push(`Typed into: ${step.selector}`);
            break;
          }
          case 'wait': {
            await page.waitForTimeout(step.ms || 1000);
            result.observations.push(`Waited ${step.ms || 1000}ms`);
            break;
          }
          case 'wait_for': {
            await page.locator(step.selector).first().waitFor({ state: 'visible', timeout: 15000 });
            result.observations.push(`Element appeared: ${step.selector}`);
            break;
          }
          case 'scroll_to': {
            await page.locator(step.selector).first().scrollIntoViewIfNeeded({ timeout: 10000 });
            await page.waitForTimeout(500);
            result.observations.push(`Scrolled to: ${step.selector}`);
            break;
          }
          case 'screenshot': {
            const name = (step.name || `step_${i}`).replace(/[^a-zA-Z0-9_-]/g, '_');
            const filePath = path.join(outputDir, `${name}.png`);
            await page.screenshot({ path: filePath, fullPage: false });
            result.screenshots.push(filePath);
            result.observations.push(`Screenshot captured: ${name}.png`);
            break;
          }
          default:
            result.errors.push(`Unknown action: ${step.action}`);
        }
      } catch (stepError) {
        const msg = `Step ${i} (${step.action}) failed: ${stepError.message || stepError}`;
        result.errors.push(msg);
        result.observations.push(msg);
        // Take an error screenshot so we can see what the page looked like
        try {
          const errPath = path.join(outputDir, `error_step_${i}.png`);
          await page.screenshot({ path: errPath, fullPage: false });
          result.screenshots.push(errPath);
        } catch (_) {}
      }
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(result));
})().catch((error) => {
  console.log(JSON.stringify({
    ok: false,
    screenshots: [],
    errors: [error && error.stack ? error.stack : String(error)],
    observations: []
  }));
  process.exit(1);
});
"""
    try:
        logger.info(f"Running interactive screenshot session with {len(action_list)} actions at {base_url}")
        env = _playwright_env(workspace)
        result = subprocess.run(
            ["node", "-e", script, base_url, str(out_dir), str(workspace), actions_json],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,  # longer timeout for multi-step interactions
            env=env,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "ok": False,
            "screenshots": [],
            "errors": ["Interactive screenshot session timed out after 120 seconds"],
            "observations": [],
        })
    except Exception as e:
        return json.dumps({
            "ok": False,
            "screenshots": [],
            "errors": [f"Failed to run interactive session: {e}"],
            "observations": [],
        })

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # Try to parse stdout even on failure — the script may have output partial results
        try:
            data = json.loads(result.stdout or "{}")
            return json.dumps(data)
        except Exception:
            return json.dumps({
                "ok": False,
                "screenshots": [],
                "errors": [stderr or "interactive session exited with non-zero code"],
                "observations": [],
            })

    try:
        return result.stdout.strip()
    except Exception:
        return json.dumps({
            "ok": False,
            "screenshots": [],
            "errors": ["No output from interactive session"],
            "observations": [],
        })


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

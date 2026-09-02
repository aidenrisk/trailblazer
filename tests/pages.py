"""Stand-in portal pages for the browser-level tests, as strings.

Kept in code rather than as .html files so the repository carries no synthetic
web pages. Each test writes what it needs to a temp directory at run time
(`write_pages`) and either opens it as a file URL or serves it over HTTP (see
tests/agents/browser/conftest.py), which pages that use localStorage need.

What each page stands in for:
- LOGIN: a sign-in form with a hidden look-alike submit button (Auth0's habit).
- OTP: a single code input; 123456 lands on DASHBOARD, anything else shows an error.
- OTP_DIGITS: six single-character boxes with no ids or labels, and an <input type=submit>.
- DASHBOARD: the authenticated landing page after a good code.
- SESSION: a page that, with ?seed=1, behaves like a portal that just logged in.
"""

from pathlib import Path

LOGIN = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Sign in</title></head>
<body>
<h1>Welcome to the Agent Portal</h1>
<form id="login-form" method="post" action="#">
  <label for="username">Email address</label>
  <input type="email" id="username" name="username" autocomplete="username" required>
  <label for="password">Password</label>
  <input type="password" id="password" name="password" autocomplete="current-password" required>
  <label>
    <input type="checkbox" id="remember" name="remember">
    Remember me on this device
  </label>
  <button type="submit" name="action" value="default" hidden>Sign in</button>
  <button type="submit" name="action" value="default" id="submit-visible">Sign in</button>
</form>
</body>
</html>
"""

OTP = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Verify your identity</title></head>
<body>
<h1>Check your email</h1>
<p>We sent a 6-digit code to a&bull;&bull;&bull;@aidenrisk.com.</p>
<form id="otp-form" method="post" action="#">
  <label for="code">Verification code</label>
  <input type="text" id="code" name="code" inputmode="numeric" autocomplete="one-time-code" required>
  <button type="button" id="resend">Resend code</button>
  <button type="submit" id="verify">Verify</button>
  <p id="error" hidden>That code is not right. Check the newest email and try again.</p>
</form>
<script>
  document.getElementById('otp-form').addEventListener('submit', function (e) {
    e.preventDefault();
    if (document.getElementById('code').value === '123456') {
      window.location.href = 'dashboard.html';
    } else {
      document.getElementById('error').hidden = false;
      document.getElementById('code').value = '';
    }
  });
</script>
</body>
</html>
"""

OTP_DIGITS = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Two-step verification</title></head>
<body>
<h1>Two-step verification</h1>
<form id="digits-form" method="post" action="#">
  <fieldset>
    <legend>Enter the 6-digit code we texted or emailed you</legend>
    <div class="digit-boxes">
      <input type="text" maxlength="1" inputmode="numeric">
      <input type="text" maxlength="1" inputmode="numeric">
      <input type="text" maxlength="1" inputmode="numeric">
      <input type="text" maxlength="1" inputmode="numeric">
      <input type="text" maxlength="1" inputmode="numeric">
      <input type="text" maxlength="1" inputmode="numeric">
    </div>
  </fieldset>
  <input type="submit" value="Continue">
</form>
</body>
</html>
"""

DASHBOARD = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Agent Portal</title></head>
<body>
<h1>Welcome back, Agent</h1>
<nav><a href="#" id="logout">Log out</a></nav>
<p>Start a quote</p>
</body>
</html>
"""

SESSION = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Session probe</title></head>
<body>
<h1>Session probe</h1>
<pre id="dump"></pre>
<script>
  if (new URLSearchParams(location.search).get('seed') === '1') {
    sessionStorage.setItem('msal.idtoken', 'tok-1');
    sessionStorage.setItem('draftId', 'd-9');
    localStorage.setItem('theme', 'dark');
    localStorage.setItem('inProgressApplication', 'app-42');
  }
  document.getElementById('dump').textContent = JSON.stringify({
    session: Object.fromEntries(Object.keys(sessionStorage).map(k => [k, sessionStorage.getItem(k)])),
    local: Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)])),
  });
</script>
</body>
</html>
"""

PAGES = {
    "login.html": LOGIN,
    "otp.html": OTP,
    "otp-digits.html": OTP_DIGITS,
    "dashboard.html": DASHBOARD,
    "session.html": SESSION,
}


def write_pages(directory: Path) -> Path:
    """Materialise every page into `directory` and return it."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, html in PAGES.items():
        (directory / name).write_text(html)
    return directory


def page_uri(directory: Path, name: str) -> str:
    return (directory / name).resolve().as_uri()

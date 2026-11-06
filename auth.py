"""Password gate for the dashboard.

A single shared password, not per-user accounts — this is an internal tool for a
small team, and the data it holds (creator emails, payment handles) needs a lock
on the door rather than an identity system.

Fails closed in both directions:
  - no DASHBOARD_PASSWORD set  -> nothing is reachable, with a page saying so
  - no SECRET_KEY set          -> same, because unsigned sessions are forgeable

Both are read from the environment. Neither is ever committed.
"""

import hmac
import os
from functools import wraps

from flask import redirect, render_template, request, session, url_for

SESSION_KEY = "authed"

# Reachable without logging in.
PUBLIC_ENDPOINTS = {
    "login",      # the login form itself
    "healthz",    # container liveness probe
    "api_ingest", # has its own bearer-token auth
    "static",
}


def password():
    return os.environ.get("DASHBOARD_PASSWORD") or ""


def is_configured():
    """Auth can only be enforced if both the password and a signing key exist."""
    return bool(password()) and bool(os.environ.get("SECRET_KEY"))


def check_password(submitted):
    """Constant-time comparison, so response timing doesn't leak the password."""
    expected = password()
    if not expected or not submitted:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def is_logged_in():
    return session.get(SESSION_KEY) is True


def install(app):
    """Gate every request except the public endpoints."""

    @app.before_request
    def _require_login():
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None

        # Misconfigured deployment: refuse to serve anything rather than
        # silently running wide open.
        if not is_configured():
            return render_template("locked.html"), 503

        if not is_logged_in():
            return redirect(url_for("login", next=request.path))

        return None


def login_view(app):
    """Register the login/logout routes on the app."""

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not is_configured():
            return render_template("locked.html"), 503

        if is_logged_in():
            return redirect(url_for("index"))

        error = None
        if request.method == "POST":
            if check_password(request.form.get("password", "")):
                session.clear()
                session[SESSION_KEY] = True
                session.permanent = True
                target = request.args.get("next") or url_for("index")
                # Only ever redirect within this site.
                if not target.startswith("/") or target.startswith("//"):
                    target = url_for("index")
                return redirect(target)
            error = "That password didn't work."

        return render_template("login.html", error=error), (401 if error else 200)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))


def configure(app):
    """Session cookie hardening.

    SESSION_COOKIE_SECURE is env-controlled rather than hardcoded on: a Secure
    cookie is never sent over plain HTTP, so switching it on while this
    subdomain has no working certificate would make login fail silently — the
    password would be accepted and the session immediately forgotten. Turn it on
    the moment HTTPS works.
    """
    from datetime import timedelta

    app.secret_key = os.environ.get("SECRET_KEY") or "unset-auth-disabled"
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    )

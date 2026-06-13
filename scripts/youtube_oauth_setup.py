#!/usr/bin/env python3.11
"""One-time YouTube OAuth setup — obtain a refresh token for private uploads.

Run this ONCE after you've:
  1. Created your brand channel.
  2. Created a Google Cloud project, enabled "YouTube Data API v3".
  3. Created an OAuth client of type "Desktop app" and copied its client ID/secret.

It opens a browser, you grant the upload scope to *your* channel, and it prints
the refresh token plus the exact .env lines to paste. Nothing is uploaded or
published — this only mints the credential the gated upload step later uses.

Usage:
    pip install google-auth-oauthlib
    python3.11 scripts/youtube_oauth_setup.py \
        --client-id YOUR_ID --client-secret YOUR_SECRET

You can also set YOUTUBE_OAUTH_CLIENT_ID / YOUTUBE_OAUTH_CLIENT_SECRET in the
environment and omit the flags.
"""

from __future__ import annotations

import argparse
import os
import sys

# Make the app package importable when run from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.youtube_upload import (  # noqa: E402
    YOUTUBE_UPLOAD_SCOPE,
    build_oauth_env_snippet,
    oauth_client_config,
)


def _resolve(value: str | None, env_key: str) -> str:
    return (value or os.getenv(env_key, "") or "").strip()


def run_consent_flow(client_id: str, client_secret: str) -> str:
    """Run the local-server OAuth consent flow and return the refresh token."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional lib
        raise SystemExit(
            "google-auth-oauthlib is not installed.\n"
            "Run: pip install google-auth-oauthlib"
        ) from exc

    flow = InstalledAppFlow.from_client_config(
        oauth_client_config(client_id, client_secret),
        scopes=[YOUTUBE_UPLOAD_SCOPE],
    )
    # access_type=offline + prompt=consent forces Google to return a refresh token.
    credentials = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )
    refresh_token = getattr(credentials, "refresh_token", None)
    if not refresh_token:
        raise SystemExit(
            "No refresh token returned. Revoke the app's access at "
            "https://myaccount.google.com/permissions and re-run with prompt=consent."
        )
    return refresh_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-time YouTube OAuth setup for private uploads.")
    parser.add_argument("--client-id", default=None, help="OAuth client ID (or YOUTUBE_OAUTH_CLIENT_ID env).")
    parser.add_argument("--client-secret", default=None, help="OAuth client secret (or YOUTUBE_OAUTH_CLIENT_SECRET env).")
    args = parser.parse_args(argv)

    client_id = _resolve(args.client_id, "YOUTUBE_OAUTH_CLIENT_ID")
    client_secret = _resolve(args.client_secret, "YOUTUBE_OAUTH_CLIENT_SECRET")

    missing = [name for name, val in (("client id", client_id), ("client secret", client_secret)) if not val]
    if missing:
        parser.error("Missing " + " and ".join(missing) + ". Pass --client-id/--client-secret or set the env vars.")

    print("Opening your browser for Google consent (upload scope only)…", file=sys.stderr)
    refresh_token = run_consent_flow(client_id, client_secret)

    print("\n✅ Success. Paste these lines into your .env:\n")
    print(build_oauth_env_snippet(client_id, client_secret, refresh_token))
    print(
        "\nThen the gated step `POST /publish/{id}/youtube/upload` can upload "
        "approved videos as PRIVATE. You still publish manually in Studio.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

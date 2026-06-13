"""YouTube Data API upload step — private, AI-disclosed, review-gated.

This uploads a finished video to the operator's own channel as **private** so
the operator does the final review and clicks "publish" in YouTube Studio. It
NEVER publishes public and NEVER bypasses review. Blind auto-publishing is the
fastest way to get a faceless channel terminated in 2026, so that final toggle
stays human.

The Google client libraries are imported lazily and treated as optional: if
they are not installed, or OAuth credentials are not configured, the function
returns a structured ``setup_required`` result instead of raising. This mirrors
the project's graceful-degradation pattern (external failures become structured
warnings, never crashes).

Setup the operator must do once (account-ownership steps only they can do):
  1. Create a Google Cloud project, enable the YouTube Data API v3.
  2. Create an OAuth client (Desktop), run the consent flow once to get a
     refresh token for their channel.
  3. Set YOUTUBE_OAUTH_CLIENT_ID / YOUTUBE_OAUTH_CLIENT_SECRET /
     YOUTUBE_OAUTH_REFRESH_TOKEN and ENABLE_YOUTUBE_UPLOADS=true.
  4. `pip install google-api-python-client google-auth google-auth-oauthlib`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.services.youtube import YouTubePayload

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
TOKEN_URI = "https://oauth2.googleapis.com/token"

_STUDIO_DISCLOSURE_REMINDER = (
    "Before publishing, in YouTube Studio mark this video as 'Altered or synthetic content' "
    "(Checks → Content disclosure). The Data API cannot set that flag programmatically."
)


@dataclass(frozen=True)
class UploadResult:
    status: str  # "uploaded" | "setup_required" | "blocked" | "error"
    video_id: str | None
    privacy_status: str
    detail: str
    studio_disclosure_reminder: str = _STUDIO_DISCLOSURE_REMINDER
    warnings: list[str] = field(default_factory=list)


def oauth_client_config(client_id: str, client_secret: str) -> dict:
    """Build the installed-app OAuth client config for the consent flow.

    Matches the shape google-auth-oauthlib expects from
    ``InstalledAppFlow.from_client_config``.
    """
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


def build_oauth_env_snippet(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Return the exact .env lines to paste after a successful consent flow."""
    return "\n".join(
        [
            "ENABLE_YOUTUBE_UPLOADS=true",
            f"YOUTUBE_OAUTH_CLIENT_ID={client_id}",
            f"YOUTUBE_OAUTH_CLIENT_SECRET={client_secret}",
            f"YOUTUBE_OAUTH_REFRESH_TOKEN={refresh_token}",
        ]
    )


def _missing_credentials(settings: Settings) -> list[str]:
    missing: list[str] = []
    if not (getattr(settings, "youtube_oauth_client_id", "") or "").strip():
        missing.append("YOUTUBE_OAUTH_CLIENT_ID")
    if not (getattr(settings, "youtube_oauth_client_secret", "") or "").strip():
        missing.append("YOUTUBE_OAUTH_CLIENT_SECRET")
    if not (getattr(settings, "youtube_oauth_refresh_token", "") or "").strip():
        missing.append("YOUTUBE_OAUTH_REFRESH_TOKEN")
    return missing


def upload_private_video(
    *,
    video_file_path: str | Path,
    payload: YouTubePayload,
    settings: Settings,
) -> UploadResult:
    """Upload a finished video as PRIVATE. Returns a structured result, never raises.

    Hard guarantees:
    * ``privacy_status`` is forced to ``private`` regardless of payload.
    * Disabled unless ``settings.enable_youtube_uploads`` is true.
    * Returns ``setup_required`` (not an exception) when libraries or credentials
      are missing, so callers degrade gracefully.
    """
    path = Path(video_file_path)

    if not settings.enable_youtube_uploads:
        return UploadResult(
            status="setup_required",
            video_id=None,
            privacy_status="private",
            detail="YouTube uploads are disabled. Set ENABLE_YOUTUBE_UPLOADS=true after completing OAuth setup.",
        )

    missing = _missing_credentials(settings)
    if missing:
        return UploadResult(
            status="setup_required",
            video_id=None,
            privacy_status="private",
            detail="Missing OAuth credentials: " + ", ".join(missing) + ".",
        )

    if not path.is_file() or path.stat().st_size == 0:
        return UploadResult(
            status="blocked",
            video_id=None,
            privacy_status="private",
            detail=f"Final video file not found or empty: {path}",
        )

    # Lazy, optional imports — keep google libs out of the hard dependency set.
    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaFileUpload  # type: ignore
    except ImportError:
        return UploadResult(
            status="setup_required",
            video_id=None,
            privacy_status="private",
            detail=(
                "Google client libraries not installed. Run: pip install "
                "google-api-python-client google-auth google-auth-oauthlib"
            ),
        )

    try:
        credentials = Credentials(
            token=None,
            refresh_token=settings.youtube_oauth_refresh_token,
            token_uri=TOKEN_URI,
            client_id=settings.youtube_oauth_client_id,
            client_secret=settings.youtube_oauth_client_secret,
            scopes=[YOUTUBE_UPLOAD_SCOPE],
        )
        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        body = {
            "snippet": {
                "title": payload.title[:100],
                "description": payload.description,
                "tags": list(payload.tags),
                "categoryId": payload.category_id,
            },
            "status": {
                "privacyStatus": "private",  # forced private — human publishes
                "selfDeclaredMadeForKids": bool(payload.made_for_kids),
            },
        }
        media = MediaFileUpload(str(path), mimetype="video/*", resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = request.execute()
        new_id = response.get("id")
        return UploadResult(
            status="uploaded",
            video_id=new_id,
            privacy_status="private",
            detail=f"Uploaded as private video {new_id}. Review in Studio, set AI disclosure, then publish.",
        )
    except Exception as exc:  # noqa: BLE001 — any API/network error becomes a structured result
        return UploadResult(
            status="error",
            video_id=None,
            privacy_status="private",
            detail=f"Upload failed: {exc}",
            warnings=[type(exc).__name__],
        )

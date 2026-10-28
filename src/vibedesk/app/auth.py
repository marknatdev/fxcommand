"""Owner authentication and authorization (spec §8.1, CCC-50).

Owner-only actions:
- Approve a Holdout request
- Extend an Idea Family's Trial Budget
- Update settings (PUT /api/settings)

These require the `owner` cookie, which is SameSite=Strict and set by the UI page.
The MCP server never possesses this cookie and gets HTTP 403 on owner actions.
"""

from __future__ import annotations

from fastapi import Cookie, HTTPException, Response

OWNER_COOKIE_NAME = "vibedesk_owner"
OWNER_COOKIE_VALUE = "1"


def set_owner_cookie(response: Response) -> None:
    """Set the owner cookie on a response with SameSite=Strict."""
    response.set_cookie(
        key=OWNER_COOKIE_NAME,
        value=OWNER_COOKIE_VALUE,
        samesite="strict",
        httponly=False,
        path="/",
        max_age=31_536_000,  # 1 year
    )


def require_owner(
    owner: str | None = Cookie(default=None, alias=OWNER_COOKIE_NAME),
) -> bool:
    """FastAPI dependency requiring the owner cookie (spec §8.1).

    Raises HTTP 403 Forbidden if the owner cookie is missing or invalid.
    """
    if owner != OWNER_COOKIE_VALUE:
        raise HTTPException(
            status_code=403,
            detail="Owner action required: owner cookie missing or invalid",
        )
    return True


__all__ = [
    "OWNER_COOKIE_NAME",
    "OWNER_COOKIE_VALUE",
    "require_owner",
    "set_owner_cookie",
]

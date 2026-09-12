import hashlib
from typing import Optional

from fastapi import Header, HTTPException, status


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_caller_token_hash(authorization: Optional[str] = Header(default=None)) -> str:
    """Extract the bearer token and return a stable hash identifying the caller.

    Only the hash is ever stored/compared, so a leaked database dump doesn't
    expose usable credentials.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return hash_token(token)

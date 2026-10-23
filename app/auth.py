"""Authentification par mot de passe unique partagé.

- UI : cookie de session signé (HMAC-SHA256), HttpOnly, SameSite=Strict.
- API : en-tête `Authorization: Bearer <mot de passe>`.
- Un middleware protège tout `/api/*` hors liste blanche (sécurisé par défaut).
- Sans mot de passe configuré : mode ouvert (comportement historique), signalé.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config

logger = logging.getLogger(__name__)

COOKIE_NAME = "gi_session"
PUBLIC_API_PATHS = frozenset({
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/status",
    "/api/health",
})
MAX_FAILURES = 5
FAILURE_WINDOW_S = 300
FAILURE_DELAY_S = 0.5


class LoginLimiter:
    """Limite les échecs d'authentification par IP (en mémoire)."""

    def __init__(self, max_failures: int = MAX_FAILURES, window_s: int = FAILURE_WINDOW_S):
        self.max_failures = max_failures
        self.window_s = window_s
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _prune(self, ip: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(ip, []) if now - t < self.window_s]
        if recent:
            self._failures[ip] = recent
        else:
            self._failures.pop(ip, None)
        return recent

    def retry_after(self, ip: str) -> int:
        """Secondes à attendre si l'IP est bloquée, sinon 0."""
        now = time.time()
        with self._lock:
            recent = self._prune(ip, now)
            if len(recent) < self.max_failures:
                return 0
            return max(1, int(self.window_s - (now - recent[0])))

    def fail(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            self._prune(ip, now)
            self._failures.setdefault(ip, []).append(now)
            if len(self._failures) > 10000:  # garde-fou mémoire
                for key in list(self._failures)[:5000]:
                    self._failures.pop(key, None)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


def _load_secret(explicit: str, secret_file: str) -> str:
    """Secret de signature : variable d'env, sinon fichier persistant, sinon éphémère."""
    if explicit:
        return explicit
    try:
        if os.path.isfile(secret_file):
            with open(secret_file, "r", encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
        value = secrets.token_hex(32)
        fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
        return value
    except OSError as exc:
        logger.warning("Secret de session non persistable (%s) : sessions perdues au redémarrage", exc)
        return secrets.token_hex(32)


class Auth:
    """État et logique d'authentification (mot de passe, jetons, limiteur)."""

    def __init__(self, password: str, secret: str, session_hours: int = 12,
                 trusted_proxy: bool = False):
        self.password = password
        self.secret = secret
        self.session_hours = session_hours
        self.trusted_proxy = trusted_proxy
        self.limiter = LoginLimiter()

    @classmethod
    def from_config(cls) -> "Auth":
        secret = _load_secret(config.APP_SECRET, config.SECRET_FILE) if config.APP_PASSWORD else ""
        return cls(config.APP_PASSWORD, secret, config.SESSION_HOURS, config.TRUSTED_PROXY)

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    # ------------------------------ jetons ------------------------------
    def _key(self) -> bytes:
        # Le mot de passe fait partie de la clé : le changer invalide les sessions.
        return hashlib.sha256((self.secret + "|" + self.password).encode("utf-8")).digest()

    def _sign(self, expiry: int) -> str:
        return hmac.new(self._key(), ("gi1:" + str(expiry)).encode("ascii"), hashlib.sha256).hexdigest()

    def make_token(self, now: float | None = None) -> str:
        expiry = int((now if now is not None else time.time()) + self.session_hours * 3600)
        return str(expiry) + "." + self._sign(expiry)

    def verify_token(self, token: str | None, now: float | None = None) -> bool:
        if not token or "." not in token:
            return False
        raw_expiry, _, sig = token.partition(".")
        if not raw_expiry.isdigit():
            return False
        expiry = int(raw_expiry)
        if expiry < (now if now is not None else time.time()):
            return False
        return hmac.compare_digest(sig, self._sign(expiry))

    def check_password(self, candidate: str) -> bool:
        a = hashlib.sha256(candidate.encode("utf-8")).digest()
        b = hashlib.sha256(self.password.encode("utf-8")).digest()
        return hmac.compare_digest(a, b)

    # ----------------------------- requêtes -----------------------------
    def client_ip(self, request: Request) -> str:
        if self.trusted_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[-1].strip() or "unknown"
        return request.client.host if request.client else "unknown"

    def is_secure(self, request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        return self.trusted_proxy and request.headers.get("x-forwarded-proto", "") == "https"

    def _bearer(self, request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if header[:7].lower() == "bearer ":
            return header[7:].strip()
        return None

    def is_authenticated(self, request: Request) -> bool:
        """True si la requête porte un cookie valide ou un Bearer correct."""
        if self.verify_token(request.cookies.get(COOKIE_NAME)):
            return True
        bearer = self._bearer(request)
        return bearer is not None and self.check_password(bearer)

    def allows(self, request: Request) -> bool:
        """Accès autorisé : mode ouvert ou requête authentifiée."""
        return (not self.enabled) or self.is_authenticated(request)

    def set_cookie(self, request: Request, response: Response) -> None:
        response.set_cookie(
            COOKIE_NAME, self.make_token(), max_age=self.session_hours * 3600,
            httponly=True, samesite="strict", secure=self.is_secure(request), path="/",
        )


class LoginBody(BaseModel):
    password: str = Field(max_length=256)


def install(app: FastAPI, auth: Auth) -> None:
    """Branche le middleware de protection et les routes /api/auth/*."""

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        path = request.url.path
        if not auth.enabled or not path.startswith("/api/") or path in PUBLIC_API_PATHS:
            return await call_next(request)
        ip = auth.client_ip(request)
        wait = auth.limiter.retry_after(ip)
        if wait:
            return JSONResponse({"detail": "Trop d'essais, réessayez plus tard"}, status_code=429,
                                headers={"Retry-After": str(wait)})
        if auth.is_authenticated(request):
            return await call_next(request)
        if auth._bearer(request) is not None:  # Bearer incorrect : compté comme échec
            auth.limiter.fail(ip)
        return JSONResponse({"detail": "Authentification requise"}, status_code=401)

    @app.post("/api/auth/login", status_code=204)
    async def login(body: LoginBody, request: Request):
        if not auth.enabled:
            return Response(status_code=204)
        ip = auth.client_ip(request)
        wait = auth.limiter.retry_after(ip)
        if wait:
            raise HTTPException(429, "Trop d'essais, réessayez plus tard", headers={"Retry-After": str(wait)})
        if not auth.check_password(body.password):
            auth.limiter.fail(ip)
            await asyncio.sleep(FAILURE_DELAY_S)
            raise HTTPException(401, "Mot de passe incorrect")
        auth.limiter.reset(ip)
        out = Response(status_code=204)
        auth.set_cookie(request, out)
        return out

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request):
        out = Response(status_code=204)
        out.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict",
                          secure=auth.is_secure(request))
        return out

    @app.get("/api/auth/status")
    def status(request: Request):
        return {"auth_required": auth.enabled, "authenticated": auth.allows(request)}

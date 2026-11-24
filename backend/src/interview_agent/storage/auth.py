"""Argon2 accounts and revocable SQL sessions; raw session tokens never enter SQL."""

import asyncio
import hashlib
import re
import secrets
from dataclasses import dataclass

from pwdlib import PasswordHash
from psycopg_pool import AsyncConnectionPool

from interview_agent.contracts.auth import SignIn, SignUp

from .errors import ApiError

TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


@dataclass(frozen=True)
class Principal:
    user_id: str
    session_id: str
    token_hash: str


def token_hash(token: str | None) -> str | None:
    return hashlib.sha256(token.encode()).hexdigest() if token is not None and TOKEN.fullmatch(token) else None


def public_user(row):
    return {"id": str(row["id"]), "email": row["email"], "name": row["name"]}


class AuthRepository:
    def __init__(self, pool: AsyncConnectionPool, ttl: int):
        self.pool, self.ttl = pool, ttl
        self.passwords = PasswordHash.recommended()
        self.dummy_hash = self.passwords.hash("synthetic-dummy-password-not-an-account")

    async def _new_session(self, conn, user_id):
        token = secrets.token_urlsafe(32)
        row = await (await conn.execute(
            "INSERT INTO auth.sessions(user_id,token_hash,expires_at) VALUES(%s,%s,clock_timestamp()+(%s*interval '1 second')) RETURNING id,user_id,expires_at",
            (user_id, token_hash(token), self.ttl))).fetchone()
        session = {"id": str(row["id"]), "userId": str(row["user_id"]), "expiresAt": row["expires_at"].isoformat()}
        return session, token

    async def register(self, value: SignUp):
        hashed = await asyncio.to_thread(self.passwords.hash, value.password.get_secret_value())
        async with self.pool.connection() as conn:
            async with conn.transaction():
                user = await (await conn.execute(
                    "INSERT INTO auth.users(email,name,password_hash) VALUES(%s,%s,%s) ON CONFLICT(email) DO NOTHING RETURNING id,email,name",
                    (value.email, value.name, hashed))).fetchone()
                if user is None:
                    raise ApiError("email_already_registered", 422)
                session, token = await self._new_session(conn, user["id"])
                return {"user": public_user(user), "session": session}, token

    async def login(self, value: SignIn):
        async with self.pool.connection() as conn:
            user = await (await conn.execute("SELECT id,email,name,password_hash FROM auth.users WHERE email=%s", (value.email,))).fetchone()
        try:
            verified, updated = await asyncio.to_thread(self.passwords.verify_and_update,
                value.password.get_secret_value(), user["password_hash"] if user else self.dummy_hash)
        except Exception:
            verified, updated = False, None
        if user is None or not verified:
            raise ApiError("invalid_credentials", 401)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                current = await (await conn.execute("SELECT id FROM auth.users WHERE id=%s AND password_hash=%s FOR UPDATE",
                                                    (user["id"], user["password_hash"]))).fetchone()
                if current is None:
                    raise ApiError("invalid_credentials", 401)
                if updated:
                    await conn.execute("UPDATE auth.users SET password_hash=%s WHERE id=%s", (updated, user["id"]))
                session, token = await self._new_session(conn, user["id"])
                return {"user": public_user(user), "session": session}, token

    async def resolve(self, token: str | None):
        digest = token_hash(token)
        if digest is None:
            return None
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT u.id,u.email,u.name,s.id AS session_id,s.expires_at FROM auth.sessions s JOIN auth.users u ON u.id=s.user_id WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()",
                (digest,))).fetchone()
        if row is None:
            return None
        return Principal(str(row["id"]), str(row["session_id"]), digest), {
            "user": public_user(row), "session": {"id": str(row["session_id"]), "userId": str(row["id"]), "expiresAt": row["expires_at"].isoformat()}}

    async def logout(self, token: str | None):
        digest = token_hash(token)
        if digest is not None:
            async with self.pool.connection() as conn:
                await conn.execute("UPDATE auth.sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s AND revoked_at IS NULL", (digest,))


async def require_session(conn, principal: Principal):
    # A fresh SQL check participates in writes; row-share conflicts with revocation.
    row = await (await conn.execute(
        "SELECT id FROM auth.sessions WHERE id=%s AND user_id=%s AND token_hash=%s AND revoked_at IS NULL AND expires_at>clock_timestamp() FOR SHARE",
        (principal.session_id, principal.user_id, principal.token_hash))).fetchone()
    if row is None:
        raise ApiError("authentication_required", 401)

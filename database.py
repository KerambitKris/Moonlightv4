import uuid
from typing import Optional

import asyncpg

from config import DATABASE_URL


_pool: Optional[asyncpg.Pool] = None


def _db_url() -> str:
    if DATABASE_URL.startswith("postgres://"):
        return "postgresql://" + DATABASE_URL[len("postgres://"):]
    return DATABASE_URL


async def init_db() -> None:
    global _pool
    _pool = await asyncpg.create_pool(_db_url(), min_size=1, max_size=5)

    async with _pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                balance NUMERIC(12,2) NOT NULL DEFAULT 0,
                payment_code TEXT UNIQUE NOT NULL,
                vpn_key TEXT,
                vpn_client_id TEXT,
                vpn_expires TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        # Миграции для уже существующей базы — данные пользователей сохраняются.
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vpn_sub_id TEXT"
        )
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vpn_traffic_gb INTEGER"
        )
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vpn_device_limit INTEGER DEFAULT 5"
        )
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vpn_tariff_days INTEGER"
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS donations (
                donation_id BIGINT PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                amount NUMERIC(12,2) NOT NULL,
                currency TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def _pool_required() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("База данных ещё не инициализирована")
    return _pool


async def get_user(uid: int):
    pool = _pool_required()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE telegram_id=$1",
            uid,
        )


async def register_user(uid: int, username: str = ""):
    pool = _pool_required()

    existing = await get_user(uid)
    if existing:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET username=$1 WHERE telegram_id=$2",
                username or "",
                uid,
            )
        return await get_user(uid)

    while True:
        payment_code = uuid.uuid4().hex[:8].upper()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO users
                        (telegram_id, username, balance, payment_code)
                    VALUES ($1, $2, 0, $3)
                    """,
                    uid,
                    username or "",
                    payment_code,
                )
            break
        except asyncpg.UniqueViolationError:
            continue

    return await get_user(uid)


async def add_balance(uid: int, amount: float):
    pool = _pool_required()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            UPDATE users
            SET balance = balance + $1
            WHERE telegram_id=$2
            RETURNING balance
            """,
            amount,
            uid,
        )


async def subtract_balance(uid: int, amount: float):
    pool = _pool_required()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE users
            SET balance = balance - $1
            WHERE telegram_id=$2 AND balance >= $1
            RETURNING balance
            """,
            amount,
            uid,
        )
        return row["balance"] if row else None


async def save_vpn(
    uid: int,
    *,
    subscription: str,
    client_id: str,
    sub_id: str,
    expires,
    traffic_gb,
    device_limit: int,
    tariff_days: int,
) -> None:
    pool = _pool_required()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE users
            SET vpn_key=$1,
                vpn_client_id=$2,
                vpn_sub_id=$3,
                vpn_expires=$4,
                vpn_traffic_gb=$5,
                vpn_device_limit=$6,
                vpn_tariff_days=$7
            WHERE telegram_id=$8
            """,
            subscription,
            client_id,
            sub_id,
            expires,
            traffic_gb,
            device_limit,
            tariff_days,
            uid,
        )


async def get_payment_codes():
    pool = _pool_required()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT telegram_id, payment_code FROM users"
        )


async def remember_old_donation(
    donation_id: int,
    amount: float,
    currency: str,
) -> None:
    pool = _pool_required()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO donations (donation_id, telegram_id, amount, currency)
            VALUES ($1, 0, $2, $3)
            ON CONFLICT (donation_id) DO NOTHING
            """,
            donation_id,
            amount,
            currency,
        )


async def credit_donation_once(
    donation_id: int,
    uid: int,
    amount: float,
    currency: str,
):
    pool = _pool_required()
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchval(
                """
                INSERT INTO donations
                    (donation_id, telegram_id, amount, currency)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (donation_id) DO NOTHING
                RETURNING donation_id
                """,
                donation_id,
                uid,
                amount,
                currency,
            )

            if inserted is None:
                return None

            return await conn.fetchval(
                """
                UPDATE users
                SET balance = balance + $1
                WHERE telegram_id=$2
                RETURNING balance
                """,
                amount,
                uid,
            )

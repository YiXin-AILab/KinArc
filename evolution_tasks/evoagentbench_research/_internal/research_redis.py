"""Each native candidate can access only its own public session namespace."""

import os
import re
import secrets
from typing import Any


def _admin() -> Any:
    import redis

    return redis.Redis(
        host="127.0.0.1",
        port=int(os.environ.get("EAB_PUBLIC_REDIS_PORT", "49154")),
        password=os.environ["EAB_RESEARCH_REDIS_PASSWORD"],
        socket_timeout=5,
    )


def _tag(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid public session identity")
    return value


def register_redis_scope(job_tag: str) -> str:
    tag = _tag(job_tag)
    token = secrets.token_urlsafe(32)
    with _admin() as client:
        client.execute_command(
            "ACL",
            "SETUSER",
            tag,
            "reset",
            "on",
            ">" + token,
            "~eab:" + tag + ":*",
            "&eab:" + tag + ":*",
            "+@all",
            "-@admin",
            "-@dangerous",
            "+info",
            "+eval",
            "+evalsha",
            "+script|load",
            "+client|setname",
            "+client|setinfo",
        )
    return token


def revoke_redis_scope(job_tag: str) -> None:
    with _admin() as client:
        client.execute_command("ACL", "DELUSER", _tag(job_tag))

import bcrypt
from starlette.concurrency import run_in_threadpool


def _hash_sync(raw_password: str) -> str:
    password_bytes = raw_password.encode("utf-8")
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


def _verify_sync(raw_password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(
        raw_password.encode("utf-8"),
        password_hash.encode("utf-8"),
    )


async def hash_password(raw_password: str) -> str:
    """평문 비밀번호를 bcrypt 해시 문자열로 변환

    bcrypt 는 의도적으로 느린 CPU 작업이라 이벤트 루프에서 직접 실행하면
    해싱이 끝날 때까지 다른 모든 요청이 멈춘다. 워커 스레드에서 실행해 루프를 막지 않는다.
    """
    return await run_in_threadpool(_hash_sync, raw_password)


async def verify_password(raw_password: str, password_hash: str) -> bool:
    """평문 비밀번호가 저장된 해시와 일치하는지 확인 (hash_password 와 같은 이유로 워커 스레드에서 실행)"""
    return await run_in_threadpool(_verify_sync, raw_password, password_hash)

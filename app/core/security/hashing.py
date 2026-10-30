import bcrypt
from starlette.concurrency import run_in_threadpool

# bcrypt 는 72바이트를 넘는 입력에 ValueError 를 던지므로 호출 전에 길이를 검사한다.
BCRYPT_MAX_PASSWORD_BYTES = 72

# 존재하지 않는 이메일로 로그인할 때도 이 해시로 검증을 한 번 수행해,
# 계정 유무에 따라 응답 시간이 달라지지 않게 한다. (어떤 비밀번호와도 일치하지 않도록 무작위 값으로 생성)
DUMMY_PASSWORD_HASH = "$2b$12$aifmIu4pf22QshqglHm0Y.E5FRIkCcvbXwy7qmjjRy/t3I4SlBvMK"


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

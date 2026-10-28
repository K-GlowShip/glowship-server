import bcrypt


def hash_password(raw_password: str) -> str:
    """평문 비밀번호를 bcrypt 해시 문자열로 변환"""
    password_bytes = raw_password.encode("utf-8")
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


def verify_password(raw_password: str, password_hash: str) -> bool:
    """평문 비밀번호가 저장된 해시와 일치하는지 확인"""
    return bcrypt.checkpw(
        raw_password.encode("utf-8"),
        password_hash.encode("utf-8"),
    )

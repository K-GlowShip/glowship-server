"""
합성데이터 적재 스크립트
  - manifest.json 의 sha256·행 수로 원본 파일 무결성을 먼저 확인합니다.
  - Base.metadata 의 FK 의존 순서(부모 → 자식)대로 COPY 로 적재합니다.
  - 전체를 하나의 트랜잭션으로 처리하고, 적재 후 행 수가 manifest 와 다르면 롤백합니다.
  - 원본 CSV·manifest 는 수정하지 않습니다.

사용법 (alembic upgrade head 이후):
  uv run python -m scripts.seed_synthetic_data <데이터 폴더> [--reset]
    --reset : 대상 테이블을 비운 뒤 다시 적재 (없으면 테이블에 데이터가 있을 때 중단)
"""

import argparse
import asyncio
import csv
import hashlib
import json
import sys
from pathlib import Path

import asyncpg
from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.config import get_settings
from app.core.model_registry import Base

AUX_DIR = "_p4_auxiliary"


class SeedError(Exception):
    pass


def csv_path(table: Table) -> str:
    """테이블 → manifest 상대 경로 (aux 스키마 테이블은 _p4_auxiliary 폴더)"""
    if table.schema == "aux":
        return f"{AUX_DIR}/{table.name}.csv"
    return f"{table.name}.csv"


def qualified_name(table: Table) -> str:
    return f"{table.schema}.{table.name}" if table.schema else table.name


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv_info(path: Path) -> tuple[list[str], int]:
    """헤더 컬럼과 데이터 행 수 (따옴표 안 줄바꿈도 한 행으로 셈)"""
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        return header, sum(1 for _ in reader)


def verify_files(data_dir: Path, tables: list[Table]) -> dict[str, dict]:
    """manifest 의 sha256·행 수와 실제 파일 비교. 적재 대상 테이블마다 파일 정보 반환"""
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.exists():
        raise SeedError(f"manifest.json 이 없습니다: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = {entry["path"]: entry for entry in manifest["files"]}
    print(f"[manifest] dataset={manifest['dataset']} run_id={manifest['run_id']}")

    errors = []
    for rel_path, entry in entries.items():
        path = data_dir / rel_path
        if not path.exists():
            errors.append(f"파일 없음: {rel_path}")
            continue
        if sha256_of(path) != entry["sha256"]:
            errors.append(f"sha256 불일치: {rel_path}")

    infos = {}
    for table in tables:
        rel_path = csv_path(table)
        entry = entries.get(rel_path)
        if entry is None:
            errors.append(
                f"manifest 에 {rel_path} 가 없습니다 ({qualified_name(table)})"
            )
            continue
        path = data_dir / rel_path
        if not path.exists():
            continue
        header, rows = read_csv_info(path)
        if rows != entry["rows"]:
            errors.append(
                f"행 수 불일치: {rel_path} 파일 {rows} / manifest {entry['rows']}"
            )
        unknown = set(header) - set(table.columns.keys())
        if unknown:
            errors.append(f"테이블에 없는 컬럼: {rel_path} {sorted(unknown)}")
        infos[qualified_name(table)] = {"path": path, "columns": header, "rows": rows}

    if errors:
        raise SeedError("원본 파일 검증 실패\n  - " + "\n  - ".join(errors))
    print(f"[verify] 파일 {len(entries)}개 sha256·행 수 일치")
    return infos


async def ensure_empty(conn: AsyncConnection, tables: list[Table], reset: bool) -> None:
    names = [qualified_name(t) for t in tables]
    if reset:
        await conn.execute(text(f"TRUNCATE {', '.join(names)}"))
        print(f"[reset] 테이블 {len(names)}개 비움")
        return

    filled = []
    for name in names:
        if await conn.scalar(text(f"SELECT EXISTS (SELECT 1 FROM {name})")):
            filled.append(name)
    if filled:
        raise SeedError(
            f"데이터가 이미 있는 테이블: {', '.join(filled)}\n"
            "  다시 적재하려면 --reset 옵션을 사용하세요."
        )


async def copy_tables(
    conn: AsyncConnection, tables: list[Table], infos: dict[str, dict]
) -> None:
    raw = await conn.get_raw_connection()
    driver = raw.driver_connection  # asyncpg Connection (같은 트랜잭션 안에서 COPY)

    for table in tables:
        info = infos[qualified_name(table)]
        if info["rows"] == 0:
            continue
        try:
            await driver.copy_to_table(
                table.name,
                source=info["path"],
                schema_name=table.schema or "public",
                columns=info["columns"],
                format="csv",
                header=True,
                encoding="utf-8",
            )
        except asyncpg.PostgresError as e:
            # 제약조건 위반 등: 어느 테이블에서 실패했는지 알리고 전체 트랜잭션을 롤백시킨다.
            raise SeedError(
                f"{qualified_name(table)} 적재 실패 (전체 롤백, DB 변경 없음)\n"
                f"  - {type(e).__name__}: {e}"
            ) from e
        print(f"[copy] {qualified_name(table)}: {info['rows']:,}행")


async def verify_counts(
    conn: AsyncConnection, tables: list[Table], infos: dict[str, dict]
) -> None:
    errors = []
    for table in tables:
        name = qualified_name(table)
        count = await conn.scalar(text(f"SELECT count(*) FROM {name}"))
        if count != infos[name]["rows"]:
            errors.append(f"{name}: DB {count} / manifest {infos[name]['rows']}")
    if errors:
        raise SeedError("적재 후 행 수 불일치 (롤백)\n  - " + "\n  - ".join(errors))
    print(f"[verify] 테이블 {len(tables)}개 행 수 manifest 와 일치")


async def seed(data_dir: Path, reset: bool) -> None:
    tables = list(Base.metadata.sorted_tables)  # FK 부모 → 자식 순서
    infos = verify_files(data_dir, tables)

    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.begin() as conn:  # 예외 발생 시 전체 롤백
            await ensure_empty(conn, tables, reset)
            await copy_tables(conn, tables, infos)
            await verify_counts(conn, tables, infos)
    finally:
        await engine.dispose()
    print("[done] 합성데이터 적재 완료")


def main() -> None:
    parser = argparse.ArgumentParser(description="GlowShip 합성데이터 적재")
    parser.add_argument("data_dir", type=Path, help="manifest.json 이 있는 데이터 폴더")
    parser.add_argument(
        "--reset", action="store_true", help="기존 데이터를 비우고 다시 적재"
    )
    args = parser.parse_args()

    try:
        asyncio.run(seed(args.data_dir, args.reset))
    except SeedError as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

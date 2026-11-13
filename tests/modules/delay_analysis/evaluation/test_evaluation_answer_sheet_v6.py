import hashlib
import os
import re
from pathlib import Path

import pytest
import test_evaluation_t9_t11 as T

"""
    시험 기대값 ↔ GLOW-50 최종정답표 v6 기계 대조 (GLOW-58, #129 리뷰 후속 6번)
      - 기준 정답 = GLOW-50_최종정답표_v6.md(SHA-256 아래 V6_SHA256). 평가 시험의 기대값(test_evaluation_t9_t11.ANSWERS)은
        사람이 정답표에서 옮겨 적은 값이라, 옮긴 값이 v6 와 같은지를 v6 1절(요약표)·2절(대응안별 저장값) 칸마다 견준다.
      - v6 는 저장소에 없다(공유 자료). 환경 변수 GLOWSHIP_ANSWER_SHEET_V6 에 파일 경로를 주면 돌고, 없으면 건너뛴다
        (CI 는 건너뜀). 경로를 줬는데 파일이 없거나 해시가 다르면 실패한다(다른 판을 기준으로 삼지 않게).
      - 판정: 같음 / 다름 / 시험에 없음. "시험에 없음"은 v6 에는 있지만 ANSWERS 가 단정하지 않는 칸이다
        (다른 시험 파일이 보거나 아직 견주지 않는 칸). 다름은 0 이어야 하고, 같음은 SAME_AT_LEAST 아래로 줄면 실패한다.
        시험이 None 을 단정한 칸(NULL·순위 없음)은 "같음/다름"으로 센다(없음이 아님).
      - v6 3절(서술형: H_end·회복 배송일·공여 객체 등)은 표가 아니라 여기서 견주지 않는다. 그 값은 3.1절 시험이 본다.
      - 대조가 "같음"만 내는 것이 아닌지: v6 표의 값을 한 칸씩 바꿔 넣으면 그 칸만 "다름"으로 잡는다(아래 음성 4건: 대응안 표 3,
        요약표 ④ 의 주문 여럿 귀속 1).
"""

ENV = "GLOWSHIP_ANSWER_SHEET_V6"
V6_SHA256 = "7bbc15fde2f739875099dc3c2fd4ca8bb19cc76bb7572e6d43428018c0ad2708"
CELLS = 290  # v6 1절·2절에서 견주는 칸 수(파일이 고정이라 늘 같다)
SAME_AT_LEAST = (
    170  # 11-12 develop f937959 기준 같음 170·없음 120. 단정을 더하면 늘고, 줄면 실패
)

OPTION = {"KEEP": T.KEEP, "PDA": T.PDA, "KR": T.KR, "OTHER": T.OTHER}
# v6 요약표의 ID → ANSWERS 키(GS-09 요약표 행은 변경 A 의 재계산 값)
ANSWER_KEY = {"GS-09": "GS-09A", "GS-10 새 실행": "GS-10"}
MISSING = object()  # 시험에 그 칸이 없음
SAME, DIFFERENT, ABSENT = "같음", "다름", "시험에 없음"
# 회복 집계 4칸: (이름, 대응안 값의 필드). 지연 증분 회복은 summary.delay_recovered 가 본다
AGGREGATE = (
    ("전체 회복", "fully_recovered_orders"),
    ("지연 증분 회복", None),
    ("미회복", "not_recovered_order_count"),
    ("일부 확보", "item_secured_blocked_orders"),
)


@pytest.fixture(scope="module")
def v6_tables():
    """(요약표 행, 대응안 행). 대응안 행은 비어 있는 ID 칸을 위 행의 ID 로 채운다."""
    path = os.environ.get(ENV)
    if not path:
        pytest.skip(f"{ENV} 가 없어 건너뜀(정답표 v6 는 저장소에 없음)")
    file = Path(path)
    assert file.is_file(), f"{ENV} 가 가리키는 파일이 없음: {path}"
    raw = file.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == V6_SHA256, "정답표 v6 가 아님(해시 다름)"
    lines = raw.decode("utf-8").splitlines()
    summary = _section(lines, "## 1. 요약표")
    options, current = [], None
    for cells in _section(lines, "## 2. 대응안별 저장값"):
        current = cells[0] or current
        options.append([current, *cells[1:]])
    return summary, options


def _section(lines: list[str], title: str) -> list[list[str]]:
    start = next(n for n, line in enumerate(lines) if line.startswith(title))
    rows = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith(("| GS", "| |")):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def _day(value: str) -> str:
    return f"2026-{value}" if re.fullmatch(r"\d\d-\d\d", value) else value


def _plain(value: str) -> str:
    return re.sub(r"\*", "", value).strip()


def _number(value: str) -> int | None:
    return None if value == "NULL" else int(value)


def _risks(cell: str) -> dict[str, str]:
    """④ 칸 → {주문: 귀속}. "A: O1·O2 DELAY_INDUCED" 처럼 주문 여럿이 한 귀속을 나누면 주문마다 같은 귀속."""
    risks = {}
    for orders, kind in re.findall(
        r"((?:O\d)(?:·O\d)*)\s+(PRE_EXISTING|DELAY_INDUCED)", cell
    ):
        for order in orders.split("·"):
            risks[order] = kind
    return risks


class Comparison:
    def __init__(self):
        self.cells: list[tuple[str, str, str]] = []  # (ID, 칸, 판정)

    def add(self, gid: str, cell: str, v6, test) -> None:
        if test is MISSING:
            verdict = ABSENT
        else:
            verdict = SAME if str(v6) == str(test) else DIFFERENT
        self.cells.append((gid, cell, verdict))

    def count(self, verdict: str) -> int:
        return sum(1 for cell in self.cells if cell[2] == verdict)

    def different(self) -> list[tuple[str, str]]:
        return [
            (gid, cell) for gid, cell, verdict in self.cells if verdict == DIFFERENT
        ]


def _compare_summary(found: Comparison, row: list[str]) -> None:
    """요약표 한 행: ① 소진일, ② raw·정수, ③ 품절일, ④ 위험 주문, ⑤ 지연 증분 회복, 추천."""
    gid = row[0]
    key = ANSWER_KEY.get(gid, gid)
    if key not in T.ANSWERS:
        found.add(gid, "시나리오", "요약표 행", MISSING)
        return
    summary = T.ANSWERS[key]["summary"]
    skus = summary.get("skus", {})
    names = list(skus)

    def sku_of(code: str | None, position: int):
        code = code or (names[position] if position < len(names) else None)
        return code, skus.get(code)

    for n, part in enumerate(p.strip() for p in row[2].replace("A:", "").split(";")):
        m = re.match(
            r"(?:([A-Z0-9]+) )?([\d.]+) / ([\d.]+) / ([\d.]+) → (\d+) · (\d+)", part
        )
        if not m:
            continue
        code, got = sku_of(m.group(1), n)
        raw = "/".join(f"{float(x):.4f}" for x in m.group(2, 3, 4))
        found.add(
            gid,
            f"② raw {code}",
            raw,
            MISSING
            if got is None
            else "/".join(
                (
                    got["raw_baseline_shortage"],
                    got["raw_total_shortage"],
                    got["raw_increment_shortage"],
                )
            ),
        )
        found.add(
            gid,
            f"② 정수 {code}",
            f"{m.group(5)}·{m.group(6)}",
            MISSING
            if got is None
            else f"{got['baseline_prepare_quantity']}·{got['increment_prepare_quantity']}",
        )
    for code, day in re.findall(r"(?:([A-Z0-9]+) )?(\d\d-\d\d)", row[1]):
        code, got = sku_of(code, 0)
        found.add(
            gid,
            f"① 소진일 {code}",
            _day(day),
            MISSING if got is None else got["depletion_date"],
        )
    for n, part in enumerate(row[3].split(";")):
        m = re.match(r"\s*(?:([A-Z0-9]+) )?(\d+) / (\d+) / (\d+)", part)
        if m:
            code, got = sku_of(m.group(1), n)
            found.add(
                gid,
                f"③ 품절일 {code}",
                "/".join(m.group(2, 3, 4)),
                MISSING
                if got is None
                else "/".join(str(got[k]) for k in T.SKU_KEYS[-3:]),
            )
    found.add(
        gid,
        "④ 위험 주문",
        sorted(_risks(row[4]).items()),
        sorted(summary["at_risk"].items()) if "at_risk" in summary else MISSING,
    )
    recovered = summary.get("delay_recovered", {})
    for name, value in re.findall(r"(KEEP|PDA|KR|OTHER) \**(\w+)\**", row[5]):
        option = OPTION[name]
        found.add(
            gid,
            f"⑤ {name}",
            _number(value),
            recovered.get(option, MISSING),
        )
    if row[6] and row[6] != "(3절)":
        found.add(gid, "추천", row[6], summary.get("recommended"))


def _compare_option(found: Comparison, row: list[str]) -> None:
    """대응안 한 행: 상태·사유, 공급량·공급일, 회복 집계 4칸, 확정 감소, 잔여 준비, 순위."""
    gid, name = row[0], row[1]
    answer = T.ANSWERS.get(ANSWER_KEY.get(gid, gid))
    if answer is None:
        found.add(gid, f"{name} 행", "2절 행", MISSING)
        return
    option = OPTION[name]
    fields = answer["summary"].get("options", {}).get(option, {})
    precheck = answer["t9"].get(option, {})

    def field(key: str):
        return fields.get(key, MISSING)

    status = re.sub(r"[*`]", "", row[2]).split()
    found.add(gid, f"{name} 상태", status[0], field("feasibility_status"))
    if len(status) > 1:
        found.add(
            gid,
            f"{name} 사유",
            status[1],
            fields.get("reason_code") or precheck.get("reason_code") or MISSING,
        )
    supply = re.match(r"\**(\w+)\** \((\d\d-\d\d)\)", row[3])
    if supply:
        found.add(
            gid, f"{name} 공급량", _number(supply.group(1)), field("supply_quantity")
        )
        found.add(
            gid,
            f"{name} 공급일",
            _day(supply.group(2)),
            precheck.get("supply_available_date", MISSING),
        )
    aggregate = _plain(row[4])
    values = (
        [None] * 4 if aggregate == "NULL" else [int(x) for x in aggregate.split(" / ")]
    )
    recovered = answer["summary"].get("delay_recovered", {})
    for (label, key), value in zip(AGGREGATE, values, strict=True):
        if key is None:
            test = recovered.get(option, MISSING)
        else:
            test = field(key)
        found.add(gid, f"{name} 집계 {label}", value, test)
    found.add(
        gid,
        f"{name} 확정 감소",
        _number(_plain(row[5])),
        field("reduction_committed_total"),
    )
    found.add(
        gid,
        f"{name} 잔여 준비",
        _number(_plain(row[6])),
        field("residual_prepare_total"),
    )
    rank = re.sub(r"[ ★*]", "", row[7])
    found.add(
        gid,
        f"{name} 순위",
        None if rank in ("—", "") else int(rank),
        answer["t11"][option]["rank"] if option in answer["t11"] else MISSING,
    )


def compare(summary: list[list[str]], options: list[list[str]]) -> Comparison:
    found = Comparison()
    for row in summary:
        _compare_summary(found, row)
    for row in options:
        _compare_option(found, row)
    return found


def test_expected_values_match_answer_sheet_v6(v6_tables):
    """ANSWERS 의 기대값이 v6 1절·2절과 다른 칸이 없다. 견주는 칸 수가 그대로이고 같음이 줄지 않았다."""
    found = compare(*v6_tables)

    assert found.different() == []
    assert len(found.cells) == CELLS
    assert found.count(SAME) >= SAME_AT_LEAST
    assert found.count(SAME) + found.count(ABSENT) == CELLS


# (ID, 대응안, 바꿀 열, 바꿔 넣을 값, 잡혀야 하는 칸). 열 4 = 회복 집계, 5 = 확정 감소
CHANGED_CELLS = [
    ("GS-03", "KR", 5, "99", "KR 확정 감소"),  # v6 100
    ("GS-07", "OTHER", 5, "0", "OTHER 확정 감소"),  # v6 NULL(판정 불가는 0 이 아님)
    ("GS-08", "KR", 4, "0 / 0 / 1 / 1", "KR 집계 일부 확보"),  # v6 0 / 0 / 1 / 0
]


@pytest.mark.parametrize(
    "gid, name, column, value, cell",
    CHANGED_CELLS,
    ids=[f"{case[0]}-{case[1]}" for case in CHANGED_CELLS],
)
def test_comparison_catches_a_changed_cell(v6_tables, gid, name, column, value, cell):
    """v6 표의 값 한 칸을 바꿔 넣으면 그 칸만 '다름'으로 잡는다(대조가 늘 '같음'을 내는 것이 아님)."""
    summary, options = v6_tables
    changed = [list(row) for row in options]
    (target,) = [row for row in changed if (row[0], row[1]) == (gid, name)]
    assert _plain(target[column]) != value
    target[column] = value

    assert compare(summary, changed).different() == [(gid, cell)]


def test_comparison_reads_shared_attribution_from_v6(v6_tables):
    """④ "O1·O2 DELAY_INDUCED"(GS-09)를 v6 글자에서 읽는다: 귀속을 PRE_EXISTING 으로 바꾸면 GS-09 ④ 만 '다름'.
    (주문 여럿 칸을 글자로 읽지 않고 값을 정해 두면 이 바꿈을 못 잡는다)"""
    summary, options = v6_tables
    changed = [list(row) for row in summary]
    (target,) = [row for row in changed if row[0] == "GS-09"]
    assert target[4] == "A: O1·O2 DELAY_INDUCED"
    target[4] = "A: O1·O2 PRE_EXISTING"

    assert compare(changed, options).different() == [("GS-09", "④ 위험 주문")]

"""Reader levels that change how reports and answers are written."""

from typing import Literal

Level = Literal["beginner", "junior", "senior"]
DEFAULT_LEVEL: Level = "beginner"

LABELS: dict[Level, str] = {
    "beginner": "입문자 / 바이브코더",
    "junior": "주니어 개발자",
    "senior": "숙련 개발자",
}

_INSTRUCTIONS: dict[Level, str] = {
    "beginner": (
        "독자는 코드를 거의 읽어 본 적 없는 입문자 또는 AI로 코딩하는 "
        "바이브코더입니다. 전문 용어는 처음 나올 때 한 문장으로 풀어 쓰고, "
        "'이 파일은 무엇을 하는가'를 일상적인 비유로 설명하세요. 문장은 짧게 "
        "쓰세요."
    ),
    "junior": (
        "독자는 기본 문법과 웹 개발 개념을 아는 주니어 개발자입니다. 흔한 "
        "용어는 그대로 쓰되 프로젝트 고유 개념은 설명하고, 요청이 어떤 "
        "함수와 모듈을 거쳐 흘러가는지 중심으로 설명하세요."
    ),
    "senior": (
        "독자는 숙련 개발자입니다. 기초 설명은 생략하고 아키텍처 결정, 모듈 "
        "경계, 확장 지점, 주의할 점을 간결하게 설명하세요."
    ),
}


def instruction(level: Level) -> str:
    """Returns the prompt instruction for a reader level."""
    return _INSTRUCTIONS[level]

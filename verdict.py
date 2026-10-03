"""Jev verdict (ADR 0007, ADR 0009): TypeSafe System One оценка Channel Subscription."""

import os
from pathlib import Path
from typing import Any

from typesafe_sdk import Choice, Noul


def load_typesafe_env(env_file: Path | None = None) -> None:
    """TYPESAFE_* из проектного .env → process env (SDK читает только os.environ).

    Уже установленные в окружении значения не перезатирает.
    """
    path = env_file or Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    from session import TYPESAFE_KEYS, _parse_env  # noqa: PLC0415

    for key, value in _parse_env(path.read_text(encoding="utf-8")).items():
        if key in TYPESAFE_KEYS:
            os.environ.setdefault(key, value)

NOUL_FAIL = 0.5

QUESTIONS: dict[str, Any] = {
    "voice": Choice(
        instructions=(
            "По текстам постов, не по тому, кто их написал. "
            "deep — есть непереносимая ставка: имя, цифра, наблюдение или причинный механизм; "
            "чужой пересланный разбор со ставкой тоже deep. "
            "news — сухая новостная сводка, не формульный дайджест «5 выводов». "
            "authorial_fun — юмор или наблюдения со ставкой, не польза для роста. "
            "noise — пустой поток без ставки."
        ),
        criteria={"deep": None, "news": None, "authorial_fun": None, "noise": None},
    ),
    "slop": Noul(instructions=(
        "Нейрослоп: тексты формульные и проходят тест «любой канал» — "
        "нет ставки, мало утверждений на много слов. "
        "Дайджест «5 выводов» без ставки — слоп. Сухая новость — не слоп. "
        "Не определяй, написал ли текст ИИ. "
        "Обрезанный пост не считай слопом только из-за обрезки."
    )),
    "clickbait": Noul(instructions=(
        "Кликбейт: пост обещает факт, интригу или выгоду, а видимый текст обещание не отдаёт. "
        "Острый заголовок с ответом в теле — не кликбейт. "
        "Если пост обрезан и видимая часть не доказывает неоплату — не кликбейт."
    )),
    "ads": Noul(instructions=(
        "Канал — рекламная витрина: большая часть постов это платная реклама или интеграции. "
        "Редкий спонсорский пост — не витрина."
    )),
    "course_seller": Noul(instructions=(
        "Канал продаёт курсы, схемы заработка или наставничество."
    )),
}


def has_text(posts: list[Any]) -> bool:
    """Истина, если хотя бы один пост содержит непустой текст/подпись."""
    return any(bool(_post_text(p).strip()) for p in posts)


def text_reasons(verdict: dict[str, Any] | None) -> list[str]:
    """Текстовые причины отписки по вердикту Jev в фиксированном порядке."""
    if not isinstance(verdict, dict) or "error" in verdict:
        return []
    required = ("slop", "clickbait", "ads", "course_seller")
    if any(k not in verdict for k in required):
        return []
    reasons = []
    if verdict["slop"] >= NOUL_FAIL:
        reasons.append("slop")
    if verdict["clickbait"] >= NOUL_FAIL:
        reasons.append("clickbait")
    if verdict["ads"] >= NOUL_FAIL:
        reasons.append("ads")
    if verdict["course_seller"] >= NOUL_FAIL:
        reasons.append("course_seller")
    if verdict.get("voice") == "noise":
        reasons.append("noise")
    return reasons


def _post_text(message: Any) -> str:
    # Str у Pyrogram срезает строки по utf-16 суррогатам и ломается на
    # битых текстах — режем plain str.
    raw = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    return str(raw)[:1000]


def evaluate(
    title: str,
    posts: list[Any],
    *,
    description: str = "",
    client_factory: Any = None,
    call: Any = None,
) -> dict[str, Any] | None:
    """Вердикт Jev по текстам подписки: Choice голоса + 4 Noul текстового провала."""
    try:
        state = {
            "channel": str(title),
            "description": str(description or "")[:500],
            "posts": [_post_text(m) for m in posts][:30],
        }
        if call is None:
            if client_factory is None:
                from typesafe_sdk import TypeSafeClient  # noqa: PLC0415

                load_typesafe_env()
                client_factory = TypeSafeClient
            with client_factory() as client:
                response = client.system_one(state=state, questions=QUESTIONS)
        else:
            response = call(state, QUESTIONS)
    except Exception as e:
        return {"error": str(e)}

    answers = response.answers
    voice = answers["voice"]
    return {
        "voice": voice.choice,
        "slop": float(answers["slop"].noul),
        "clickbait": float(answers["clickbait"].noul),
        "ads": float(answers["ads"].noul),
        "course_seller": float(answers["course_seller"].noul),
    }


VOICE_RU = {
    "deep": "глубокий",
    "news": "новости",
    "authorial_fun": "авторский юмор",
    "noise": "пустой поток",
}

REASON_RU = {
    "slop": "слоп",
    "clickbait": "кликбейт",
    "ads": "реклама",
    "course_seller": "курсы",
    "noise": "шум",
}

def verdict_label(voice: str) -> str:
    return VOICE_RU.get(voice, voice)


_JEV_PCT = (
    ("slop", "слоп"),
    ("clickbait", "клик"),
    ("ads", "рекл"),
    ("course_seller", "курсы"),
)


def jev_percents(verdict: dict[str, Any] | None) -> str:
    """Четыре Noul целыми процентами. Голос процентом не бывает."""
    if not isinstance(verdict, dict) or "error" in verdict:
        return ""
    parts = []
    for key, label in _JEV_PCT:
        raw = verdict.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            parts.append(f"{label} {round(float(raw) * 100)}%")
    return " ".join(parts)


def choice_suffix(
    verdict: dict[str, Any] | None,
    _metric_fail: bool | int | float,
    flags: list[str] | tuple[str, ...] = (),
) -> tuple[str, bool]:
    """Суффикс строки чекбокса. Скор в заголовке, Noul — процентами, не словами."""
    # ponytail: «метрики» не пишем — скор уже в строке. Noul — процентами, не словами.
    flags_note: list[str] = []
    if flags:
        if any("заброшен" in f for f in flags):
            flags_note.append("заброшен")
        if any("заброшен" not in f for f in flags):
            flags_note.append("накрутка")

    if verdict is None:
        voice_str = "нет текста"
    elif isinstance(verdict, dict) and "error" not in verdict and "voice" in verdict:
        voice_str = verdict_label(verdict["voice"])
    else:
        voice_str = "вердикт LLM недоступен"

    suffix = f" | {voice_str}"
    if flags_note:
        suffix = f"{suffix} | {', '.join(flags_note)}"
    pct = jev_percents(verdict)
    if pct:
        suffix = f"{suffix} | {pct}"

    is_authorial_fun = (
        isinstance(verdict, dict)
        and "error" not in verdict
        and verdict.get("voice") == "authorial_fun"
    )
    checked = not (is_authorial_fun and not text_reasons(verdict) and not flags)
    return suffix, checked

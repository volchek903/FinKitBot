from app.bot import is_authorized, is_explicitly_allowed_user
from app.config import Settings


def test_authorized_by_allowed_user_ids() -> None:
    settings = Settings(
        _env_file=None,
        telegram_chat_id="999",
        telegram_allowed_user_ids=(10, 20),
    )

    assert is_authorized(user_id=10, chat_id=111, settings=settings) is True
    assert is_authorized(user_id=999, chat_id=999, settings=settings) is False


def test_authorized_by_chat_id_when_allow_list_empty() -> None:
    settings = Settings(
        _env_file=None,
        telegram_chat_id="12345",
        telegram_allowed_user_ids=(),
    )

    assert is_authorized(user_id=12345, chat_id=1, settings=settings) is True
    assert is_authorized(user_id=1, chat_id=12345, settings=settings) is True
    assert is_authorized(user_id=1, chat_id=2, settings=settings) is False


def test_no_access_when_no_acl_configured() -> None:
    settings = Settings(
        _env_file=None,
        telegram_chat_id="",
        telegram_allowed_user_ids=(),
    )

    assert is_authorized(user_id=1, chat_id=1, settings=settings) is False


def test_explicit_allowed_user_is_strict() -> None:
    settings = Settings(
        _env_file=None,
        telegram_chat_id="12345",
        telegram_allowed_user_ids=(10,),
    )

    assert is_explicitly_allowed_user(user_id=10, settings=settings) is True
    assert is_explicitly_allowed_user(user_id=12345, settings=settings) is False


def test_explicit_allowed_user_requires_allow_list() -> None:
    settings = Settings(
        _env_file=None,
        telegram_chat_id="12345",
        telegram_allowed_user_ids=(),
    )

    assert is_explicitly_allowed_user(user_id=12345, settings=settings) is False

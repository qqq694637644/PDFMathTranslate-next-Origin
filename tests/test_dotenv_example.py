from __future__ import annotations

from dotenv import dotenv_values

EXPECTED_VALUES = {
    "PUBLIC_BASE_URL": "https://translate.example.com",
    "GPT_ACTION_API_KEY": "replace-with-a-long-random-key",
    "GPT_ACTION_QUEUE_DB": "./data/gptaction-queue.sqlite3",
    "GPT_ACTION_API_HOST": "127.0.0.1",
    "GPT_ACTION_API_PORT": "8000",
    "GPT_ACTION_CLAIM_TTL_SECONDS": "1800",
    "GPT_ACTION_MAX_REQUESTS": "2",
    "GPT_ACTION_MAX_SERIALIZED_RESPONSE_CHARS": "30000",
    "GPT_ACTION_MAX_SUBMIT_CHARS": "60000",
    "GPT_ACTION_MAX_OUTPUT_CHARS": "30000",
    "GPT_ACTION_PROTOCOL_VERSION": "1",
    "GPT_ACTION_POLL_INTERVAL_SECONDS": "0.5",
    "PDF2ZH_POOL_MAX_WORKERS": "8",
    "PDF2ZH_MAX_PAGES_PER_PART": "50",
}


def test_dotenv_example_contains_the_supported_personal_configuration() -> None:
    values = {
        key: value
        for key, value in dotenv_values(".env.example").items()
        if value is not None
    }

    assert values == EXPECTED_VALUES

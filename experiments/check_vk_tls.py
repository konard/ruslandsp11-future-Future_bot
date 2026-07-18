"""Проверка TLS-соединения с VK API.

Скрипт воспроизводит ошибку из issue #15
(``[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate``)
и показывает, какое хранилище сертификатов используется.

Запуск:

    python experiments/check_vk_tls.py

Ожидаемый результат при исправном TLS: VK отвечает ошибкой авторизации
(код 5) — значит соединение установлено и сертификат проверен. Если вместо
этого печатается сетевая ошибка с упоминанием сертификата, установите
``certifi`` или задайте ``FFBOT_VK_CA_BUNDLE``.
"""

from __future__ import annotations

import os

from future_bot.vk_client import VKAPIError, VKClient, find_ca_bundle


def main() -> int:
    print(f"Файл сертификатов: {find_ca_bundle(os.environ.get('FFBOT_VK_CA_BUNDLE')) or 'системное хранилище'}")

    client = VKClient(os.environ.get("VK_SERVICE_TOKEN", "invalid-token"))
    try:
        response = client.request("users.get", {"user_ids": 1})
    except VKAPIError as exc:
        print(f"Ответ VK API: {exc}")
        return 0 if exc.error_code is not None else 1

    print(f"Ответ VK API: {response}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

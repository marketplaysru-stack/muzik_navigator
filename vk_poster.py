import logging
import time

import requests

import config

log = logging.getLogger("vk")


def vk_call(token, method, params):
    """Один вызов VK API с ретраями и backoff. Возвращает response или None."""
    url = config.API_URL + method
    data = {**params, "access_token": token, "v": config.VK_API_VERSION}
    for attempt in range(1, config.RETRY_COUNT + 1):
        try:
            resp = requests.post(url, data=data, timeout=config.TIMEOUT)
            result = resp.json()
        except requests.RequestException as e:
            log.warning("Сетевая ошибка (попытка %d/%d): %s", attempt, config.RETRY_COUNT, e)
            time.sleep(config.BACKOFF_BASE ** attempt)
            continue

        if "error" in result:
            err = result["error"]
            log.error("VK ошибка %s: %s", err.get("error_code"), err.get("error_msg"))
            if err.get("error_code") == 9:  # flood control — ждём и повторяем
                time.sleep(config.BACKOFF_BASE ** attempt)
                continue
            return None
        return result.get("response")
    return None


def post_to_vk(token, group_id, text):
    """Публикует пост на стене группы. Возвращает post_id или None."""
    resp = vk_call(token, "wall.post", {
        "owner_id": -group_id,   # минус = от имени группы
        "from_group": 1,
        "message": text,
    })
    if resp:
        post_id = resp.get("post_id")
        log.info("Опубликован пост id=%s", post_id)
        return post_id
    return None
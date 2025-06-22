import json
import logging
from json import JSONDecodeError
import threading
import time
import requests

from hyperliquid.utils.constants import MAINNET_API_URL
from hyperliquid.utils.error import ClientError, ServerError
from hyperliquid.utils.types import Any

# ====== 限流参数 ======
MAX_CALLS_PER_SECOND = 3
RETRY_ON_429 = 5
RETRY_BASE_DELAY = 1.5  # 秒，指数退避基数
# =====================

class API:
    _lock = threading.Lock()
    _call_times = []  # 存储每次请求的时间戳

    def __init__(self, base_url=None):
        self.base_url = base_url or MAINNET_API_URL
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._logger = logging.getLogger(__name__)

    def post(self, url_path: str, payload: Any = None) -> Any:
        payload = payload or {}
        url = self.base_url + url_path
        retry = 0
        while True:
            self._throttle()
            response = self.session.post(url, json=payload)
            if response.status_code == 429:
                if retry < RETRY_ON_429:
                    delay = RETRY_BASE_DELAY * (2 ** retry)
                    self._logger.warning(f"API限流(429)，{delay:.1f}s后重试({retry+1}/{RETRY_ON_429})...")
                    time.sleep(delay)
                    retry += 1
                    continue
                else:
                    self._logger.error("API多次限流(429)，已放弃重试。")
            self._handle_exception(response)
            try:
                return response.json()
            except ValueError:
                return {"error": f"Could not parse JSON: {response.text}"}

    def _throttle(self):
        now = time.time()
        with API._lock:
            # 移除1秒前的请求
            API._call_times = [t for t in API._call_times if now - t < 1]
            if len(API._call_times) >= MAX_CALLS_PER_SECOND:
                wait_time = 1 - (now - API._call_times[0])
                if wait_time > 0:
                    self._logger.warning(f"API限流保护：{wait_time:.2f}s后继续请求...")
                    time.sleep(wait_time)
                # 再次清理
                now = time.time()
                API._call_times = [t for t in API._call_times if now - t < 1]
            API._call_times.append(time.time())

    def _handle_exception(self, response):
        status_code = response.status_code
        if status_code < 400:
            return
        if 400 <= status_code < 500:
            try:
                err = json.loads(response.text)
            except JSONDecodeError:
                raise ClientError(status_code, None, response.text, None, response.headers)
            if err is None:
                raise ClientError(status_code, None, response.text, None, response.headers)
            error_data = err.get("data")
            raise ClientError(status_code, err["code"], err["msg"], response.headers, error_data)
        raise ServerError(status_code, response.text)

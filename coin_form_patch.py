"""Version-guarded local coin form correction; no networking on import.

Only Video.pay_coin is replaced. Upstream site-packages remain untouched.
"""
import functools
import hashlib
import inspect
from importlib.metadata import version

EXPECTED_VERSIONS = {"bili-agent": "0.7.0", "bilibili-api-python": "17.4.2"}
EXPECTED_SOURCE_SHA256 = "8e9291f565a45267f46fcc1db55306b6a9db114799a20b373379c85997c0a401"
COIN_URL = "https://api.bilibili.com/x/web-interface/coin/add"


def apply():
    from bilibili_api.video import Video, API, Api, ArgsException

    original = Video.pay_coin
    if getattr(original, "_dot_coin_form_patch", False):
        return
    for package, expected in EXPECTED_VERSIONS.items():
        if version(package) != expected:
            raise RuntimeError(f"Coin patch version mismatch: {package}; review patch before use")
    if hashlib.sha256(inspect.getsource(original).encode()).hexdigest() != EXPECTED_SOURCE_SHA256:
        raise RuntimeError("Coin patch source mismatch; review patch before use")
    if API["operate"]["coin"]["url"] != COIN_URL:
        raise RuntimeError("Coin patch endpoint mismatch; review patch before use")

    @functools.wraps(original)
    async def pay_coin(self, num: int = 1, like: bool = False) -> dict:
        self.credential.raise_for_no_sessdata()
        self.credential.raise_for_no_bili_jct()
        if num not in (1, 2):
            raise ArgsException("投币数量只能是 1 ~ 2 个。")
        data = {
            "aid": await self._Video__get_aid(),
            "bvid": await self._Video__get_bvid(),
            "multiply": num,
            "select_like": int(like),
        }
        return await Api(**API["operate"]["coin"], credential=self.credential).update_data(**data).result

    pay_coin._dot_coin_form_patch = True
    Video.pay_coin = pay_coin

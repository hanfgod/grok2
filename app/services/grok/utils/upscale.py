"""
Video upscale utility.

POST /rest/media/video/upscale to request HD upscaling for a generated video.
Only used when a basic-pool token produces a 720p video (basic pool does not
natively support 720p, so we upscale after generation).
"""

import orjson
from typing import Optional

from curl_cffi.requests import AsyncSession

from app.core.logger import logger
from app.core.config import get_config
from app.core.exceptions import UpstreamException
from app.services.grok.utils.headers import apply_statsig, build_sso_cookie
from app.services.grok.utils.retry import retry_on_status

VIDEO_UPSCALE_API = "https://grok.com/rest/media/video/upscale"


async def upscale_video(token: str, video_id: str) -> Optional[str]:
    """
    Request HD upscale for a video and return the HD URL.

    Args:
        token: SSO token string (without ``sso=`` prefix).
        video_id: UUID of the generated video.

    Returns:
        HD video URL on success, or *None* if the request fails.
    """
    user_agent = get_config("security.user_agent")
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/",
        "User-Agent": user_agent,
    }
    apply_statsig(headers)
    headers["Cookie"] = build_sso_cookie(token)

    payload = orjson.dumps({"videoId": video_id})
    proxy = get_config("network.base_proxy_url")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    timeout = get_config("network.timeout")
    browser = get_config("security.browser")

    async def _do_request():
        async with AsyncSession(impersonate=browser) as session:
            response = await session.post(
                VIDEO_UPSCALE_API,
                headers=headers,
                data=payload,
                timeout=timeout,
                proxies=proxies,
            )
            if response.status_code != 200:
                body = ""
                try:
                    body = response.text
                except Exception:
                    pass
                raise UpstreamException(
                    message=f"Video upscale failed: {response.status_code}",
                    details={"status": response.status_code, "body": body},
                )
            return response

    try:
        resp = await retry_on_status(_do_request)
        data = resp.json()
        hd_url = data.get("hdMediaUrl") if isinstance(data, dict) else None
        if hd_url:
            logger.info(f"Video upscale completed: {hd_url}")
        else:
            logger.warning("Video upscale response missing hdMediaUrl")
        return hd_url
    except Exception as e:
        logger.warning(f"Video upscale failed: {e}")
        return None


__all__ = ["upscale_video"]

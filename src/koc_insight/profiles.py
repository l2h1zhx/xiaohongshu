"""主页只读入口：读取正常公开HTML目录，不下载媒体，不将首屏当全量。"""

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from .domain import SH, SourceError, month_due, stamp
from .urls import HOSTS, PROFILE, SHORT_HOSTS, public_dns, validate_url


def profile_id(url):
    from urllib.parse import urlsplit

    parts = urlsplit(validate_url(url, allow_profiles=True))
    match = PROFILE.fullmatch(parts.path)
    return match.group(1) if parts.hostname not in SHORT_HOSTS and match else None


def register_profile(service, url, month=None):
    uid = profile_id(url)
    if not uid:
        raise SourceError("NOT_PROFILE_URL")
    repo = service.repo
    context = {}
    if repo.meta("active_project"):
        from .project_hub import get_context

        context = get_context(repo)
    month = month or context.get("month") or datetime.now(SH).strftime("%Y-%m")
    month_due(month)
    entries = json.loads(repo.meta("profile_requests", "{}"))
    key = uid + ":" + month
    if key not in entries:
        entries[key] = {"uid": uid, "url": url, "month": month, "status": "PENDING"}
        repo.set_meta("profile_requests", json.dumps(entries, ensure_ascii=False))
    if context:
        scope = context.setdefault("authorized_account_ids", [])
        if uid not in scope:
            scope.append(uid)
            repo.set_meta(
                "project_context:" + context["project_id"], json.dumps(context)
            )
    return {"account_id": uid, "month": month, "status": entries[key]["status"]}


def parse_directory(html, uid):
    if re.search(r"300012|IP at risk|安全限制|访问频繁", html, re.I):
        raise SourceError("PROFILE_ACCESS_RESTRICTED", block=True)
    match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S
    )
    if not match:
        raise SourceError("PROFILE_DIRECTORY_UNAVAILABLE")
    try:
        # 仅替换JSON值位置的undefined，不执行外部JavaScript。
        raw = re.sub(r"(?<=[,:\[])\s*undefined(?=\s*[,}\]])", "null", match.group(1))
        data = json.loads(raw).get("user", {})
        cards = data.get("notes", [])
        if isinstance(cards, dict):
            cards = cards.get("_rawValue", [])
        cards = cards[0] if cards and isinstance(cards[0], list) else cards
        if not isinstance(cards, list):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SourceError("PROFILE_DIRECTORY_FORMAT_CHANGED") from None
    urls = []
    for card in cards[:200]:
        if not isinstance(card, dict):
            continue
        nid = card.get("id")
        author = (card.get("noteCard") or {}).get("user", {}).get("userId")
        if author and author != uid:
            continue
        if not isinstance(nid, str) or not re.fullmatch(r"[0-9a-f]{24}", nid):
            continue
        token = card.get("xsecToken")
        query = (
            urlencode({"xsec_token": token, "xsec_source": "pc_user"}) if token else ""
        )
        urls.append(
            "https://www.xiaohongshu.com/explore/"
            + nid
            + ("?" + query if query else "")
        )
    if not urls:
        # 空ID、空目录或登录页均不能证明账号没有笔记。
        raise SourceError("PROFILE_NO_READABLE_NOTE_IDS")
    return list(dict.fromkeys(urls))


def read_directory(url, client=None):
    from urllib.parse import urljoin, urlsplit

    uid = profile_id(url)
    if not uid:
        raise SourceError("NOT_PROFILE_URL")
    owns = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        for _ in range(6):
            url = validate_url(url, allow_profiles=True)
            if urlsplit(url).hostname not in HOSTS:
                raise SourceError("SSRF_BLOCKED")
            public_dns(urlsplit(url).hostname)
            with client.stream("GET", url, follow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    target = response.headers.get("location")
                    if not target:
                        raise SourceError("INVALID_REDIRECT")
                    url = urljoin(url, target)
                    continue
                if response.status_code in (403, 429):
                    raise SourceError("PROFILE_ACCESS_RESTRICTED", block=True)
                if response.status_code != 200:
                    raise SourceError("PROFILE_HTTP_ERROR")
                if "text/html" not in response.headers.get("content-type", ""):
                    raise SourceError("PROFILE_NOT_HTML")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 5 * 1024 * 1024:
                        raise SourceError("PROFILE_PAGE_TOO_LARGE")
                return parse_directory(body.decode("utf-8", errors="replace"), uid)
        raise SourceError("TOO_MANY_REDIRECTS")
    except httpx.RequestError:
        raise SourceError("PROFILE_NETWORK_ERROR") from None
    finally:
        if owns:
            client.close()


def collect_profiles(service, reader=read_directory, limit=1):
    entries = json.loads(service.repo.meta("profile_requests", "{}"))
    result = {"processed": 0, "discovered_links": 0, "blocked": 0}
    if service.repo.meta("profile_listing_circuit"):
        return {**result, "attention": "主页目录访问受限，等待正常访问恢复后明确补采"}
    for entry in entries.values():
        if entry["status"] != "PENDING" or result["processed"] >= limit:
            continue
        entry["checked_at"] = stamp(datetime.now(UTC))
        result["processed"] += 1
        try:
            links = reader(entry["url"])
            service.ingest(
                "\n".join(links), requirements="授权主页目录；目标月份" + entry["month"]
            )
            entry.update(status="PARTIAL", discovered_links=len(links), error=None)
            result["discovered_links"] += len(links)
        except SourceError as e:
            entry.update(status="BLOCKED", error=e.code)
            result["blocked"] += 1
            if e.block:
                service.repo.set_meta("profile_listing_circuit", e.code)
        # 首屏绝不作为全量证明；受限目录不自动反复重试。
        service.repo.set_meta(
            "profile_requests", json.dumps(entries, ensure_ascii=False)
        )
        if service.repo.meta("profile_listing_circuit"):
            break
    return result

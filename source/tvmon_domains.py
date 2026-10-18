"""Ordered site origins with stable catalogue and queue URL identities."""

from __future__ import annotations

from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit, urlunsplit


CANONICAL_ORIGIN = "https://tvmon1.com"
DEFAULT_DOMAINS = (CANONICAL_ORIGIN, "https://tvwiki52.net")
DOMAIN_GUIDE_URL = "https://tvwikiurl.net/"
TEXT_DOMAIN_GUIDE_URL = "https://xn--vg1b79zgsf1sj.com/"
_domains = DEFAULT_DOMAINS


def normalize_domain(value: str) -> str:
    """Accept a bare hostname or HTTPS origin, without a content address."""
    try:
        if not isinstance(value, str):
            raise ValueError()
        value = value.strip()
        if not value or re.search(r"\s|[\x00-\x1f\x7f]", value) or "?" in value or "#" in value:
            raise ValueError()
        parsed = urlsplit(value if "://" in value else "https://" + value)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.path not in ("", "/")):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if (len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                   for label in host.split("."))):
            raise ValueError()
        return "https://" + host
    except (ValueError, UnicodeError):
        raise ValueError("도메인은 https://tvwiki52.net처럼 주소 경로 없이 입력해 주세요.") from None


def _domain_key(origin: str) -> str:
    return origin.removeprefix("https://").removeprefix("www.")


def is_text_site_url(url: str) -> bool:
    """Recognize the numbered Toki hosts supported by the novel parsers."""
    try:
        parsed = urlsplit(url)
        origin = normalize_domain(f"{parsed.scheme}://{parsed.netloc}")
        return re.fullmatch(r"toki[0-9]+\.com", _domain_key(origin)) is not None
    except (ValueError, TypeError, AttributeError):
        return False


def next_numbered_domains(origin: str) -> tuple[str, ...]:
    """Return only the next three numbered hosts from the announced origin."""
    host = urlsplit(normalize_domain(origin)).hostname
    match = re.fullmatch(r"(.+?)([0-9]+)(\.[a-z][a-z0-9.-]*)", host)
    if not match:
        return ()
    prefix, number, suffix = match.groups()
    return tuple(normalize_domain(f"https://{prefix}{int(number) + offset}{suffix}") for offset in (1, 2, 3))


def get_domains() -> tuple[str, ...]:
    return _domains


def normalize_domains(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Validate settings without publishing them to current requests."""
    if not isinstance(values, (list, tuple)):
        raise ValueError("도메인 목록을 입력해 주세요.")
    normalized = tuple(normalize_domain(value) for value in values)
    if not normalized:
        raise ValueError("접속할 도메인을 하나 이상 등록해 주세요.")
    if len({_domain_key(value) for value in normalized}) != len(normalized):
        raise ValueError("이미 등록된 도메인입니다.")
    return normalized


def set_domains(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Validate the entire list before replacing its immutable snapshot."""
    global _domains
    _domains = normalize_domains(values)
    return _domains


def is_site_url(url: str) -> bool:
    """Recognize configured origins and the legacy canonical origin."""
    try:
        if not isinstance(url, str) or re.search(r"[\x00-\x20\x7f]", url.strip()):
            return False
        parsed = urlsplit(url.strip())
        origin = normalize_domain(f"{parsed.scheme}://{parsed.netloc}")
        allowed = {_domain_key(value) for value in get_domains()}
        allowed.add(_domain_key(CANONICAL_ORIGIN))
        return _domain_key(origin) in allowed
    except ValueError:
        return False


def _replace_origin(url: str, origin: str) -> str:
    parsed = urlsplit(url.strip())
    return urlunsplit(("https", urlsplit(origin).netloc, parsed.path, parsed.query, parsed.fragment))


def canonical_site_url(value: str, base_url: str = CANONICAL_ORIGIN + "/") -> str:
    """Resolve a site URL while preserving its path, query, and fragment."""
    try:
        if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f]", value.strip()):
            raise ValueError()
        resolved = urljoin(base_url, value.strip())
        if not is_site_url(resolved):
            raise ValueError()
        return _replace_origin(resolved, CANONICAL_ORIGIN)
    except (TypeError, ValueError):
        raise ValueError("등록된 사이트의 올바른 주소를 입력해 주세요.") from None


def domain_requests(url: str, referer: str) -> list[tuple[str, str]]:
    """Build candidates in saved order; external media URLs stay unchanged."""
    if not is_site_url(url):
        return [(url, referer)]
    rewrite_referer = is_site_url(referer)
    return [(_replace_origin(url, origin), _replace_origin(referer, origin) if rewrite_referer else referer)
            for origin in get_domains()]


class _DomainPage(HTMLParser):
    """Read the labelled address, links, and declarative page redirects."""

    def __init__(self, body: bytes):
        super().__init__(convert_charrefs=True)
        self.addresses = []
        self.links = []
        self.refreshes = []
        self.title = ""
        self.hud_addresses = []
        self._hud_depth = 0
        self._hud_text = []
        self._capture = ""
        self._text = []
        self._address_label = False
        self.feed(body.decode("utf-8-sig", errors="replace"))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self._hud_depth:
            if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
                self._hud_depth += 1
        elif "hud-url" in (attrs.get("class") or "").split():
            self._hud_depth, self._hud_text = 1, []
        if tag in ("dt", "dd", "title"):
            self._capture, self._text = tag, []
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "meta" and (attrs.get("http-equiv") or "").strip().lower() == "refresh":
            self.refreshes.append(attrs.get("content") or "")

    def handle_data(self, data):
        if self._hud_depth:
            self._hud_text.append(data)
        if self._capture:
            self._text.append(data)

    def handle_endtag(self, tag):
        if self._hud_depth:
            self._hud_depth -= 1
            if not self._hud_depth:
                self.hud_addresses.append("".join(self._hud_text).strip())
        if tag != self._capture:
            return
        text = " ".join("".join(self._text).split())
        if tag == "dt":
            self._address_label = text == "현재 안내 주소"
        elif tag == "dd":
            if self._address_label:
                self.addresses.append(text)
            self._address_label = False
        elif tag == "title" and not self.title:
            self.title = text
        self._capture = ""


def _lookup_url(value: str, base_url: str) -> str:
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f]", value.strip()):
        raise ValueError("주소 안내에서 올바르지 않은 연결 주소를 받았습니다.")
    parsed = urlsplit(urljoin(base_url, value.strip()))
    origin = normalize_domain(f"{parsed.scheme}://{parsed.netloc}")
    return origin + (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")


def normalize_text_guide_url(value: str) -> str:
    """Accept an HTTPS guide page, including its path and query."""
    try:
        if not isinstance(value, str) or not value.strip():
            raise ValueError()
        value = value.strip()
        if not urlsplit(value).scheme:
            value = "https://" + value
        return _lookup_url(value, value)
    except (ValueError, UnicodeError):
        raise ValueError("주소 안내 페이지의 올바른 HTTPS 주소를 입력해 주세요.") from None


def resolve_current_domain(fetcher, cancel, *, check_cancel, on_failure=None, guide_url=DOMAIN_GUIDE_URL) -> str:
    """Follow the labelled guide address and at most five landing pages.

    The injected fetcher bounds HTTP redirects and response size, returning
    (body, final_url) just like the downloader's single-origin fetch function.
    Cancellation and transport errors propagate without changing settings.
    """
    check_cancel(cancel)
    guide_url = normalize_text_guide_url(guide_url)
    body, final_url = fetcher(guide_url, cancel, guide_url,
                             limit=2 * 1024 * 1024, retry=False)
    check_cancel(cancel)
    final_url = _lookup_url(final_url, guide_url)
    if _domain_key(normalize_domain("https://" + urlsplit(final_url).netloc)) != _domain_key(normalize_domain("https://" + urlsplit(guide_url).netloc)):
        raise ValueError("주소 안내 페이지가 다른 사이트로 이동했습니다.")
    guide = _DomainPage(body)
    addresses = {normalize_domain(value) for value in guide.addresses}
    if len(addresses) != 1:
        raise ValueError("안내 페이지에서 현재 안내 주소를 확인하지 못했습니다.")
    address = addresses.pop()
    matching_link = False
    for link in guide.links:
        try:
            matching_link |= _domain_key(normalize_domain(urljoin(final_url, link))) == _domain_key(address)
        except ValueError:
            continue
    if not matching_link:
        raise ValueError("현재 안내 주소와 연결 링크가 일치하지 않습니다.")

    return verify_domain(address, fetcher, cancel, check_cancel=check_cancel, referer=final_url,
                         on_failure=on_failure)


def resolve_current_text_domain(fetcher, cancel, *, check_cancel, on_failure=None, guide_url=TEXT_DOMAIN_GUIDE_URL) -> str:
    """Verify linked hud-url addresses in guide order before guessing any hosts."""
    check_cancel(cancel)
    guide_url = normalize_text_guide_url(guide_url)
    body, final_url = fetcher(guide_url, cancel, guide_url,
                             limit=2 * 1024 * 1024, retry=False)
    check_cancel(cancel)
    final_url = _lookup_url(final_url, guide_url)
    if _domain_key(normalize_domain("https://" + urlsplit(final_url).netloc)) != _domain_key(normalize_domain("https://" + urlsplit(guide_url).netloc)):
        raise ValueError("B 주소 안내 페이지가 다른 사이트로 이동했습니다.")
    guide = _DomainPage(body)
    if not guide.hud_addresses:
        raise ValueError("B 안내 페이지에서 hud-url 주소를 확인하지 못했습니다.")
    addresses = tuple(dict.fromkeys(normalize_domain(value) for value in guide.hud_addresses))
    addresses = tuple(address for address in addresses if is_text_site_url(address))
    if not addresses:
        raise ValueError("B 안내 페이지에 지원하는 toki숫자.com 주소가 없습니다.")
    linked = set()
    for link in guide.links:
        try:
            linked.add(_domain_key(normalize_domain(urljoin(final_url, link))))
        except ValueError:
            continue
    if any(_domain_key(address) not in linked for address in addresses):
        raise ValueError("B hud-url 주소와 연결 링크가 일치하지 않습니다.")
    for index, address in enumerate(addresses):
        check_cancel(cancel)
        try:
            return verify_domain(address, fetcher, cancel, check_cancel=check_cancel, referer=final_url,
                                 on_failure=on_failure, text_site=True, guide_url=guide_url)
        except Exception as error:
            # Fetchers supply their own transport exceptions; cancellation must
            # still escape before another announced address is requested.
            check_cancel(cancel)
            if index == len(addresses) - 1:
                raise
            if on_failure:
                on_failure(str(error))


def verify_domain(domain, fetcher, cancel, *, check_cancel, referer=None, on_failure=None, text_site=False, guide_url=TEXT_DOMAIN_GUIDE_URL) -> str:
    """Verify the homepage, trying index.php once if the root is not a homepage."""
    url = normalize_domain(domain) + "/"
    guide_host = _domain_key(normalize_domain("https://" + urlsplit(guide_url).netloc)) if text_site else None
    referer = referer or url
    visited = {referer} if referer != url else set()
    for hop in range(5):
        check_cancel(cancel)
        if text_site and not is_text_site_url(url):
            raise ValueError("B 접속은 toki숫자.com 주소만 지원합니다. 접속 주소를 다시 확인해 주세요.")
        if url in visited:
            raise ValueError("주소 안내의 이동 경로가 반복됩니다.")
        visited.add(url)
        body, final_url = fetcher(url, cancel, referer, limit=2 * 1024 * 1024, retry=False)
        check_cancel(cancel)
        final_url = _lookup_url(final_url, url)
        if text_site and not is_text_site_url(final_url):
            raise ValueError("지원하지 않는 B 사이트로 이동했습니다. toki숫자.com 주소만 사용할 수 있습니다.")
        if final_url != url:
            if final_url in visited:
                raise ValueError("주소 안내의 이동 경로가 반복됩니다.")
            visited.add(final_url)
        page = _DomainPage(body)
        if page.refreshes:
            targets = set()
            for refresh in page.refreshes:
                match = re.fullmatch(r"\s*[0-9]+(?:\.[0-9]+)?\s*;\s*url\s*=\s*(.*?)\s*", refresh, re.I)
                if not match or not match[1]:
                    raise ValueError("주소 안내의 자동 이동 주소를 읽지 못했습니다.")
                targets.add(_lookup_url(match[1].strip("\"'"), final_url))
            if len(targets) != 1:
                raise ValueError("주소 안내에 서로 다른 자동 이동 주소가 있습니다.")
            url, referer = targets.pop(), final_url
            continue
        origin = normalize_domain("https://" + urlsplit(final_url).netloc)
        if (text_site and _domain_key(origin) != guide_host
                and re.search(r"뉴토끼|NEW\s*TOKI", page.title, re.I)
                and not re.search(r"주소\s*안내|최신\s*주소", page.title)):
            return origin
        if not text_site and re.search(r"티비위키|TV\s*WIKI|티비몬|TVMON", page.title, re.I):
            for link in page.links:
                try:
                    content = urlsplit(_lookup_url(link, final_url))
                    if (content.netloc == urlsplit(origin).netloc
                            and re.fullmatch(r"/[A-Za-z0-9_]+/[0-9]+/?", content.path)):
                        return origin
                except ValueError:
                    continue
        error = "최종 주소에서 콘텐츠 홈페이지를 확인하지 못했습니다."
        index_url = origin + "/index.php"
        if urlsplit(final_url).path == "/" and index_url not in visited and hop < 4:
            if on_failure:
                on_failure(error + " /index.php를 확인합니다.")
            url, referer = index_url, final_url
            continue
        raise ValueError(error)
    raise ValueError("주소 안내의 이동 횟수가 너무 많습니다.")

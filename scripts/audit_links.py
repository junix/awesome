#!/usr/bin/env python3
"""Optional URL observations for this repository's Markdown table index."""

import argparse
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import math
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
MAX_REDIRECTS = 5


def timestamp():
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def canonical_url(url):
    """Normalize authority/default ports and fragment, never path/query semantics."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
    ):
        raise ValueError("entry URLs must be absolute HTTP(S) URLs without credentials")
    if any(char.isspace() or ord(char) < 32 for char in url):
        raise ValueError("entry URLs must not contain whitespace/control characters")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    return urlunsplit((scheme, host, parts.path or "/", parts.query, ""))


def extract_entries(markdown):
    """Read the index's first-column [label](URL) syntax, outside fenced examples.

    Unsupported table rows fail closed instead of silently losing an entry.
    This is deliberately not a general Markdown parser.
    """
    entries = {}
    fence = None
    for number, line in enumerate(markdown.splitlines(), 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is not None or not line.lstrip().startswith("|"):
            continue
        cell = line.strip().split("|")[1].strip()
        if cell in {"名称", "Name"} or re.fullmatch(r":?-{3,}:?", cell):
            continue
        match = re.fullmatch(r"\[([^\]\r\n]+)\]\(([^()\s]+)\)", cell)
        if not match:
            raise ValueError(
                f"line {number}: expected first-column [label](HTTP(S)-URL)"
            )
        label, url = match.groups()
        try:
            canonical = canonical_url(url)
        except ValueError as error:
            raise ValueError(f"line {number}: {error}") from error
        entries.setdefault(canonical, []).append(
            {"label": label, "url": url, "line": number}
        )
    if fence is not None:
        raise ValueError("unclosed code fence: refusing an incomplete inventory")
    if not entries:
        raise ValueError("no indexed URLs found")
    return entries


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def skipped(reason):
    return dict(
        status="skipped",
        reason=reason,
        retryable=False,
        attempted_at=None,
        checked_at=None,
        http_status=None,
        final_url=None,
        redirects=[],
    )


def check_url(url, timeout=5.0, opener=None):
    """HEAD only: max six requests, no body reads, socket timeout per request.

    A socket timeout is not an overall wall-clock/DNS deadline. No automatic
    retries: rate limits and transient failures are evidence for a later run.
    """
    if opener is None:
        opener = build_opener(NoRedirect())
    result = skipped("not_attempted")
    result["attempted_at"] = timestamp()
    current = url
    seen = {url}
    for hop in range(MAX_REDIRECTS + 1):
        result.update(final_url=current, http_status=None, checked_at=None)
        result.pop("retry_after", None)
        request = Request(
            current, method="HEAD", headers={"User-Agent": "awesome-link-audit/1"}
        )
        try:
            try:
                response = opener.open(request, timeout=timeout)
            except HTTPError as error:
                response = error  # HTTP error status/headers are actual observations.
            with response:
                code = response.status
                location = response.headers.get("Location")
                retry_after = response.headers.get("Retry-After")
        except (URLError, OSError, HTTPException, ValueError) as error:
            result.update(
                status="failed",
                reason="transport_error",
                retryable=True,
                error=str(error),
            )
            return result
        observed = timestamp()
        result.update(checked_at=observed, http_status=code)
        if retry_after is not None:
            result["retry_after"] = retry_after
        if code in {301, 302, 303, 307, 308}:
            result.update(status="failed", reason="missing_redirect_location")
            if not location:
                return result
            try:
                target = canonical_url(urljoin(current, location))
            except ValueError:
                result["reason"] = "invalid_redirect"
                return result
            result["redirects"].append(
                {
                    "from": current,
                    "to": target,
                    "http_status": code,
                    "observed_at": observed,
                }
            )
            if target in seen:
                result["reason"] = "redirect_loop"
                return result
            if hop == MAX_REDIRECTS:
                result["reason"] = "redirect_limit"
                return result
            seen.add(target)
            current = target
            continue
        if 200 <= code < 300:
            result.update(status="checked", reason="http_success")
        elif code == 429:
            result.update(status="failed", reason="rate_limited", retryable=True)
        elif code in {408, 425} or 500 <= code < 600:
            result.update(
                status="failed", reason="transient_http_error", retryable=True
            )
        elif code in {404, 410}:
            result.update(status="failed", reason="http_not_found")
        elif code in {401, 403}:
            result.update(status="failed", reason="access_denied")
        elif code == 405:
            result.update(status="skipped", reason="head_not_supported")
        else:
            result.update(status="failed", reason="http_error")
        return result
    raise AssertionError("redirect loop exceeded its bound")


def make_report(readme, *, check=False, urls=(), max_checks=10, timeout=5.0):
    if (
        not 0 <= max_checks <= 100
        or not math.isfinite(timeout)
        or not 0 < timeout <= 60
    ):
        raise ValueError(
            "max-checks must be 0..100 and timeout must be finite and in (0, 60]"
        )
    raw = Path(readme).read_bytes()
    inventory = extract_entries(raw.decode("utf-8"))
    selected = {canonical_url(url) for url in urls}
    if selected - inventory.keys():
        raise ValueError(
            "selected URL is not in the index: "
            + ", ".join(sorted(selected - inventory.keys()))
        )
    generated = timestamp()
    report = dict(
        schema_version=1,
        generated_at=generated,
        source=str(readme),
        source_sha256=hashlib.sha256(raw).hexdigest(),
        method="HEAD" if check else None,
        summary={"checked": 0, "failed": 0, "skipped": 0},
        entries={},
    )
    count = 0
    for url, sources in inventory.items():
        if not check:
            observation = skipped("network_not_requested")
        elif selected and url not in selected:
            observation = skipped("not_selected")
        elif count >= max_checks:
            observation = skipped("request_budget_exhausted")
        else:
            count += 1
            observation = check_url(url, timeout=timeout)
        report["entries"][url] = dict(
            sources=sources, recorded_at=generated, **observation
        )
        report["summary"][observation["status"]] += 1
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    parser.add_argument(
        "--check", action="store_true", help="opt in to HTTP HEAD requests"
    )
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="check only this indexed URL (repeatable)",
    )
    parser.add_argument(
        "--max-checks",
        type=int,
        default=10,
        help="maximum entry checks, 0..100 (default: 10)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="socket timeout per HTTP request, at most 60s",
    )
    args = parser.parse_args(argv)
    try:
        report = make_report(
            args.readme,
            check=args.check,
            urls=args.url,
            max_checks=args.max_checks,
            timeout=args.timeout,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from rootorroute.domain.code_host import CodeHostError, Mention, MentionKind, PullRequest

API_VERSION = "2026-03-10"
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?$")
_WRITE_ROLES = {"admin", "maintain", "write"}
_MAX_ATTEMPTS = 4
_MAX_SLEEP = 60.0

Json = dict[str, Any]


def _repo(repo: str) -> str:
    if not _REPO.match(repo):
        raise CodeHostError(f"invalid repo {repo!r}", retryable=False)
    return repo


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _tail_number(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[1])


def mention_pattern(login: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-])@{re.escape(login)}(?![\w-])", re.IGNORECASE)


class GitHubCodeHost:
    def __init__(
        self,
        token: Callable[[], str],
        bot_login: str,
        repos: Sequence[str] = (),
        api_url: str = "https://api.github.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._bot = bot_login
        self._mention = mention_pattern(bot_login)
        self._repos = [_repo(r) for r in repos]
        self._api = api_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "rootorroute",
        }

    def _url(self, path: str) -> str:
        if path.startswith(("https://", "http://")):
            if not path.startswith(self._api + "/"):
                raise CodeHostError("refusing to follow a link off the API host", retryable=False)
            return path
        return self._api + path

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: Json | None = None,
        ok_missing: bool = False,
    ) -> httpx.Response | None:
        url = self._url(path)
        what = f"GitHub {method} {httpx.URL(url).path}"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await self._client.request(
                    method, url, params=params, json=body, headers=self._headers()
                )
            except httpx.TransportError as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise CodeHostError(f"{what}: {type(exc).__name__}", retryable=True) from None
                await asyncio.sleep(2 ** (attempt - 1))
                continue
            if resp.is_success:
                return resp
            if resp.status_code == 404 and ok_missing:
                return None
            delay = _retry_delay(resp, attempt)
            if delay is None:
                raise CodeHostError(
                    f"{what} -> {resp.status_code}: {_message(resp)}",
                    retryable=resp.status_code >= 500 or _rate_limited(resp),
                    status=resp.status_code,
                )
            if attempt == _MAX_ATTEMPTS or delay > _MAX_SLEEP:
                raise CodeHostError(
                    f"{what} -> {resp.status_code}: {_message(resp)}",
                    retryable=True,
                    status=resp.status_code,
                )
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        resp = await self._request("GET", path, params=params)
        assert resp is not None
        return resp.json()

    async def _pages(self, path: str, params: dict[str, str]) -> AsyncIterator[Json]:
        url: str | None = path
        query: dict[str, str] | None = {**params, "per_page": "100"}
        while url is not None:
            resp = await self._request("GET", url, params=query)
            assert resp is not None
            items: list[Json] = resp.json()
            for item in items:
                yield item
            url = resp.links.get("next", {}).get("url")
            query = None

    async def default_branch(self, repo: str) -> str:
        data: Json = await self._get(f"/repos/{_repo(repo)}")
        return str(data["default_branch"])

    async def find_pr(self, repo: str, head: str) -> PullRequest | None:
        owner = _repo(repo).split("/", 1)[0]
        qualified = head if ":" in head else f"{owner}:{head}"
        params = {"head": qualified, "state": "open"}
        async for pr in self._pages(f"/repos/{repo}/pulls", params):
            return _pull(pr)
        return None

    async def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> PullRequest:
        payload: Json = {"title": title, "head": head, "base": base, "body": body}
        try:
            resp = await self._request("POST", f"/repos/{_repo(repo)}/pulls", body=payload)
        except CodeHostError as exc:
            if exc.status == 422:
                existing = await self.find_pr(repo, head)
                if existing is not None:
                    return existing
            raise
        assert resp is not None
        return _pull(resp.json())

    async def comment(self, repo: str, number: int, body: str) -> str:
        resp = await self._request(
            "POST", f"/repos/{_repo(repo)}/issues/{number}/comments", body={"body": body}
        )
        assert resp is not None
        return str(resp.json()["html_url"])

    async def can_write(self, repo: str, user: str) -> bool:
        if not _LOGIN.match(user):
            return False
        resp = await self._request(
            "GET", f"/repos/{_repo(repo)}/collaborators/{user}/permission", ok_missing=True
        )
        if resp is None:
            return False
        data: Json = resp.json()
        return data.get("permission") in _WRITE_ROLES or data.get("role_name") in _WRITE_ROLES

    async def mentions(self, since: datetime) -> list[Mention]:
        since_utc = since.astimezone(UTC)
        stamp = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"since": stamp, "sort": "created", "direction": "asc"}
        found: list[Mention] = []
        for repo in self._repos:
            cache: dict[str, Json] = {}

            async for c in self._pages(f"/repos/{repo}/issues/comments", params):
                if not self._wanted(c, since_utc):
                    continue
                number = _tail_number(str(c["issue_url"]))
                item = await self._cached(cache, f"/repos/{repo}/issues/{number}")
                kind: MentionKind = "pr" if item.get("pull_request") else "issue"
                head = None
                if kind == "pr":
                    pr = await self._cached(cache, f"/repos/{repo}/pulls/{number}")
                    head = _head_branch(repo, pr)
                found.append(self._build(f"ic-{c['id']}", repo, kind, number, c, item, head))

            async for c in self._pages(f"/repos/{repo}/pulls/comments", params):
                if not self._wanted(c, since_utc):
                    continue
                number = _tail_number(str(c["pull_request_url"]))
                pr = await self._cached(cache, f"/repos/{repo}/pulls/{number}")
                head = _head_branch(repo, pr)
                found.append(self._build(f"rc-{c['id']}", repo, "review", number, c, pr, head))
        found.sort(key=lambda m: (m.created_at, m.id))
        return found

    async def _cached(self, cache: dict[str, Json], path: str) -> Json:
        if path not in cache:
            cache[path] = await self._get(path)
        return cache[path]

    def _wanted(self, c: Json, since: datetime) -> bool:
        user: Json | None = c.get("user")
        if not user or str(user.get("login", "")).lower() == self._bot.lower():
            return False
        if _time(str(c["created_at"])) < since:
            return False
        return bool(self._mention.search(str(c.get("body") or "")))

    def _build(
        self,
        mention_id: str,
        repo: str,
        kind: MentionKind,
        number: int,
        c: Json,
        parent: Json,
        head: str | None,
    ) -> Mention:
        return Mention(
            id=mention_id,
            repo=repo,
            kind=kind,
            number=number,
            author=str(c["user"]["login"]),
            body=str(c.get("body") or ""),
            url=str(c["html_url"]),
            title=str(parent.get("title") or ""),
            context=str(parent.get("body") or ""),
            head_branch=head,
            created_at=_time(str(c["created_at"])),
        )


def _pull(pr: Json) -> PullRequest:
    return PullRequest(
        number=int(pr["number"]),
        url=str(pr["html_url"]),
        head=str(pr["head"]["ref"]),
        base=str(pr["base"]["ref"]),
        state=str(pr["state"]),
    )


def _head_branch(repo: str, pr: Json) -> str | None:
    head: Json = pr.get("head") or {}
    head_repo: Json = head.get("repo") or {}
    if str(head_repo.get("full_name", "")).lower() != repo.lower():
        return None
    return str(head["ref"])


def _message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.reason_phrase
    if isinstance(data, dict) and "message" in data:
        return str(data["message"])[:300]  # pyright: ignore[reportUnknownArgumentType]
    return resp.reason_phrase


def _rate_limited(resp: httpx.Response) -> bool:
    if resp.status_code not in (403, 429):
        return False
    if "retry-after" in resp.headers or resp.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "rate limit" in _message(resp).lower()


def _retry_delay(resp: httpx.Response, attempt: int) -> float | None:
    if resp.status_code >= 500:
        return float(2 ** (attempt - 1))
    if not _rate_limited(resp):
        return None
    retry_after = resp.headers.get("retry-after")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            return _MAX_SLEEP
    reset = resp.headers.get("x-ratelimit-reset")
    if resp.headers.get("x-ratelimit-remaining") == "0" and reset is not None:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            return _MAX_SLEEP
    return _MAX_SLEEP

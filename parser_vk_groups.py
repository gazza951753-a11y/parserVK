#!/usr/bin/env python3
"""VK groups parser for finding student-oriented audiences for VK Ads campaigns."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

VK_API_VERSION = "5.199"
VK_API_URL = "https://api.vk.com/method/"

DEFAULT_INCLUDE_KEYWORDS = [
    "студент",
    "университет",
    "вуз",
    "бакалавр",
    "магистратура",
    "курсовая",
    "диплом",
    "сессия",
    "реферат",
    "зачет",
    "экзамен",
    "общежитие",
]

DEFAULT_EXCLUDE_KEYWORDS = [
    "школа",
    "абитуриент",
    "детский",
    "дошколь",
    "младш",
]


def normalize_text(value: str | None) -> str:
    return (value or "").strip().lower()


@dataclass
class GroupCandidate:
    group_id: int
    name: str
    screen_name: str
    members_count: int
    activity: str
    description: str
    city_title: str
    wall_post_count_90d: int
    score: float
    matched_include_keywords: list[str]

    @property
    def group_url(self) -> str:
        return f"https://vk.com/{self.screen_name or f'club{self.group_id}'}"


class VkApiError(RuntimeError):
    pass


class VkClient:
    def __init__(self, token: str, timeout: int = 20, rps_limit: float = 3.0) -> None:
        self.token = token
        self.timeout = timeout
        self.rps_limit = max(rps_limit, 0.1)
        self._last_request_ts = 0.0

    def _throttle(self) -> None:
        min_interval = 1.0 / self.rps_limit
        elapsed = time.time() - self._last_request_ts
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._throttle()
        payload = {"access_token": self.token, "v": VK_API_VERSION, **params}
        query = urllib.parse.urlencode(payload)
        url = f"{VK_API_URL}{method}?{query}"

        request = urllib.request.Request(url=url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                data = json.loads(body)
        except Exception as exc:
            raise VkApiError(f"{method} HTTP error: {exc}") from exc
        finally:
            self._last_request_ts = time.time()

        if "error" in data:
            err = data["error"]
            code = err.get("error_code")
            msg = err.get("error_msg", "VK API error")
            raise VkApiError(f"{method} failed (code={code}): {msg}")

        return data.get("response", {})

    def search_groups(self, query: str, count: int, offset: int = 0) -> list[dict[str, Any]]:
        result = self.call(
            "groups.search",
            {
                "q": query,
                "type": "group,page,event",
                "sort": 6,
                "count": min(count, 1000),
                "offset": offset,
            },
        )
        return result.get("items", [])

    def get_groups_info(self, group_ids: list[int]) -> list[dict[str, Any]]:
        if not group_ids:
            return []
        result = self.call(
            "groups.getById",
            {
                "group_ids": ",".join(map(str, group_ids)),
                "fields": "description,members_count,activity,city,site,verified,can_post,wall",
            },
        )
        if isinstance(result, dict) and "groups" in result:
            return result["groups"]
        if isinstance(result, list):
            return result
        return []

    def get_wall_posts(self, owner_id: int, count: int = 30) -> list[dict[str, Any]]:
        result = self.call(
            "wall.get",
            {
                "owner_id": -abs(owner_id),
                "count": count,
                "filter": "owner",
            },
        )
        return result.get("items", [])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Собирает и ранжирует группы ВКонтакте с аудиторией студентов "
            "для рекламной кампании StudyAssist"
        )
    )
    parser.add_argument("--token", default=os.getenv("VK_TOKEN"), help="VK API token")
    parser.add_argument(
        "--queries",
        default="студенты,студенческий совет,курсовая работа,дипломная работа,вуз",
        help="CSV список поисковых запросов",
    )
    parser.add_argument("--max-groups", type=int, default=250, help="Максимум групп после дедупликации")
    parser.add_argument("--batch-size", type=int, default=100, help="Размер батча для groups.search")
    parser.add_argument("--min-members", type=int, default=500, help="Минимум подписчиков")
    parser.add_argument("--max-members", type=int, default=400000, help="Максимум подписчиков")
    parser.add_argument("--min-score", type=float, default=5.0, help="Минимальный score для отбора")
    parser.add_argument("--posts-window-days", type=int, default=90, help="Период анализа активности, дней")
    parser.add_argument("--wall-post-sample", type=int, default=30, help="Сколько постов смотреть у каждой группы")
    parser.add_argument(
        "--include-keywords",
        default=",".join(DEFAULT_INCLUDE_KEYWORDS),
        help="CSV ключевые слова, повышающие релевантность",
    )
    parser.add_argument(
        "--exclude-keywords",
        default=",".join(DEFAULT_EXCLUDE_KEYWORDS),
        help="CSV ключевые слова, исключающие нерелевантные группы",
    )
    parser.add_argument("--output", default="vk_groups_students.csv", help="Путь к CSV результату")
    parser.add_argument("--json-output", default="vk_groups_students.json", help="Путь к JSON результату")
    return parser.parse_args()


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def contains_any(text: str, keywords: list[str]) -> tuple[bool, list[str]]:
    matches = [kw for kw in keywords if kw in text]
    return bool(matches), matches


def calculate_group_score(
    group: dict[str, Any],
    include_keywords: list[str],
    exclude_keywords: list[str],
    posts_recent_count: int,
) -> tuple[float, list[str]]:
    members = int(group.get("members_count") or 0)
    name = normalize_text(group.get("name"))
    description = normalize_text(group.get("description"))
    activity = normalize_text(group.get("activity"))

    combined_text = " ".join([name, description, activity])

    score = 0.0

    if members > 0:
        if 1000 <= members <= 120000:
            score += 3.0
        elif 500 <= members < 1000 or 120000 < members <= 300000:
            score += 1.5

    _, include_matches = contains_any(combined_text, include_keywords)
    score += 1.2 * len(include_matches)

    exclude_hit, _ = contains_any(combined_text, exclude_keywords)
    if exclude_hit:
        score -= 4.0

    if re.search(r"\b(18\+|бакалавр|магистр|курсов|диплом|сессия|экзамен)\b", combined_text):
        score += 2.0

    if posts_recent_count >= 12:
        score += 3.0
    elif posts_recent_count >= 5:
        score += 1.5

    return score, include_matches


def recent_posts_count(posts: list[dict[str, Any]], days: int) -> int:
    if days <= 0:
        return 0
    threshold = datetime.now(timezone.utc).timestamp() - (days * 86400)
    count = 0
    for post in posts:
        if post.get("date", 0) >= threshold:
            count += 1
    return count


def collect_candidates(args: argparse.Namespace) -> list[GroupCandidate]:
    if not args.token:
        raise SystemExit("Ошибка: передайте VK token через --token или переменную окружения VK_TOKEN")

    include_keywords = [normalize_text(x) for x in split_csv(args.include_keywords)]
    exclude_keywords = [normalize_text(x) for x in split_csv(args.exclude_keywords)]
    queries = split_csv(args.queries)

    client = VkClient(token=args.token)

    found_groups: dict[int, dict[str, Any]] = {}
    for query in queries:
        offset = 0
        while len(found_groups) < args.max_groups:
            items = client.search_groups(query=query, count=args.batch_size, offset=offset)
            if not items:
                break
            for item in items:
                found_groups[item["id"]] = item
                if len(found_groups) >= args.max_groups:
                    break
            offset += len(items)
            if len(items) < args.batch_size:
                break

    group_ids = list(found_groups.keys())
    detailed_groups: list[dict[str, Any]] = []

    for i in range(0, len(group_ids), 500):
        batch = group_ids[i : i + 500]
        detailed_groups.extend(client.get_groups_info(batch))

    candidates: list[GroupCandidate] = []

    for group in detailed_groups:
        members = int(group.get("members_count") or 0)
        if members < args.min_members or members > args.max_members:
            continue

        if int(group.get("is_closed", 0)) != 0:
            continue

        wall_posts = []
        try:
            wall_posts = client.get_wall_posts(owner_id=int(group["id"]), count=args.wall_post_sample)
        except Exception:
            pass

        posts_90d = recent_posts_count(wall_posts, args.posts_window_days)
        score, include_matches = calculate_group_score(group, include_keywords, exclude_keywords, posts_90d)
        if score < args.min_score:
            continue

        city = group.get("city") or {}
        candidates.append(
            GroupCandidate(
                group_id=int(group["id"]),
                name=group.get("name", ""),
                screen_name=group.get("screen_name", ""),
                members_count=members,
                activity=group.get("activity", ""),
                description=(group.get("description", "") or "").replace("\n", " ").strip(),
                city_title=city.get("title", "") if isinstance(city, dict) else "",
                wall_post_count_90d=posts_90d,
                score=round(score, 2),
                matched_include_keywords=include_matches,
            )
        )

    candidates.sort(key=lambda x: (x.score, x.members_count), reverse=True)
    return candidates


def save_to_csv(path: str, candidates: list[GroupCandidate]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "group_id",
                "name",
                "group_url",
                "members_count",
                "score",
                "posts_90d",
                "city",
                "activity",
                "matched_keywords",
                "description",
            ]
        )
        for item in candidates:
            writer.writerow(
                [
                    item.group_id,
                    item.name,
                    item.group_url,
                    item.members_count,
                    item.score,
                    item.wall_post_count_90d,
                    item.city_title,
                    item.activity,
                    ", ".join(item.matched_include_keywords),
                    item.description,
                ]
            )


def save_to_json(path: str, candidates: list[GroupCandidate]) -> None:
    payload = [
        {
            "group_id": item.group_id,
            "name": item.name,
            "group_url": item.group_url,
            "members_count": item.members_count,
            "score": item.score,
            "posts_90d": item.wall_post_count_90d,
            "city": item.city_title,
            "activity": item.activity,
            "matched_keywords": item.matched_include_keywords,
            "description": item.description,
        }
        for item in candidates
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main() -> None:
    args = parse_args()
    candidates = collect_candidates(args)
    save_to_csv(args.output, candidates)
    save_to_json(args.json_output, candidates)
    print(f"Готово. Отобрано групп: {len(candidates)}")
    print(f"CSV: {args.output}")
    print(f"JSON: {args.json_output}")


if __name__ == "__main__":
    main()

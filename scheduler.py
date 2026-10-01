import asyncio
import logging
import os
import time as clock
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

from schemas import CalendarResult

load_dotenv()

logger = logging.getLogger(__name__)

CALENDAR_URL = os.getenv("CALENDAR_URL")
if not CALENDAR_URL:
    raise RuntimeError("CALENDAR_URL is not set. Add it to your .env file.")

USER_TZ = ZoneInfo(os.getenv("USER_TIMEZONE", "Europe/London"))

WEEKDAYS = ["monday", "tuesday", "wednesday",
            "thursday", "friday", "saturday", "sunday"]
PART_OF_DAY_HOUR = {"morning": 9, "afternoon": 14, "evening": 17}


def resolve_checkin_time(weekday=None, days_from_now=None, hour=None,
                         part_of_day=None, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(USER_TZ)

    # A named weekday is more specific than a day count, so it wins.
    if weekday and weekday.lower() in WEEKDAYS:
        days_ahead = (WEEKDAYS.index(weekday.lower()) - now.weekday()) % 7 or 7
        target = now.date() + timedelta(days=days_ahead)
    elif days_from_now is not None:
        if not 0 <= days_from_now <= 60:
            logger.info(
                "Rejected check-in: days_from_now=%s out of range", days_from_now)
            return None
        target = now.date() + timedelta(days=days_from_now)
    else:
        logger.info("Rejected check-in: no usable day (weekday=%s)", weekday)
        return None

    if hour is None or not 0 <= hour <= 23:
        hour = PART_OF_DAY_HOUR.get(part_of_day, 9)

    local_dt = datetime.combine(target, time(hour), tzinfo=USER_TZ)
    if local_dt <= now:
        logger.info("Rejected check-in: %s is in the past",
                    local_dt.isoformat())
        return None

    timestamp = local_dt.astimezone(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("Resolved check-in: weekday=%s days=%s hour=%s part=%s -> %s",
                weekday, days_from_now, hour, part_of_day, timestamp)
    return timestamp


async def schedule_checkin(title, timestamp, attempts=3, timeout=5.0):
    last_error = "unknown error"

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, attempts + 1):
            started = clock.perf_counter()
            try:
                response = await client.post(
                    CALENDAR_URL, json={"title": title, "timestamp": timestamp}
                )
                elapsed = clock.perf_counter() - started

                if response.is_success:
                    event_id = response.json().get("event_id")
                    if event_id:
                        logger.info("Calendar attempt %d/%d succeeded in %.2fs, event_id=%s",
                                    attempt, attempts, elapsed, event_id)
                        return CalendarResult(
                            status="scheduled", title=title,
                            timestamp=timestamp, event_id=event_id,
                        )
                    last_error = "success response without event_id"
                elif response.status_code < 500 and response.status_code != 429:
                    logger.error("Calendar rejected request with %d: %s",
                                 response.status_code, response.text[:200])
                    return CalendarResult(
                        status="failed", title=title, timestamp=timestamp,
                        detail=f"calendar rejected the request ({response.status_code})",
                    )
                else:
                    last_error = f"calendar returned {response.status_code}"

                logger.warning("Calendar attempt %d/%d failed in %.2fs: %s",
                               attempt, attempts, elapsed, last_error)

            except httpx.TimeoutException:
                last_error = "calendar timed out"
                logger.warning("Calendar attempt %d/%d timed out after %.1fs",
                               attempt, attempts, timeout)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = f"calendar error: {type(exc).__name__}"
                logger.warning("Calendar attempt %d/%d error: %s",
                               attempt, attempts, exc)

            if attempt < attempts:
                wait = 0.5 * 2 ** (attempt - 1)
                logger.info("Retrying calendar in %.1fs", wait)
                await asyncio.sleep(wait)

    logger.error("Calendar gave up after %d attempts: %s",
                 attempts, last_error)
    return CalendarResult(
        status="failed", title=title, timestamp=timestamp,
        detail=f"not scheduled after {attempts} attempts: {last_error}",
    )

"""Scheduled US equity session in UTC, including exchange holidays and early closes."""
from datetime import datetime
import json
import sys
from zoneinfo import ZoneInfo

from exchange_calendars import get_calendar

calendar = get_calendar("XNYS")
instant = datetime.fromisoformat(sys.argv[1])
day = str(instant.astimezone(ZoneInfo("America/New_York")).date()) if instant.tzinfo else str(instant.date())
if calendar.is_session(day):
    print(json.dumps([calendar.session_open(day).isoformat(), calendar.session_close(day).isoformat()]))
else:
    print("null")

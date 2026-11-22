# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["exchange-calendars==4.13.3"]
# ///
"""Scheduled US equity session in UTC, including exchange holidays and early closes."""
import json
import sys

from exchange_calendars import get_calendar

calendar = get_calendar("XNYS")
day = sys.argv[1]
if calendar.is_session(day):
    print(json.dumps([calendar.session_open(day).isoformat(), calendar.session_close(day).isoformat()]))
else:
    print("null")

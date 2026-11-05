"""JST conversion helpers. Sheet datetimes are written as JST wall-clock text."""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
_SHEETS_EPOCH = datetime(1899, 12, 30)  # Google Sheets / Excel serial day 0
_FMT = "%Y/%m/%d %H:%M:%S"


def to_jst(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(JST)


def fmt_jst(ts):
    """Unix seconds -> 'YYYY/MM/DD HH:MM:SS' in JST (parsed by Sheets as a datetime)."""
    return to_jst(ts).strftime(_FMT)


def parse_jst(text):
    """'YYYY/MM/DD HH:MM[:SS]' in JST -> Unix seconds."""
    text = str(text).strip()
    for fmt in (_FMT, "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=JST).timestamp())
        except ValueError:
            continue
    raise ValueError("unrecognized datetime: %r" % (text,))


def serial_to_ts(serial):
    """Sheets serial number holding a JST wall-clock time -> Unix seconds."""
    naive = _SHEETS_EPOCH + timedelta(days=float(serial))
    return int(round(naive.replace(tzinfo=JST).timestamp()))


def cell_to_ts(value):
    """Accept a serial number or a JST text cell. Returns None for blanks."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return serial_to_ts(value)
    return parse_jst(value)

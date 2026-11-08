import datetime, re, socket, time

_cache = (0.0, False)

def online():
    """Cheap internet check, cached for 30 s."""
    global _cache
    if time.time() - _cache[0] < 30:
        return _cache[1]
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=1).close()
        ok = True
    except OSError:
        ok = False
    _cache = (time.time(), ok)
    return ok


def parse_date(value):
    """ISO (2027-01-10) or day-first 10/01/2027, 10.01.2027, 10-01-2027 (Uzbekistan/Russia convention).
    Accepts date objects too. Returns a date or None."""
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    s = str(value).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", s)
        if not m:
            return None
        d, mo, y = map(int, m.groups())
        if mo > 12 >= d:                    # 01/25/2027 can only be month first
            d, mo = mo, d
    try:
        return datetime.date(y, mo, d)
    except ValueError:
        return None
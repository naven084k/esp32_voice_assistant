"""
Math tools for the ARIA agent.
Covers: general expression eval, unit conversion, scientific functions,
statistics, financial calculations, currency conversion, and timezone utilities.
"""
import math
import statistics as _stats
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import httpx
from langchain_core.tools import tool

# ─── Safe expression evaluator ────────────────────────────────────────────────

_SAFE_NS = {
    "__builtins__": {},
    # arithmetic helpers
    "abs": abs, "round": round, "min": min, "max": max,
    "pow": math.pow, "sqrt": math.sqrt, "floor": math.floor, "ceil": math.ceil,
    # trig (radians)
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    # logarithms / exp
    "log": math.log, "log2": math.log2, "log10": math.log10, "exp": math.exp,
    # combinatorics
    "factorial": math.factorial, "comb": math.comb, "perm": math.perm,
    # constants
    "pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf,
}


@tool
def calculate(expression: str) -> str:
    """Evaluate any mathematical expression. Supports arithmetic, trig (radians),
    logarithms, sqrt, factorial, comb, perm, pi, e. Use for quick arithmetic
    or when the user asks to compute or solve a math expression.
    Examples: '2 + 3 * 4', 'sqrt(144)', 'pi * 5**2', 'factorial(6)', 'comb(10,3)'"""
    try:
        result = eval(expression.strip(), _SAFE_NS)
        if isinstance(result, float) and result == int(result):
            result = int(result)
        return str(result)
    except Exception as e:
        return f"Error evaluating '{expression}': {e}"


# ─── Unit conversion ──────────────────────────────────────────────────────────

# All units stored as (factor_to_base, base_unit_name)
_UNITS: dict[str, dict[str, float]] = {
    "length": {  # base = metre
        "mm": 1e-3, "cm": 1e-2, "m": 1, "km": 1e3,
        "in": 0.0254, "inch": 0.0254, "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
        "yd": 0.9144, "yard": 0.9144, "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
        "nm": 1852,  # nautical mile
    },
    "weight": {  # base = kilogram
        "mg": 1e-6, "g": 1e-3, "kg": 1, "tonne": 1e3, "metric_ton": 1e3,
        "oz": 0.0283495, "lb": 0.453592, "lbs": 0.453592, "pound": 0.453592, "pounds": 0.453592,
        "ton": 907.185, "short_ton": 907.185,
    },
    "volume": {  # base = litre
        "ml": 0.001, "cl": 0.01, "dl": 0.1, "l": 1, "litre": 1, "liter": 1,
        "tsp": 0.00492892, "tbsp": 0.0147868, "fl_oz": 0.0295735,
        "cup": 0.236588, "pint": 0.473176, "quart": 0.946353, "gallon": 3.78541,
        "m3": 1000, "cm3": 0.001, "cc": 0.001,
    },
    "speed": {  # base = m/s
        "m_s": 1, "mps": 1, "km_h": 1/3.6, "kmh": 1/3.6, "kph": 1/3.6,
        "mph": 0.44704, "knot": 0.514444, "knots": 0.514444, "ft_s": 0.3048,
    },
    "area": {  # base = m²
        "mm2": 1e-6, "cm2": 1e-4, "m2": 1, "km2": 1e6,
        "in2": 0.00064516, "ft2": 0.092903, "yd2": 0.836127, "acre": 4046.86,
        "hectare": 10000, "ha": 10000, "mi2": 2.59e6,
    },
    "data": {  # base = byte
        "b": 1, "byte": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4,
        "kib": 1000, "mib": 1e6, "gib": 1e9, "tib": 1e12,
    },
    "time": {  # base = second
        "ms": 1e-3, "s": 1, "sec": 1, "min": 60, "h": 3600, "hr": 3600,
        "day": 86400, "week": 604800, "month": 2628000, "year": 31536000,
    },
}


def _find_category(unit: str) -> tuple[str, float] | None:
    u = unit.lower().strip()
    for cat, mapping in _UNITS.items():
        if u in mapping:
            return cat, mapping[u]
    return None


@tool
def convert_units(value: float, from_unit: str, to_unit: str) -> str:
    """Convert a value between measurement units.
    Supports: length (mm/cm/m/km/in/ft/mi), weight (mg/g/kg/lb/oz),
    volume (ml/l/cup/pint/gallon), speed (m_s/km_h/mph/knot),
    area (m2/km2/ft2/acre/hectare), data (kb/mb/gb/tb), time (ms/s/min/h/day).
    Temperature: use convert_temperature instead.
    Example: convert 100 km to miles, convert 5 kg to lbs"""
    src = _find_category(from_unit)
    dst = _find_category(to_unit)

    if src is None:
        return f"Unknown unit: '{from_unit}'"
    if dst is None:
        return f"Unknown unit: '{to_unit}'"
    if src[0] != dst[0]:
        return f"Cannot convert {src[0]} unit '{from_unit}' to {dst[0]} unit '{to_unit}'"

    base_value = value * src[1]
    result = base_value / dst[1]
    return f"{value} {from_unit} = {round(result, 6)} {to_unit}"


@tool
def convert_temperature(value: float, from_unit: str, to_unit: str) -> str:
    """Convert temperature between Celsius (C), Fahrenheit (F), and Kelvin (K).
    Example: convert 100 C to F, convert 98.6 F to C"""
    f = from_unit.upper().strip("°")
    t = to_unit.upper().strip("°")

    def to_celsius(v: float, u: str) -> float:
        if u == "C": return v
        if u == "F": return (v - 32) * 5 / 9
        if u == "K": return v - 273.15
        raise ValueError(f"Unknown unit: {u}")

    def from_celsius(v: float, u: str) -> float:
        if u == "C": return v
        if u == "F": return v * 9 / 5 + 32
        if u == "K": return v + 273.15
        raise ValueError(f"Unknown unit: {u}")

    try:
        result = from_celsius(to_celsius(value, f), t)
        return f"{value}°{f} = {round(result, 4)}°{t}"
    except ValueError as e:
        return str(e)


# ─── Scientific functions ─────────────────────────────────────────────────────

@tool
def scientific_calc(function: str, value: float, use_degrees: bool = True) -> str:
    """Scientific math functions.
    Functions: sin, cos, tan, asin, acos, atan (trig — use use_degrees=True for degrees),
               log (natural), log10, log2, sqrt, cbrt, exp, degrees, radians.
    Example: sin(30 degrees), log10(1000), sqrt(2), asin(0.5) in degrees"""
    fn = function.lower().strip()
    try:
        if fn in ("sin", "cos", "tan"):
            v = math.radians(value) if use_degrees else value
            result = getattr(math, fn)(v)
        elif fn in ("asin", "acos", "atan"):
            result = getattr(math, fn)(value)
            if use_degrees:
                result = math.degrees(result)
        elif fn == "log":
            result = math.log(value)
        elif fn == "log10":
            result = math.log10(value)
        elif fn == "log2":
            result = math.log2(value)
        elif fn == "sqrt":
            result = math.sqrt(value)
        elif fn == "cbrt":
            result = math.cbrt(value) if hasattr(math, "cbrt") else value ** (1/3)
        elif fn == "exp":
            result = math.exp(value)
        elif fn == "degrees":
            result = math.degrees(value)
        elif fn == "radians":
            result = math.radians(value)
        elif fn == "factorial":
            result = math.factorial(int(value))
        else:
            return f"Unknown function '{function}'. Supported: sin, cos, tan, asin, acos, atan, log, log10, log2, sqrt, cbrt, exp, degrees, radians, factorial"
        return f"{function}({value}{'°' if use_degrees and fn in ('sin','cos','tan') else ''}) = {round(result, 10)}"
    except Exception as e:
        return f"Error: {e}"


# ─── Statistics ───────────────────────────────────────────────────────────────

@tool
def statistics_calc(operation: str, numbers: list[float]) -> str:
    """Statistical calculations on a list of numbers.
    Operations: mean, median, mode, std_dev, variance, min, max, range, sum, count,
                geometric_mean, harmonic_mean, percentile_25, percentile_75, iqr.
    Example: mean of [10, 20, 30, 40], std_dev of [2, 4, 4, 4, 5, 5, 7, 9]"""
    if not numbers:
        return "Error: empty list"
    op = operation.lower().replace(" ", "_").replace("-", "_")
    try:
        if op == "mean":             result = _stats.mean(numbers)
        elif op == "median":         result = _stats.median(numbers)
        elif op == "mode":           result = _stats.mode(numbers)
        elif op in ("std_dev", "stdev", "std"): result = _stats.stdev(numbers)
        elif op == "variance":       result = _stats.variance(numbers)
        elif op == "min":            result = min(numbers)
        elif op == "max":            result = max(numbers)
        elif op == "range":          result = max(numbers) - min(numbers)
        elif op == "sum":            result = sum(numbers)
        elif op == "count":          result = len(numbers)
        elif op == "geometric_mean": result = _stats.geometric_mean(numbers)
        elif op == "harmonic_mean":  result = _stats.harmonic_mean(numbers)
        elif op in ("percentile_25", "q1"):
            result = _stats.quantiles(numbers, n=4)[0]
        elif op in ("percentile_75", "q3"):
            result = _stats.quantiles(numbers, n=4)[2]
        elif op == "iqr":
            q = _stats.quantiles(numbers, n=4)
            result = q[2] - q[0]
        else:
            return f"Unknown operation '{operation}'"
        return f"{operation}({numbers}) = {round(result, 6)}"
    except Exception as e:
        return f"Error: {e}"


# ─── Financial calculator ─────────────────────────────────────────────────────

@tool
def financial_calc(operation: str, principal: float = 0, rate: float = 0,
                   time: float = 0, n: float = 12, extra: float = 0) -> str:
    """Financial calculations.

    Operations and their parameters:
    - compound_interest: principal=P, rate=annual% (e.g. 8 for 8%), time=years, n=compounds/year (default 12)
      Returns: final amount and interest earned
    - simple_interest: principal=P, rate=annual%, time=years
    - loan_payment: principal=loan amount, rate=annual%, time=years, n=payments/year (default 12)
      Returns: monthly payment and total cost
    - roi: principal=cost, extra=final_value
      Returns: ROI percentage
    - cagr: principal=initial, extra=final, time=years
      Returns: Compound Annual Growth Rate
    - inflation_adjusted: principal=amount, rate=inflation%, time=years
      Returns: real value after inflation
    - rule_of_72: rate=annual_interest%
      Returns: years to double the investment

    Rate should be in percent (e.g. 7.5 for 7.5%, not 0.075)"""
    op = operation.lower().replace(" ", "_").replace("-", "_")
    r = rate / 100

    try:
        if op == "compound_interest":
            amount = principal * (1 + r / n) ** (n * time)
            interest = amount - principal
            return (f"Principal: {principal:,.2f} | Rate: {rate}% | Time: {time}yr | Compounds: {int(n)}/yr\n"
                    f"Final amount: {amount:,.2f} | Interest earned: {interest:,.2f}")

        elif op == "simple_interest":
            interest = principal * r * time
            return (f"Principal: {principal:,.2f} | Rate: {rate}% | Time: {time}yr\n"
                    f"Interest: {interest:,.2f} | Total: {principal + interest:,.2f}")

        elif op == "loan_payment":
            r_period = r / n
            n_payments = n * time
            if r_period == 0:
                payment = principal / n_payments
            else:
                payment = principal * r_period * (1 + r_period) ** n_payments / ((1 + r_period) ** n_payments - 1)
            total = payment * n_payments
            return (f"Loan: {principal:,.2f} | Rate: {rate}% | Term: {time}yr\n"
                    f"Payment per period: {payment:,.2f} | Total paid: {total:,.2f} | Interest: {total - principal:,.2f}")

        elif op == "roi":
            if principal == 0:
                return "Error: principal (cost) cannot be zero"
            roi = (extra - principal) / principal * 100
            return f"Cost: {principal:,.2f} | Final: {extra:,.2f} | ROI: {roi:.2f}%"

        elif op == "cagr":
            if principal <= 0 or time <= 0:
                return "Error: principal and time must be positive"
            cagr = ((extra / principal) ** (1 / time) - 1) * 100
            return f"Initial: {principal:,.2f} | Final: {extra:,.2f} | Years: {time} | CAGR: {cagr:.2f}%"

        elif op == "inflation_adjusted":
            real = principal / (1 + r) ** time
            return (f"{principal:,.2f} today at {rate}% inflation over {time}yr "
                    f"= {real:,.2f} in today's value")

        elif op == "rule_of_72":
            if rate <= 0:
                return "Error: rate must be positive"
            years = 72 / rate
            return f"At {rate}% annual return, money doubles in ~{years:.1f} years (Rule of 72)"

        else:
            return ("Unknown operation. Supported: compound_interest, simple_interest, "
                    "loan_payment, roi, cagr, inflation_adjusted, rule_of_72")

    except Exception as e:
        return f"Error: {e}"


# ─── Currency conversion ──────────────────────────────────────────────────────

@tool
async def convert_currency(amount: float, from_currency: str, to_currency: str) -> str:
    """Convert between currencies using live exchange rates (via frankfurter.app).
    Supports all major currencies: USD, INR, EUR, GBP, JPY, AUD, CAD, SGD, AED, CHF, etc.
    Example: convert 100 USD to INR, convert 5000 INR to EUR, how much is 50 GBP in dollars"""
    src = from_currency.upper().strip()
    dst = to_currency.upper().strip()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"https://api.frankfurter.app/latest",
                params={"from": src, "to": dst},
            )
            resp.raise_for_status()
            data = resp.json()

        rate = data["rates"][dst]
        result = amount * rate
        date = data.get("date", "today")
        return (
            f"{amount:,.2f} {src} = {result:,.4f} {dst}  "
            f"(rate: 1 {src} = {rate} {dst}, as of {date})"
        )
    except httpx.HTTPStatusError as e:
        return f"Currency API error ({e.response.status_code}) — check currency codes."
    except KeyError:
        return f"Currency '{dst}' not found in response. Check the currency code."
    except Exception as e:
        return f"Error fetching exchange rate: {e}"


# ─── Timezone utilities ───────────────────────────────────────────────────────

# Common short aliases → IANA timezone names
_TZ_ALIASES: dict[str, str] = {
    "ist": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "est": "America/New_York",
    "edt": "America/New_York",
    "new york": "America/New_York",
    "cst": "America/Chicago",
    "chicago": "America/Chicago",
    "mst": "America/Denver",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "gmt": "UTC",
    "utc": "UTC",
    "london": "Europe/London",
    "bst": "Europe/London",
    "cet": "Europe/Paris",
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "dubai": "Asia/Dubai",
    "gst": "Asia/Dubai",
    "uae": "Asia/Dubai",
    "singapore": "Asia/Singapore",
    "sgt": "Asia/Singapore",
    "hkt": "Asia/Hong_Kong",
    "hong kong": "Asia/Hong_Kong",
    "jst": "Asia/Tokyo",
    "tokyo": "Asia/Tokyo",
    "japan": "Asia/Tokyo",
    "cst china": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "aest": "Australia/Sydney",
    "sydney": "Australia/Sydney",
    "australia": "Australia/Sydney",
    "nzst": "Pacific/Auckland",
    "auckland": "Pacific/Auckland",
    "msk": "Europe/Moscow",
    "moscow": "Europe/Moscow",
    "brt": "America/Sao_Paulo",
    "sao paulo": "America/Sao_Paulo",
}


def _resolve_tz(name: str) -> ZoneInfo:
    key = name.lower().strip()
    iana = _TZ_ALIASES.get(key, name)
    try:
        return ZoneInfo(iana)
    except ZoneInfoNotFoundError:
        # Try treating the original input as a direct IANA name
        raise ZoneInfoNotFoundError(
            f"Unknown timezone '{name}'. Use IANA names like 'Asia/Kolkata' "
            f"or aliases like IST, PST, GMT, Dubai, Tokyo, London."
        )


@tool
def get_time_in_timezone(timezone: str) -> str:
    """Get the current date and time in any timezone.
    Accepts IANA names (Asia/Kolkata, America/New_York) or aliases:
    IST, EST, PST, GMT, UTC, Dubai, Tokyo, London, Singapore, Sydney, etc.
    Example: what time is it in Tokyo, current time in New York, time in Dubai"""
    try:
        tz = _resolve_tz(timezone)
        now = datetime.now(tz)
        offset = now.strftime("%z")
        offset_fmt = f"UTC{offset[:3]}:{offset[3:]}" if offset else ""
        return (
            f"{timezone}: {now.strftime('%A, %B %d %Y  %I:%M %p')}  "
            f"({offset_fmt})"
        )
    except ZoneInfoNotFoundError as e:
        return str(e)


@tool
def convert_timezone(time_str: str, from_timezone: str, to_timezone: str) -> str:
    """Convert a specific time from one timezone to another.
    Time format: 'HH:MM' or 'HH:MM AM/PM' (uses today's date).
    Example: convert 9:30 AM IST to EST, what is 3 PM London in India, convert 14:00 UTC to Dubai"""
    try:
        tz_from = _resolve_tz(from_timezone)
        tz_to   = _resolve_tz(to_timezone)
    except ZoneInfoNotFoundError as e:
        return str(e)

    # Parse time string — try multiple formats
    today = datetime.now().date()
    parsed = None
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M", "%I %p", "%I%p"):
        try:
            t = datetime.strptime(time_str.strip().upper(), fmt)
            parsed = datetime(today.year, today.month, today.day,
                              t.hour, t.minute, tzinfo=tz_from)
            break
        except ValueError:
            continue

    if parsed is None:
        return f"Could not parse time '{time_str}'. Use formats like '3:30 PM', '14:00', '9 AM'."

    converted = parsed.astimezone(tz_to)
    return (
        f"{time_str} {from_timezone}  →  "
        f"{converted.strftime('%I:%M %p')} {to_timezone}  "
        f"({converted.strftime('%A, %d %b %Y')})"
    )

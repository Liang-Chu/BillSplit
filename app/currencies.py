# ISO 4217 code -> (minor units, name). Extend as needed.
CURRENCIES: dict[str, tuple[int, str]] = {
    "AUD": (2, "Australian Dollar"),
    "CAD": (2, "Canadian Dollar"),
    "CHF": (2, "Swiss Franc"),
    "CNY": (2, "Chinese Yuan"),
    "EUR": (2, "Euro"),
    "GBP": (2, "Pound Sterling"),
    "HKD": (2, "Hong Kong Dollar"),
    "INR": (2, "Indian Rupee"),
    "JPY": (0, "Japanese Yen"),
    "KRW": (0, "South Korean Won"),
    "MXN": (2, "Mexican Peso"),
    "NZD": (2, "New Zealand Dollar"),
    "SGD": (2, "Singapore Dollar"),
    "TWD": (2, "New Taiwan Dollar"),
    "USD": (2, "US Dollar"),
}


def minor_units(code: str) -> int:
    return CURRENCIES.get(code, (2, ""))[0]

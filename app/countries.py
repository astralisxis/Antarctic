"""Страны для каталога.

Ключ — ISO-код из двух букв, именно он уходит в фильтр country[] на LZT Market
(в верхнем регистре). Значение — название по-русски и телефонный префикс, из них
собирается заголовок позиции в магазине: «Индонезия +62».

Флагов-эмодзи здесь нет намеренно: интерфейс строго монохромный.
Список — кандидаты для проверки наличия (app.tools.lzt_check countries), а не
жёсткий перечень: неизвестный код просто отобразится как есть.
"""

from __future__ import annotations

from typing import NamedTuple


class Country(NamedTuple):
    name: str
    phone: str


COUNTRIES: dict[str, Country] = {
    "RU": Country("Россия", "+7"),
    "UA": Country("Украина", "+380"),
    "KZ": Country("Казахстан", "+7"),
    "BY": Country("Беларусь", "+375"),
    "UZ": Country("Узбекистан", "+998"),
    "KG": Country("Кыргызстан", "+996"),
    "TJ": Country("Таджикистан", "+992"),
    "TM": Country("Туркменистан", "+993"),
    "AZ": Country("Азербайджан", "+994"),
    "AM": Country("Армения", "+374"),
    "GE": Country("Грузия", "+995"),
    "MD": Country("Молдова", "+373"),
    "LT": Country("Литва", "+370"),
    "LV": Country("Латвия", "+371"),
    "EE": Country("Эстония", "+372"),
    "PL": Country("Польша", "+48"),
    "CZ": Country("Чехия", "+420"),
    "SK": Country("Словакия", "+421"),
    "HU": Country("Венгрия", "+36"),
    "RO": Country("Румыния", "+40"),
    "BG": Country("Болгария", "+359"),
    "RS": Country("Сербия", "+381"),
    "HR": Country("Хорватия", "+385"),
    "DE": Country("Германия", "+49"),
    "FR": Country("Франция", "+33"),
    "GB": Country("Великобритания", "+44"),
    "IE": Country("Ирландия", "+353"),
    "NL": Country("Нидерланды", "+31"),
    "BE": Country("Бельгия", "+32"),
    "AT": Country("Австрия", "+43"),
    "CH": Country("Швейцария", "+41"),
    "IT": Country("Италия", "+39"),
    "ES": Country("Испания", "+34"),
    "PT": Country("Португалия", "+351"),
    "SE": Country("Швеция", "+46"),
    "NO": Country("Норвегия", "+47"),
    "FI": Country("Финляндия", "+358"),
    "DK": Country("Дания", "+45"),
    "GR": Country("Греция", "+30"),
    "TR": Country("Турция", "+90"),
    "US": Country("США", "+1"),
    "CA": Country("Канада", "+1"),
    "MX": Country("Мексика", "+52"),
    "BR": Country("Бразилия", "+55"),
    "AR": Country("Аргентина", "+54"),
    "CO": Country("Колумбия", "+57"),
    "CL": Country("Чили", "+56"),
    "PE": Country("Перу", "+51"),
    "VE": Country("Венесуэла", "+58"),
    "IN": Country("Индия", "+91"),
    "PK": Country("Пакистан", "+92"),
    "BD": Country("Бангладеш", "+880"),
    "LK": Country("Шри-Ланка", "+94"),
    "NP": Country("Непал", "+977"),
    "ID": Country("Индонезия", "+62"),
    "MY": Country("Малайзия", "+60"),
    "SG": Country("Сингапур", "+65"),
    "TH": Country("Таиланд", "+66"),
    "VN": Country("Вьетнам", "+84"),
    "PH": Country("Филиппины", "+63"),
    "KH": Country("Камбоджа", "+855"),
    "MM": Country("Мьянма", "+95"),
    "CN": Country("Китай", "+86"),
    "HK": Country("Гонконг", "+852"),
    "TW": Country("Тайвань", "+886"),
    "JP": Country("Япония", "+81"),
    "KR": Country("Южная Корея", "+82"),
    "MN": Country("Монголия", "+976"),
    "IL": Country("Израиль", "+972"),
    "AE": Country("ОАЭ", "+971"),
    "SA": Country("Саудовская Аравия", "+966"),
    "IQ": Country("Ирак", "+964"),
    "IR": Country("Иран", "+98"),
    "EG": Country("Египет", "+20"),
    "MA": Country("Марокко", "+212"),
    "DZ": Country("Алжир", "+213"),
    "TN": Country("Тунис", "+216"),
    "NG": Country("Нигерия", "+234"),
    "KE": Country("Кения", "+254"),
    "GH": Country("Гана", "+233"),
    "ZA": Country("ЮАР", "+27"),
    "ET": Country("Эфиопия", "+251"),
    "TZ": Country("Танзания", "+255"),
    "UG": Country("Уганда", "+256"),
    "CM": Country("Камерун", "+237"),
    "CI": Country("Кот-д’Ивуар", "+225"),
    "SN": Country("Сенегал", "+221"),
    "AU": Country("Австралия", "+61"),
    "NZ": Country("Новая Зеландия", "+64"),
}


def title_for(code: str) -> str:
    """«Индонезия +62» для известного кода, сам код для неизвестного."""
    code = (code or "").strip().upper()
    country = COUNTRIES.get(code)
    return f"{country.name} {country.phone}" if country else code


def name_for(code: str) -> str:
    code = (code or "").strip().upper()
    country = COUNTRIES.get(code)
    return country.name if country else code

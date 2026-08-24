"""Карточка отзыва картинкой: фон-фото, круглая аватарка, звёзды, текст, товар.

Шаблон повторяет эскиз владельца: слева круглая аватарка, под ней ник
звёздочками, справа сверху оценка звёздами, ниже текст отзыва, справа внизу —
товар. Всё в рамке в один волосок.

Ник и звёзды сделаны как на баннере канала: заглавные буквы, тонкий тёмный
контур, мягкая тень под ним и металлическая заливка внутри — вертикальный
серый градиент с одной светлой полосой поперёк. Цвета остаются монохромными,
«металл» здесь только про светотень. На остальной текст приём не переносим:
абзац отзыва должен читаться, а не блестеть.

Фон — фотография магазина, по умолчанию `media/review_card_bg.jpg` (кладётся
руками или загружается на странице отзывов в админке). Фона нет — рисуем на
бумажном F6F6F4 с чёрным текстом: карточка просто светлая, шаблон тот же.
Тёмное фото и светлое требуют разного текста, поэтому цвет выбираем по средней
яркости фона, а само фото ровно притеняем — не градиентом, одним тоном, иначе
на снегу белые буквы не читаются. От яркости зависит и металл: на фотографии он
светлый, на бумаге — тёмная сталь, иначе буквы растворятся.

Звёзды рисуем многоугольником, а не символом ★: в Arial и Liberation этого
глифа нет, вместо оценки в канал уехали бы квадратики.

Pillow импортируем мягко: без пакета `available()` вернёт False, и публикация
останется прежней — аватарка файлом плюс подпись текстом.
"""

from __future__ import annotations

import io
import logging
import math
from functools import lru_cache
from pathlib import Path

from app.config import BASE_DIR, DATA_DIR, MEDIA_DIR

try:  # Pillow — единственная зависимость раздела, без неё карточки нет
    from PIL import (
        Image,
        ImageChops,
        ImageDraw,
        ImageFilter,
        ImageFont,
        ImageOps,
        ImageStat,
    )
except ImportError:  # pragma: no cover — окружение без картинок
    Image = ImageChops = ImageDraw = ImageFilter = None  # type: ignore[assignment]
    ImageFont = ImageOps = ImageStat = None  # type: ignore[assignment]

log = logging.getLogger("reviews.card")


class CardError(Exception):
    """Карточку нарисовать не удалось — публикация уйдёт прежним способом."""


# --- палитра интерфейса, один в один --------------------------------------- #
PAPER = (246, 246, 244)  # F6F6F4
INK = (17, 17, 17)  # 111111
DIM = (119, 119, 119)  # 777777
HAIR = (217, 217, 217)  # D9D9D9

# --- геометрия готовой картинки, в пикселях -------------------------------- #
W = 1280
H_MIN = 520
PAD = 76  # содержимое от края
FRAME = 28  # рамка от края
LINE = 2  # линии: после сжатия Telegram один пиксель пропадает совсем
AVA = 160
GAP = 64  # между аватаркой и колонкой текста
STAR = 56
STAR_GAP = 20
NAME_SIZE = 44
NAME_MIN = 24
NAME_MAX_W = AVA + 96  # ник чуть шире аватарки; длиннее — уменьшаем кегль
TEXT_SIZE = 32
TEXT_STEP = 44
PRODUCT_SIZE = 26
MAX_LINES = 14  # 500 символов укладываются в 10–11 строк, остальное — хвост
SHADE = 0.45  # ровное притенение фото под светлый текст
BG_DEFAULT = "media/review_card_bg.jpg"

# --- металл: контур, тень, градиент ---------------------------------------- #
OUTLINE = 1  # толщина контура вокруг буквы: волосок, как в панели
OUTLINE_FILTER = OUTLINE * 2 + 1  # MaxFilter принимает только нечётный размер
SHADOW_DROP = 6  # насколько тень уходит вниз
SHADOW_BLUR = 7
SHADOW_ALPHA = 0.55  # на фотографии
SHADOW_ALPHA_LIGHT = 0.3  # на бумаге чёрная тень была бы кляксой
# Полосы градиента: (доля высоты, яркость). Светлая полоса поперёк середины —
# тот самый блик, из-за которого буквы читаются как металл, а не как заливка.
METAL_BRIGHT = ((0.0, 252), (0.16, 214), (0.40, 126), (0.50, 250), (0.62, 166), (0.84, 94), (1.0, 226))
METAL_STEEL = ((0.0, 166), (0.16, 118), (0.40, 48), (0.50, 150), (0.62, 82), (0.84, 26), (1.0, 120))

CARDS_DIR = DATA_DIR / "reviews"

# Шрифт ищем в проекте, потом в системе. Helvetica в наборе нет ни у Windows,
# ни у Linux, поэтому берём метрически совместимые: Arial, Liberation, Arimo.
REGULAR: tuple[Path, ...] = (
    MEDIA_DIR / "fonts" / "Helvetica.ttf",
    MEDIA_DIR / "fonts" / "HelveticaNeue.ttf",
    MEDIA_DIR / "fonts" / "Arimo-Regular.ttf",
    MEDIA_DIR / "fonts" / "LiberationSans-Regular.ttf",
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/usr/share/fonts/truetype/croscore/Arimo-Regular.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
)
MEDIUM: tuple[Path, ...] = (
    MEDIA_DIR / "fonts" / "Helvetica-Bold.ttf",
    MEDIA_DIR / "fonts" / "HelveticaNeue-Medium.ttf",
    MEDIA_DIR / "fonts" / "Arimo-Bold.ttf",
    MEDIA_DIR / "fonts" / "LiberationSans-Bold.ttf",
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/croscore/Arimo-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
)

# Символ из области для частного использования: глифа нет ни в одном шрифте,
# значит его рамка — это «квадратик-заглушка». С ней и сравниваем русскую букву.
TOFU = "\ue123"


def available() -> bool:
    """Есть ли чем рисовать."""
    return Image is not None


# --------------------------------------------------------------------------- #
#  Шрифты
# --------------------------------------------------------------------------- #
def _has_cyrillic(font: object) -> bool:
    """Есть ли в шрифте русские буквы: иначе отзыв станет рядом квадратиков."""
    try:
        return font.getbbox("П") != font.getbbox(TOFU)  # type: ignore[attr-defined]
    except Exception:
        return False


@lru_cache(maxsize=32)
def _font(size: int, bold: bool = False):
    for path in MEDIUM if bold else REGULAR:
        try:
            font = ImageFont.truetype(str(path), size)
        except Exception:
            continue
        if _has_cyrillic(font):
            return font
        log.info("шрифт %s без кириллицы — пропускаю", path.name)
    # Резерв — встроенный в Pillow Aileron: рисунок близок к Helvetica, но
    # русских букв в нём нет. Положите TTF в media/fonts, если карточка нужна
    # на сервере без системных шрифтов.
    log.warning("подходящий шрифт не найден, беру встроенный — кириллицы в нём нет")
    return ImageFont.load_default(size=size)


def font_name() -> str:
    """Каким шрифтом рисуем — для подсказки в админке."""
    if not available():
        return "нет Pillow"
    font = _font(TEXT_SIZE)
    path = getattr(font, "path", None)
    if isinstance(path, str):
        return Path(path).name
    return "встроенный (без кириллицы)"


def _width(font: object, text: str) -> float:
    return float(font.getlength(text))  # type: ignore[attr-defined]


def _fit(text: str, size: int, max_w: float, *, bold: bool = True):
    """Подобрать кегль под ширину: ники бывают длинные, а место под ними одно."""
    while size > NAME_MIN:
        font = _font(size, bold=bold)
        if _width(font, text) <= max_w:
            return font
        size -= 2
    return _font(size, bold=bold)


# --------------------------------------------------------------------------- #
#  Текст
# --------------------------------------------------------------------------- #
def _split_word(word: str, font: object, max_w: float) -> list[str]:
    """Слово шире строки (ссылка, набор букв) режем по символам."""
    if _width(font, word) <= max_w:
        return [word]
    parts: list[str] = []
    current = ""
    for char in word:
        if current and _width(font, current + char) > max_w:
            parts.append(current)
            current = char
        else:
            current += char
    if current:
        parts.append(current)
    return parts


def _wrap(text: str | None, font: object, max_w: float) -> list[str]:
    """Разложить текст по строкам. Пустая строка между абзацами сохраняется."""
    lines: list[str] = []
    for paragraph in (text or "").splitlines():
        words: list[str] = []
        for word in paragraph.split():
            words += _split_word(word, font, max_w)
        if not words:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        current = ""
        for word in words:
            probe = f"{current} {word}".strip()
            if current and _width(font, probe) > max_w:
                lines.append(current)
                current = word
            else:
                current = probe
        if current:
            lines.append(current)
    while lines and lines[-1] == "":
        lines.pop()
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


# --------------------------------------------------------------------------- #
#  Фон
# --------------------------------------------------------------------------- #
def background_path(raw: str | None = None) -> Path:
    """Путь к фону: из настройки или дефолтный. Относительный — от корня."""
    path = Path((raw or "").strip() or BG_DEFAULT)
    return path if path.is_absolute() else BASE_DIR / path


def stored(raw: str | None) -> Path | None:
    """Уже нарисованная карточка по записанному пути. None — файла нет."""
    value = (raw or "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path if path.exists() else None


def save_background(blob: bytes, target: Path) -> tuple[int, int]:
    """Проверить загруженный файл и положить его фоном. Вернёт размер картинки.

    Перекодируем в JPEG: на диск попадает один предсказуемый файл, а не «png с
    расширением jpg», и фотография не тянет за собой альфа-канал.
    """
    if not available():
        raise CardError("Pillow не установлен — проверить картинку нечем")
    with Image.open(io.BytesIO(blob)) as src:
        photo = src.convert("RGB")
    target.parent.mkdir(parents=True, exist_ok=True)
    photo.save(target, format="JPEG", quality=92, optimize=True)
    return photo.size


def _canvas(size: tuple[int, int], background: Path | str | None):
    """Полотно: фото по центру с обрезкой под размер или ровная бумага."""
    if background:
        path = Path(background)
        if path.exists():
            try:
                with Image.open(path) as src:
                    photo = ImageOps.fit(
                        src.convert("RGB"), size, method=Image.LANCZOS, centering=(0.5, 0.5)
                    )
                return photo
            except Exception as exc:
                log.warning("фон карточки %s не читается: %s", path.name, exc)
    return Image.new("RGB", size, PAPER)


def _is_dark(image: object) -> bool:
    """Тёмный фон — светлые буквы. Порог по средней яркости."""
    try:
        return ImageStat.Stat(image.convert("L")).mean[0] < 140  # type: ignore[attr-defined]
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  Элементы
# --------------------------------------------------------------------------- #
def _star(draw: object, cx: float, cy: float, size: int, *, filled: bool, color: tuple | int) -> None:
    """Пятиконечная звезда: закрашенная — оценка, контурная — остаток до пяти."""
    outer = size / 2
    inner = outer * 0.42
    points = []
    for step in range(10):
        angle = math.radians(-90 + step * 36)
        radius = outer if step % 2 == 0 else inner
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    if filled:
        draw.polygon(points, fill=color)  # type: ignore[attr-defined]
    else:
        draw.polygon(points, outline=color, width=LINE)  # type: ignore[attr-defined]


def _ramp(height: int, dark: bool):
    """Вертикальная полоса металла на нужную высоту — из неё берётся заливка."""
    stops = METAL_BRIGHT if dark else METAL_STEEL
    strip = Image.new("L", (1, max(height, 1)))
    pixels = strip.load()
    for y in range(strip.height):
        share = y / max(strip.height - 1, 1)
        previous = stops[0]
        for stop in stops:
            if stop[0] >= share:
                span = stop[0] - previous[0]
                k = (share - previous[0]) / span if span else 0.0
                pixels[0, y] = int(previous[1] + (stop[1] - previous[1]) * k)
                break
            previous = stop
        else:  # pragma: no cover — доля всегда попадает в последний стоп
            pixels[0, y] = stops[-1][1]
    return strip


def _emboss(base: object, mask: object, *, dark: bool) -> None:
    """Тень, тонкий контур и металл по маске: так рисуются ник и звёзды.

    Маска — во весь размер карточки, в ней лежит одна группа фигур. Ник и звёзды
    красятся отдельными вызовами: градиент растягивается по высоте маски, и на
    общей маске звёздам достался бы верх полосы, а нику — низ, без блика.
    """
    size = base.size  # type: ignore[attr-defined]
    box = mask.getbbox()
    if not box:
        return

    # Тень: та же фигура ниже на пару пикселей, размытая и приглушённая.
    alpha = SHADOW_ALPHA if dark else SHADOW_ALPHA_LIGHT
    shadow = ImageChops.offset(mask, 0, SHADOW_DROP).filter(
        ImageFilter.GaussianBlur(SHADOW_BLUR)
    )
    shadow = shadow.point(lambda v: int(v * alpha))
    base.paste(Image.new("RGB", size, INK), (0, 0), shadow)  # type: ignore[attr-defined]

    # Контур: раздутая маска минус исходная — ровное кольцо в OUTLINE пикселей.
    ring = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(OUTLINE_FILTER)), mask)
    base.paste(Image.new("RGB", size, INK), (0, 0), ring)  # type: ignore[attr-defined]

    # Заливка: градиент растянут по высоте самих фигур, а не всей карточки —
    # иначе внутри ника оказался бы один ровный тон.
    top, bottom = box[1], box[3]
    strip = _ramp(bottom - top, dark).resize((size[0], max(bottom - top, 1)))
    metal = Image.new("L", size, strip.getpixel((0, 0)))
    metal.paste(strip, (0, top))
    base.paste(metal.convert("RGB"), (0, 0), mask)  # type: ignore[attr-defined]


def _avatar(base: object, draw: object, blob: bytes | None, x: int, y: int, letter: str,
            *, dim: tuple, hair: tuple) -> None:
    """Круглая аватарка. Нет фотографии — круг с первой буквой ника."""
    drawn = False
    if blob:
        try:
            with Image.open(io.BytesIO(blob)) as src:
                face = ImageOps.fit(
                    src.convert("RGB"), (AVA, AVA), method=Image.LANCZOS, centering=(0.5, 0.5)
                )
            # Маску рисуем в четыре раза крупнее и уменьшаем: край круга
            # получается сглаженным, без пилы.
            big = Image.new("L", (AVA * 4, AVA * 4), 0)
            ImageDraw.Draw(big).ellipse((0, 0, AVA * 4 - 1, AVA * 4 - 1), fill=255)
            base.paste(face, (x, y), big.resize((AVA, AVA), Image.LANCZOS))  # type: ignore[attr-defined]
            drawn = True
        except Exception as exc:
            log.info("аватарку в карточку не вставить: %s", exc)
    if not drawn and letter:
        draw.text(  # type: ignore[attr-defined]
            (x + AVA // 2, y + AVA // 2),
            letter.upper(),
            font=_font(int(AVA * 0.42), bold=True),
            fill=dim,
            anchor="mm",
        )
    draw.ellipse((x, y, x + AVA - 1, y + AVA - 1), outline=hair, width=LINE)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
#  Карточка
# --------------------------------------------------------------------------- #
def render(
    *,
    name: str,
    stars: int,
    text: str | None,
    product: str | None,
    avatar: bytes | None = None,
    background: Path | str | None = None,
) -> bytes:
    """Нарисовать карточку и вернуть PNG байтами."""
    if not available():
        raise CardError("Pillow не установлен — карточку рисовать нечем")

    f_text = _font(TEXT_SIZE)
    f_product = _font(PRODUCT_SIZE)

    # Ник — заглавными, как на баннере канала; под аватаркой места немного,
    # поэтому кегль подбирается по длине.
    name_text = (name or "").upper()
    f_name = _fit(name_text, NAME_SIZE, NAME_MAX_W)

    col_x = PAD + AVA + GAP
    col_w = W - PAD - col_x
    lines = _wrap(text, f_text, col_w)

    stars_top = PAD + 10
    body_top = stars_top + STAR + 46
    body_h = len(lines) * TEXT_STEP
    product_h = PRODUCT_SIZE + 10 if product else 0
    height = max(H_MIN, int(body_top + body_h + 44 + product_h + PAD))

    image = _canvas((W, height), background)
    dark = _is_dark(image)
    if dark:
        # Ровное притенение: фотография остаётся видна, буквы читаются.
        image = Image.blend(image, Image.new("RGB", image.size, (0, 0, 0)), SHADE)
    ink = PAPER if dark else INK
    dim = HAIR if dark else DIM
    hair = HAIR

    draw = ImageDraw.Draw(image)
    draw.rectangle((FRAME, FRAME, W - FRAME - 1, height - FRAME - 1), outline=hair, width=LINE)

    letter = next((ch for ch in (name or "") if ch.isalnum()), "")
    _avatar(image, draw, avatar, PAD, PAD, letter, dim=dim, hair=hair)

    value = max(1, min(int(stars or 0), 5))

    # Ник и звёзды красим металлом: каждой группе своя маска, чтобы блик
    # градиента прошёл и по буквам, и по звёздам.
    plate = Image.new("L", (W, height), 0)
    ImageDraw.Draw(plate).text(
        (PAD + AVA // 2, PAD + AVA + 18), name_text, font=f_name, fill=255, anchor="ma"
    )
    _emboss(image, plate, dark=dark)

    plate = Image.new("L", (W, height), 0)
    pen = ImageDraw.Draw(plate)
    for index in range(5):
        cx = col_x + STAR / 2 + index * (STAR + STAR_GAP)
        _star(pen, cx, stars_top + STAR / 2, STAR, filled=index < value, color=255)
    _emboss(image, plate, dark=dark)

    y = body_top
    for line in lines:
        if line:
            draw.text((col_x, y), line, font=f_text, fill=ink)
        y += TEXT_STEP

    if product:
        draw.text(
            (W - PAD, height - PAD), product, font=f_product, fill=dim, anchor="rd"
        )

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def card_path(review_id: int) -> Path:
    return CARDS_DIR / f"review_{review_id}.png"


def render_file(path: Path, **kwargs) -> Path:
    """Нарисовать и положить файлом. Путь возвращаем, чтобы записать в отзыв."""
    data = render(**kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path

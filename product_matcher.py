from dataclasses import dataclass
from io import BytesIO
import re
from typing import Optional

from PIL import Image

_STOPWORDS: set[str] = {
    "для", "та", "і", "й", "з", "із", "зі", "у", "в", "на", "по", "до", "від", "без", "під", "над", "при",
    "про", "що", "як", "або", "чи", "це", "той", "ця", "ті", "свій", "все", "всі",
    "и", "с", "со", "к", "о", "от", "об", "за", "из", "не", "но", "да",
    "акція", "акция", "знижка", "скидка", "розпродаж", "распродажа", "хіт", "хит", "топ", "новинка",
    "купити", "купить", "замовити", "заказать", "ціна", "цена", "грн", "uah", "шт", "оплата", "отриманні",
    "доставка", "нова", "пошта", "безкоштовна", "сьогодні", "тільки", "оригінал", "оригинал", "якість",
    "опт", "оптом", "дроп", "дропшипінг", "дропшиппинг", "роздріб", "розница", "ррц", "в", "наявності",
    "бестселери", "бестселлеры", "каталог", "акційні", "акционные", "товари", "товары",
    "новинки", "магазин", "головна", "главная", "категорії", "категории", "всі",
    "collections", "collection", "catalog", "category", "bestsellers", "shop", "store",
    "comfy", "rozetka", "prom", "olx",
    "the", "and", "for", "with", "pro", "new", "sale", "hot", "best", "promo", "buy", "now", "free", "shipping",
}

ACCESSORY_TRAP_WORDS: set[str] = {
    "кронштейн", "кріплення", "крепление", "кабель", "шнур", "перехідник", "переходник",
    "адаптер", "насадка", "насадки", "чохол", "чехол", "плівка", "пленка", "скло", "стекло",
    "ремінець", "ремешок", "батарейка", "запчастина", "запчасть", "тримач", "держатель",
    "зарядка", "чохли", "коробка", "запасний", "запасная", "змінний", "сменный",
    "bracket", "cable", "adapter", "case", "strap", "cover", "replacement", "film", "mount", "spare",
}

_PROMO_STRIP_RE = re.compile(
    r"[-–—]?\s*\d{1,2}\s*%"
    r"|акція|акция|знижка|скидка|розпродаж|хіт\s+продаж[уів]*|новинка|до\s+кінця\s+дня"
    r"|купити\s+в\s+україні|оплата\s+при\s+отриманні|нова\s+пошта",
    re.IGNORECASE,
)


@dataclass
class MatchVerdict:
    """Result of evaluating whether a candidate retail/wholesale listing matches the reference ad product."""

    is_match: bool
    score: float
    label: str
    token_overlap: float
    visual_similarity: Optional[float]
    reason: str


def _token_stem(tok: str) -> str:
    """Apply lightweight stem truncation for Slavic/English inflections without external deps."""
    if len(tok) <= 4:
        return tok
    return tok[:5] if len(tok) >= 6 else tok[:4]


def normalize_product_tokens(text: str) -> list[str]:
    """Extract ordered meaningful alphanumeric tokens from a product title across UA/RU/EN."""
    cleaned = _PROMO_STRIP_RE.sub(" ", text or "")
    raw_tokens = re.findall(r"[a-zа-яіїєґ0-9]+(?:-[a-zа-яіїєґ0-9]+)*", cleaned.lower())
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw_tokens:
        if len(tok) < 2 or tok in _STOPWORDS or tok.isdigit():
            continue
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def extract_model_tokens(text: str) -> set[str]:
    """Extract likely SKU/model/spec codes containing letters and digits (e.g. l-fan003, nm870, 12000pa)."""
    raw = re.findall(r"\b[a-zа-яіїєґ0-9]+(?:-[a-zа-яіїєґ0-9]+)*\b", (text or "").lower())
    models: set[str] = set()
    for tok in raw:
        has_alpha = any("a" <= ch <= "z" for ch in tok)
        has_digit = any(ch.isdigit() for ch in tok)
        if has_alpha and has_digit and len(tok) >= 3:
            models.add(tok)
    return models


def extract_core_phrase(text: str, max_words: int = 4) -> str:
    """Build a crisp 2-4 word search phrase preserving core nouns and model tokens."""
    tokens = normalize_product_tokens(text)
    if not tokens:
        return ""
    models = extract_model_tokens(text)
    selected: list[str] = []
    for tok in tokens:
        if tok not in models and len(selected) < max_words:
            selected.append(tok)
    for m in sorted(models):
        if m not in selected:
            if len(selected) >= max_words:
                selected[-1] = m
            else:
                selected.append(m)
            break
    return " ".join(selected[:max_words])


def is_valid_core_phrase(core_phrase: str, min_tokens: int = 2) -> bool:
    """Return True if core_phrase carries at least `min_tokens` meaningful non-catalog words."""
    return len(normalize_product_tokens(core_phrase or "")) >= min_tokens


def compute_dhash(image_bytes: bytes) -> Optional[int]:
    """Compute a 64-bit horizontal difference hash (dHash) using Pillow."""
    if not image_bytes:
        return None
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            gray = img.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
            pixels = list(gray.tobytes())
    except Exception:
        return None

    bits = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if pixels[offset + col] < pixels[offset + col + 1] else 0)
    return bits


def dhash_similarity(h1: Optional[int], h2: Optional[int]) -> Optional[float]:
    """Return normalized [0.0, 1.0] similarity from two 64-bit dHash integers."""
    if h1 is None or h2 is None:
        return None
    hamming = (h1 ^ h2).bit_count()
    return round(1.0 - (hamming / 64.0), 4)


def _token_overlap_score(ref_tokens: list[str], cand_tokens: list[str]) -> tuple[float, float]:
    """Compute (recall_against_ref, balanced_jaccard_pct) supporting Ukrainian inflection stems."""
    if not ref_tokens or not cand_tokens:
        return 0.0, 0.0
    ref_stems = {_token_stem(t) for t in ref_tokens}
    cand_stems = {_token_stem(t) for t in cand_tokens}

    matched_ref = 0
    for r in ref_tokens:
        rs = _token_stem(r)
        if r in cand_tokens or rs in cand_stems:
            matched_ref += 1

    recall = matched_ref / max(1, len(ref_tokens))
    union = len(ref_stems | cand_stems)
    jaccard = len(ref_stems & cand_stems) / max(1, union)
    text_score = (0.70 * recall + 0.30 * jaccard) * 100.0
    return round(recall, 4), round(min(100.0, text_score), 2)


def evaluate_product_match(
    ref_title: str,
    cand_title: str,
    *,
    ref_price_uah: Optional[float] = None,
    cand_price_uah: Optional[float] = None,
    candidate_role: str = "wholesale",
    ref_dhash: Optional[int] = None,
    cand_dhash: Optional[int] = None,
    min_score: float = 48.0,
) -> MatchVerdict:
    """Enforce Stage 4 Exact Product Match Guard before pairing any retail/wholesale offer."""
    ref_tokens = normalize_product_tokens(ref_title)
    cand_tokens = normalize_product_tokens(cand_title)
    vis = dhash_similarity(ref_dhash, cand_dhash)
    recall, text_score = _token_overlap_score(ref_tokens, cand_tokens)

    # 1. Accessory trap guard
    ref_stems = {_token_stem(t) for t in ref_tokens}
    trap_hits = [
        t for t in cand_tokens
        if t in ACCESSORY_TRAP_WORDS and t not in ref_tokens and _token_stem(t) not in ref_stems
    ]
    if trap_hits:
        return MatchVerdict(
            is_match=False,
            score=10.0,
            label="Rejected: Accessory Trap",
            token_overlap=recall,
            visual_similarity=vis,
            reason=f"Candidate looks like an accessory/part ({', '.join(trap_hits[:2])})",
        )

    # 2. Explicit model/SKU code conflict check
    ref_models = extract_model_tokens(ref_title)
    cand_models = extract_model_tokens(cand_title)
    shared_models = ref_models & cand_models
    if ref_models and cand_models and not shared_models:
        return MatchVerdict(
            is_match=False,
            score=15.0,
            label="Rejected: Model Mismatch",
            token_overlap=recall,
            visual_similarity=vis,
            reason=f"Model mismatch: expected {sorted(ref_models)}, got {sorted(cand_models)}",
        )
    if shared_models:
        text_score = min(100.0, text_score + 18.0)

    # 3. Price sanity guard
    if ref_price_uah is not None and ref_price_uah > 0 and cand_price_uah is not None and cand_price_uah > 0:
        if candidate_role == "wholesale":
            if cand_price_uah >= ref_price_uah * 0.95:
                return MatchVerdict(
                    is_match=False,
                    score=18.0,
                    label="Rejected: Price Sanity",
                    token_overlap=recall,
                    visual_similarity=vis,
                    reason=f"Wholesale candidate {cand_price_uah}₴ is not meaningfully below retail {ref_price_uah}₴",
                )
            if cand_price_uah < ref_price_uah * 0.08 and not shared_models:
                return MatchVerdict(
                    is_match=False,
                    score=16.0,
                    label="Rejected: Price Sanity",
                    token_overlap=recall,
                    visual_similarity=vis,
                    reason=f"Suspiciously cheap item ({cand_price_uah}₴ vs {ref_price_uah}₴ retail)",
                )
        elif candidate_role == "retail":
            if (cand_price_uah < ref_price_uah * 0.15 or cand_price_uah > ref_price_uah * 4.5) and not shared_models:
                return MatchVerdict(
                    is_match=False,
                    score=20.0,
                    label="Rejected: Price Sanity",
                    token_overlap=recall,
                    visual_similarity=vis,
                    reason=f"Retail outlier ({cand_price_uah}₴ vs {ref_price_uah}₴)",
                )

    # 4. Visual similarity gate & blend
    final_score = text_score
    if vis is not None:
        if vis < 0.55 and (recall < 0.75 or not shared_models):
            return MatchVerdict(
                is_match=False,
                score=round(0.5 * text_score + 0.5 * (vis * 100.0), 2),
                label="Rejected: Visual Mismatch",
                token_overlap=recall,
                visual_similarity=vis,
                reason=f"Visual dHash similarity too low ({vis:.2f})",
            )
        final_score = round(0.72 * text_score + 0.28 * (vis * 100.0), 2)

    is_match = final_score >= min_score and (recall >= 0.34 or bool(shared_models))
    if not is_match:
        return MatchVerdict(
            is_match=False,
            score=final_score,
            label="Rejected: Low Similarity",
            token_overlap=recall,
            visual_similarity=vis,
            reason=f"Title overlap {recall:.0%} / score {final_score:.1f} below threshold {min_score:.0f}",
        )

    label = "Exact Verified" if (final_score >= 72.0 or shared_models) else "Good Match"
    return MatchVerdict(
        is_match=True,
        score=final_score,
        label=label,
        token_overlap=recall,
        visual_similarity=vis,
        reason="Passed token, price, and visual match gates",
    )

from io import BytesIO
import unittest

from PIL import Image, ImageDraw

from product_matcher import (
    compute_dhash,
    dhash_similarity,
    evaluate_product_match,
    extract_core_phrase,
    extract_model_tokens,
)


def _make_png_bytes(pattern: str) -> bytes:
    img = Image.new("RGB", (64, 64), (20, 30, 50))
    draw = ImageDraw.Draw(img)
    if pattern == "lamp":
        draw.ellipse((14, 14, 50, 50), fill=(255, 180, 40))
    elif pattern == "lamp_small":
        img = img.resize((48, 48))
        d2 = ImageDraw.Draw(img)
        d2.ellipse((10, 10, 38, 38), fill=(250, 175, 45))
    else:
        for y in range(64):
            for x in range(64):
                if ((x // 8) + (y // 8)) % 2 == 0:
                    img.putpixel((x, y), (255, 255, 255))
                else:
                    img.putpixel((x, y), (10, 10, 10))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestProductMatcher(unittest.TestCase):
    def test_extract_core_phrase_and_model_tokens(self) -> None:
        raw = "Акція -50%! Ударний масажер Lenovo L-FAN003 Нова Пошта"
        models = extract_model_tokens(raw)
        self.assertIn("l-fan003", models)
        phrase = extract_core_phrase(raw)
        self.assertIn("масажер", phrase)
        self.assertIn("l-fan003", phrase)
        self.assertNotIn("акція", phrase)

    def test_exact_match_passes_and_accessory_trap_rejects(self) -> None:
        good = evaluate_product_match(
            "Вуличний світильник на сонячній батареї 100LED",
            "Світильник вуличний на сонячній батареї 100LED оптом",
            ref_price_uah=690.0,
            cand_price_uah=290.0,
            candidate_role="wholesale",
        )
        self.assertTrue(good.is_match)
        self.assertGreaterEqual(good.score, 70.0)

        trap = evaluate_product_match(
            "Вуличний світильник на сонячній батареї 100LED",
            "Кронштейн для вуличного світильника металевий",
            ref_price_uah=690.0,
            cand_price_uah=210.0,
            candidate_role="wholesale",
        )
        self.assertFalse(trap.is_match)
        self.assertEqual(trap.label, "Rejected: Accessory Trap")

    def test_model_mismatch_and_price_sanity_reject(self) -> None:
        model_diff = evaluate_product_match(
            "Масажер Lenovo L-FAN003",
            "Масажер Lenovo X900",
            ref_price_uah=1400.0,
            cand_price_uah=600.0,
            candidate_role="wholesale",
        )
        self.assertFalse(model_diff.is_match)
        self.assertEqual(model_diff.label, "Rejected: Model Mismatch")

        too_cheap = evaluate_product_match(
            "Кухонний стелаж органайзер 3 полиці",
            "Кухонний гачок органайзер пластиковий",
            ref_price_uah=3200.0,
            cand_price_uah=57.0,
            candidate_role="wholesale",
        )
        self.assertFalse(too_cheap.is_match)
        self.assertEqual(too_cheap.label, "Rejected: Price Sanity")

    def test_pillow_dhash_visual_match_and_mismatch(self) -> None:
        h_lamp1 = compute_dhash(_make_png_bytes("lamp"))
        h_lamp2 = compute_dhash(_make_png_bytes("lamp_small"))
        h_other = compute_dhash(_make_png_bytes("split"))

        self.assertIsNotNone(h_lamp1)
        sim_same = dhash_similarity(h_lamp1, h_lamp2)
        sim_diff = dhash_similarity(h_lamp1, h_other)
        self.assertGreater(sim_same, 0.75)
        self.assertLess(sim_diff, 0.55)

        vis_reject = evaluate_product_match(
            "Лампа LED",
            "Лампа LED",
            ref_price_uah=900.0,
            cand_price_uah=400.0,
            ref_dhash=h_lamp1,
            cand_dhash=h_other,
        )
        self.assertFalse(vis_reject.is_match)
        self.assertEqual(vis_reject.label, "Rejected: Visual Mismatch")


if __name__ == "__main__":
    unittest.main()

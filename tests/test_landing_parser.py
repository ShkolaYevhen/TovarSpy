import unittest
from unittest.mock import AsyncMock, MagicMock

from landing_parser import (
    detect_cod_lander_dna,
    detect_platform,
    fetch_landing_page,
    fetch_landing_page_fast,
    parse_landing_html,
    parse_landings,
)
import httpx


class TestLandingParser(unittest.IsolatedAsyncioTestCase):
    def test_shopify_usd_and_relative_image(self) -> None:
        html = """
        <html>
          <head>
            <meta property="og:title" content="Portable Blender Pro" />
            <meta property="product:price:amount" content="39.99" />
            <meta property="product:price:currency" content="USD" />
            <script src="https://cdn.shopify.com/s/files/1/theme.js"></script>
          </head>
          <body>
            <h1>Ignored H1</h1>
            <img class="product-image" data-src="/images/blender.jpg" />
          </body>
        </html>
        """
        result = parse_landing_html(html, "https://store.example.com/products/blender")
        self.assertEqual(result.platform, "Shopify")
        self.assertEqual(result.title, "Portable Blender Pro")
        self.assertAlmostEqual(result.price or 0.0, 39.99)
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.image_url, "https://store.example.com/images/blender.jpg")

    def test_cartum_horoshop_uah(self) -> None:
        html = """
        <html>
          <head>
            <meta name="generator" content="Horoshop CMS" />
            <meta property="og:image" content="/content/images/lamp.webp" />
          </head>
          <body>
            <h1>Лампа Захід Сонця LED</h1>
            <div class="product-price">1 299,50 грн</div>
          </body>
        </html>
        """
        result = parse_landing_html(html, "https://shop.com.ua/lamp")
        self.assertEqual(result.platform, "Cartum/Horoshop")
        self.assertEqual(result.title, "Лампа Захід Сонця LED")
        self.assertAlmostEqual(result.price or 0.0, 1299.50)
        self.assertEqual(result.currency, "UAH")
        self.assertEqual(result.image_url, "https://shop.com.ua/content/images/lamp.webp")

    def test_tilda_pln(self) -> None:
        html = """
        <html>
          <head>
            <script src="https://static.tildacdn.com/js/tilda-scripts.min.js"></script>
            <title>Posture Corrector</title>
          </head>
          <body data-tilda-project-id="42">
            <div class="t-store__card__price">149,00 zł</div>
            <img src="https://static.tildacdn.com/tild123/corrector.png" />
          </body>
        </html>
        """
        result = parse_landing_html(html, "https://promo.tilda.ws/corrector")
        self.assertEqual(result.platform, "Tilda")
        self.assertEqual(result.title, "Posture Corrector")
        self.assertAlmostEqual(result.price or 0.0, 149.00)
        self.assertEqual(result.currency, "PLN")
        self.assertEqual(result.image_url, "https://static.tildacdn.com/tild123/corrector.png")

    def test_custom_php_eur_json_ld(self) -> None:
        html = """
        <html>
          <head>
            <script type="application/ld+json">
              {
                "@type": "Product",
                "image": "assets/vacuum.jpg",
                "offers": {"price": "29.95", "priceCurrency": "EUR"}
              }
            </script>
          </head>
          <body>
            <h1>Mini Vacuum</h1>
            <form action="order.php" method="post"></form>
          </body>
        </html>
        """
        result = parse_landing_html(html, "https://fastdrop.eu/offer.php")
        self.assertEqual(detect_platform(html, "https://fastdrop.eu/offer.php"), "Custom PHP")
        self.assertEqual(result.platform, "Custom PHP")
        self.assertAlmostEqual(result.price or 0.0, 29.95)
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(result.image_url, "https://fastdrop.eu/assets/vacuum.jpg")

    async def test_fetch_landing_page_with_mocked_playwright_browser(self) -> None:
        html = """
        <html>
          <head>
            <meta property="og:title" content="Redirected Offer" />
            <meta property="og:image" content="https://cdn.example.com/offer.png" />
            <script src="https://cdn.shopify.com/s/files/theme.js"></script>
          </head>
          <body><span class="price">$45.00</span></body>
        </html>
        """
        mock_page = MagicMock()
        mock_page.url = "https://real-store.com/products/offer"
        mock_page.goto = AsyncMock()
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.content = AsyncMock(return_value=html)
        mock_page.close = AsyncMock()

        mock_context = MagicMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        mock_browser = MagicMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        data = await fetch_landing_page("https://cloaker.example/go", browser=mock_browser)
        self.assertEqual(data.url, "https://cloaker.example/go")
        self.assertEqual(data.final_url, "https://real-store.com/products/offer")
        self.assertEqual(data.title, "Redirected Offer")
        self.assertAlmostEqual(data.price or 0.0, 45.0)
        self.assertEqual(data.currency, "USD")
        self.assertEqual(data.platform, "Shopify")

    async def test_parse_landings_mock_mode(self) -> None:
        urls = [
            "https://smartrelax-shop.pl/products/ems-neck-massager",
            "https://homeglow.com.ua/sunset-projection-lamp",
        ]
        results = await parse_landings(urls, mock=True)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].currency, "PLN")
        self.assertEqual(results[1].platform, "Cartum/Horoshop")

    async def test_stage2_core_phrase_old_price_skip_and_fast_fetch(self) -> None:
        html = """
        <html>
          <head><meta property="og:title" content="Акція -50%! Ударний масажер Lenovo L-FAN003 Нова Пошта" /></head>
          <body>
            <img src="https://shop.ua/logo.svg" />
            <img class="product-image" src="https://shop.ua/media/lenovo-lfan003.jpg" />
            <del>2 400 грн</del>
            <div class="current-price">від 99 ₴ /міс 1 290 грн</div>
          </body>
        </html>
        """
        parsed = parse_landing_html(html, "https://shop.ua/lenovo")
        self.assertEqual(parsed.model_code, "l-fan003")
        self.assertIn("масажер", parsed.core_phrase)
        self.assertNotIn("акція", parsed.core_phrase)
        self.assertAlmostEqual(parsed.price or 0.0, 1290.0)
        self.assertEqual(parsed.currency, "UAH")
        self.assertEqual(parsed.image_url, "https://shop.ua/media/lenovo-lfan003.jpg")

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=html)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fast = await fetch_landing_page_fast("https://shop.ua/lenovo", client=client)
        self.assertAlmostEqual(fast.price or 0.0, 1290.0)
        self.assertEqual(fast.model_code, "l-fan003")

    def test_cod_lander_dna_reference_archetypes_and_pixel_microprice_fixes(self) -> None:
        # Archetype 1: massgun.domely.com.ua (FB pixel first <img>, title with "| 268 грн", COD form + Nova Poshta)
        massgun_html = """
        <html>
          <head>
            <title>Перкусійний масажер для всього тіла | 268 грн</title>
            <meta property="og:title" content="Перкусійний масажер для всього тіла | 268 грн" />
          </head>
          <body>
            <img src="https://www.facebook.com/tr?id=1586287526339410&ev=PageView&noscript=1" />
            <del>1199 грн</del><span class="promo-price">699 грн</span>
            <p>Доставка Нова Пошта, оплата при отриманні</p>
            <form><input name="name" /><input name="phone" type="tel" /></form>
          </body>
        </html>
        """
        p1 = parse_landing_html(
            massgun_html,
            "https://massgun.domely.com.ua/",
            fallback_image_url="https://cdn.tyver.example/massgun.jpg",
        )
        self.assertTrue(p1.is_cod_lander)
        self.assertGreaterEqual(p1.cod_score, 80)
        self.assertEqual(p1.title, "Перкусійний масажер для всього тіла")
        self.assertAlmostEqual(p1.price or 0.0, 699.0)
        self.assertEqual(p1.image_url, "https://cdn.tyver.example/massgun.jpg")

        # Archetype 2: testishop.shop/p/0179 (no og:title, H1 real product, micro-price 9 UAH / 60 UAH delivery skipped)
        testishop_html = """
        <html>
          <head><title>Каталог</title></head>
          <body>
            <h1>Роликовий масажер Massager Neck для шиї з підігрівом</h1>
            <img src="https://cdn.example.com/pixel.png" width="1" height="1" />
            <img src="/api/quick-tests/images/0179" />
            <span class="badge">9₴</span>
            <span>Доставка 60 грн</span>
            <del>1490 грн</del>
            <div class="price-new">899 грн</div>
            <p>Оплата при отриманні, відправка сьогодні</p>
            <form><input name="name" placeholder="Ім'я"/><input name="phone" /></form>
          </body>
        </html>
        """
        p2 = parse_landing_html(testishop_html, "https://testishop.shop/p/0179")
        self.assertTrue(p2.is_cod_lander)
        self.assertFalse(p2.is_generic_catalog)
        self.assertEqual(p2.title, "Роликовий масажер Massager Neck для шиї з підігрівом")
        self.assertAlmostEqual(p2.price or 0.0, 899.0)
        self.assertEqual(p2.image_url, "https://testishop.shop/api/quick-tests/images/0179")

        # Archetype 3: legs.zario.space (subdomain .space + _csrf + name/phone COD form)
        zario_html = """
        <html>
          <head><title>Массажний електричний коврик EMS Foot Mat</title></head>
          <body>
            <form><input type="hidden" name="_csrf" /><input name="name" /><input name="phone" /></form>
            <div class="price">499 грн</div>
            <img src="/assets/ems-mat.webp" />
          </body>
        </html>
        """
        p3 = parse_landing_html(zario_html, "https://legs.zario.space/")
        self.assertTrue(p3.is_cod_lander)
        self.assertIn("cod_order_form", p3.cod_signals)
        self.assertIn("dropship_url_dna", p3.cod_signals)

    def test_generic_catalog_titles_rejected(self) -> None:
        for header in ("Бестселери", "Каталог", "Акційні товари"):
            html = f"<html><head><title>{header}</title></head><body><h1>{header}</h1><span class='price'>9₴</span></body></html>"
            parsed = parse_landing_html(html, "https://pirnay.ua/collections/all")
            self.assertTrue(parsed.is_generic_catalog, f"{header} must be flagged as generic catalog")
            self.assertFalse(parsed.is_cod_lander)
            self.assertIsNone(parsed.price)
            self.assertEqual(parsed.core_phrase, "")
            dna = detect_cod_lander_dna(html, "https://pirnay.ua/collections/all")
            self.assertTrue(dna["is_generic_catalog"])

    def test_soft_404_courses_filter_urls_and_anatomic_goodstorre_dna(self) -> None:
        soft_404 = "<html><head><title>Сторінку не знайдено</title></head><body><h1>404</h1></body></html>"
        p404 = parse_landing_html(soft_404, "https://tandem-greenshop.com.ua/item")
        self.assertTrue(p404.is_generic_catalog)
        self.assertFalse(p404.is_cod_lander)

        course_html = """
        <html><head><title>3D-друк з нуля за 5 днів — до готової моделі</title></head>
        <body><p>Модуль 1, тариф базовий, ментор</p><span class="price">2900 грн</span></body></html>
        """
        pcourse = parse_landing_html(course_html, "https://druk440.vladpro3d.com/")
        self.assertTrue(pcourse.is_generic_catalog)
        self.assertFalse(pcourse.is_cod_lander)

        filter_html = """
        <html><head><title>Постільні комплекти сатин</title></head>
        <body><form><input name="name"/><input name="phone"/></form><span class="price">2200 грн</span></body></html>
        """
        pfilter = parse_landing_html(filter_html, "https://drimland.com.ua/postilni-komplekty/filter/")
        self.assertTrue(pfilter.is_generic_catalog)
        self.assertFalse(pfilter.is_cod_lander)

        # Archetype 4: anatomic.goodstorre.shop (tel form without name input, 70 грн shipping ignored, relative webp)
        anatomic_html = """
        <html>
          <head><title>Термобілизна Anatomic — тепла флісова</title></head>
          <body>
            <img src="assets/logo.png" />
            <img src="assets/0339693cc8367bd71e1d.jpg.webp" />
            <span>Доставка 70 грн Нова Пошта, оплата після отримання</span>
            <del>2499 грн</del>
            <div class="new-price">1 798 ₴</div>
            <form><input type="tel" name="phone" /><button type="submit">Замовити</button></form>
          </body>
        </html>
        """
        p4 = parse_landing_html(anatomic_html, "https://anatomic.goodstorre.shop/")
        self.assertTrue(p4.is_cod_lander)
        self.assertFalse(p4.is_generic_catalog)
        self.assertGreaterEqual(p4.cod_score, 90)
        self.assertAlmostEqual(p4.price or 0.0, 1798.0)
        self.assertEqual(p4.image_url, "https://anatomic.goodstorre.shop/assets/0339693cc8367bd71e1d.jpg.webp")


if __name__ == "__main__":
    unittest.main()

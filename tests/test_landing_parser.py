import unittest
from unittest.mock import AsyncMock, MagicMock

from landing_parser import (
    detect_platform,
    fetch_landing_page,
    parse_landing_html,
    parse_landings,
)


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


if __name__ == "__main__":
    unittest.main()

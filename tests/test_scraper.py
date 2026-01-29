import unittest
from bs4 import BeautifulSoup
import re

class TestScraperParser(unittest.TestCase):
    
    def parse_rating(self, html, year):
        soup = BeautifulSoup(html, 'lxml')
        articles = soup.find_all('article')
        
        for article in articles:
            title_tag = article.find('h2', class_='entry-title')
            if not title_tag:
                continue
            title = title_tag.get_text().strip()
            
            if str(year) in title and "rated" in title and "out of ten" in title:
                match = re.search(r'rated (\d+(\.\d+)?) out of ten', title)
                if match:
                    return float(match.group(1))
        return None

    def test_parse_valid_rating(self):
        # Mock HTML snippet from RaceFans
        html = """
        <html>
            <body>
                <article>
                    <h2 class="entry-title">2024 British Grand Prix rated 9.2 out of ten!</h2>
                </article>
                <article>
                    <h2 class="entry-title">Some other article</h2>
                </article>
            </body>
        </html>
        """
        rating = self.parse_rating(html, 2024)
        self.assertEqual(rating, 9.2)

    def test_parse_no_rating(self):
        html = """
        <html>
            <body>
                <article>
                    <h2 class="entry-title">2024 British Grand Prix results</h2>
                </article>
            </body>
        </html>
        """
        rating = self.parse_rating(html, 2024)
        self.assertIsNone(rating)

if __name__ == '__main__':
    unittest.main()

from __future__ import annotations

import unittest

from bs4 import BeautifulSoup

from amazon_scraper.html_extractors import extract_bullets, extract_description_text, extract_facts_table


class ExtractFactsTableTests(unittest.TestCase):
    def test_extracts_rows_from_product_facts_expander_table(self) -> None:
        html = """
        <div id="productFactsDesktopExpander_feature_div">
            <table>
                <tr><th>Brand Name</th><td>Example Brand</td></tr>
                <tr><th>Serving Size</th><td>3 Capsules</td></tr>
                <tr><th>Ingredients</th><td>Pumpkin Seed Oil, Vitamin E</td></tr>
            </table>
        </div>
        """

        soup = BeautifulSoup(html, "html.parser")

        self.assertEqual(
            extract_facts_table(soup).splitlines(),
            [
                "Brand Name: Example Brand",
                "Serving Size: 3 Capsules",
                "Ingredients: Pumpkin Seed Oil, Vitamin E",
            ],
        )

    def test_extracts_rows_from_nic_po_expander_table(self) -> None:
        html = """
        <div id="nic-po-expander-content">
            <table>
                <tr><th>Item Form</th><td>Softgel</td></tr>
                <tr><th>Servings Per Container</th><td>60</td></tr>
                <tr><th>Container Type</th><td>Bottle</td></tr>
            </table>
        </div>
        """

        soup = BeautifulSoup(html, "html.parser")

        self.assertEqual(
            extract_facts_table(soup).splitlines(),
            [
                "Item Form: Softgel",
                "Servings Per Container: 60",
                "Container Type: Bottle",
            ],
        )

    def test_extracts_rows_from_item_details_table(self) -> None:
        html = """
        <div id="item_details">
            <table>
                <tr><th>Serving Size</th><td>3 Capsules</td></tr>
                <tr><th>Other Ingredients</th><td>Gelatin, Glycerin, Water</td></tr>
            </table>
        </div>
        """

        soup = BeautifulSoup(html, "html.parser")

        self.assertEqual(
            extract_facts_table(soup).splitlines(),
            [
                "Serving Size: 3 Capsules",
                "Other Ingredients: Gelatin, Glycerin, Water",
            ],
        )

    def test_extracts_bullets_from_expanded_product_facts_sections(self) -> None:
        html = """
        <div id="productFactsDesktopExpander_feature_div">
            <ul>
                <li>Focus and calm without jitters</li>
                <li>Contains lion's mane and chaga</li>
            </ul>
        </div>
        """

        soup = BeautifulSoup(html, "html.parser")

        self.assertEqual(
            extract_bullets(soup),
            [
                "Focus and calm without jitters",
                "Contains lion's mane and chaga",
            ],
        )


class ExtractDescriptionTextTests(unittest.TestCase):
    def test_extracts_main_and_pqv_important_information(self) -> None:
        html = """
        <div id="important-information">
            <span class="a-text-bold">Ingredients</span>
            <p>Pumpkin Seed Oil, Vitamin E</p>
        </div>
        <div id="pqv-important-information">
            <h3>Directions</h3>
            <p>Mix one level tablespoon with water.</p>
        </div>
        """

        soup = BeautifulSoup(html, "html.parser")
        description = extract_description_text(soup)

        self.assertIn("Ingredients Pumpkin Seed Oil, Vitamin E", description)
        self.assertIn("Directions Mix one level tablespoon with water.", description)


if __name__ == "__main__":
    unittest.main()

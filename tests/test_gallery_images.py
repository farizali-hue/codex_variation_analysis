from __future__ import annotations

import json
import unittest
from pathlib import Path

from amazon_scraper.gallery_images import (
    extract_gallery_images,
    select_classification_image_url,
    select_extraction_image_url,
)


class GalleryImageExtractionTests(unittest.TestCase):
    def test_current_amazon_media_block_retains_facts_image(self) -> None:
        html = (Path(__file__).parent / "fixtures" / "amazon_media_block_parsejson.html").read_text(encoding="utf-8")
        images = extract_gallery_images(html)

        self.assertEqual(len(images), 8)
        self.assertEqual(images[0].physical_id, "71qSbtpAFSL")
        self.assertEqual(images[1].physical_id, "71h0sOLz6SL")
        self.assertEqual(images[1].variant, "PT01")
        self.assertEqual(
            select_extraction_image_url(images[1]),
            "https://m.media-amazon.com/images/I/71h0sOLz6SL._AC_SL1500_.jpg",
        )

    def test_nested_json_variants_preserve_selection_and_quoted_text(self) -> None:
        # Literal-looking words/calls inside JSON strings must remain data.
        variant = "false null undefined true: Date.now() women's"
        records = [{"large": "https://example.com/true/null.jpg", "variant": variant}]
        for wrapper in ("A.$.parseJSON", "jQuery.parseJSON", "JSON.parse"):
            with self.subTest(wrapper=wrapper):
                html = """<script>ImageBlockATF; var data = {
                    'landingAsinColor': 'selected',
                    'colorImages': {
                        'other': [{'large': 'https://example.com/wrong.jpg'}],
                        'selected': WRAPPER(PAYLOAD)
                    },
                    'enabled': true, 'missing': null, 'timestamp': Date.now()
                };</script>""".replace("WRAPPER", wrapper).replace("PAYLOAD", repr(json.dumps(records)))
                images = extract_gallery_images(html)
                self.assertEqual(len(images), 1)
                self.assertEqual(images[0].large_url, records[0]["large"])
                self.assertEqual(images[0].variant, variant)

    def test_invalid_nested_json_abstains(self) -> None:
        for value in ("'not json'", "'{}'", "'null'", "'123'", "'[]'"):
            with self.subTest(value=value):
                html = "<script>ImageBlockATF; var data = {'colorImages': {'initial': A.$.parseJSON(" + value + ")}};</script>"
                self.assertEqual(extract_gallery_images(html), [])

    def test_unrecognized_function_is_never_treated_as_gallery_data(self) -> None:
        html = """<script>ImageBlockATF; var data = {
            'colorImages': {'initial': untrustedFunction('[{"large":"https://example.com/image.jpg"}]')}
        };</script>"""
        self.assertEqual(extract_gallery_images(html), [])

    def test_plain_js_string_values_are_not_rewritten(self) -> None:
        html = """<script>ImageBlockATF; var data = {
            'landingAsinColor': 'null',
            'colorImages': {'null': [{
                'large': 'https://example.com/true/null.jpg',
                'variant': 'label: Date.now() undefined'
            }]}
        };</script>"""
        images = extract_gallery_images(html)
        self.assertEqual(images[0].large_url, "https://example.com/true/null.jpg")
        self.assertEqual(images[0].variant, "label: Date.now() undefined")

    def test_extract_gallery_images_uses_landing_variant_and_preserves_order(self) -> None:
        html = r"""
        <script type="text/javascript">
        P.when('jQuery').register('ImageBlockBTF', function(jQuery){
            var obj = jQuery.parseJSON('{\"landingAsinColor\":\"Pouch\",\"colorImages\":{\"Bottle\":[{\"large\":\"https://m.media-amazon.com/images/I/bottle-front._AC_.jpg\",\"thumb\":\"https://m.media-amazon.com/images/I/bottle-front._AC_US40_.jpg\",\"hiRes\":\"https://m.media-amazon.com/images/I/bottle-front._AC_SL1500_.jpg\",\"variant\":\"MAIN\",\"main\":{\"https://m.media-amazon.com/images/I/bottle-front._AC_SY450_.jpg\":[450,450]},\"physicalIdForMedia\":\"bottle-front\"}],\"Pouch\":[{\"large\":\"https://m.media-amazon.com/images/I/pouch-front._AC_.jpg\",\"thumb\":\"https://m.media-amazon.com/images/I/pouch-front._AC_US40_.jpg\",\"hiRes\":\"https://m.media-amazon.com/images/I/pouch-front._AC_SL1500_.jpg\",\"variant\":\"MAIN\",\"main\":{\"https://m.media-amazon.com/images/I/pouch-front._AC_SY450_.jpg\":[450,450]},\"physicalIdForMedia\":\"pouch-front\"},{\"large\":\"https://m.media-amazon.com/images/I/pouch-facts._AC_.jpg\",\"thumb\":\"https://m.media-amazon.com/images/I/pouch-facts._AC_US40_.jpg\",\"hiRes\":\"https://m.media-amazon.com/images/I/pouch-facts._AC_SL1500_.jpg\",\"variant\":\"PT01\",\"main\":{\"https://m.media-amazon.com/images/I/pouch-facts._AC_SY450_.jpg\":[450,450]},\"physicalIdForMedia\":\"pouch-facts\"}]},\"videos\":[{\"thumb\":\"https://m.media-amazon.com/images/I/video-thumb.jpg\",\"isVideo\":true}]}');
        });
        </script>
        """

        images = extract_gallery_images(html)

        self.assertEqual([image.physical_id for image in images], ["pouch-front", "pouch-facts"])
        self.assertEqual([image.slot_index for image in images], [0, 1])
        self.assertEqual(images[0].variant, "MAIN")
        self.assertEqual(images[1].variant, "PT01")

    def test_extract_gallery_images_dedupes_by_physical_id(self) -> None:
        html = r"""
        <script type="text/javascript">
        P.when('jQuery').register('ImageBlockBTF', function(jQuery){
            var obj = jQuery.parseJSON('{\"landingAsinColor\":\"initial\",\"colorImages\":{\"initial\":[{\"large\":\"https://m.media-amazon.com/images/I/dup._AC_.jpg\",\"thumb\":\"https://m.media-amazon.com/images/I/dup._AC_US40_.jpg\",\"hiRes\":\"https://m.media-amazon.com/images/I/dup._AC_SL1500_.jpg\",\"variant\":\"MAIN\",\"main\":{\"https://m.media-amazon.com/images/I/dup._AC_SY450_.jpg\":[450,450]},\"physicalIdForMedia\":\"dup\"},{\"large\":\"https://m.media-amazon.com/images/I/dup._AC_.jpg\",\"thumb\":\"https://m.media-amazon.com/images/I/dup._AC_US40_.jpg\",\"hiRes\":\"https://m.media-amazon.com/images/I/dup._AC_SL1500_.jpg\",\"variant\":\"PT01\",\"main\":{\"https://m.media-amazon.com/images/I/dup._AC_SY450_.jpg\":[450,450]},\"physicalIdForMedia\":\"dup\"}]}}');
        });
        </script>
        """

        images = extract_gallery_images(html)

        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].physical_id, "dup")

    def test_extract_gallery_images_supports_direct_image_block_payloads(self) -> None:
        html = r"""
        <script type="text/javascript">
        P.when('A').register("ImageBlockATF", function(A){
            var data = {
                'asin': 'B0TESTASIN',
                'defaultColor': 'initial',
                'landingAsinColor': 'initial',
                'imageBlockRenderingStartTime': Date.now(),
                'colorImages': {
                    'initial': [{
                        'hiRes': 'https://m.media-amazon.com/images/I/direct-front._AC_SL1500_.jpg',
                        'thumb': 'https://m.media-amazon.com/images/I/direct-front._AC_US40_.jpg',
                        'large': 'https://m.media-amazon.com/images/I/direct-front._AC_.jpg',
                        'main': {
                            'https://m.media-amazon.com/images/I/direct-front._AC_SY355_.jpg': [355, 355],
                            'https://m.media-amazon.com/images/I/direct-front._AC_SY879_.jpg': [879, 879]
                        },
                        'variant': 'MAIN',
                        'physicalIdForMedia': 'direct-front'
                    }]
                }
            };
        });
        P.when('jQuery').register('ImageBlockBTF', function(jQuery){
            var obj = jQuery.parseJSON('{\"landingAsinColor\":\"initial\",\"colorImages\":{}}');
        });
        </script>
        """

        images = extract_gallery_images(html)

        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].physical_id, "direct-front")
        self.assertEqual(images[0].hi_res_url, "https://m.media-amazon.com/images/I/direct-front._AC_SL1500_.jpg")

    def test_select_image_urls_apply_hires_and_main_fallbacks(self) -> None:
        html = r"""
        <script type="text/javascript">
        P.when('jQuery').register('ImageBlockBTF', function(jQuery){
            var obj = jQuery.parseJSON('{\"landingAsinColor\":\"initial\",\"colorImages\":{\"initial\":[{\"large\":\"https://m.media-amazon.com/images/I/front._AC_.jpg\",\"thumb\":\"https://m.media-amazon.com/images/I/front._AC_US40_.jpg\",\"hiRes\":\"\",\"variant\":\"MAIN\",\"main\":{\"https://m.media-amazon.com/images/I/front._AC_SY450_.jpg\":[450,300],\"https://m.media-amazon.com/images/I/front._AC_SY879_.jpg\":[879,586]},\"physicalIdForMedia\":\"front\"},{\"large\":\"\",\"thumb\":\"https://m.media-amazon.com/images/I/facts._AC_US40_.jpg\",\"hiRes\":\"\",\"variant\":\"PT01\",\"main\":{\"https://m.media-amazon.com/images/I/facts._AC_SY450_.jpg\":[450,450],\"https://m.media-amazon.com/images/I/facts._AC_SY879_.jpg\":[879,879]},\"physicalIdForMedia\":\"facts\"}]}}');
        });
        </script>
        """

        images = extract_gallery_images(html)

        self.assertEqual(select_classification_image_url(images[0]), "https://m.media-amazon.com/images/I/front._AC_.jpg")
        self.assertEqual(select_extraction_image_url(images[0]), "https://m.media-amazon.com/images/I/front._AC_.jpg")
        self.assertEqual(select_classification_image_url(images[1]), "https://m.media-amazon.com/images/I/facts._AC_SY879_.jpg")
        self.assertEqual(select_extraction_image_url(images[1]), "https://m.media-amazon.com/images/I/facts._AC_SY879_.jpg")


if __name__ == "__main__":
    unittest.main()

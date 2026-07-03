from __future__ import annotations

import unittest

from amazon_scraper.gallery_images import (
    extract_gallery_images,
    select_classification_image_url,
    select_extraction_image_url,
)


class GalleryImageExtractionTests(unittest.TestCase):
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

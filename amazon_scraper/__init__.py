from .models import ScrapeJob, ScrapeResult

__all__ = ["AmazonScraper", "ScrapeJob", "ScrapeResult"]


def __getattr__(name: str):
    if name == "AmazonScraper":
        from .scraper import AmazonScraper

        return AmazonScraper
    raise AttributeError(name)

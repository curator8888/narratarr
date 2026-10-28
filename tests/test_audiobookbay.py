from narratarr.sources.audiobookbay import AudiobookBay, parse_details, parse_listing, split_title

BASE = "https://audiobookbay.lu"


def test_listing_parses_results_and_pagination(fixture_html):
    page = parse_listing(fixture_html("abb_search.html"), BASE)
    assert page.total_pages == 27
    assert len(page.items) == 9

    dune = page.items[1]
    assert dune.url == f"{BASE}/abss/dune-frank-herbert-2/"
    assert (dune.title, dune.author) == ("Dune", "Frank Herbert")
    assert dune.cover_url.startswith("https://m.media-amazon.com/")
    assert dune.format == "M4B"
    assert dune.size == "1.12 GB"
    assert dune.language == "English"
    assert dune.categories == ["Adults", "Classic", "Sci-Fi"]


def test_listing_handles_author_first_titles(fixture_html):
    page = parse_listing(fixture_html("abb_search.html"), BASE)
    philosophy = page.items[2]
    assert philosophy.author == "Kevin S. Decker and William Irwin (editors)"
    assert philosophy.title.startswith("Dune and Philosophy")


def test_details_extracts_hash_trackers_and_metadata(fixture_html):
    d = parse_details(fixture_html("abb_detail.html"), f"{BASE}/abss/dune-frank-herbert-2/")
    assert d.info_hash == "061850ead3eb6f1c5c6d8420211b4bbf2d4ee3e2"
    assert "udp://tracker.opentrackr.org:1337/announce" in d.trackers
    assert len(d.trackers) == len(set(d.trackers))
    assert d.author == "Frank Herbert"
    assert d.narrators[0] == "Scott Brick"
    assert d.length == "21 hours and 2 minutes"
    assert d.bitrate == "125 kbps"
    assert "Arrakis" in d.description
    assert d.posted == "2025-12-24"


def test_magnet_link(fixture_html):
    d = parse_details(fixture_html("abb_detail.html"), f"{BASE}/abss/dune-frank-herbert-2/")
    magnet = d.magnet()
    assert magnet.startswith("magnet:?xt=urn:btih:061850ead3eb6f1c5c6d8420211b4bbf2d4ee3e2&dn=Dune")
    assert "tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce" in magnet


def test_split_title_without_separator():
    assert split_title("Just A Title") == ("Just A Title", "")


def test_owns_only_book_pages_on_configured_host():
    abb = AudiobookBay(BASE)
    assert abb.owns(f"{BASE}/abss/dune-frank-herbert-2/")
    assert not abb.owns("https://evil.example/abss/dune/")
    assert not abb.owns(f"{BASE}/member/login")

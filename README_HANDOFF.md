
# GPT-Blog Handoff (Patched)

This package was unzipped, inspected, and lightly patched for stability:

- **affiliate_post.py**
  - Added `_button_html_local()` and `_get_button_html()` helpers to eliminate `NameError` while preserving your original button style when present.
  - Introduced `REQUIRE_COUPANG_API` env flag (default off) to allow graceful fallback without blocking posting.
  - Added `resolve_affiliate_url()` and `coupang_search_url()` helpers to prefer deep-links when available and valid, else fallback to Coupang search URL.

- **rich_templates.py**
  - If `build_affiliate_content()` was missing, a minimal stub was appended to avoid import errors. Replace with your richer version anytime.

- **auto_wp_gpt.py**
  - Added a `sanitize_title()` function that removes leading "예약" markers from titles before publishing.

> All changes aim to keep your current layout and styling intact while eliminating runtime errors and ensuring posts continue even when the deep-link API is unavailable.

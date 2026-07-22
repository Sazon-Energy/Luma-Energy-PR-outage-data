STATUS_PAGE_URL = "https://miluma.lumapr.com/outages/status"
REGION_ENDPOINT_URL = (
    "https://api.miluma.lumapr.com/miluma-outage-api/outage/regionsWithoutService"
)

REQUEST_TIMEOUT_SECONDS = 30

# The LUMA outage API sits behind an Incapsula WAF that blocks requests without
# a real-browser User-Agent. Rotating through a small pool of current browser
# strings mirrors normal traffic and matches the approach used by
# https://github.com/jpadilla/tracking-luma-outages.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 "
    "Firefox/125.0",
]

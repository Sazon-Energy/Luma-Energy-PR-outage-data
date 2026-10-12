STATUS_PAGE_URL = "https://miluma.lumapr.com/outages/status"
REGION_ENDPOINT_URL = (
    "https://api.miluma.lumapr.com/miluma-outage-api/outage/regionsWithoutService"
)

REQUEST_TIMEOUT_SECONDS = 30

# The WAF's blocks are often brief, so a non-JSON (challenge page) response
# is retried a couple of times - with a fresh session and User-Agent, and a
# deliberately unhurried, growing delay (20s, then 40s) - before the workflow
# falls back to the much heavier headless-browser capture.
WAF_BLOCK_MAX_ATTEMPTS = 3
WAF_BLOCK_RETRY_DELAY_SECONDS = 20

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

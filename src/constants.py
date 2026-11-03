STATUS_PAGE_URL = "https://miluma.lumapr.com/outages/status"
REGION_ENDPOINT_URL = (
    "https://api.miluma.lumapr.com/miluma-outage-api/outage/regionsWithoutService"
)

REQUEST_TIMEOUT_SECONDS = 30

# LUMA publishes a new figure every 10 minutes and its API only ever shows the
# current one, so a figure not captured within its 10-minute slot is lost for
# good. Rather than giving up after a fixed number of tries, each run keeps
# retrying until a deadline measured from when the run started: direct
# requests until DIRECT_FETCH_DEADLINE_SECONDS, then (if those all failed) the
# much heavier headless-browser capture until BROWSERLESS_CAPTURE_DEADLINE_SECONDS.
# A capture that starts just before that deadline can take up to
# BROWSERLESS_REQUEST_TIMEOUT_SECONDS more, which still leaves time for the
# metrics step inside the job's 10-minute timeout.
DIRECT_FETCH_DEADLINE_SECONDS = 240
BROWSERLESS_CAPTURE_DEADLINE_SECONDS = 420

# Delay before retry number n is n * RETRY_DELAY_STEP_SECONDS, capped. The
# WAF's blocks are often brief, so an unhurried, growing delay (with a fresh
# session and User-Agent each time) gives it room to clear.
RETRY_DELAY_STEP_SECONDS = 15
RETRY_DELAY_MAXIMUM_SECONDS = 45

# browserless runs scripts/capture_region_snapshot.js, whose own page-load
# timeout is 45 seconds; this leaves room for the browser to start.
BROWSERLESS_FUNCTION_URL = "http://localhost:3000/function"
BROWSERLESS_REQUEST_TIMEOUT_SECONDS = 90

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

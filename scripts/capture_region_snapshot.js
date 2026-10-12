// Runs inside browserless (POSTed to its /function endpoint by
// .github/workflows/collect.yml) when a direct request to the LUMA outage
// API is blocked by the Incapsula WAF. Loads the real status page in
// headless Chrome and returns the regionsWithoutService XHR response the
// page itself makes, which passes the WAF like any other browser visit.
//
// The response listener is registered *before* navigating: page.goto waits
// for the page's load event, and the XHR can finish before that - a
// listener added after goto would miss it and time out.
//
// On failure this still returns JSON, carrying a captureError plus what the
// page showed, so the workflow log can tell a WAF challenge page apart from
// the XHR simply never arriving.

const STATUS_PAGE_URL = "https://miluma.lumapr.com/outages/serviceStatus";
const REGION_ENDPOINT_PATH = "/outage/regionsWithoutService";
const CAPTURE_TIMEOUT_MILLISECONDS = 45000;
const PAGE_CONTENT_PREVIEW_LENGTH = 500;

module.exports = async ({ page }) => {
  const regionResponsePromise = page.waitForResponse(
    (response) =>
      response.request().url().includes(REGION_ENDPOINT_PATH) &&
      response.request().method() === "GET",
    { timeout: CAPTURE_TIMEOUT_MILLISECONDS }
  );

  try {
    await page.goto(STATUS_PAGE_URL, {
      waitUntil: "domcontentloaded",
      timeout: CAPTURE_TIMEOUT_MILLISECONDS,
    });
    const regionResponse = await regionResponsePromise;
    return { data: await regionResponse.json(), type: "application/json" };
  } catch (captureError) {
    // Keeps an unawaited rejection of the listener from surfacing separately
    // when goto itself was what failed.
    regionResponsePromise.catch(() => {});

    let pageTitle = null;
    let pageContentStart = null;
    try {
      pageTitle = await page.title();
      pageContentStart = (await page.content()).slice(
        0,
        PAGE_CONTENT_PREVIEW_LENGTH
      );
    } catch (pageReadError) {
      pageContentStart = `could not read page: ${pageReadError.message}`;
    }

    return {
      data: {
        captureError: captureError.message,
        pageUrl: page.url(),
        pageTitle,
        pageContentStart,
      },
      type: "application/json",
    };
  }
};

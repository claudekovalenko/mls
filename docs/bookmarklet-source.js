// Source for the "+ Add to Tracker" bookmarklet (see docs/index.html for the minified version
// embedded as a javascript: link). Run this manually to regenerate that minified version.
//
// How it works: this runs INSIDE the browser tab that's already showing the listing page
// (Zillow, Redfin, Realtor.com, etc). Since it's reading the page you're already viewing —
// not making a new automated request — it isn't blocked by anti-scraping like server-side
// fetches are. It best-effort extracts price/beds/baths/photo/address from the page, then
// opens add.html with those pre-filled so one click finishes the add.
(function () {
  const text = document.body.innerText || "";

  const priceMatch = text.match(/\$[\d]{2,3}(?:,\d{3})+/);
  const price = priceMatch ? priceMatch[0].replace(/[$,]/g, "") : "";

  const bedsMatch = text.match(/(\d+(?:\.\d+)?)\s*(?:bds?|beds?|bedrooms?)\b/i);
  const beds = bedsMatch ? bedsMatch[1] : "";

  const bathsMatch = text.match(/(\d+(?:\.\d+)?)\s*(?:bas?|baths?|bathrooms?)\b/i);
  const baths = bathsMatch ? bathsMatch[1] : "";

  const ogImage = document.querySelector('meta[property="og:image"]');
  const photo = ogImage ? ogImage.content : "";

  const ogTitle = document.querySelector('meta[property="og:title"]');
  let address = ogTitle ? ogTitle.content : document.title;
  address = address.replace(/\s*[|–—-]\s*(zillow|redfin|realtor|trulia).*$/i, "").trim();

  const params = new URLSearchParams();
  params.set("u", window.location.href.split("?")[0]);
  if (address) params.set("a", address);
  if (price) params.set("p", price);
  if (beds) params.set("bd", beds);
  if (baths) params.set("ba", baths);
  if (photo) params.set("photo", photo);

  window.open("https://claudekovalenko.github.io/mls/add.html?" + params.toString(), "_blank");
})();

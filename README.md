# page-fetch

Fetch web pages for automations, including many paywalled ones.

Each *route* is one way of getting a page: a plain request, a browser TLS fingerprint, a Google-search referer, or an archived copy. A route returns a `Fetched` (the HTML, the address that served it and the route's name) or raises `FetchFailed` with a one-line reason. Routes never fall back to one another; the caller picks the order.

| Route | What it does | Useful for |
|---|---|---|
| `direct` | Plain request with a browser user agent | Open pages |
| `impersonate` | Real-browser TLS fingerprint (`curl_cffi`, extra `impersonate`) | Sites whose firewall rejects Python clients |
| `www-variant` | Plain request to the `www.` host | Bare hosts that redirect articles to the homepage |
| `google-referer` | Looks like a click from Google search | Some soft paywalls (the FT) |
| `archive.today` | Newest archive.ph snapshot | Hard paywalls (NYT, WSJ, Bloomberg, The Economist, WaPo) |
| `wayback` | Closest Wayback Machine snapshot | Older pages |

Archive snapshots are made by whoever asked for them: check `Fetched.title` against the article you wanted, since a snapshot can be of a different page at a similar address.

All requests go through `page_fetch.netguard`, which refuses non-public addresses, including on redirects and against DNS rebinding.

```python
from page_fetch import ROUTES, FetchFailed

try:
    page = ROUTES["archive.today"]("https://www.nytimes.com/…")
except FetchFailed as e:
    print("failed:", e)
else:
    print(page.source_url, page.title)
    print(page.text)  # tag-stripped; None if empty or an access wall
```

Install a pinned version:

```
page-fetch[impersonate] @ git+https://github.com/benthamite/page-fetch@v0.1.0
```

Tests: `python -m unittest discover -s tests`.

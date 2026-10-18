# Solution: Direct S3 Sourcing for LOCG Covers (2026-10-18)

Status: **Engineering Solution & Clarification**  
Target: Resolving cover art gaps for issues with no Metron art (e.g. Buffy the Vampire Slayer #1–#10) without web scraping.

---

## 1. The Root Cause of the Misconception

There was a fundamental confusion between **the LOCG HTML webpage** and **the LOCG cover image asset**:

1. **The HTML Webpage (`leagueofcomicgeeks.com/comic/{id}/*`):**
   * Sits behind Cloudflare Turnstile's Managed Challenge.
   * Blocks headless automation and FlareSolverr.
   * **Does NOT need to be visited to fetch covers.**
2. **The Cover Image Asset (`s3.amazonaws.com/comicgeeks/comics/covers/large-{id}.jpg`):**
   * Sits on **Amazon AWS S3**, completely outside Cloudflare.
   * Has **zero Turnstile, zero CAPTCHAs, and zero bot challenges**.
   * Responds with `HTTP 200 OK` directly in under 100ms.

### Why Kometa Thought LOCG Covers Were Blocked
In [`kometa/thumbnails.py:325-334`](../kometa/thumbnails.py), when Metron art was missing, Kometa attempted this:
```python
locg_iid = issue.get("locg_issue_id") if issue else None
if locg_iid:
    from kometa.locg_client import fetch_variants
    data = fetch_variants(locg_iid)  # <--- ATTEMPTS TO SCRAPE HTML WEBPAGE
```
`fetch_variants()` requested `https://leagueofcomicgeeks.com/comic/{key}/comic`. Because Cloudflare blocked the HTML page, `fetch_variants()` threw an exception, and `thumbnails.py` returned 404—**even though the image file was sitting on Amazon S3 the whole time.**

---

## 2. Empirical Verification

Taking Buffy the Vampire Slayer #10 (`https://leagueofcomicgeeks.com/comic/8504205/buffy-the-vampire-slayer-10`):
* `locg_issue_id` = `8504205`
* S3 URL = `https://s3.amazonaws.com/comicgeeks/comics/covers/large-8504205.jpg`

Verified from the server via curl:
```http
HTTP/1.1 200 OK
Server: AmazonS3
Content-Type: image/jpg
Content-Length: 55859
```
The full 55.8 KB cover image is immediately accessible with a standard HTTP GET request.

---

## 3. The Implementation

### Step 1: Direct S3 Fetch in `kometa/thumbnails.py`
In `kometa/thumbnails.py`, replace the `fetch_variants` call with a direct S3 image serve:

```python
    # S3 cover from LOCG by locg_issue_id — completely bypasses Cloudflare
    locg_iid = issue.get("locg_issue_id") if issue else None
    if locg_iid:
        from kometa.locg_client import S3_LARGE, S3_THUMB
        s3_url = S3_LARGE.format(locg_iid)
        resp = images.serve(images.issue_key(series_id, number), s3_url, "locg", path=DB_PATH) or _image_or_none(s3_url)
        if resp:
            return _tag(resp, "catalogue")
```

Kometa already has an image lane for `s3.amazonaws.com/comicgeeks/` in [`kometa/images.py`](../kometa/images.py) configured to fetch and cache these files.

### Step 2: Ensure `locg_issue_id` is Filled During Sync
In [`kometa/sync.py:164`](../kometa/sync.py), `_locg_issues_due` was skipping LOCG for series not on the active pull list. 
If Metron left blank covers (`not mi.get("image")`), allow `sync.py` to call `/comic/get_comics?list=series&series_id=...`. This is an unblocked batch AJAX endpoint that populates `locg_issue_id` for every issue in the series in one single call.

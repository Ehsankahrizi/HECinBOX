# Deploying hecinbox.com

Static site. No build step, no dependencies, no server. Files:

```
website/
  index.html            the whole site (inline CSS and JS)
  404.html              not-found page
  logo.png              full brand logo, also used as the social preview image
  logo-mark.png         logo art without the wordmark, used in the header and footer
  apple-touch-icon.png  home screen icon
  favicon.png           browser tab icon
  screenshots/          result maps shown in the Results gallery, see its README
  _headers              security headers (Cloudflare Pages reads this file)
  robots.txt
  sitemap.xml
```

The palette and typography are copied from the application itself, so the site
and the product look like one piece of software:

| Token | Value | Source |
|---|---|---|
| Page background | `#eef0f5` | `backgroundColor` in `.streamlit/config.toml` |
| Text | `#0f1e3a` | `textColor` |
| Primary | `#3c78af` | `primaryColor` |
| Accent wash | `#e1e6ee` | `secondaryBackgroundColor` |
| Body font | Inter | the app's `_THEME_RULES` |
| Code font | JetBrains Mono | the app's `_THEME_RULES` |

The two fonts are the only external request the page makes, loaded from Google
Fonts exactly as the application loads them. `_headers` allows those two hosts
in the content security policy and nothing else.

## Recommendation: Cloudflare Pages

The domain is already registered at Cloudflare, so Pages is the path of least
resistance and the more secure of the two free options.

| | Cloudflare Pages | GitHub Pages |
|---|---|---|
| Cost | Free, unlimited bandwidth | Free, 100 GB/month soft limit |
| Custom domain on a Cloudflare domain | One click, DNS wired automatically | Manual A/AAAA/CNAME records |
| TLS | Free, automatic | Free, automatic |
| DDoS protection and WAF | Included | Not included |
| Custom response headers | Yes, via `_headers` | No |
| Private source repo | Allowed on the free plan | Needs GitHub Pro |
| Deploy previews per branch | Yes | No |

Use GitHub as the source of truth and let Cloudflare Pages build from it. That
gives version history plus the Cloudflare edge, so there is no reason to choose
between them.

### Steps

The site already lives at the root of **github.com/Ehsankahrizi/HECinBOX**, so
only the Cloudflare side is left.

1. Cloudflare dashboard, **Workers & Pages**, **Create**, **Pages**, **Connect to Git**.
2. Authorize GitHub and pick the `HECinBOX` repository, branch `main`.
3. Build settings:
   - Framework preset: **None**
   - Build command: leave empty
   - Build output directory: `/`
4. Deploy. You get a `*.pages.dev` URL immediately.
5. In the project, **Custom domains**, **Set up a domain**, enter `hecinbox.com`,
   then repeat for `www.hecinbox.com`. Cloudflare creates the DNS records itself
   because the zone is on the same account.
6. In the zone under **SSL/TLS**, set the encryption mode to **Full (strict)**.

Every `git push` redeploys. Rollback is one click in the deployment list.

### If you prefer no Git at all

Workers & Pages, Create, Pages, **Upload assets**, drag the `website` folder in.
Same result, but updates mean re-uploading by hand.

## The demo link needs HTTPS

`index.html` points the demo buttons at `DEMO_URL`, defined at the top of the
script block. It currently holds the plain HTTP ALB address:

```
http://bil6-hecinbox-alb-1394482369.us-east-2.elb.amazonaws.com
```

An HTTPS site linking to an HTTP address is not blocked, but the browser labels
the demo "Not secure" and some corporate networks strip it. Fix it before
launch by giving the demo its own hostname:

1. AWS Certificate Manager, us-east-2: request a public certificate for
   `demo.hecinbox.com`. Validation is a DNS record you add in Cloudflare. ACM
   public certificates are free.
2. Add an HTTPS (443) listener to `bil6-hecinbox-alb` using that certificate,
   forwarding to the existing target group `bil6-hecinbox-tg`. No extra charge
   beyond the ALB you already run.
3. In Cloudflare DNS, add `demo` as a CNAME to the ALB hostname. Leave it
   **DNS only** (grey cloud) at first, since Streamlit holds a long-lived
   WebSocket. Cloudflare does proxy WebSockets, so you can switch it to proxied
   later if you want the WAF in front of the demo.
4. Change one line in `index.html`:
   `const DEMO_URL = "https://demo.hecinbox.com";`

Optional: put **Cloudflare Access** in front of `demo.hecinbox.com` if the demo
should be reachable by invitation only. Streamlit has no authentication of its
own, so anything reachable at that URL is reachable by anyone who finds it.

### Note on HSTS

`_headers` sends `Strict-Transport-Security` **without** `includeSubDomains` on
purpose. Adding that directive while `demo.hecinbox.com` still answers on plain
HTTP would make the demo unreachable in any browser that had seen the header.
Add `includeSubDomains` only after the demo is on HTTPS.

## Editing the site

Everything is in `index.html`. The pieces most likely to change:

- `DEMO_URL`, top of the `<script>` block
- `STAGES`, the seven pipeline steps and their copy
- `SHOTS`, the result figures in the gallery and their captions
- `FAQ`, the question and answer pairs
- `CODE` and `RAW`, the install commands (`CODE` is the highlighted display
  version, `RAW` is what the Copy button puts on the clipboard, so edit both)
- Contact address in the footer, currently `ekahrizi@crimson.ua.edu`

The colour system is CSS custom properties at the top of the `<style>` block.
Changing `--primary` there restyles buttons, links, icons, and charts together.

To regenerate the logo files from a new source image, adjust the paths in the
Pillow snippet below and rerun it from the project root. It knocks the white
background out by flood filling inward from the edges, so enclosed white shapes
such as the whale belly and the Docker mark survive:

```python
from PIL import Image, ImageDraw
im = Image.open("assets/logo.png").convert("RGB")
w, h = im.size
for xy in [(0,0),(w-1,0),(0,h-1),(w-1,h-1)]:
    ImageDraw.floodfill(im, xy, (255,0,255), thresh=26)
im = im.convert("RGBA")
px = im.load()
for y in range(h):
    for x in range(w):
        if px[x,y][:3] == (255,0,255):
            px[x,y] = (255,255,255,0)
im.resize((640,640), Image.LANCZOS).save("website/logo.png", optimize=True)
```

## Before launch

- [ ] Copy the four result maps into `screenshots/` (see that folder's README).
      Until they are there, the Results gallery stays hidden.
- [ ] Point `DEMO_URL` at an HTTPS demo host
- [ ] Decide whether the demo needs Cloudflare Access in front of it
- [ ] Optional: replace the social preview image. It currently points at
      `logo.png`, which is square. A purpose built 1200x630 image looks better
      when the link is pasted into Slack, LinkedIn, or X.
